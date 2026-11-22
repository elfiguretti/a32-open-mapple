/*
 * MediaTek MT6768/MT6769T (Samsung Galaxy A32 4G, SM-A325M) eMMC MSDC0 block device driver.
 *
 * Adapted for Open Mapple (xnu-8796.141.3) from HakiOS MT6768's redwood_emmc.c and
 * Open Mapple's bsd/dev/mtk_ufs.c.
 *
 * Uses MSDC0 @ 0x11230000 in PIO mode (LK leaves the card powered, bus-width/clock tuned,
 * and in transfer state). Discovers a GPT partition by name (boot-arg mp_emmcroot=, default
 * "xnusys"), exposes it as /dev/emmc0 (/dev/remmc0) with 512-byte sectors for rd=emmc0, and
 * supports HFS+/HFSX (Open Mapple rootfs), APFS ("NXSB"), or raw GPT partitions.
 *
 * Read-only by default; boot-arg mp_rootrw=1 enables range-checked writes strictly confined
 * to [emmc_lba, emmc_lba + emmc_blocks) so no other Android partition can ever be modified.
 */
#include <pexpert/arm64/board_config.h>

#if defined(ARM64_BOARD_CONFIG_MT6781)
#include <kern/locks.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <miscfs/devfs/devfs.h>
#include <pexpert/pexpert.h>
#include <sys/buf.h>
#include <sys/conf.h>
#include <sys/disk.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern void usblink_probe(void);

#define MSDC0_PHYS              0x11230000UL
#define EMMC_DEFAULT_PART       "xnusys"
#define EMMC_MAX_BLOCKS         256U            /* 128 KiB per transfer */
#define EMMC_FIFO_BYTES         128U

#define MSDC_CFG                0x00U
#define MSDC_CFG_PIO            (1u << 3)
#define MSDC_INT                0x0cU
#define MSDC_FIFOCS             0x14U
#define MSDC_FIFOCS_CLR         (1u << 31)
#define MSDC_TXDATA             0x18U
#define MSDC_RXDATA             0x1cU
#define SDC_CMD                 0x34U
#define SDC_ARG                 0x38U
#define SDC_STS                 0x3cU
#define SDC_STS_BUSY            3u              /* SDCBUSY | CMDBUSY */
#define SDC_RESP0               0x40U
#define SDC_BLK_NUM             0x50U

#define INT_CMDRDY              (1u << 8)
#define INT_CMDTMO              (1u << 9)
#define INT_RSPCRCERR           (1u << 10)
#define INT_XFER_COMPL          (1u << 12)
#define INT_DATTMO              (1u << 14)
#define INT_DATCRCERR           (1u << 15)

#define CMD_RSP_R1              (1u << 7)
#define CMD_RSP_R1B             (6u << 7)
#define CMD_DTYPE_SINGLE        (1u << 11)
#define CMD_DTYPE_MULTI         (2u << 11)
#define CMD_RW                  (1u << 13)
#define CMD_STOP                (1u << 14)
#define CMD_BLKLEN(n)           (((n) & 0xfffu) << 16)

/* Card R1 status bits (CMD13) */
#define R1_READY_FOR_DATA       (1u << 8)
#define R1_CUR_STATE_MASK       (0xfu << 9)
#define R1_STATE_TRAN           (4u << 9)

static vm_offset_t emmc_base;
static bool emmc_ready;
static bool emmc_writable;
static uint64_t emmc_lba;
static uint64_t emmc_blocks;
static uint64_t emmc_part_blocks;               /* full GPT partition size (tail Wi-Fi FW) */
static uint32_t emmc_secsize = 512u;            /* logical sector size (512..4096 via DKIOCSETBLOCKSIZE) */
static int emmc_bmajor = -1;
static int emmc_cmajor = -1;
static dev_t emmc_bdev;
static uint32_t emmc_int_seen;
static const char *emmc_failed;
static uint32_t emmc_failed_status;
static uint64_t emmc_errors;

static uint32_t emmc_sector[128] __attribute__((aligned(16)));
static uint32_t emmc_gpt_entries[128 * 128 / 4] __attribute__((aligned(16)));
static uint32_t emmc_gpt_count;
static uint32_t emmc_gpt_entsize;
static bool emmc_gpt_loaded;

