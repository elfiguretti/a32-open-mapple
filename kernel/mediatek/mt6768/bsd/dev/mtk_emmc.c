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
#include <arm/caches_internal.h>
#include <kern/clock.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <miscfs/devfs/devfs.h>
#include <pexpert/pexpert.h>
#include <sys/buf.h>
#include <sys/conf.h>
#include <sys/disk.h>
#include <sys/errno.h>
#include <sys/fcntl.h>
#include <sys/systm.h>
#include <vm/pmap.h>
#include <vm/vm_kern.h>

extern ppnum_t pmap_find_phys(pmap_t pmap, addr64_t va);
extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern void usblink_probe(void);
extern kern_return_t thread_terminate(thread_t thread);
static void emmc_reloc_boot_sync(void);

#define MSDC0_PHYS              0x11230000UL
#define EMMC_DEFAULT_PART       "xnusys"
#define EMMC_MAX_BLOCKS         256U            /* 128 KiB per transfer */
#define EMMC_DMA_SIZE           (EMMC_MAX_BLOCKS * 512U)
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

/* MT6768 ComboA MSDC0 Basic DMA registers (drivers/mmc/host/mediatek/ComboA/msdc_reg.h) */
#define MSDC_DMA_SA_HIGH        0x8cU
#define MSDC_DMA_SA             0x90U
#define MSDC_DMA_CTRL           0x98U
#define MSDC_DMA_CFG            0x9cU
#define MSDC_DMA_LEN            0xa8U
#define MSDC_DMA_CTRL_START     (1u << 0)
#define MSDC_DMA_CTRL_STOP      (1u << 1)
#define MSDC_DMA_CTRL_LASTBUF   (1u << 10)
#define MSDC_DMA_BURST_64B      6u
#define MSDC_DMA_CFG_STS        (1u << 0)

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
static vm_offset_t emmc_dma_va;
static uint64_t emmc_dma_pa;
static bool emmc_dma_ok;
static uint64_t emmc_dma_ops;
static uint32_t emmc_dma_errs;

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

static void
emmc_dma_stop_quiet(void)
{
	if (emmc_rd(MSDC_DMA_CFG) & MSDC_DMA_CFG_STS) {
		emmc_wr(MSDC_DMA_CTRL, MSDC_DMA_CTRL_STOP);
		for (uint32_t i = 0; i < 200000U && (emmc_rd(MSDC_DMA_CFG) & MSDC_DMA_CFG_STS); i++) {
		}
	}
}