static LCK_GRP_DECLARE(mtk_emmc_grp, "mtk_emmc");
static LCK_MTX_DECLARE(mtk_emmc_lock, &mtk_emmc_grp);

static inline uint32_t
emmc_rd(uint32_t reg)
{
	return *(volatile uint32_t *)(emmc_base + reg);
}

static inline void
emmc_wr(uint32_t reg, uint32_t value)
{
	*(volatile uint32_t *)(emmc_base + reg) = value;
}

static bool
emmc_fail(const char *what, uint32_t status)
{
	emmc_failed = what;
	emmc_failed_status = status;
	return false;
}

static bool
emmc_wait_idle(void)
{
	for (uint32_t i = 0; i < 4000000U; i++) {
		if (!(emmc_rd(SDC_STS) & SDC_STS_BUSY)) {
			return true;
		}
	}
	return emmc_fail("controller busy", emmc_rd(SDC_STS));
}

static bool
emmc_wait_int(uint32_t want, uint32_t bad, const char *what)
{
	for (uint32_t i = 0; i < 20000000U; i++) {
		uint32_t status = emmc_rd(MSDC_INT);

		if (status != 0) {
			emmc_wr(MSDC_INT, status);
			emmc_int_seen |= status;
		}
		if (emmc_int_seen & bad) {
			status = emmc_int_seen;
			emmc_int_seen = 0;
			return emmc_fail(what, status);
		}
		if (emmc_int_seen & want) {
			emmc_int_seen &= ~want;
			return true;
		}
	}
	return emmc_fail(what, emmc_int_seen);
}

static void
emmc_pio_mode(void)
{
	emmc_wr(MSDC_CFG, emmc_rd(MSDC_CFG) | MSDC_CFG_PIO);
	emmc_wr(MSDC_INT, emmc_rd(MSDC_INT));
	emmc_int_seen = 0;
	emmc_wr(MSDC_FIFOCS, emmc_rd(MSDC_FIFOCS) | MSDC_FIFOCS_CLR);
	for (uint32_t i = 0; i < 100000U && (emmc_rd(MSDC_FIFOCS) & MSDC_FIFOCS_CLR); i++) {
	}
}

static bool
emmc_stop(void)
{
	emmc_wr(SDC_ARG, 0);
	emmc_wr(SDC_CMD, 12u | CMD_RSP_R1B | CMD_STOP);
	return emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, "stop") && emmc_wait_idle();
}

static bool
emmc_wait_card_ready(void)
{
	/* Poll CMD13 (RCA 1, standard eMMC init RCA used by LK) until card returns to TRAN + ready. */
	for (uint32_t tries = 0; tries < 50000U; tries++) {
		if (!emmc_wait_idle()) {
			return false;
		}
		emmc_wr(MSDC_INT, emmc_rd(MSDC_INT));
		emmc_int_seen = 0;
		emmc_wr(SDC_ARG, 1u << 16);
		emmc_wr(SDC_CMD, 13u | CMD_RSP_R1);
		if (!emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, "status")) {
			return false;
		}
		uint32_t r1 = emmc_rd(SDC_RESP0);
		if ((r1 & R1_READY_FOR_DATA) && (r1 & R1_CUR_STATE_MASK) == R1_STATE_TRAN) {
			return true;
		}
	}
	return emmc_fail("card programming timeout", emmc_rd(SDC_RESP0));
}

static bool
emmc_read_blocks(uint64_t lba, uint32_t blocks, uint32_t *dst)
{
	if (!emmc_wait_idle()) {
		return false;
	}
	emmc_pio_mode();
	emmc_wr(SDC_BLK_NUM, blocks);
	emmc_wr(SDC_ARG, (uint32_t)lba);
	emmc_wr(SDC_CMD, (blocks == 1 ? 17u : 18u) | CMD_RSP_R1 |
	    (blocks == 1 ? CMD_DTYPE_SINGLE : CMD_DTYPE_MULTI) | CMD_BLKLEN(512u));
	if (!emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, "read cmd")) {
		return false;
	}
	for (uint32_t left = blocks * (512u / 4u); left > 0;) {
		uint32_t fifo = emmc_rd(MSDC_FIFOCS) & 0xffu;
		uint32_t spin = 0;

		while (fifo < 4u) {
			uint32_t status = emmc_rd(MSDC_INT);

			if (status & (INT_DATTMO | INT_DATCRCERR)) {
				emmc_wr(MSDC_INT, status);
				if (blocks > 1u) {
					(void)emmc_stop();
				}
				return emmc_fail("read data", status);
			}
			if (++spin > 20000000U) {
				return emmc_fail("read stalled", emmc_rd(MSDC_FIFOCS));
			}
			fifo = emmc_rd(MSDC_FIFOCS) & 0xffu;
		}
		uint32_t words = (fifo / 4u) < left ? (fifo / 4u) : left;
		for (uint32_t i = 0; i < words; i++) {
			*dst++ = emmc_rd(MSDC_RXDATA);
		}
		left -= words;
	}
	if (!emmc_wait_int(INT_XFER_COMPL, INT_DATTMO | INT_DATCRCERR, "read end")) {
		return false;
	}
	return blocks == 1u || emmc_stop();
}

static bool
emmc_read_retrying(uint64_t lba, uint32_t blocks, uint32_t *dst)
{
	if (emmc_read_blocks(lba, blocks, dst)) {
		return true;
	}
	emmc_errors++;
	printf("emmc: read %u blocks @ %llu failed (#%llu): %s (%#x); retrying\n",
	    blocks, lba, emmc_errors, emmc_failed ? emmc_failed : "?", emmc_failed_status);
	emmc_pio_mode();
	(void)emmc_stop();
	return emmc_read_blocks(lba, blocks, dst);
}

static bool
emmc_write_blocks(uint64_t lba, uint32_t blocks, const uint32_t *src)
{
	/* Hard safety fence: never allow writes outside the discovered root partition. */
	if (!emmc_writable || emmc_lba == 0 || lba < emmc_lba ||
	    blocks == 0 || blocks > emmc_blocks || (lba - emmc_lba) > (emmc_blocks - blocks)) {
		return emmc_fail("write out of partition bounds", (uint32_t)lba);
	}
	if (!emmc_wait_idle()) {
		return false;
	}
	emmc_pio_mode();
	emmc_wr(SDC_BLK_NUM, blocks);
	emmc_wr(SDC_ARG, (uint32_t)lba);
	emmc_wr(SDC_CMD, (blocks == 1 ? 24u : 25u) | CMD_RSP_R1 | CMD_RW |
	    (blocks == 1 ? CMD_DTYPE_SINGLE : CMD_DTYPE_MULTI) | CMD_BLKLEN(512u));
	if (!emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, "write cmd")) {
		return false;
	}
	for (uint32_t left = blocks * (512u / 4u); left > 0;) {
		uint32_t txcnt = (emmc_rd(MSDC_FIFOCS) >> 16) & 0xffu;
		uint32_t spin = 0;

		while (txcnt > EMMC_FIFO_BYTES - 4u) {
			uint32_t status = emmc_rd(MSDC_INT);

			if (status & (INT_DATTMO | INT_DATCRCERR)) {
				emmc_wr(MSDC_INT, status);
				if (blocks > 1u) {
					(void)emmc_stop();
				}
				return emmc_fail("write data", status);
			}
			if (++spin > 20000000U) {
				return emmc_fail("write stalled", emmc_rd(MSDC_FIFOCS));
			}
			txcnt = (emmc_rd(MSDC_FIFOCS) >> 16) & 0xffu;
		}
		uint32_t room_words = (EMMC_FIFO_BYTES - txcnt) / 4u;
		uint32_t words = room_words < left ? room_words : left;
		for (uint32_t i = 0; i < words; i++) {
			emmc_wr(MSDC_TXDATA, *src++);
		}
		left -= words;
	}
	if (!emmc_wait_int(INT_XFER_COMPL, INT_DATTMO | INT_DATCRCERR, "write end")) {
		return false;
	}
	if (blocks > 1u && !emmc_stop()) {
		return false;
	}
	return emmc_wait_card_ready();
}

/* ---- GPT lookup (mtk_emmc_lock held) ---- */