static bool
emmc_dma_xfer(uint64_t lba, uint32_t blocks, void *buf, bool write)
{
	if (emmc_dma_va == 0 || blocks == 0 || blocks > EMMC_MAX_BLOCKS) {
		return false;
	}
	if (!emmc_wait_idle()) {
		return false;
	}
	emmc_dma_stop_quiet();
	emmc_wr(MSDC_CFG, emmc_rd(MSDC_CFG) & ~MSDC_CFG_PIO);
	emmc_wr(MSDC_INT, emmc_rd(MSDC_INT));
	emmc_int_seen = 0;
	emmc_wr(MSDC_FIFOCS, emmc_rd(MSDC_FIFOCS) | MSDC_FIFOCS_CLR);
	for (uint32_t i = 0; i < 100000U && (emmc_rd(MSDC_FIFOCS) & MSDC_FIFOCS_CLR); i++) {
	}

	uint32_t len = blocks * 512u;
	if (write) {
		memcpy((void *)emmc_dma_va, buf, len);
		clean_dcache64((addr64_t)emmc_dma_pa, len, TRUE);
	} else {
		flush_dcache64((addr64_t)emmc_dma_pa, len, TRUE);
	}
	__builtin_arm_dsb(0xf);

	emmc_wr(SDC_BLK_NUM, blocks);
	emmc_wr(SDC_ARG, (uint32_t)lba);
	emmc_wr(SDC_CMD, (write ? (blocks == 1u ? 24u : 25u) : (blocks == 1u ? 17u : 18u)) |
	    CMD_RSP_R1 | (write ? CMD_RW : 0u) |
	    (blocks == 1u ? CMD_DTYPE_SINGLE : CMD_DTYPE_MULTI) | CMD_BLKLEN(512u));
	if (!emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, write ? "dma write cmd" : "dma read cmd")) {
		return false;
	}

	emmc_wr(MSDC_DMA_SA_HIGH, (uint32_t)(emmc_dma_pa >> 32) & 0xfu);
	emmc_wr(MSDC_DMA_SA, (uint32_t)emmc_dma_pa);
	emmc_wr(MSDC_DMA_LEN, len);
	uint32_t ctrl = MSDC_DMA_CTRL_LASTBUF | (MSDC_DMA_BURST_64B << 12);
	emmc_wr(MSDC_DMA_CTRL, ctrl);
	__builtin_arm_dsb(0xf);
	emmc_wr(MSDC_DMA_CTRL, ctrl | MSDC_DMA_CTRL_START);

	if (!emmc_wait_int(INT_XFER_COMPL, INT_DATTMO | INT_DATCRCERR, write ? "dma write end" : "dma read end")) {
		emmc_dma_stop_quiet();
		if (blocks > 1u) {
			(void)emmc_stop();
		}
		return false;
	}
	for (uint32_t i = 0; i < 1000000U && (emmc_rd(MSDC_DMA_CFG) & MSDC_DMA_CFG_STS); i++) {
	}
	if (emmc_rd(MSDC_DMA_CFG) & MSDC_DMA_CFG_STS) {
		emmc_dma_stop_quiet();
		if (blocks > 1u) {
			(void)emmc_stop();
		}
		return emmc_fail("dma busy", emmc_rd(MSDC_DMA_CFG));
	}
	if (!write) {
		__builtin_arm_dsb(0xf);
		flush_dcache64((addr64_t)emmc_dma_pa, len, TRUE);
		__builtin_arm_dsb(0xf);
		memcpy(buf, (const void *)emmc_dma_va, len);
	}
	if (blocks > 1u && !emmc_stop()) {
		return false;
	}
	if (write && !emmc_wait_card_ready()) {
		return false;
	}
	emmc_dma_ops++;
	return true;
}