static bool
emmc_load_gpt(void)
{
	if (emmc_gpt_loaded) {
		return true;
	}
	if (emmc_base == 0) {
		emmc_base = ml_io_map(MSDC0_PHYS, 0x1000);
	}
	const uint8_t *s = (const uint8_t *)emmc_sector;
	if (!emmc_read_retrying(1, 1, emmc_sector) || memcmp(s, "EFI PART", 8) != 0) {
		printf("emmc: no GPT header (%s)\n", emmc_failed ? emmc_failed : "bad sig");
		return false;
	}
	uint64_t entries_lba = 0;
	uint32_t count = 0, entsize = 0;
	memcpy(&entries_lba, s + 72, 8);
	memcpy(&count, s + 80, 4);
	memcpy(&entsize, s + 84, 4);
	if (count == 0 || count > 128u || entsize != 128u) {
		printf("emmc: unsupported GPT geometry count=%u entsize=%u\n", count, entsize);
		return false;
	}
	uint32_t sectors = (count * entsize + 511u) / 512u;
	if (!emmc_read_retrying(entries_lba, sectors, emmc_gpt_entries)) {
		printf("emmc: failed to read %u GPT sectors @ %llu\n", sectors, entries_lba);
		return false;
	}
	emmc_gpt_count = count;
	emmc_gpt_entsize = entsize;
	emmc_gpt_loaded = true;
	printf("emmc: MSDC0 GPT OK (%u entries @ LBA %llu)\n", count, entries_lba);
	return true;
}

static bool
emmc_lookup_part_locked(const char *name, uint64_t *firstp, uint64_t *blocksp)
{
	if (!emmc_load_gpt()) {
		return false;
	}
	for (uint32_t i = 0; i < emmc_gpt_count; i++) {
		const uint8_t *e = (const uint8_t *)emmc_gpt_entries + i * emmc_gpt_entsize;
		size_t k;
		for (k = 0; name[k] != '\0' && k < 36u &&
		    e[56 + 2 * k] == (uint8_t)name[k] && e[57 + 2 * k] == 0; k++) {
		}
		if (name[k] != '\0' || (k < 36u && (e[56 + 2 * k] != 0 || e[57 + 2 * k] != 0))) {
			continue;
		}
		uint64_t first = 0, last = 0;
		memcpy(&first, e + 32, 8);
		memcpy(&last, e + 40, 8);
		if (first == 0 || last < first) {
			return false;
		}
		*firstp = first;
		*blocksp = last - first + 1;
		return true;
	}
	return false;
}

static int
emmc_read_range_locked(uint64_t first, uint64_t nblocks, uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
	uint64_t total = nblocks * 512ULL;
	if (sizep != NULL) {
		*sizep = total;
	}
	if (len == 0) {
		return 0;
	}
	if (buf == NULL || off >= total || len > total - off) {
		return EINVAL;
	}
	uint8_t *dst = (uint8_t *)buf;
	uint32_t done = 0;
	while (done < len) {
		uint64_t cur_off = off + done;
		uint64_t sec = cur_off / 512ULL;
		uint32_t sub = (uint32_t)(cur_off % 512ULL);
		uint32_t rem = len - done;

		if (sub == 0 && rem >= 512u && (((uintptr_t)(dst + done)) & 3u) == 0) {
			uint32_t blks = rem / 512u;
			if (blks > EMMC_MAX_BLOCKS) {
				blks = EMMC_MAX_BLOCKS;
			}
			if (!emmc_read_retrying(first + sec, blks, (uint32_t *)(void *)(dst + done))) {
				return EIO;
			}
			done += blks * 512u;
		} else {
			uint32_t n = 512u - sub;
			if (n > rem) {
				n = rem;
			}
			if (!emmc_read_retrying(first + sec, 1u, emmc_sector)) {
				return EIO;
			}
			memcpy(dst + done, (const uint8_t *)emmc_sector + sub, n);
			done += n;
		}
	}
	return 0;
}

int mtk_emmc_read_part(const char *name, uint64_t off, void *buf, uint32_t len, uint64_t *sizep);
int
mtk_emmc_read_part(const char *name, uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
	if (name == NULL || name[0] == '\0') {
		return EINVAL;
	}
	lck_mtx_lock(&mtk_emmc_lock);
	uint64_t first = 0, nblocks = 0;
	int err = emmc_lookup_part_locked(name, &first, &nblocks)
	    ? emmc_read_range_locked(first, nblocks, off, buf, len, sizep)
	    : ENOENT;
	lck_mtx_unlock(&mtk_emmc_lock);
	return err;
}

int mtk_emmc_read_root(uint64_t off, void *buf, uint32_t len, uint64_t *sizep);
int
mtk_emmc_read_root(uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
	if (!emmc_ready) {
		return ENXIO;
	}
	lck_mtx_lock(&mtk_emmc_lock);
	int err = emmc_read_range_locked(emmc_lba, emmc_part_blocks, off, buf, len, sizep);
	lck_mtx_unlock(&mtk_emmc_lock);
	return err;
}

/* ---- BSD block/char device ---- */

static int
mtk_emmc_open(__unused dev_t dev, int flags, __unused int devtype, __unused proc_t p)
{
	if ((flags & FWRITE) && !emmc_writable) {
		return EROFS;
	}
	return 0;
}

static int
mtk_emmc_close(__unused dev_t dev, __unused int flags, __unused int devtype, __unused proc_t p)
{
	return 0;
}

static void
mtk_emmc_strategy(buf_t bp)
{
	uint64_t off = (uint64_t)buf_blkno(bp) * emmc_secsize;
	uint64_t part_bytes = emmc_blocks * 512ULL;
	uint32_t count = buf_count(bp);
	uint32_t done = 0;
	caddr_t vaddr = NULL;
	bool is_read = (buf_flags(bp) & B_READ) != 0;
	int error = 0;

	buf_setresid(bp, count);
	if (!is_read && !emmc_writable) {
		error = EROFS;
		goto out;
	}
	if (off >= part_bytes) {
		error = (off == part_bytes) ? 0 : EINVAL;
		goto out;
	}
	if (count == 0) {
		goto out;
	}
	if ((uint64_t)count > part_bytes - off) {
		count = (uint32_t)(part_bytes - off);
	}
	/* Cluster I/O (UPL) buffers have NULL buf_dataptr(bp); buf_map maps them safely! */
	if (buf_map(bp, &vaddr) != 0 || vaddr == NULL) {
		error = EIO;
		goto out;
	}
	uint8_t *data = (uint8_t *)vaddr;

	lck_mtx_lock(&mtk_emmc_lock);
	while (done < count && error == 0) {
		uint64_t cur_off = off + done;
		uint64_t cur_lba = emmc_lba + (cur_off / 512ULL);
		uint32_t sub = (uint32_t)(cur_off % 512ULL);
		uint32_t rem = count - done;
		uint32_t blks = (sub == 0) ? (rem / 512u) : 0u;

		if (is_read) {
			if (blks == 0 || (((uintptr_t)(data + done)) & 3u) != 0) {
				uint32_t n = 512u - sub;
				if (n > rem) {
					n = rem;
				}
				if (!emmc_read_retrying(cur_lba, 1u, emmc_sector)) {
					error = EIO;
					break;
				}
				memcpy(data + done, (const uint8_t *)emmc_sector + sub, n);
				done += n;
				continue;
			}
			if (blks > EMMC_MAX_BLOCKS) {
				blks = EMMC_MAX_BLOCKS;
			}
			if (!emmc_read_retrying(cur_lba, blks, (uint32_t *)(void *)(data + done))) {
				error = EIO;
				break;
			}
			done += blks * 512u;
		} else {
			if (blks == 0 || (((uintptr_t)(data + done)) & 3u) != 0) {
				uint32_t n = 512u - sub;
				if (n > rem) {
					n = rem;
				}
				if (sub != 0 || n < 512u) {
					if (!emmc_read_retrying(cur_lba, 1u, emmc_sector)) {
						error = EIO;
						break;
					}
				}
				memcpy((uint8_t *)emmc_sector + sub, data + done, n);
				if (!emmc_write_blocks(cur_lba, 1u, emmc_sector)) {
					error = EIO;
					break;
				}
				done += n;
				continue;
			}
			if (blks > EMMC_MAX_BLOCKS) {
				blks = EMMC_MAX_BLOCKS;
			}
			if (!emmc_write_blocks(cur_lba, blks, (const uint32_t *)(const void *)(data + done))) {
				error = EIO;
				break;
			}
			done += blks * 512u;
		}
	}
	lck_mtx_unlock(&mtk_emmc_lock);
	buf_unmap(bp);
	buf_setresid(bp, buf_count(bp) - done);
	if (error != 0) {
		printf("emmc: %s off %llu (%u B) failed: %s (%#x)\n",
		    is_read ? "read" : "write", off, count,
		    emmc_failed ? emmc_failed : "?", emmc_failed_status);
	}
out:
	if (error != 0) {
		buf_seterror(bp, error);
	}
	buf_biodone(bp);
}