static bool
emmc_read_retrying(uint64_t lba, uint32_t blocks, uint32_t *dst)
{
	if (emmc_dma_ok && emmc_dma_xfer(lba, blocks, dst, false)) {
		return true;
	}
	if (emmc_dma_ok) {
		emmc_dma_errs++;
		printf("emmc: DMA read %u blocks @ %llu failed (#%u after %llu ops: %s %#x); falling back to PIO\n",
		    blocks, lba, emmc_dma_errs, emmc_dma_ops, emmc_failed ? emmc_failed : "?", emmc_failed_status);
		if (emmc_dma_errs >= 3u) {
			emmc_dma_ok = false;
			printf("emmc: disabling DMA after %u errors\n", emmc_dma_errs);
		}
		emmc_pio_mode();
		(void)emmc_stop();
	}
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
	if (emmc_dma_ok) {
		if (emmc_dma_xfer(lba, blocks, (void *)(uintptr_t)src, true)) {
			return true;
		}
		emmc_dma_errs++;
		printf("emmc: DMA write %u blocks @ %llu failed (#%u: %s %#x); falling back to PIO\n",
		    blocks, lba, emmc_dma_errs, emmc_failed ? emmc_failed : "?", emmc_failed_status);
		if (emmc_dma_errs >= 3u) {
			emmc_dma_ok = false;
			printf("emmc: disabling DMA after %u errors\n", emmc_dma_errs);
		}
		emmc_pio_mode();
		(void)emmc_stop();
		(void)emmc_wait_card_ready();
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

	uint32_t want_dma = 1;
	PE_parse_boot_argn("a32_emmc_dma", &want_dma, sizeof(want_dma));
	if (want_dma != 0 && emmc_dma_va == 0) {
		ppnum_t max_pnum = (ppnum_t)(atop_64(1ULL << 32) - 1);
		if (kmem_alloc_contig(kernel_map, &emmc_dma_va, EMMC_DMA_SIZE, PAGE_MASK, max_pnum, 0,
		    KMA_DATA | KMA_ZERO | KMA_PERMANENT, VM_KERN_MEMORY_IOKIT) == KERN_SUCCESS) {
			ppnum_t pn = pmap_find_phys(kernel_pmap, (addr64_t)emmc_dma_va);
			if (pn != 0) {
				emmc_dma_pa = ptoa_64(pn);
			} else {
				emmc_dma_va = 0;
			}
		} else {
			printf("emmc: failed to allocate 128 KiB DMA bounce buffer; staying on PIO\n");
		}
	}

	lck_mtx_lock(&mtk_emmc_lock);
	uint64_t first = 0, nblocks = 0;
	if (!emmc_lookup_part_locked(want, &first, &nblocks)) {
		lck_mtx_unlock(&mtk_emmc_lock);
		printf("emmc: GPT partition '%s' not found on MSDC0\n", want);
		return ENOENT;
	}
	if (want_dma != 0 && emmc_dma_va != 0 && emmc_dma_pa != 0) {
		static uint32_t pio_ref[4 * 128] __attribute__((aligned(16)));
		static uint32_t dma_ref[4 * 128] __attribute__((aligned(16)));
		if (emmc_read_blocks(0ULL, 4u, pio_ref) &&
		    emmc_dma_xfer(0ULL, 4u, dma_ref, false) &&
		    memcmp(pio_ref, dma_ref, sizeof(pio_ref)) == 0) {
			emmc_dma_ok = true;
			printf("emmc: MSDC0 Basic DMA enabled (128 KiB bounce @ 0x%llx, verified 4-blk LBA 0)\n",
			    emmc_dma_pa);
		} else {
			printf("emmc: MSDC0 Basic DMA self-test failed (%s %#x); staying on PIO\n",
			    emmc_failed ? emmc_failed : "mismatch", emmc_failed_status);
			emmc_pio_mode();
			(void)emmc_stop();
		}
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

	{
		int do_reloc = 0;
		if (!emmc_writable &&
		    PE_parse_boot_argn("a32_reloc107g", &do_reloc, sizeof(do_reloc)) &&
		    do_reloc == 1) {
			emmc_reloc_boot_sync();
		}
	}

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

/* ---- One-shot PIT-safe 107.35 GiB xnusys relocation (RO boot only, explicit USB command) ---- */

static struct {
	int      state;          /* 0=idle, 1=running, 2=done, -1=error */
	uint64_t done_sec, total_sec;
	char     msg[160];
} reloc107;

static uint32_t
reloc_crc32(const uint8_t *buf, size_t len)
{
	uint32_t c = 0xffffffffu;
	for (size_t i = 0; i < len; i++) {
		c ^= buf[i];
		for (int b = 0; b < 8; b++) {
			c = (c & 1u) ? ((c >> 1) ^ 0xedb88320u) : (c >> 1);
		}
	}
	return c ^ 0xffffffffu;
}

/* Raw write strictly fenced to the relocation target [19144704..160391167] or GPT sectors 1..15 / 244285407..244285439. */
static bool
emmc_reloc_write_locked(uint64_t lba, uint32_t blocks, const uint32_t *src)
{
	bool ok_data = (lba >= 19144704ULL && blocks > 0 && blocks <= EMMC_MAX_BLOCKS &&
	    lba + blocks <= 160391168ULL);
	bool ok_pri  = (lba == 1ULL && blocks == 15u);
	bool ok_bak  = (lba == 244285407ULL && blocks == 33u);
	if (!ok_data && !ok_pri && !ok_bak) {
		return emmc_fail("reloc write out of allowed range", (uint32_t)lba);
	}
	if (emmc_dma_ok && emmc_dma_xfer(lba, blocks, (void *)(uintptr_t)src, true)) {
		return true;
	}
	if (!emmc_wait_idle()) {
		return false;
	}
	emmc_pio_mode();
	emmc_wr(SDC_BLK_NUM, blocks);
	emmc_wr(SDC_ARG, (uint32_t)lba);
	emmc_wr(SDC_CMD, (blocks == 1 ? 24u : 25u) | CMD_RSP_R1 | CMD_RW |
	    (blocks == 1 ? CMD_DTYPE_SINGLE : CMD_DTYPE_MULTI) | CMD_BLKLEN(512u));
	if (!emmc_wait_int(INT_CMDRDY, INT_CMDTMO | INT_RSPCRCERR, "reloc wcmd")) {
		return false;
	}
	for (uint32_t left = blocks * (512u / 4u); left > 0;) {
		uint32_t txcnt = (emmc_rd(MSDC_FIFOCS) >> 16) & 0xffu;
		uint32_t spin = 0;
		while (txcnt > EMMC_FIFO_BYTES - 4u) {
			uint32_t st = emmc_rd(MSDC_INT);
			if (st & (INT_DATTMO | INT_DATCRCERR)) {
				emmc_wr(MSDC_INT, st);
				if (blocks > 1u) (void)emmc_stop();
				return emmc_fail("reloc wdata", st);
			}
			if (++spin > 20000000U) {
				return emmc_fail("reloc wstalled", emmc_rd(MSDC_FIFOCS));
			}
			txcnt = (emmc_rd(MSDC_FIFOCS) >> 16) & 0xffu;
		}
		uint32_t words = (EMMC_FIFO_BYTES - txcnt) / 4u;
		if (words > left) words = left;
		for (uint32_t i = 0; i < words; i++) {
			emmc_wr(MSDC_TXDATA, *src++);
		}
		left -= words;
	}
	if (!emmc_wait_int(INT_XFER_COMPL, INT_DATTMO | INT_DATCRCERR, "reloc wend")) {
		return false;
	}
	if (blocks > 1u && !emmc_stop()) {
		return false;
	}
	return emmc_wait_card_ready();
}

static void
emmc_reloc_do_work(void)
{
	static uint32_t buf[EMMC_MAX_BLOCKS * 128] __attribute__((aligned(64)));
	static uint8_t pri[15 * 512] __attribute__((aligned(64)));
	static uint8_t bak[33 * 512] __attribute__((aligned(64)));
	static uint8_t rb[33 * 512] __attribute__((aligned(64)));
	static uint32_t vh_src[128], vh_dst[128], alt_src[128], alt_dst[128];
	const uint64_t OLD_FIRST = 160391168ULL, NEW_FIRST = 19144704ULL;
	const uint64_t USER_FIRST = 19111936ULL, USER_NEW_LAST = 19144703ULL, PART_LAST = 244277247ULL;
	const uint64_t BAK_LBA = 244285407ULL;

	uint64_t total = reloc107.total_sec;
	uint64_t done = 0;
	while (done < total) {
		uint32_t n = (total - done > EMMC_MAX_BLOCKS) ? EMMC_MAX_BLOCKS : (uint32_t)(total - done);
		lck_mtx_lock(&mtk_emmc_lock);
		bool ok = emmc_read_retrying(OLD_FIRST + done, n, buf) &&
		    emmc_reloc_write_locked(NEW_FIRST + done, n, buf);
		lck_mtx_unlock(&mtk_emmc_lock);
		if (!ok) {
			snprintf(reloc107.msg, sizeof(reloc107.msg), "copy failed at LBA +%llu (%s %#x)",
			    done, emmc_failed ? emmc_failed : "?", emmc_failed_status);
			printf("emmc: relocate107g ERROR: %s\n", reloc107.msg);
			reloc107.state = -1;
			return;
		}
		done += n;
		reloc107.done_sec = done;
		if ((done & ((1ULL << 20) - 1)) == 0 || done == total) {
			printf("emmc: relocate107g %llu / %llu MiB (DMA=%u)\n",
			    (done * 512ULL) >> 20, (total * 512ULL) >> 20, emmc_dma_ok ? 1u : 0u);
		}
	}

	/* Verify Primary (+2) and Alternate (+total-2) HFSX Volume Headers bit-for-bit */
	lck_mtx_lock(&mtk_emmc_lock);
	bool vok = emmc_read_retrying(OLD_FIRST + 2, 1, vh_src) &&
	    emmc_read_retrying(NEW_FIRST + 2, 1, vh_dst) &&
	    emmc_read_retrying(OLD_FIRST + total - 2, 1, alt_src) &&
	    emmc_read_retrying(NEW_FIRST + total - 2, 1, alt_dst);
	lck_mtx_unlock(&mtk_emmc_lock);
	if (!vok || memcmp(vh_src, vh_dst, 512) != 0 || memcmp(alt_src, alt_dst, 512) != 0) {
		snprintf(reloc107.msg, sizeof(reloc107.msg), "HFSX header verify mismatch! GPT NOT modified.");
		printf("emmc: relocate107g ERROR: %s\n", reloc107.msg);
		reloc107.state = -1;
		return;
	}
	printf("emmc: relocate107g HFSX Primary+Alternate headers verified bit-for-bit!\n");

	/* Read live Primary (LBA 1..15) + Backup (LBA 244285407..244285439) GPT, patch slot #49 & #50, recompute CRCs */
	lck_mtx_lock(&mtk_emmc_lock);
	bool gok = emmc_read_retrying(1, 15, (uint32_t *)(void *)pri) &&
	    emmc_read_retrying(BAK_LBA, 33, (uint32_t *)(void *)bak);
	if (gok) {
		uint32_t hsz = 0, npe = 0, pesz = 0;
		memcpy(&hsz, pri + 12, 4);
		memcpy(&npe, pri + 80, 4);
		memcpy(&pesz, pri + 84, 4);
		uint64_t f49 = 0, l49 = 0, f50 = 0, l50 = 0;
		memcpy(&f49, pri + 512 + 48 * 128 + 32, 8);
		memcpy(&l49, pri + 512 + 48 * 128 + 40, 8);
		memcpy(&f50, pri + 512 + 49 * 128 + 32, 8);
		memcpy(&l50, pri + 512 + 49 * 128 + 40, 8);
		if (memcmp(pri, "EFI PART", 8) != 0 || hsz != 512 || npe != 54 || pesz != 128 ||
		    f49 != USER_FIRST || l49 != OLD_FIRST - 1 || f50 != OLD_FIRST || l50 != PART_LAST ||
		    memcmp(bak + 32 * 512, "EFI PART", 8) != 0) {
			gok = false;
			snprintf(reloc107.msg, sizeof(reloc107.msg), "unexpected live GPT geometry");
		} else {
			uint64_t u_last = USER_NEW_LAST, x_first = NEW_FIRST;
			memcpy(pri + 512 + 48 * 128 + 40, &u_last, 8);
			memcpy(pri + 512 + 49 * 128 + 32, &x_first, 8);
			uint32_t pecrc = reloc_crc32(pri + 512, npe * pesz);
			memcpy(pri + 88, &pecrc, 4);
			uint32_t zero = 0;
			memcpy(pri + 16, &zero, 4);
			uint32_t hcrc = reloc_crc32(pri, hsz);
			memcpy(pri + 16, &hcrc, 4);

			/* Backup GPT: copy 54 entries, zero 54..127, recompute header CRC (bhsz=92, bnpe=128) */
			uint32_t bhsz = 0, bnpe = 0;
			memcpy(&bhsz, bak + 32 * 512 + 12, 4);
			memcpy(&bnpe, bak + 32 * 512 + 80, 4);
			if (bhsz != 92 || bnpe != 128) {
				gok = false;
				snprintf(reloc107.msg, sizeof(reloc107.msg), "unexpected backup GPT bhsz=%u bnpe=%u", bhsz, bnpe);
			} else {
				memcpy(bak, pri + 512, npe * 128);
				bzero(bak + npe * 128, (bnpe - npe) * 128);
				uint32_t bpecrc = reloc_crc32(bak, bnpe * 128);
				memcpy(bak + 32 * 512 + 88, &bpecrc, 4);
				memcpy(bak + 32 * 512 + 16, &zero, 4);
				uint32_t bhcrc = reloc_crc32(bak + 32 * 512, bhsz);
				memcpy(bak + 32 * 512 + 16, &bhcrc, 4);

				gok = emmc_reloc_write_locked(1, 15, (const uint32_t *)(const void *)pri) &&
				    emmc_reloc_write_locked(BAK_LBA, 33, (const uint32_t *)(const void *)bak) &&
				    emmc_read_retrying(1, 15, (uint32_t *)(void *)rb) &&
				    memcmp(rb, pri, 15 * 512) == 0 &&
				    emmc_read_retrying(BAK_LBA, 33, (uint32_t *)(void *)rb) &&
				    memcmp(rb, bak, 33 * 512) == 0;
				if (!gok) {
					snprintf(reloc107.msg, sizeof(reloc107.msg), "GPT write/verify failed");
				}
			}
		}
	} else {
		snprintf(reloc107.msg, sizeof(reloc107.msg), "failed to read live GPT");
	}
	lck_mtx_unlock(&mtk_emmc_lock);
	if (!gok) {
		printf("emmc: relocate107g ERROR: %s\n", reloc107.msg);
		reloc107.state = -1;
		return;
	}
	snprintf(reloc107.msg, sizeof(reloc107.msg), "DONE! Relocated %llu MiB + wrote 107.35 GiB GPT. Put device in Download Mode and flash RW kernel!",
	    (total * 512ULL) >> 20);
	printf("emmc: relocate107g: %s\n", reloc107.msg);
	reloc107.state = 2;
}

static void
emmc_reloc_worker(__unused void *arg, __unused wait_result_t wr)
{
	emmc_reloc_do_work();
	thread_terminate(current_thread());
}

static void
emmc_reloc_boot_sync(void)
{
	printf("emmc: a32_reloc107g=1 pre-mount relocation starting (zero filesystems mounted)...\n");
	if (emmc_lba != 160391168ULL || emmc_part_blocks != 83886080ULL) {
		printf("emmc: relocate107g: xnusys @ LBA %llu (%llu MiB) -- already relocated or unexpected!\n",
		    emmc_lba, (emmc_part_blocks * 512ULL) >> 20);
		for (;;) {
			delay(1000000);
		}
	}
	uint32_t vh[128];
	lck_mtx_lock(&mtk_emmc_lock);
	bool ok = emmc_read_retrying(emmc_lba + 2, 1, vh);
	lck_mtx_unlock(&mtk_emmc_lock);
	const uint8_t *b = (const uint8_t *)vh;
	uint16_t sig = (uint16_t)((uint16_t)b[0] << 8 | b[1]);
	uint32_t bsz = (uint32_t)b[40] << 24 | (uint32_t)b[41] << 16 | (uint32_t)b[42] << 8 | b[43];
	uint32_t tot = (uint32_t)b[44] << 24 | (uint32_t)b[45] << 16 | (uint32_t)b[46] << 8 | b[47];
	uint64_t vol_sec = ((uint64_t)tot * bsz) / 512ULL;
	if (!ok || sig != 0x4858u || bsz != 4096u || vol_sec == 0 || vol_sec > emmc_part_blocks) {
		printf("emmc: relocate107g: bad HFSX header (sig=0x%04x bsz=%u tot=%u)\n", sig, bsz, tot);
		for (;;) {
			delay(1000000);
		}
	}
	reloc107.done_sec = 0;
	reloc107.total_sec = vol_sec;
	reloc107.state = 1;
	printf("emmc: relocate107g: copying %llu MiB (LBA 160391168 -> 19144704, DMA=%u)...\n",
	    (vol_sec * 512ULL) >> 20, emmc_dma_ok ? 1u : 0u);
	emmc_reloc_do_work();
	printf("emmc: relocate107g finished (state=%d). Hold Vol- + Power -> Vol+ + Vol- for Download Mode.\n", reloc107.state);
	for (;;) {
		delay(1000000);
	}
}

void mtk_emmc_relocate_107g_link(const char *args, char *out, size_t len);
void
mtk_emmc_relocate_107g_link(const char *args, char *out, size_t len)
{
	while (*args == ' ') args++;
	if (strncmp(args, "start", 5) != 0) {
		snprintf(out, len, "relocate107g: state=%d (%s) progress=%llu/%llu MiB | %s\n",
		    reloc107.state,
		    reloc107.state == 0 ? "idle" : reloc107.state == 1 ? "running" : reloc107.state == 2 ? "DONE" : "ERROR",
		    (reloc107.done_sec * 512ULL) >> 20, (reloc107.total_sec * 512ULL) >> 20, reloc107.msg);
		return;
	}
	if (reloc107.state == 1) {
		snprintf(out, len, "relocate107g: already running (%llu/%llu MiB)\n",
		    (reloc107.done_sec * 512ULL) >> 20, (reloc107.total_sec * 512ULL) >> 20);
		return;
	}
	if (emmc_writable) {
		snprintf(out, len, "relocate107g: REFUSED while mp_rootrw=1! Boot with mp_rootrw=0 (RO) first.\n");
		return;
	}
	if (!emmc_ready || emmc_lba != 160391168ULL || emmc_part_blocks != 83886080ULL) {
		snprintf(out, len, "relocate107g: unexpected xnusys LBA=%llu blks=%llu (already relocated?)\n",
		    emmc_lba, emmc_part_blocks);
		return;
	}
	uint32_t vh[128];
	lck_mtx_lock(&mtk_emmc_lock);
	bool ok = emmc_read_retrying(emmc_lba + 2, 1, vh);
	lck_mtx_unlock(&mtk_emmc_lock);
	const uint8_t *b = (const uint8_t *)vh;
	uint16_t sig = (uint16_t)((uint16_t)b[0] << 8 | b[1]);
	uint32_t bsz = (uint32_t)b[40] << 24 | (uint32_t)b[41] << 16 | (uint32_t)b[42] << 8 | b[43];
	uint32_t tot = (uint32_t)b[44] << 24 | (uint32_t)b[45] << 16 | (uint32_t)b[46] << 8 | b[47];
	uint64_t vol_sec = ((uint64_t)tot * bsz) / 512ULL;
	if (!ok || sig != 0x4858u || bsz != 4096u || vol_sec == 0 || vol_sec > emmc_part_blocks) {
		snprintf(out, len, "relocate107g: bad HFSX header (sig=0x%04x bsz=%u tot=%u)\n", sig, bsz, tot);
		return;
	}
	reloc107.done_sec = 0;
	reloc107.total_sec = vol_sec;
	reloc107.state = 1;
	snprintf(reloc107.msg, sizeof(reloc107.msg), "copying %llu MiB (DMA=%u)", (vol_sec * 512ULL) >> 20, emmc_dma_ok ? 1u : 0u);
	thread_t th;
	if (kernel_thread_start(emmc_reloc_worker, NULL, &th) != KERN_SUCCESS) {
		reloc107.state = -1;
		snprintf(out, len, "relocate107g: failed to spawn worker thread\n");
		return;
	}
	thread_deallocate(th);
	snprintf(out, len, "relocate107g: started (%llu MiB, DMA=%u). Poll with 'relocate107g status'.\n",
	    (vol_sec * 512ULL) >> 20, emmc_dma_ok ? 1u : 0u);
}
#endif /* ARM64_BOARD_CONFIG_MT6781 */