static int
mtk_emmc_ioctl(__unused dev_t dev, u_long cmd, caddr_t data, __unused int fflag, __unused proc_t p)
{
	switch (cmd) {
	case DKIOCGETBLOCKSIZE:
		*(uint32_t *)data = emmc_secsize;
		return 0;
	case DKIOCSETBLOCKSIZE: {
		uint32_t bs = *(uint32_t *)data;
		if (bs < 512u || bs > 4096u || (bs & (bs - 1u)) != 0) {
			return EINVAL;
		}
		emmc_secsize = bs;
		return 0;
	}
	case DKIOCGETPHYSICALBLOCKSIZE:
		*(uint32_t *)data = 512u;
		return 0;
	case DKIOCGETBLOCKCOUNT:
		*(uint64_t *)data = (emmc_blocks * 512ULL) / emmc_secsize;
		return 0;
	case DKIOCISWRITABLE:
		*(uint32_t *)data = emmc_writable ? 1u : 0u;
		return 0;
	case DKIOCSYNCHRONIZE:
	case DKIOCSYNCHRONIZECACHE:
		return 0;
	case DKIOCGETMAXBLOCKCOUNTREAD:
		*(uint64_t *)data = (EMMC_MAX_BLOCKS * 512ULL) / emmc_secsize;
		return 0;
	case DKIOCGETMAXBLOCKCOUNTWRITE:
		*(uint64_t *)data = emmc_writable ? ((EMMC_MAX_BLOCKS * 512ULL) / emmc_secsize) : 0ULL;
		return 0;
	case DKIOCGETMAXSEGMENTCOUNTREAD:
	case DKIOCGETMAXSEGMENTCOUNTWRITE:
		*(uint64_t *)data = 1ULL;
		return 0;
	default:
		return ENOTTY;
	}
}

static int
mtk_emmc_size(__unused dev_t dev)
{
	return 512;
}

static int
mtk_emmc_read(dev_t dev, struct uio *uio, __unused int ioflag)
{
	return physio(mtk_emmc_strategy, NULL, dev, B_READ, (u_int (*)(buf_t))mtk_emmc_size, uio, 512);
}

static int
mtk_emmc_write(dev_t dev, struct uio *uio, __unused int ioflag)
{
	if (!emmc_writable) {
		return EROFS;
	}
	return physio(mtk_emmc_strategy, NULL, dev, B_WRITE, (u_int (*)(buf_t))mtk_emmc_size, uio, 512);
}

static const struct bdevsw mtk_emmc_bdevsw = {
	.d_open     = mtk_emmc_open,
	.d_close    = mtk_emmc_close,
	.d_strategy = mtk_emmc_strategy,
	.d_ioctl    = mtk_emmc_ioctl,
	.d_dump     = eno_dump,
	.d_psize    = mtk_emmc_size,
	.d_type     = D_DISK,
};

static const struct cdevsw mtk_emmc_cdevsw = {
	.d_open       = mtk_emmc_open,
	.d_close      = mtk_emmc_close,
	.d_read       = mtk_emmc_read,
	.d_write      = mtk_emmc_write,
	.d_ioctl      = mtk_emmc_ioctl,
	.d_stop       = eno_stop,
	.d_reset      = eno_reset,
	.d_ttys       = NULL,
	.d_select     = eno_select,
	.d_mmap       = eno_mmap,
	.d_strategy   = eno_strat,
	.d_reserved_1 = eno_getc,
	.d_reserved_2 = eno_putc,
	.d_type       = D_DISK,
};

static int
mtk_emmc_init(void)
{
	char want[36] = EMMC_DEFAULT_PART;
	uint32_t rw = 0;

	PE_parse_boot_argn("mp_emmcroot", want, sizeof(want));
	PE_parse_boot_argn("mp_rootrw", &rw, sizeof(rw));
	emmc_writable = (rw != 0);

	lck_mtx_lock(&mtk_emmc_lock);
	uint64_t first = 0, nblocks = 0;
	if (!emmc_lookup_part_locked(want, &first, &nblocks)) {
		lck_mtx_unlock(&mtk_emmc_lock);
		printf("emmc: GPT partition '%s' not found on MSDC0\n", want);
		return ENOENT;
	}
	emmc_lba = first;
	emmc_part_blocks = nblocks;
	emmc_blocks = nblocks;

	/* Probe filesystem signature: support HFS+/HFSX (Open Mapple), APFS ("NXSB"), or raw. */
	uint8_t probe[1536];
	if (emmc_read_range_locked(first, nblocks, 0, probe, sizeof(probe), NULL) == 0) {
		uint16_t hfs_sig = (uint16_t)((uint16_t)probe[1024] << 8 | probe[1025]);
		uint32_t apfs_sig = 0;
		memcpy(&apfs_sig, probe + 32, 4);
		if (hfs_sig == 0x482bu || hfs_sig == 0x4858u) {
			printf("emmc: '%s' @ LBA %llu (%llu MiB): HFS%c volume (%s)\n",
			    want, first, (nblocks * 512ULL) >> 20,
			    hfs_sig == 0x4858u ? 'X' : '+', emmc_writable ? "RW" : "RO");
		} else if (apfs_sig == 0x4253584eu) {
			uint64_t at = 0;
			while (at < nblocks * 512ULL) {
				uint32_t bs = 0;
				uint64_t nb = 0;
				if (!emmc_read_retrying(first + at / 512ULL, 1u, emmc_sector) ||
				    emmc_sector[8] != 0x4253584eu) {
					break;
				}
				bs = emmc_sector[9];
				memcpy(&nb, ((const uint8_t *)emmc_sector) + 40, 8);
				if (bs < 512u || bs > 65536u || nb == 0 || nb * bs > nblocks * 512ULL - at) {
					break;
				}
				at += nb * bs;
			}
			if (at > 0) {
				emmc_blocks = at / 512ULL;
			}
			printf("emmc: '%s' @ LBA %llu: APFS container %llu MiB of %llu MiB (%s)\n",
			    want, first, (emmc_blocks * 512ULL) >> 20, (nblocks * 512ULL) >> 20,
			    emmc_writable ? "RW" : "RO");
		} else {
			printf("emmc: '%s' @ LBA %llu (%llu MiB): raw partition (%s)\n",
			    want, first, (nblocks * 512ULL) >> 20, emmc_writable ? "RW" : "RO");
		}
	}
	emmc_ready = true;
	lck_mtx_unlock(&mtk_emmc_lock);

	emmc_bmajor = bdevsw_add(-1, &mtk_emmc_bdevsw);
	if (emmc_bmajor < 0 ||
	    (emmc_cmajor = cdevsw_add_with_bdev(-1, &mtk_emmc_cdevsw, emmc_bmajor)) < 0) {
		printf("emmc: failed to allocate major numbers (%d, %d)\n", emmc_bmajor, emmc_cmajor);
		emmc_ready = false;
		return ENXIO;
	}
	emmc_bdev = makedev(emmc_bmajor, 0);
	devfs_make_node(emmc_bdev, DEVFS_BLOCK, UID_ROOT, GID_OPERATOR, 0640, "emmc0");
	devfs_make_node(makedev(emmc_cmajor, 0), DEVFS_CHAR, UID_ROOT, GID_OPERATOR, 0640, "remmc0");
	return 0;
}

int mtk_emmc_rootdev(dev_t *dev);
int
mtk_emmc_rootdev(dev_t *dev)
{
	static bool usblink_done;
	if (!usblink_done) {
		usblink_done = true;
		usblink_probe();
	}
	if (!emmc_ready) {
		int err = mtk_emmc_init();
		if (err != 0) {
			return err;
		}
	}
	*dev = emmc_bdev;
	return 0;
}
#endif /* ARM64_BOARD_CONFIG_MT6781 */
