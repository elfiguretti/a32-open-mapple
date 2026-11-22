#!/usr/bin/env python3
"""
Deterministic, self-verifying XNU source overlay for Samsung Galaxy A32 4G (SM-A325M,
MT6769T / mt6768, S6E3FC3 Super AMOLED DSI CMD, eMMC MSDC0, STM FTS5CU56A touch)
on top of Open Mapple's xnu_kernel_mediatek_common + xnu_kernel_mediatek_mt6781.

Applies:
  1) Copies SM-A325M MT6768 drivers into the XNU tree:
     - bsd/dev/mtk_emmc.c (Hito 4: MSDC0 @ 0x11230000 PIO block device, HFS+/HFSX/APFS, /dev/emmc0)
     - iokit/Kernel/arm/hid/STMTS.cpp (Hito 5: STMicroelectronics FTS5CU56A on i2c6 @ 0x49, EINT 10)
  2) Registers both drivers in bsd/conf/files, iokit/conf/files.arm64, iokit/KernelConfigTables.cpp,
     and hooks rd=emmc0 in iokit/bsddev/IOKitBSDInit.cpp.
  3) Patches bsd/dev/mtk_ufs.c:
     - Adds a32_console_hw_init() and a32_dsi_cmd_flush_now(): maps ramoops (0x4d09f000),
       infracfg_ao (0x10001000 -> ungates CLK_IFR_I2C_AP, CLK_IFR_I2C6, CLK_IFR_AP_MSDC0),
       OVL0 (0x1400b000), DISP_MUTEX (0x14001000), and DSI0 (0x14014000) via ml_io_map().
     - Mirrors all console output from mapple_log_append() into ramoops (survives warm reset
       -> /sys/fs/pstore/console-ramoops-2) and triggers the 7-write MT6768 DSI CMD flush on
       newlines (rate-limited) so XNU boot logs scroll live on the S6E3FC3 AMOLED.
     - Falls back to mtk_emmc_read_root()/mtk_emmc_read_part() when UFS is not present.
  4) Patches iokit/Kernel/arm/MapplePlatformExpert.cpp:
     - Calls a32_console_hw_init() at the very start of MapplePlatformExpert::start().
  5) Patches iokit/Kernel/arm/MappleFramebuffer.cpp:
     - Declares a32_dsi_cmd_flush_now() at the top of the file (before AppleCLCD::presentQueued).
     - Updates DISP_OVL0_PA/OVL0_2L/RDMA0/DSI0/SMI_LARB1/CCORR0 to verified MT6768 bases.
     - Calls a32_dsi_cmd_flush_now() whenever AppleCLCD::presentQueued() presents a frame.
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def replace_exact(path: Path, old: str, new: str, label: str):
    txt = path.read_text(encoding="utf-8")
    if old not in txt:
        if new == "" or new in txt:
            print(f"  [already applied] {label}")
            return
        raise SystemExit(f"ERROR: target snippet not found for {label} in {path}")
    if new and new not in old and new in txt:
        print(f"  [already applied] {label}")
        return
    cnt = txt.count(old)
    if cnt != 1:
        raise SystemExit(f"ERROR: expected 1 match for {label} in {path}, found {cnt}")
    path.write_text(txt.replace(old, new, 1), encoding="utf-8", newline="\n")
    print(f"  [ok] {label}")


def copy_drivers(xnu: Path):
    copies = [
        (HERE / "bsd" / "dev" / "mtk_emmc.c", xnu / "bsd" / "dev" / "mtk_emmc.c"),
        (HERE / "bsd" / "dev" / "a32_cpufreq.c", xnu / "bsd" / "dev" / "a32_cpufreq.c"),
        (HERE / "iokit" / "Kernel" / "arm" / "hid" / "STMTS.cpp",
         xnu / "iokit" / "Kernel" / "arm" / "hid" / "STMTS.cpp"),
    ]
    for src, dst in copies:
        if not src.is_file():
            raise SystemExit(f"ERROR: missing driver source {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"  [copy] {dst.relative_to(xnu)}")


def patch_build_lists(xnu: Path):
    replace_exact(
        xnu / "bsd" / "conf" / "files",
        "bsd/dev/mtk_ufs.c\t\t\tstandard",
        "bsd/dev/mtk_ufs.c\t\t\tstandard\nbsd/dev/mtk_emmc.c\t\t\tstandard\nbsd/dev/a32_cpufreq.c\t\t\tstandard",
        "bsd/conf/files: add bsd/dev/mtk_emmc.c and bsd/dev/a32_cpufreq.c",
    )
    replace_exact(
        xnu / "iokit" / "conf" / "files.arm64",
        "iokit/Kernel/arm/hid/GT9896.cpp                              optional iokitcpp",
        "iokit/Kernel/arm/hid/GT9896.cpp                              optional iokitcpp\n"
        "iokit/Kernel/arm/hid/STMTS.cpp                               optional iokitcpp",
        "iokit/conf/files.arm64: add iokit/Kernel/arm/hid/STMTS.cpp",
    )
    replace_exact(
        xnu / "iokit" / "KernelConfigTables.cpp",
        '    "   {"\n'
        '    "     \'IOClass\'         = GT9896;"\n'
        '    "     \'IOProviderClass\' = MappleDTNub;"\n'
        '    "     \'IONameMatch\'     = \'goodix,gt9896\';"\n'
        '    "   },"',
        '    "   {"\n'
        '    "     \'IOClass\'         = GT9896;"\n'
        '    "     \'IOProviderClass\' = MappleDTNub;"\n'
        '    "     \'IONameMatch\'     = \'goodix,gt9896\';"\n'
        '    "   },"\n'
        '    "   {"\n'
        '    "     \'IOClass\'         = STMTS;"\n'
        '    "     \'IOProviderClass\' = IOPlatformDevice;"\n'
        '    "     \'IONameMatch\'     = \'stm,stm_ts\';"\n'
        '    "   },"',
        "iokit/KernelConfigTables.cpp: register STMTS (IOPlatformDevice stm,stm_ts)",
    )
    replace_exact(
        xnu / "iokit" / "KernelConfigTables.cpp",
        '    "   {"\n'
        '    "     \'IOClass\'         = MappleHIDTouch;"\n'
        '    "     \'IOProviderClass\' = IOResources;"\n'
        '    "     \'IOMatchCategory\' = MappleHIDTouch;"\n'
        '    "   },"',
        "",
        "iokit/KernelConfigTables.cpp: remove duplicate generic MappleHIDTouch",
    )
    replace_exact(
        xnu / "iokit" / "bsddev" / "IOKitBSDInit.cpp",
        '\tif (strcmp(rdBootVar, "ufsroot0") == 0) {\n'
        '\t\textern int mtk_ufs_rootdev(dev_t *dev);\n'
        '\t\tOSSafeReleaseNULL(matching);\n'
        '\t\tint ufs_error = mtk_ufs_rootdev(root);\n'
        '\t\tif (ufs_error) {\n'
        '\t\t\tpanic("IOFindBSDRoot: MT6781 UFS root %s unavailable: %d", rdBootVar, ufs_error);\n'
        '\t\t}\n'
        '\t\tstrlcpy(rootName, rdBootVar, rootNameSize);\n'
        '\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));\n'
        '\t\t*oflags = 0;\n'
        '\t\tgoto iofrootx;\n'
        '\t}',
        '\tif (strcmp(rdBootVar, "ufsroot0") == 0) {\n'
        '\t\textern int mtk_ufs_rootdev(dev_t *dev);\n'
        '\t\tOSSafeReleaseNULL(matching);\n'
        '\t\tint ufs_error = mtk_ufs_rootdev(root);\n'
        '\t\tif (ufs_error) {\n'
        '\t\t\tpanic("IOFindBSDRoot: MT6781 UFS root %s unavailable: %d", rdBootVar, ufs_error);\n'
        '\t\t}\n'
        '\t\tstrlcpy(rootName, rdBootVar, rootNameSize);\n'
        '\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));\n'
        '\t\t*oflags = 0;\n'
        '\t\tgoto iofrootx;\n'
        '\t}\n'
        '\tif (strcmp(rdBootVar, "emmc0") == 0) {\n'
        '\t\textern int mtk_emmc_rootdev(dev_t *dev);\n'
        '\t\tOSSafeReleaseNULL(matching);\n'
        '\t\tint emmc_error = mtk_emmc_rootdev(root);\n'
        '\t\tif (emmc_error) {\n'
        '\t\t\tpanic("IOFindBSDRoot: MT6768 eMMC root %s unavailable: %d", rdBootVar, emmc_error);\n'
        '\t\t}\n'
        '\t\tstrlcpy(rootName, rdBootVar, rootNameSize);\n'
        '\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));\n'
        '\t\t*oflags = 0;\n'
        '\t\tgoto iofrootx;\n'
        '\t}',
        "iokit/bsddev/IOKitBSDInit.cpp: hook rd=emmc0 via mtk_emmc_rootdev",
    )


def patch_mtk_ufs(xnu: Path):
    p = xnu / "bsd" / "dev" / "mtk_ufs.c"

    # 1) Fall back to mtk_emmc when UFS is absent (Wi-Fi firmware tail + usb_link partition reads)
    old_read_part = """int
mtk_ufs_read_part(const char *name, uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
\tstatic char cached[37];
\tstatic uint64_t cfirst, cblocks;
\tint error = 0;

\tif (!su.ready) {
\t\treturn ENXIO;
\t}"""
    new_read_part = """extern int mtk_emmc_read_part(const char *name, uint64_t off, void *buf, uint32_t len, uint64_t *sizep);
extern int mtk_emmc_read_root(uint64_t off, void *buf, uint32_t len, uint64_t *sizep);

int
mtk_ufs_read_part(const char *name, uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
\tstatic char cached[37];
\tstatic uint64_t cfirst, cblocks;
\tint error = 0;

\tif (!su.ready) {
\t\treturn mtk_emmc_read_part(name, off, buf, len, sizep);
\t}"""
    replace_exact(p, old_read_part, new_read_part, "mtk_ufs.c: mtk_emmc_read_part fallback")

    old_read_root = """int
mtk_ufs_read_root(uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
\tif (!su.ready) {
\t\treturn ENXIO;
\t}"""
    new_read_root = """int
mtk_ufs_read_root(uint64_t off, void *buf, uint32_t len, uint64_t *sizep)
{
\tif (!su.ready) {
\t\treturn mtk_emmc_read_root(off, buf, len, sizep);
\t}"""
    replace_exact(p, old_read_root, new_read_root, "mtk_ufs.c: mtk_emmc_read_root fallback")

    # 2) Live ramoops + infracfg_ao clock ungate + S6E3FC3 DSI CMD flush
    old = """/* Called for all console output, any context: never blocks. */
void mapple_log_append(const char *c, int size);

void
mapple_log_append(const char *c, int size)
{
\tif (size <= 0) {
\t\treturn;
\t}
\tuint64_t at = os_atomic_add_orig(&mapple_log_total, (uint64_t)size, relaxed);
\tfor (int i = 0; i < size; i++) {
\t\tmapple_log_ring[(at + (uint64_t)i) % MAPPLE_LOG_SIZE] = c[i];
\t}
}"""

    new = """/* ---- SM-A325M (MT6769T): live ramoops console + clock gates + S6E3FC3 DSI CMD flush ---- */
#define A32_RAMOOPS_PA       0x4d09f000UL
#define A32_RAMOOPS_SIZE     0x40000U
#define A32_RAMOOPS_SIG      0x43474244U /* "DBGC" */
#define A32_INFRACFG_PA      0x10001000UL
#define A32_IFR2_CG_CLR      0x084U      /* bit 8 = CLK_IFR_ICUSB, bit 11 = CLK_IFR_I2C_AP */
#define A32_IFR3_CG_CLR      0x08cU      /* bit 18 = CLK_IFR_AP_DMA */
#define A32_IFR5_CG_CLR      0x0c4U      /* bit 6 = CLK_IFR_I2C6, bit 7 = CLK_IFR_AP_MSDC0 */
#define A32_OVL0_PA          0x1400b000UL
#define A32_MUTEX_PA         0x14001000UL
#define A32_DSI0_PA          0x14014000UL

struct a32_ramoops_hdr {
\tuint32_t sig;
\tuint32_t start;
\tuint32_t size;
};

static vm_offset_t a32_ramoops_va;
static vm_offset_t a32_ovl0_va;
static vm_offset_t a32_mutex_va;
static vm_offset_t a32_dsi0_va;
static uint64_t a32_last_flush_ticks;
static uint32_t a32_hw_ready;
static uint32_t a32_dsi_inflight;
static uint32_t _Atomic a32_dsi_dcs_busy;
static uint32_t a32_bl_inited;

static inline void
a32_io_wr32(vm_offset_t base, uint32_t off, uint32_t val)
{
\t*(volatile uint32_t *)(base + off) = val;
}

static inline uint32_t
a32_io_rd32(vm_offset_t base, uint32_t off)
{
\treturn *(volatile uint32_t *)(base + off);
}

static void
a32_ramoops_putc_raw(char ch)
{
\tif (a32_ramoops_va == 0) {
\t\treturn;
\t}
\tvolatile struct a32_ramoops_hdr *h = (volatile struct a32_ramoops_hdr *)a32_ramoops_va;
\tvolatile char *data = (volatile char *)(a32_ramoops_va + sizeof(*h));
\tuint32_t cap = A32_RAMOOPS_SIZE - (uint32_t)sizeof(*h);
\tuint32_t at = h->start, sz = h->size;
\tif (h->sig != A32_RAMOOPS_SIG || at >= cap || sz > cap) {
\t\th->sig = A32_RAMOOPS_SIG;
\t\tat = 0;
\t\tsz = 0;
\t}
\tdata[at] = ch;
\tif (++at == cap) {
\t\tat = 0;
\t}
\tif (sz < cap) {
\t\tsz++;
\t}
\th->start = at;
\th->size = sz;
}

static inline uint64_t
a32_cntpct_now(void)
{
\tuint64_t v;
\t__asm__ volatile("mrs %0, cntpct_el0" : "=r"(v));
\treturn v;
}

void a32_ramoops_putc_early(uint8_t ch);
void
a32_ramoops_putc_early(uint8_t ch)
{
\tif (a32_hw_ready) {
\t\ta32_ramoops_putc_raw((char)ch);
\t}
}

static inline int
a32_dsi_is_busy(void)
{
\tif ((a32_io_rd32(a32_mutex_va, 0x020) & 1u) || (a32_io_rd32(a32_dsi0_va, 0x004) & (1u << 31))) {
\t\treturn 1;
\t}
\tif (a32_dsi_inflight && !(a32_io_rd32(a32_dsi0_va, 0x00c) & 0x10u)) {
\t\treturn 1;
\t}
\ta32_dsi_inflight = 0;
\treturn 0;
}

int a32_dsi_is_busy_ext(void);
int
a32_dsi_is_busy_ext(void)
{
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0) {
\t\treturn 0;
\t}
\tif (os_atomic_load(&a32_dsi_dcs_busy, acquire) != 0) {
\t\treturn 1;
\t}
\treturn a32_dsi_is_busy();
}

void a32_dsi_cmd_flush_mode(int force);
void
a32_dsi_cmd_flush_mode(int force)
{
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0 || a32_ovl0_va == 0) {
\t\treturn;
\t}
\tif (os_atomic_load(&a32_dsi_dcs_busy, acquire) != 0) {
\t\tif (!force) {
\t\t\treturn;
\t\t}
\t\tuint64_t deadline = a32_cntpct_now() + 520000ULL;
\t\twhile (os_atomic_load(&a32_dsi_dcs_busy, acquire) != 0) {
\t\t\tif (a32_cntpct_now() >= deadline) {
\t\t\t\treturn;
\t\t\t}
\t\t}
\t}
\t__asm__ volatile("dsb sy" ::: "memory");
\tif (a32_dsi_is_busy()) {
\t\tif (!force) {
\t\t\treturn; /* mid-frame: never tear or lock up DSI0 */
\t\t}
\t\tuint64_t deadline = a32_cntpct_now() + 520000ULL; /* 40 ms @ 13 MHz */
\t\twhile (a32_dsi_is_busy()) {
\t\t\tif (a32_cntpct_now() >= deadline) {
\t\t\t\tbreak;
\t\t\t}
\t\t}
\t}
\ta32_io_wr32(a32_ovl0_va, 0x02c, 1u);
\ta32_io_wr32(a32_dsi0_va, 0x00c, 0u);
\ta32_io_wr32(a32_dsi0_va, 0x200, 0x002c3909u);
\ta32_io_wr32(a32_dsi0_va, 0x060, 1u);
\ta32_io_wr32(a32_mutex_va, 0x020, 1u);
\ta32_io_wr32(a32_dsi0_va, 0x000, 0u);
\ta32_io_wr32(a32_dsi0_va, 0x000, 1u);
\ta32_dsi_inflight = 1;
\t__asm__ volatile("dsb sy" ::: "memory");
}

void a32_dsi_cmd_flush_now(void);
void
a32_dsi_cmd_flush_now(void)
{
\ta32_dsi_cmd_flush_mode(1);
}

static void
a32_dsi_send_cmdq(uint32_t w0, uint32_t w1, uint32_t size)
{
\ta32_io_wr32(a32_dsi0_va, 0x00c, a32_io_rd32(a32_dsi0_va, 0x00c) & ~0x02u);
\ta32_io_wr32(a32_dsi0_va, 0x200, w0);
\tif (size > 1u) {
\t\ta32_io_wr32(a32_dsi0_va, 0x204, w1);
\t}
\ta32_io_wr32(a32_dsi0_va, 0x060, size);
\t__asm__ volatile("dsb sy" ::: "memory");
\ta32_io_wr32(a32_dsi0_va, 0x000, 0u);
\ta32_io_wr32(a32_dsi0_va, 0x000, 1u);
\tuint64_t deadline = a32_cntpct_now() + 260000ULL; /* 20 ms @ 13 MHz */
\twhile (!(a32_io_rd32(a32_dsi0_va, 0x00c) & 0x02u)) {
\t\tif (a32_cntpct_now() >= deadline) {
\t\t\tbreak;
\t\t}
\t}
\ta32_io_wr32(a32_dsi0_va, 0x00c, a32_io_rd32(a32_dsi0_va, 0x00c) & ~0x02u);
}

int a32_dsi_set_brightness(uint32_t level_0_2047);
int
a32_dsi_set_brightness(uint32_t level_0_2047)
{
\tuint32_t bl_en = 1;
\t(void)PE_parse_boot_argn("a32_bl", &bl_en, sizeof(bl_en));
\tif (bl_en == 0) {
\t\treturn 1;
\t}
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0) {
\t\treturn 0;
\t}
\tif (os_atomic_xchg(&a32_dsi_dcs_busy, 1u, acq_rel) != 0) {
\t\treturn 0;
\t}
\t__asm__ volatile("dsb sy" ::: "memory");
\tif (a32_dsi_is_busy()) {
\t\tuint64_t deadline = a32_cntpct_now() + 520000ULL;
\t\twhile (a32_dsi_is_busy()) {
\t\t\tif (a32_cntpct_now() >= deadline) {
\t\t\t\tos_atomic_store(&a32_dsi_dcs_busy, 0u, release);
\t\t\t\treturn 0;
\t\t\t}
\t\t}
\t}
\tif (level_0_2047 > 2047u) {
\t\tlevel_0_2047 = 2047u;
\t}
\tuint32_t b10 = level_0_2047 >> 1;
\tif (level_0_2047 > 0u && b10 < 10u) {
\t\tb10 = 10u;
\t}
\tif (!a32_bl_inited) {
\t\ta32_dsi_send_cmdq(0x007a1508u, 0u, 1u); /* self-mask off */
\t\ta32_dsi_send_cmdq(0x20531508u, 0u, 1u); /* normal display brightness control */
\t\ta32_bl_inited = 1u;
\t}
\tuint32_t w0 = (3u << 16) | (0x39u << 8) | 0x0au;
\tuint32_t w1 = 0x51u | (((b10 >> 8) & 0xffu) << 8) | ((b10 & 0xffu) << 16);
\ta32_dsi_send_cmdq(w0, w1, 2u);
\tos_atomic_store(&a32_dsi_dcs_busy, 0u, release);
\treturn 1;
}

int a32_dsi_set_fps(uint32_t hz);
int
a32_dsi_set_fps(uint32_t hz)
{
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0) {
\t\treturn 0;
\t}
\tif (hz != 60u && hz != 90u) {
\t\treturn 0;
\t}
\tif (os_atomic_xchg(&a32_dsi_dcs_busy, 1u, acq_rel) != 0) {
\t\treturn 0;
\t}
\t__asm__ volatile("dsb sy" ::: "memory");
\tif (a32_dsi_is_busy()) {
\t\tuint64_t deadline = a32_cntpct_now() + 520000ULL;
\t\twhile (a32_dsi_is_busy()) {
\t\t\tif (a32_cntpct_now() >= deadline) {
\t\t\t\tos_atomic_store(&a32_dsi_dcs_busy, 0u, release);
\t\t\t\treturn 0;
\t\t\t}
\t\t}
\t}
\t/* Samsung S6E3FC3 (s6e3fc3_a32.c / s6e3fc3_a32_param.h): F0 5A 5A -> 60 (08|00) 00 -> F7 0F -> F0 A5 A5 */
\tuint32_t w0_long3 = (3u << 16) | (0x39u << 8) | 0x0au;
\ta32_dsi_send_cmdq(w0_long3, 0x005a5af0u, 2u);
\ta32_dsi_send_cmdq(w0_long3, (hz == 90u) ? 0x00000860u : 0x00000060u, 2u);
\ta32_dsi_send_cmdq(0x0ff71508u, 0u, 1u);
\ta32_dsi_send_cmdq(w0_long3, 0x00a5a5f0u, 2u);
\tos_atomic_store(&a32_dsi_dcs_busy, 0u, release);
\treturn 1;
}

void a32_console_hw_init(void);
void
a32_console_hw_init(void)
{
\tif (a32_hw_ready) {
\t\treturn;
\t}
\ta32_ramoops_va = ml_io_map(A32_RAMOOPS_PA, A32_RAMOOPS_SIZE);
\ta32_ovl0_va    = ml_io_map(A32_OVL0_PA, 0x1000);
\ta32_mutex_va   = ml_io_map(A32_MUTEX_PA, 0x1000);
\ta32_dsi0_va    = ml_io_map(A32_DSI0_PA, 0x1000);

\t/* Ensure MT6768 infracfg_ao clock gates for USB, I2C6 (touch), AP_DMA, and MSDC0 (eMMC) are open */
\tvm_offset_t infra = ml_io_map(A32_INFRACFG_PA, 0x1000);
\tif (infra != 0) {
\t\ta32_io_wr32(infra, A32_IFR2_CG_CLR, (1u << 8) | (1u << 11));
\t\ta32_io_wr32(infra, A32_IFR3_CG_CLR, (1u << 18));
\t\ta32_io_wr32(infra, A32_IFR5_CG_CLR, (1u << 6) | (1u << 7));
\t\t__asm__ volatile("dsb sy" ::: "memory");
\t}

\ta32_hw_ready   = 1;

\t/* Replay early Mach console bytes buffered before IOKit started into ramoops */
\tuint64_t total = os_atomic_load(&mapple_log_total, relaxed);
\tuint64_t start = total > MAPPLE_LOG_SIZE ? total - MAPPLE_LOG_SIZE : 0;
\tfor (uint64_t i = start; i < total; i++) {
\t\ta32_ramoops_putc_raw(mapple_log_ring[i % MAPPLE_LOG_SIZE]);
\t}
\ta32_dsi_cmd_flush_now();
}

/* Called for all console output, any context: never blocks. */
void mapple_log_append(const char *c, int size);

void
mapple_log_append(const char *c, int size)
{
\tif (size <= 0) {
\t\treturn;
\t}
\tuint64_t at = os_atomic_add_orig(&mapple_log_total, (uint64_t)size, relaxed);
\tint saw_nl = 0;
\tfor (int i = 0; i < size; i++) {
\t\tchar ch = c[i];
\t\tmapple_log_ring[(at + (uint64_t)i) % MAPPLE_LOG_SIZE] = ch;
\t\tif (ch == '\\n') {
\t\t\tsaw_nl = 1;
\t\t}
\t}
\tif (a32_hw_ready && saw_nl) {
\t\t/* Rate-limit console AMOLED refreshes to ~25 Hz (13 MHz cntpct -> 520000 ticks ~ 40 ms) */
\t\tuint64_t now = a32_cntpct_now();
\t\tif (now - a32_last_flush_ticks >= 520000ULL) {
\t\t\ta32_last_flush_ticks = now;
\t\t\ta32_dsi_cmd_flush_mode(0);
\t\t}
\t}
}"""
    replace_exact(p, old, new, "mtk_ufs.c: a32 ramoops + infracfg gates + DSI CMD live console")


def patch_pe_serial(xnu: Path):
    """Hook SM-A325M ramoops + AMOLED DSI CMD flush into early XNU fbcon (pe_serial.c)."""
    p = xnu / "pexpert" / "arm" / "pe_serial.c"
    old_init = """static void
fbcon_init(void)
{
\tfbcon_cols = fbcon_width / (8 * FBCON_SCALE);
\tfbcon_rows = fbcon_height / (16 * FBCON_SCALE);
\tfbcon_col = fbcon_row = 0;
\tfor (uint32_t r = 0; r < fbcon_rows; r++) {
\t\tfbcon_clear_row(r);
\t}
}"""
    new_init = """extern void a32_console_hw_init(void);
extern void a32_ramoops_putc_early(uint8_t ch);
extern void a32_dsi_cmd_flush_mode(int force);

static void
fbcon_init(void)
{
\tfbcon_cols = fbcon_width / (8 * FBCON_SCALE);
\tfbcon_rows = fbcon_height / (16 * FBCON_SCALE);
\tfbcon_col = fbcon_row = 0;
\tfor (uint32_t r = 0; r < fbcon_rows; r++) {
\t\tfbcon_clear_row(r);
\t}
\ta32_dsi_cmd_flush_mode(1);
}"""
    replace_exact(p, old_init, new_init, "pe_serial.c: flush AMOLED at end of fbcon_init()")

    old_tx = """\tif (c == '\\n') {
\t\tfbcon_newline();
\t\treturn;
\t}"""
    new_tx = """\ta32_ramoops_putc_early(c);
\tif (c == '\\n') {
\t\tfbcon_newline();
\t\ta32_dsi_cmd_flush_mode(panic_active() ? 1 : 0);
\t\treturn;
\t}"""
    replace_exact(p, old_tx, new_tx, "pe_serial.c: mirror fbcon_transmit_data to ramoops + DSI CMD flush")

    old_probe = """\tif (fbcon_base != NULL) {
\t\tregister_serial_functions(&mapple_fbcon_serial_functions);
\t}"""
    new_probe = """\tif (fbcon_base != NULL) {
\t\ta32_console_hw_init();
\t\tregister_serial_functions(&mapple_fbcon_serial_functions);
\t}"""
    replace_exact(p, old_probe, new_probe, "pe_serial.c: call a32_console_hw_init() in fbcon_probe()")


def patch_platform_expert(xnu: Path):
    p = xnu / "iokit" / "Kernel" / "arm" / "MapplePlatformExpert.cpp"
    old = """extern "C" vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);"""
    new = """extern "C" vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern "C" void a32_console_hw_init(void);"""
    replace_exact(p, old, new, "MapplePlatformExpert.cpp: declare a32_console_hw_init")

    old2 = """bool
MapplePlatformExpert::start(IOService *provider)
{
\tIOLog("starting device-tree platform expert\\n");"""
    new2 = """bool
MapplePlatformExpert::start(IOService *provider)
{
\ta32_console_hw_init();
\tIOLog("starting device-tree platform expert (SM-A325M MT6769T)\\n");"""
    replace_exact(p, old2, new2, "MapplePlatformExpert.cpp: call a32_console_hw_init in start()")


def patch_framebuffer(xnu: Path):
    p = xnu / "iokit" / "Kernel" / "arm" / "MappleFramebuffer.cpp"

    # Forward-declare a32_dsi_cmd_flush_now / a32_dsi_set_brightness near the top of the file (before AppleCLCD!)
    old_decl = """extern "C" void mtk_ufs_safe_clear(void);                /* bsd/dev/mtk_ufs.c */"""
    new_decl = """extern "C" void mtk_ufs_safe_clear(void);                /* bsd/dev/mtk_ufs.c */
extern "C" int a32_dsi_is_busy_ext(void);                /* bsd/dev/mtk_ufs.c (SM-A325M DSI CMD) */
extern "C" void a32_dsi_cmd_flush_now(void);             /* bsd/dev/mtk_ufs.c (SM-A325M DSI CMD) */
extern "C" int a32_dsi_set_brightness(uint32_t level);   /* bsd/dev/mtk_ufs.c (SM-A325M S6E3FC3 DCS) */
extern "C" int a32_dsi_set_fps(uint32_t hz);             /* bsd/dev/mtk_ufs.c (SM-A325M S6E3FC3 60/90 Hz) */
extern "C" void a32_touch_display_power(int on);         /* hid/STMTS.cpp */"""
    replace_exact(p, old_decl, new_decl, "MappleFramebuffer.cpp: declare a32 DSI helpers at top")

    old_pa = """#define DISP_OVL0_PA            0x14005000ull
#define DISP_OVL0_2L_PA         0x14006000ull
#define SMI_LARB0_PA            0x14003000ull
#define SMI_LARB1_PA            0x14004000ull"""
    new_pa = """/* SM-A325M (MT6769T / mt6768) verified display MMIO addresses */
#define DISP_OVL0_PA            0x1400b000ull
#define DISP_OVL0_2L_PA         0x1400c000ull
#define SMI_LARB0_PA            0x14003000ull
#define SMI_LARB1_PA            0x16010000ull"""
    replace_exact(p, old_pa, new_pa, "MappleFramebuffer.cpp: MT6768 OVL0/LARB1 bases")

    old_dsi = """#define DISP_DSI0_PA            0x14013000ull"""
    new_dsi = """#define DISP_DSI0_PA            0x14014000ull"""
    replace_exact(p, old_dsi, new_dsi, "MappleFramebuffer.cpp: MT6768 DSI0 base")

    old_ccorr = """#define DISP_CCORR0_PA          0x1400b000ull"""
    new_ccorr = """#define DISP_CCORR0_PA          0x14010000ull"""
    replace_exact(p, old_ccorr, new_ccorr, "MappleFramebuffer.cpp: MT6768 CCORR0 base")

    old_rdma = """#define DISP_RDMA0_PA           0x14007000ull
#define DISP_RSZ0_PA            0x14008000ull"""
    new_rdma = """#define DISP_RDMA0_PA           0x1400d000ull
#define DISP_RSZ0_PA            0x14015000ull"""
    replace_exact(p, old_rdma, new_rdma, "MappleFramebuffer.cpp: MT6768 RDMA0/RSZ0 bases")

    old_pres = """\treturn present(recs + (size_t)pick * MAPPLE_FB_SWAP_REC_SIZE, force, jobs) ? pick : -1;"""
    new_pres = """\tfor (int a32_w = 0; a32_w < 30 && a32_dsi_is_busy_ext(); a32_w++) {
\t\tIOSleep(1);
\t}
\tbool ok = present(recs + (size_t)pick * MAPPLE_FB_SWAP_REC_SIZE, force, jobs);
\tif (ok) {
\t\ta32_dsi_cmd_flush_now();
\t}
\treturn ok ? pick : -1;"""
    replace_exact(p, old_pres, new_pres, "MappleFramebuffer.cpp: flush S6E3FC3 DSI CMD after present()")

    old_bl = """\tif (bl != 0xffffffffu) {
\t\tuint32_t pl = blPanel(bl);
\t\tuint8_t cmd[3] = { 0x51, (uint8_t)(pl >> 8), (uint8_t)pl };
\t\tif (bl == fBlLevel && fBlSent != 0 && !fBlResend) {
\t\t\tblLog(bl, dark ? "display off" : fBlFrom, ": already there");
\t\t\tfBlWant = 0xffffffffu;
\t\t} else if (dsiSendDcs(cmd, 3, false, false)) {      /* non-blocking: a fade must not hold the frames */"""
    new_bl = """\tif (bl != 0xffffffffu) {
\t\tuint32_t pl = blPanel(bl);
\t\tif (bl == fBlLevel && fBlSent != 0 && !fBlResend) {
\t\t\tblLog(bl, dark ? "display off" : fBlFrom, ": already there");
\t\t\tfBlWant = 0xffffffffu;
\t\t} else if (a32_dsi_set_brightness(pl)) {"""
    replace_exact(p, old_bl, new_bl, "MappleFramebuffer.cpp: S6E3FC3 command-mode brightness via a32_dsi_set_brightness")

    old_pwr = """\t\t\tIOLog("FB: display %s\\n", off ? "off" : "on");"""
    new_pwr = """\t\t\tIOLog("FB: display %s\\n", off ? "off" : "on");
\t\t\tif (!off && fFB->fPanelHz == 90) {
\t\t\t\ta32_dsi_set_fps(90);
\t\t\t}
\t\t\ta32_touch_display_power(off ? 0 : 1);"""
    replace_exact(p, old_pwr, new_pwr, "MappleFramebuffer.cpp: notify STMTS and restore 90 Hz on display wake")

    old_period = """\tuint64_t periodOr60(void)
\t{
\t\tuint64_t p = fPeriod;
\t\tif (p == 0) {
\t\t\tnanoseconds_to_absolutetime(16666667, &p);
\t\t}
\t\treturn p;
\t}"""
    new_period = """\tuint64_t periodOr60(void)
\t{
\t\tuint64_t p = fPeriod;
\t\tif (p == 0) {
\t\t\tnanoseconds_to_absolutetime((uint64_t)(fTickUs ? fTickUs : MAPPLE_FB_VSYNC_US) * 1000ull, &p);
\t\t}
\t\treturn p;
\t}"""
    replace_exact(p, old_period, new_period, "MappleFramebuffer.cpp: use fTickUs in periodOr60() for 90 Hz")

    old_hz = """\tfVsync->setTimeoutUS(MAPPLE_FB_VSYNC_US);

\tsetProperty(kIOUserClientClassKey, "IOMobileFramebufferUserClient");
\tsetProperty("external", false);
\tsetProperty("IOMFBBrightnessLevel", (unsigned long long)(200u << 16), 32);     /* 0191: 200 nits until set */
\tif (fHzMode != 60) {"""
    new_hz = """\tuint32_t a32_hz = 60;
\tPE_parse_boot_argn("a32_panel_hz", &a32_hz, sizeof(a32_hz));
\tif (a32_hz == 90 && a32_dsi_set_fps(90)) {
\t\tfPanelHz = 90;
\t\tfTickUs = 11111;
\t\tIOLog("FB: S6E3FC3 panel switched to 90 Hz (11111 us)\\n");
\t}
\tfVsync->setTimeoutUS(fTickUs);

\tsetProperty(kIOUserClientClassKey, "IOMobileFramebufferUserClient");
\tsetProperty("external", false);
\tsetProperty("IOMFBBrightnessLevel", (unsigned long long)(200u << 16), 32);     /* 0191: 200 nits until set */
\tif (fPanelHz == 90) {
\t\tOSDictionary *refresh = OSDictionary::withCapacity(2);
\t\tuint64_t step = 0;
\t\tnanoseconds_to_absolutetime(11111111, &step);
\t\tOSNumber *minI = OSNumber::withNumber((unsigned long long)((11111111ull << 32) / 1000000000ull), 32);
\t\tOSNumber *stepN = OSNumber::withNumber((unsigned long long)step, 32);
\t\tif (refresh != NULL && minI != NULL && stepN != NULL) {
\t\t\trefresh->setObject("displayMinRefreshInterval", minI);
\t\t\trefresh->setObject("displayRefreshStepMachTime", stepN);
\t\t\tsetProperty("IOMFBDisplayRefresh", refresh);
\t\t}
\t\tOSSafeReleaseNULL(minI);
\t\tOSSafeReleaseNULL(stepN);
\t\tOSSafeReleaseNULL(refresh);
\t} else if (fHzMode != 60) {"""
    replace_exact(p, old_hz, new_hz, "MappleFramebuffer.cpp: a32_panel_hz=90 S6E3FC3 DCS + IOMFBDisplayRefresh")

    old_rearm = """\tif (!fFlipReady) {
\t\ttimer->setTimeoutUS(fTickUs);
\t\treturn;
\t}"""
    new_rearm = """\tif (!fFlipReady) {
\t\tstatic uint64_t a32NextTick = 0;
\t\tuint64_t now = mach_absolute_time(), step = 0;
\t\tnanoseconds_to_absolutetime((uint64_t)fTickUs * 1000ull, &step);
\t\tif (a32NextTick == 0 || now >= a32NextTick + 2 * step) {
\t\t\ta32NextTick = now + step;
\t\t} else {
\t\t\ta32NextTick += step;
\t\t\tif (a32NextTick <= now) {
\t\t\t\ta32NextTick = now + step;
\t\t\t}
\t\t}
\t\ttimer->wakeAtTime(kIOTimeOptionsSysCritical, a32NextTick, 0);
\t\treturn;
\t}"""
    replace_exact(p, old_rearm, new_rearm, "MappleFramebuffer.cpp: jitter-free critical-urgency phase-locked VSync timer")


def patch_pmic(xnu: Path):
    """Accept MT6358 (hwcid 0x58xx, SM-A325M = 0x5831) alongside MT6366 (0x66xx), and allow VCAMA2 for touch AVDD."""
    p = xnu / "iokit" / "Kernel" / "arm" / "MT6358PMIC.cpp"
    replace_exact(
        p,
        "\tif (read(MT6358_PMIC_HWCID, &id) != kIOReturnSuccess || (id & 0xff00) != 0x6600) {\n"
        "\t\tIOLog(\"PMIC: hwcid 0x%04x is not an MT6366, not touching it\\n\", id);",
        "\tif (read(MT6358_PMIC_HWCID, &id) != kIOReturnSuccess || ((id & 0xff00) != 0x6600 && (id & 0xff00) != 0x5800)) {\n"
        "\t\tIOLog(\"PMIC: hwcid 0x%04x is not an MT6358/MT6366, not touching it\\n\", id);",
        "MT6358PMIC.cpp: accept MT6358 hwcid 0x58xx (SM-A325M = 0x5831)",
    )
    pw = xnu / "iokit" / "Kernel" / "arm" / "MTKPwrap.cpp"
    replace_exact(
        pw,
        "\t{ 0x1d90, 0x0002, 0x01 },       /* LDO_VCN28_OP_CFG: HW0_OP_CFG */\n};",
        "\t{ 0x1d90, 0x0002, 0x01 },       /* LDO_VCN28_OP_CFG: HW0_OP_CFG */\n"
        "\t/* SM-A325M FTS5CU56A touch 3.0V AVDD (tsp_avdd_ldo = ldo_vcama2) */\n"
        "\t{ 0x1e0c, 0x0f00, 0x0c },       /* VCAMA2_ANA_CON0: VOSEL[11:8] <= 0xc (3.0 V) */\n"
        "\t{ 0x1c8a, 0x0001, 0x01 },       /* LDO_VCAMA2_OP_EN: SW_OP_EN */\n"
        "\t{ 0x1c88, 0x0001, 0x01 },       /* LDO_VCAMA2_CON0: RG_LDO_VCAMA2_EN */\n"
        "\t/* SM-A325M a32_cpufreq VSRAM rails (0.5 V + 6.25 mV * step <= 1.11875 V) */\n"
        "\t{ 0x1bee, 0x007f, 0x63 },       /* LDO_VSRAM_CON0: VSRAM_PROC11 */\n"
        "\t{ 0x1bf0, 0x007f, 0x63 },       /* LDO_VSRAM_CON1: VSRAM_PROC12 */\n};",
        "MTKPwrap.cpp: whitelist VCAMA2 (touch AVDD) and VSRAM_PROC11/12",
    )
    replace_exact(
        pw,
        'extern "C" void mt6781_cpufreq_start(void);       /* osfmk/arm64/mt6781_cpufreq.c: CPU DVFS needs the PMIC */',
        'extern "C" void mt6781_cpufreq_start(void);       /* osfmk/arm64/mt6781_cpufreq.c: CPU DVFS needs the PMIC */\n'
        'extern "C" void a32_cpufreq_start(void);          /* bsd/dev/a32_cpufreq.c: SM-A325M fixed CPU raise */',
        "MTKPwrap.cpp: declare a32_cpufreq_start",
    )
    replace_exact(
        pw,
        "\tmt6781_cpufreq_start();",
        "\tmt6781_cpufreq_start();\n\ta32_cpufreq_start();",
        "MTKPwrap.cpp: call a32_cpufreq_start() after PMIC attach",
    )


def patch_usb_link(xnu: Path):
    """Port usb_link.c (mp_usblink=3) to MT6768 T-PHY (0x11cc0000 + 0x800) and add chown/fpatch."""
    p = xnu / "bsd" / "dev" / "usb_link.c"
    replace_exact(
        p,
        "#define TPHY_PA         0x11ca0000u\n"
        "#define U2PORT          0x300           /* u2port0 inside the T-PHY block */",
        "#define TPHY_PA         0x11cc0000u     /* MT6768 / SM-A325M T-PHY base */\n"
        "#define U2PORT          0x800           /* MT6768 u2port0 offset inside T-PHY */",
        "usb_link.c: MT6768 TPHY_PA (0x11cc0000) and U2PORT (0x800)",
    )
    replace_exact(
        p,
        '\t} else if (IS("pull")) {\n'
        '\t\tvl_pull_start(rest);',
        '\t} else if (IS("pull")) {\n'
        '\t\tvl_pull_start(rest);\n'
        '\t} else if (IS("chown")) {\n'
        '\t\tuint64_t uid = 0, gid = 0, mode = 0;\n'
        '\t\tif (!vl_arg(&rest, &uid) || !vl_arg(&rest, &gid) || !vl_arg(&rest, &mode)) {\n'
        '\t\t\tvl_reply("usage: chown UID GID MODE PATH\\n");\n'
        '\t\t} else {\n'
        '\t\t\twhile (*rest == \' \') { rest++; }\n'
        '\t\t\tif (!vl_path_ok(rest) || rootvnode == NULLVP) {\n'
        '\t\t\t\tvl_reply("chown: bad path or no root\\n");\n'
        '\t\t\t} else {\n'
        '\t\t\t\tvfs_context_t ctx = vfs_context_kernel();\n'
        '\t\t\t\tvnode_t vp = NULLVP;\n'
        '\t\t\t\tint err = vnode_open(rest, FREAD, 0, 0, &vp, ctx);\n'
        '\t\t\t\tif (err) {\n'
        '\t\t\t\t\tvl_reply("chown %s: open error %d\\n", rest, err);\n'
        '\t\t\t\t} else {\n'
        '\t\t\t\t\tstruct vnode_attr va;\n'
        '\t\t\t\t\tVATTR_INIT(&va);\n'
        '\t\t\t\t\tVATTR_SET(&va, va_uid, (uid_t)uid);\n'
        '\t\t\t\t\tVATTR_SET(&va, va_gid, (gid_t)gid);\n'
        '\t\t\t\t\tVATTR_SET(&va, va_mode, (mode_t)(mode & 07777));\n'
        '\t\t\t\t\terr = vnode_setattr(vp, &va, ctx);\n'
        '\t\t\t\t\tvnode_close(vp, FREAD, ctx);\n'
        '\t\t\t\t\tvl_reply("chown %s (%llu:%llu 0%llo): %s (%d)\\n", rest, uid, gid, mode & 07777, err ? "ERR" : "ok", err);\n'
        '\t\t\t\t}\n'
        '\t\t\t}\n'
        '\t\t}\n'
        '\t} else if (IS("fpatch")) {\n'
        '\t\tuint64_t off = 0, val = 0;\n'
        '\t\tif (!vl_arg(&rest, &off) || !vl_arg(&rest, &val)) {\n'
        '\t\t\tvl_reply("usage: fpatch OFF U64_LE PATH\\n");\n'
        '\t\t} else {\n'
        '\t\t\twhile (*rest == \' \') { rest++; }\n'
        '\t\t\tif (!vl_path_ok(rest) || rootvnode == NULLVP) {\n'
        '\t\t\t\tvl_reply("fpatch: bad path or no root\\n");\n'
        '\t\t\t} else {\n'
        '\t\t\t\tvfs_context_t ctx = vfs_context_kernel();\n'
        '\t\t\t\tvnode_t vp = NULLVP;\n'
        '\t\t\t\tint err = vnode_open(rest, FREAD | FWRITE, 0, 0, &vp, ctx);\n'
        '\t\t\t\tif (err) {\n'
        '\t\t\t\t\tvl_reply("fpatch %s: open error %d\\n", rest, err);\n'
        '\t\t\t\t} else {\n'
        '\t\t\t\t\tuint64_t cur = 0;\n'
        '\t\t\t\t\tint resid = 0;\n'
        '\t\t\t\t\terr = vn_rdwr(UIO_READ, vp, (caddr_t)&cur, 8, (off_t)off, UIO_SYSSPACE, IO_NOAUTH, vfs_context_ucred(ctx), &resid, vfs_context_proc(ctx));\n'
        '\t\t\t\t\tif (!err && resid == 0) {\n'
        '\t\t\t\t\t\terr = vn_rdwr(UIO_WRITE, vp, (caddr_t)&val, 8, (off_t)off, UIO_SYSSPACE, IO_NOAUTH | IO_UNIT, vfs_context_ucred(ctx), &resid, vfs_context_proc(ctx));\n'
        '\t\t\t\t\t}\n'
        '\t\t\t\t\tvnode_close(vp, FREAD | FWRITE, ctx);\n'
        '\t\t\t\t\tvl_reply("fpatch %s +0x%llx: 0x%016llx -> 0x%016llx (%s %d)\\n", rest, off, cur, val, err ? "ERR" : "ok", err);\n'
        '\t\t\t\t}\n'
        '\t\t\t}\n'
        '\t\t}\n'
        '\t} else if (IS("growfs")) {\n'
        '\t\tuint64_t newsz = 0;\n'
        '\t\tif (!vl_arg(&rest, &newsz) || newsz == 0) {\n'
        '\t\t\tvl_reply("usage: growfs NEW_BYTES (e.g. 0x980000000 for 38 GiB)\\n");\n'
        '\t\t} else if (rootvnode == NULLVP) {\n'
        '\t\t\tvl_reply("growfs: no rootvnode\\n");\n'
        '\t\t} else {\n'
        '\t\t\textern int hfs_extendfs(void *hfsmp, uint64_t newsize, vfs_context_t context);\n'
        '\t\t\tmount_t mp = vnode_mount(rootvnode);\n'
        '\t\t\tvoid *hfsmp = mp ? vfs_fsprivate(mp) : NULL;\n'
        '\t\t\tif (hfsmp == NULL) {\n'
        '\t\t\t\tvl_reply("growfs: no hfsmp\\n");\n'
        '\t\t\t} else {\n'
        '\t\t\t\tint err = hfs_extendfs(hfsmp, newsz, vfs_context_kernel());\n'
        '\t\t\t\tvl_reply("growfs 0x%llx (%llu MiB): %s (%d)\\n", newsz, newsz >> 20, err ? "ERR" : "ok", err);\n'
        '\t\t\t}\n'
        '\t\t}',
        "usb_link.c: add chown, fpatch, and growfs commands for live rootfs repair/resize",
    )


def patch_gpu_mt6768(xnu: Path):
    """Port MappleGPU.{h,cpp} from MT6781 Valhall (Mali-G57) to MT6768 Bifrost (Mali-G52 MC2)."""
    h = xnu / "iokit" / "Kernel" / "arm" / "MappleGPU.h"
    replace_exact(
        h,
        "\tvoid protRelease(int from = 3);",
        "\tvoid protRelease(int from = 1);",
        "MappleGPU.h: protRelease default 1 (MT6768 2-step bus protect)",
    )
    replace_exact(
        h,
        "\tint cacheMode(void) const { return fCacheMode; }",
        "\tint cacheMode(void) const { return fCacheMode; }\n"
        "\tuint32_t vaBits(void) const { uint32_t b = fMmuFeatures & 0xff; return (b >= 31 && b <= 48) ? b : 48; }",
        "MappleGPU.h: add vaBits() helper for 35-bit Bifrost MMU",
    )

    p = xnu / "iokit" / "Kernel" / "arm" / "MappleGPU.cpp"
    replace_exact(
        p,
        """/* SPM 0x10006000 */
#define SPM_POWERON_CONFIG_EN   0x000
#define SPM_PROJECT_KEY         0x0b160001u
#define SPM_PWR_STATUS          0x16c
#define SPM_PWR_STATUS_2ND      0x170
#define SPM_MFG_PWR_CON(n)      (0x308 + 4 * (n))
#define PWR_RST_B               (1u << 0)
#define PWR_ISO                 (1u << 1)
#define PWR_ON                  (1u << 2)
#define PWR_ON_2ND              (1u << 3)
#define PWR_CLK_DIS             (1u << 4)
#define PWR_SRAM_PDN            (1u << 8)
#define PWR_SRAM_PDN_ACK        (1u << 12)
#define MFG_STATUS_BIT(n)       (1u << (2 + (n)))

/* infracfg_ao 0x10001000: bus protection for MFG1 */
#define INFRA_PROT_SET          0x2a0
#define INFRA_PROT_CLR          0x2a4
#define INFRA_PROT_STA1         0x228
#define INFRA_PROT1_SET         0x2a8
#define INFRA_PROT1_CLR         0x2ac
#define INFRA_PROT1_STA1        0x258
#define INFRA_ACP_CON           0x290   /* bit 9: ACP off (gpufreq probe) */

/* topckgen 0x10000000, apmixed 0x1000c000 */
#define CK_CFG_UPDATE           0x004
#define CK_CFG_UPDATE_MFG       (1u << 2)
#define CK_CFG_0                0x040
#define CK_CFG_0_SET            0x044
#define CK_CFG_0_CLR            0x048
#define CK_MFG_SEL_SHIFT        16
#define CK_MFG_SEL_MASK         (3u << 16)
#define CK_MFG_PDN              (1u << 23)
#define CK_MFG_MFGPLL           1u
#define CK_MFG_MAINPLL_D5       3u
#define PLL_MFG_CON0            0x250   /* bit 0 EN */
#define PLL_MFG_CON1            0x254
#define PLL_MFG_PWR_CON0        0x25c   /* bit 0 PWR_ON, bit 1 ISO */""",
        """/* SPM 0x10006000 (MT6768 / SM-A325M) */
#define SPM_POWERON_CONFIG_EN   0x000
#define SPM_PROJECT_KEY         0x0b160001u
#define SPM_PWR_STATUS          0x180
#define SPM_PWR_STATUS_2ND      0x184
/* MT6768 SPM domains (clk-mt6768-pg.c): MFG_ASYNC (no SRAM), MFG, MFG_CORE0, MFG_CORE1 */
static const struct { uint32_t con, bit; bool hasSram; } gMfgDom[4] = {
	{ 0x334, 1u << 14, false },
	{ 0x338, 1u << 11, true  },
	{ 0x34c, 1u << 12, true  },
	{ 0x310, 1u << 13, true  },
};
#define SPM_MFG_PWR_CON(n)      (gMfgDom[(n)].con)
#define MFG_STATUS_BIT(n)       (gMfgDom[(n)].bit)
#define PWR_RST_B               (1u << 0)
#define PWR_ISO                 (1u << 1)
#define PWR_ON                  (1u << 2)
#define PWR_ON_2ND              (1u << 3)
#define PWR_CLK_DIS             (1u << 4)
#define PWR_SRAM_PDN            (1u << 8)
#define PWR_SRAM_PDN_ACK        (1u << 12)

/* infracfg_ao 0x10001000: bus protection for MFG1 */
#define INFRA_PROT_SET          0x2a0
#define INFRA_PROT_CLR          0x2a4
#define INFRA_PROT_STA1         0x228
#define INFRA_PROT1_SET         0x2a8
#define INFRA_PROT1_CLR         0x2ac
#define INFRA_PROT1_STA1        0x258
#define INFRA_ACP_CON           0x290

/* topckgen 0x10000000 (CLK_CFG_1 mfg_sel), apmixed 0x1000c000 (MFGPLL) for MT6768 */
#define CK_CFG_UPDATE           0x004
#define CK_CFG_UPDATE_MFG       (1u << 4)
#define CK_CFG_0                0x050
#define CK_CFG_0_SET            0x054
#define CK_CFG_0_CLR            0x058
#define CK_MFG_SEL_SHIFT        0
#define CK_MFG_SEL_MASK         (3u << 0)
#define CK_MFG_PDN              (1u << 7)
#define CK_MFG_MFGPLL           1u
#define CK_MFG_MAINPLL_D5       2u      /* syspll_d3 (364 MHz) */
#define PLL_MFG_CON0            0x248   /* bit 0 EN */
#define PLL_MFG_CON1            0x24c
#define PLL_MFG_PWR_CON0        0x254   /* bit 0 PWR_ON, bit 1 ISO */""",
        "MappleGPU.cpp: MT6768 SPM, topckgen CLK_CFG_1, apmixed MFGPLL offsets",
    )

    replace_exact(
        p,
        """\tuint64_t pa = rootPA;
\tfor (int level = 0; level < 3; level++) {""",
        """\tuint64_t pa = rootPA;
\tint startLevel = (gpu != NULL && gpu->vaBits() <= 39) ? 1 : 0;
\tfor (int level = startLevel; level < 3; level++) {""",
        "MappleGPU.cpp: 3-level page table walk for <=39-bit VA GPUs (G52 35-bit)",
    )

    replace_exact(
        p,
        """#define LK_VGPU_SEL     0x40    /* 900 mV, as LK leaves VGPU on the realme 8i */
#define LK_VSRAM_SEL    0x50    /* 1000 mV */""",
        """#define LK_VGPU_SEL     0x30    /* 800 mV, as LK leaves VGPU on SM-A325M */
#define LK_VSRAM_SEL    0x40    /* 900 mV */""",
        "MappleGPU.cpp: SM-A325M LK default GPU rails (800/900 mV)",
    )

    replace_exact(
        p,
        """/* MFG1 bus protection steps (infracfg_ao), set in this order on power-off, released in reverse. */
static const struct { uint32_t set, clr, sta, mask; } gMfgProt[4] = {
	{ INFRA_PROT1_SET, INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x18000000 },
	{ INFRA_PROT_SET, INFRA_PROT_CLR, INFRA_PROT_STA1, 0x00600000 },
	{ INFRA_PROT_SET, INFRA_PROT_CLR, INFRA_PROT_STA1, 0x02000000 },
	{ INFRA_PROT1_SET, INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x20000000 },
};""",
        """/* MFG1 bus protection steps (infracfg_ao MT6768), set on power-off, released in reverse. */
static const struct { uint32_t set, clr, sta, mask; } gMfgProt[2] = {
	{ INFRA_PROT_SET, INFRA_PROT_CLR, INFRA_PROT_STA1, 0x02000000 },
	{ INFRA_PROT_SET, INFRA_PROT_CLR, INFRA_PROT_STA1, 0x00600000 },
};""",
        "MappleGPU.cpp: MT6768 2-step MFG bus protection",
    )

    replace_exact(
        p,
        """\tset32(fSpm, con, PWR_RST_B);
\tclr32(fSpm, con, PWR_SRAM_PDN);
\tif (!pollUs(1000, [&]{ return !(rd32(fSpm, con) & PWR_SRAM_PDN_ACK); })) {
\t\tsay(out, end, "  MFG%u: no SRAM ack (pwr_con 0x%08x)\\n", n, rd32(fSpm, con));
\t\treturn false;
\t}""",
        """\tset32(fSpm, con, PWR_RST_B);
\tif (gMfgDom[n].hasSram) {
\t\tclr32(fSpm, con, PWR_SRAM_PDN);
\t\tif (!pollUs(1000, [&]{ return !(rd32(fSpm, con) & PWR_SRAM_PDN_ACK); })) {
\t\t\tsay(out, end, "  MFG%u: no SRAM ack (pwr_con 0x%08x)\\n", n, rd32(fSpm, con));
\t\t\treturn false;
\t\t}
\t}""",
        "MappleGPU.cpp: skip SRAM_PDN polling for MFG_ASYNC in mtcmosOn",
    )

    replace_exact(
        p,
        """\tif (n == 1) {
\t\tfor (int i = 0; i < 4; i++) {
\t\t\twr32(fInfra, gMfgProt[i].set, gMfgProt[i].mask);
\t\t\tif (!pollUs(1000, [&]{ return (rd32(fInfra, gMfgProt[i].sta) & gMfgProt[i].mask) == gMfgProt[i].mask; })) {
\t\t\t\t/* the bus did not drain: powering off now could hang it; leave MFG1 on and usable */
\t\t\t\tsay(out, end, "  MFG1: bus protection step %d not acknowledged (sta 0x%08x), left on\\n", i + 1,
\t\t\t\t    rd32(fInfra, gMfgProt[i].sta));
\t\t\t\tprotRelease(i);
\t\t\t\treturn false;
\t\t\t}
\t\t}
\t}
\tset32(fSpm, con, PWR_SRAM_PDN);
\tif (!pollUs(1000, [&]{ return (rd32(fSpm, con) & PWR_SRAM_PDN_ACK) != 0; })) {
\t\tsay(out, end, "  MFG%u: no SRAM power-down ack\\n", n);
\t}""",
        """\tif (n == 1) {
\t\tfor (int i = 0; i < 2; i++) {
\t\t\twr32(fInfra, gMfgProt[i].set, gMfgProt[i].mask);
\t\t\tif (!pollUs(1000, [&]{ return (rd32(fInfra, gMfgProt[i].sta) & gMfgProt[i].mask) == gMfgProt[i].mask; })) {
\t\t\t\t/* the bus did not drain: powering off now could hang it; leave MFG1 on and usable */
\t\t\t\tsay(out, end, "  MFG1: bus protection step %d not acknowledged (sta 0x%08x), left on\\n", i + 1,
\t\t\t\t    rd32(fInfra, gMfgProt[i].sta));
\t\t\t\tprotRelease(i);
\t\t\t\treturn false;
\t\t\t}
\t\t}
\t}
\tif (gMfgDom[n].hasSram) {
\t\tset32(fSpm, con, PWR_SRAM_PDN);
\t\tif (!pollUs(1000, [&]{ return (rd32(fSpm, con) & PWR_SRAM_PDN_ACK) != 0; })) {
\t\t\tsay(out, end, "  MFG%u: no SRAM power-down ack\\n", n);
\t\t}
\t}""",
        "MappleGPU.cpp: 2-step bus prot and hasSram guard in mtcmosOff",
    )

    replace_exact(
        p,
        """static const struct { uint32_t khz, vgpu, vsram; } gMappleOpp[] = {
\t{ 950000, 90000, 100000 }, { 940000, 89375, 99375 }, { 930000, 88750, 98750 }, { 920000, 88125, 98125 },
\t{ 910000, 87500, 97500 }, { 900000, 86875, 96875 }, { 890000, 86250, 96250 }, { 880000, 85625, 95625 },
\t{ 870000, 85000, 95000 }, { 860000, 84375, 94375 }, { 850000, 83750, 93750 }, { 840000, 83125, 93125 },
\t{ 830000, 82500, 92500 }, { 820000, 81875, 91875 }, { 810000, 81250, 91250 }, { 800000, 80625, 90625 },
\t{ 790000, 80000, 90000 }, { 775000, 79375, 89375 }, { 761000, 78750, 88750 }, { 746000, 78125, 88125 },
\t{ 732000, 77500, 87500 }, { 718000, 76875, 86875 }, { 703000, 76250, 86250 }, { 689000, 75625, 85625 },
\t{ 675000, 75000, 85000 }, { 660000, 74375, 85000 }, { 646000, 73750, 85000 }, { 631000, 73125, 85000 },
\t{ 617000, 72500, 85000 }, { 603000, 71875, 85000 }, { 588000, 71250, 85000 }, { 574000, 70625, 85000 },
\t{ 560000, 70000, 85000 }, { 543000, 69375, 85000 }, { 527000, 68750, 85000 }, { 511000, 68125, 85000 },
\t{ 495000, 67500, 85000 }, { 478000, 66875, 85000 }, { 462000, 66250, 85000 }, { 446000, 65625, 85000 },
\t{ 430000, 65000, 85000 }, { 413000, 64375, 85000 }, { 397000, 63750, 85000 }, { 381000, 63125, 85000 },
\t{ 365000, 62500, 85000 }, { 348000, 61875, 85000 }, { 332000, 61250, 85000 }, { 316000, 60625, 85000 },
\t{ 300000, 60000, 85000 },
};""",
        """static const struct { uint32_t khz, vgpu, vsram; } gMappleOpp[] = {
\t{ 950000, 90000, 100000 }, { 925000, 87500, 97500 }, { 900000, 85000, 95000 }, { 875000, 82500, 92500 },
\t{ 850000, 80000, 90000 }, { 823000, 79375, 89375 }, { 796000, 78125, 88125 }, { 769000, 76875, 86875 },
\t{ 743000, 75625, 85625 }, { 716000, 75000, 85000 }, { 690000, 73750, 85000 }, { 663000, 72500, 85000 },
\t{ 637000, 71250, 85000 }, { 611000, 70625, 85000 }, { 586000, 70000, 85000 }, { 560000, 69375, 85000 },
\t{ 535000, 68750, 85000 }, { 509000, 68125, 85000 }, { 484000, 66875, 85000 }, { 467000, 66875, 85000 },
\t{ 450000, 66250, 85000 }, { 434000, 65625, 85000 }, { 417000, 65000, 85000 }, { 400000, 64375, 85000 },
\t{ 383000, 64375, 85000 }, { 366000, 63750, 85000 }, { 349000, 63125, 85000 }, { 332000, 62500, 85000 },
\t{ 315000, 61875, 85000 }, { 299000, 61250, 85000 },
};""",
        "MappleGPU.cpp: MT6768 SEG_GPU_DVFS OPP table",
    )

    replace_exact(
        p,
        """\t/* clock gate, then the mfgcfg setup gpufreq does after power on */
\twr32(fMfgcfg, MFG_CG_CLR, MFG_CG_BG3D);
\tset32(fMfgcfg, MFG_ASYNC_CON, (1u << 22) | (1u << 23) | (1u << 24) | (1u << 25));
\tclr32(fMfgcfg, MFG_GLOBAL_CON, (1u << 10) | (1u << 9));
\tset32(fMfgcfg, MFG_QCHANNEL_CON, 1u << 4);
\tclr32(fMfgcfg, MFG_GLOBAL_CON, (1u << 19) | (1u << 8));
\tset32(fMfgcfg, MFG_ASYNC_CON_1, 1u << 0);
\tset32(fMfgcfg, MFG_I2M_PROTECTOR, 1u << 0);
\twr32(fMfgcfg, MFG_TIMESTAMP, 1);
\tif (!fAcpDone) {
\t\tset32(fInfra, INFRA_ACP_CON, 1u << 9);
\t\tfAcpDone = true;
\t}
\tsay(out, end, "mfgcfg: cg 0x%08x async 0x%08x/0x%08x global 0x%08x qchannel 0x%08x\\n",
\t    rd32(fMfgcfg, MFG_CG_CON), rd32(fMfgcfg, MFG_ASYNC_CON), rd32(fMfgcfg, MFG_ASYNC_CON_1),
\t    rd32(fMfgcfg, MFG_GLOBAL_CON), rd32(fMfgcfg, MFG_QCHANNEL_CON));""",
        """\t/* MT6768 mfgcfg: ungate BG3D only (no MT6781 async/global/qchannel/I2M/ACP regs) */
\twr32(fMfgcfg, MFG_CG_CLR, MFG_CG_BG3D);
\tsay(out, end, "mfgcfg: cg 0x%08x\\n", rd32(fMfgcfg, MFG_CG_CON));""",
        "MappleGPU.cpp: MT6768 mfgOn (BG3D CG_CLR only)",
    )

    replace_exact(
        p,
        """\tif ((st & MFG_STATUS_BIT(0)) && (st & MFG_STATUS_BIT(1))) {
\t\t/* wait for the GPU bus to go idle */
\t\twr32(fMfgcfg, MFG_QCHANNEL_CON, 1);
\t\twr32(fMfgcfg, MFG_BUS_IDLE_CON, 3);
\t\tif (!pollUs(10000, [&]{ return (rd32(fMfgcfg, MFG_BUS_IDLE_STA) & (1u << 2)) != 0; })) {
\t\t\tsay(out, end, "  bus not idle (0x%08x)\\n", rd32(fMfgcfg, MFG_BUS_IDLE_STA));
\t\t}
\t\twr32(fMfgcfg, MFG_CG_SET, MFG_CG_BG3D);
\t}""",
        """\tif ((st & MFG_STATUS_BIT(0)) && (st & MFG_STATUS_BIT(1))) {
\t\twr32(fMfgcfg, MFG_CG_SET, MFG_CG_BG3D);
\t}""",
        "MappleGPU.cpp: MT6768 mfgOff (BG3D CG_SET only)",
    )

    replace_exact(
        p,
        """\tif ((s1 & s2 & MFG_STATUS_BIT(0)) && (s1 & s2 & MFG_STATUS_BIT(1))) {
\t\tsay(out, end, "mfgcfg: cg 0x%08x async 0x%08x global 0x%08x qchannel 0x%08x\\n", rd32(fMfgcfg, MFG_CG_CON),
\t\t    rd32(fMfgcfg, MFG_ASYNC_CON), rd32(fMfgcfg, MFG_GLOBAL_CON), rd32(fMfgcfg, MFG_QCHANNEL_CON));
\t}""",
        """\tif ((s1 & s2 & MFG_STATUS_BIT(0)) && (s1 & s2 & MFG_STATUS_BIT(1))) {
\t\tsay(out, end, "mfgcfg: cg 0x%08x\\n", rd32(fMfgcfg, MFG_CG_CON));
\t}""",
        "MappleGPU.cpp: MT6768 mfgDump (MFG_CG_CON only)",
    )

    replace_exact(
        p,
        """\treadFeatures();
\t/* Panfrost's quirks for G57: TTRX_2968/TTRX_3162 varying algorithm (all revisions), IDVS group size */
\twr(GPU_SHADER_CONFIG, rd(GPU_SHADER_CONFIG) | SC_VAR_ALGORITHM);
\twr(GPU_JM_CONFIG, rd(GPU_JM_CONFIG) | JM_IDVS_GROUP_SIZE(0xf));
\twr(GPU_COHERENCY_ENABLE, COHERENCY_NONE);""",
        """\treadFeatures();
\tif ((fGpuId >> 28) >= 9) {
\t\t/* Panfrost's quirks for Valhall G57: TTRX_2968/TTRX_3162 varying algorithm, IDVS group size */
\t\twr(GPU_SHADER_CONFIG, rd(GPU_SHADER_CONFIG) | SC_VAR_ALGORITHM);
\t\twr(GPU_JM_CONFIG, rd(GPU_JM_CONFIG) | JM_IDVS_GROUP_SIZE(0xf));
\t} else {
\t\t/* Bifrost (Mali-G52): no JS_FLUSH_ID_NEXT, and floor THREAD_TLS_ALLOC at THREAD_MAX_THREADS */
\t\tfFlushReduce = false;
\t\tif (fThreadTlsAlloc < fThreadMaxThreads) {
\t\t\tfThreadTlsAlloc = fThreadMaxThreads;
\t\t}
\t}
\twr(GPU_COHERENCY_ENABLE, COHERENCY_NONE);""",
        "MappleGPU.cpp: Bifrost quirks/TLS floor/flush-reduce off in maliInit",
    )

    replace_exact(
        p,
        """\tif ((fMmuFeatures & 0xff) != 48) {
\t\tsay(out, end, "warning: MMU reports %u VA bits, page tables assume 48\\n", fMmuFeatures & 0xff);
\t}""",
        """\tif (vaBits() < 35 || vaBits() > 48) {
\t\tsay(out, end, "warning: MMU reports %u VA bits, unsupported\\n", fMmuFeatures & 0xff);
\t}""",
        "MappleGPU.cpp: validate 35..48 VA bits in maliInit",
    )

    replace_exact(
        p,
        """\tuint64_t region;
\tif (size == 0 || size >= (1ull << 48)) {
\t\tregion = 47;    /* the whole 48-bit space */""",
        """\tuint64_t region;
\tuint32_t bits = vaBits();
\tif (size == 0 || size >= (1ull << bits)) {
\t\tregion = bits - 1;    /* the whole VA space */""",
        "MappleGPU.cpp: use vaBits() in asLock",
    )

    replace_exact(
        p,
        """\twr(AS_BASE(as) + AS_TRANSCFG_LO, TRANSCFG_ADRMODE_AARCH64_4K | TRANSCFG_INA_BITS(55 - 48) |
\t    TRANSCFG_PTW_MEMATTR_WB | TRANSCFG_PTW_RA);""",
        """\twr(AS_BASE(as) + AS_TRANSCFG_LO, TRANSCFG_ADRMODE_AARCH64_4K | TRANSCFG_INA_BITS(55 - vaBits()) |
\t    TRANSCFG_PTW_MEMATTR_WB | TRANSCFG_PTW_RA);""",
        "MappleGPU.cpp: use vaBits() for TRANSCFG_INA_BITS in asProgram",
    )

    replace_exact(
        p,
        """\tsnprintf(v->name, sizeof(v->name), "Mali-G57 MC%u", (unsigned)__builtin_popcountll(fShaderPresent));""",
        """\tsnprintf(v->name, sizeof(v->name), "%s MC%u", (fGpuId >> 16) == 0x7402 ? "Mali-G52" : "Mali-G57",
\t    (unsigned)__builtin_popcountll(fShaderPresent));""",
        "MappleGPU.cpp: report Mali-G52 in getVersion",
    )

    replace_exact(
        p,
        """extern "C" void mt6781_cpufreq_boost_stats(int *on, uint64_t *starts, uint64_t *ms, uint64_t *signals);  /* 0183 */""",
        """extern "C" void mt6781_cpufreq_boost_stats(int *on, uint64_t *starts, uint64_t *ms, uint64_t *signals);  /* 0183 */
extern "C" vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);""",
        "MappleGPU.cpp: declare ml_io_map for MT6768 sysirq INTPOL",
    )

    replace_exact(
        p,
        """\tfor (int i = 0; i < 3; i++) {
\t\t/* the job interrupt has a filter: K0e fast kick and the interrupt timestamps */""",
        """\t/* MT6768 intpol-controller (0x0c53a650): Mali SPIs 0x112..0x114 (274..276) are active-LOW */
\tif (vm_offset_t intpol = ml_io_map(0x0c53a000ull, 0x1000)) {
\t\tset32(intpol, 0x670, 7u << 18);
\t\t__asm__ volatile ("dsb sy" : : : "memory");
\t}
\tfor (int i = 0; i < 3; i++) {
\t\t/* the job interrupt has a filter: K0e fast kick and the interrupt timestamps */""",
        "MappleGPU.cpp: invert MT6768 Mali SPIs 274..276 in sysirq INTPOL",
    )

    replace_exact(
        p,
        """\tvolatile uint32_t *w = (volatile uint32_t *)mem->getBytesNoCopy();
\tuint64_t target = fault ? bo->va + (64ull << 20) : bo->va + 0x200;       /* fault: an unmapped address */
\tuint32_t value = 0xc0ffee00u | (uint32_t)(fJobs & 0xff);
\tfor (int i = 0; i < 256; i++) {
\t\tw[i] = 0;
\t}
\tw[4] = (2u << 1) | (1u << 16);          /* job header: type WRITE_VALUE, index 1, no next */
\tw[8] = (uint32_t)target;                /* payload: address */
\tw[9] = (uint32_t)(target >> 32);
\tw[10] = 6;                              /* IMMEDIATE_32 */
\tw[12] = value;
\t__asm__ volatile ("dsb sy" : : : "memory");

\tuint64_t t0 = nowMs(), mmu0 = fMmuFaults, resets0 = fResets;
\tif (count > 1) {
\t\tobjectCounts(out, end, "before");
\t}
\tMappleGPUFence *fence = NULL;
\tOSArray *bos = NULL;
\tIOReturn ret = kIOReturnSuccess;
\t/* count > 1: the same job again, each with its own fence and BO list as a client's submit has */
\tfor (uint32_t k = 0; k < count && ret == kIOReturnSuccess; k++) {""",
        """\tvolatile uint32_t *w = (volatile uint32_t *)mem->getBytesNoCopy();
\tuint64_t target = fault ? bo->va + (64ull << 20) : bo->va + 0x200;       /* fault: an unmapped address */
\tuint32_t value = 0xc0ffee00u | (uint32_t)(fJobs & 0xff);

\tuint64_t t0 = nowMs(), mmu0 = fMmuFaults, resets0 = fResets;
\tif (count > 1) {
\t\tobjectCounts(out, end, "before");
\t}
\tMappleGPUFence *fence = NULL;
\tOSArray *bos = NULL;
\tIOReturn ret = kIOReturnSuccess;
\t/* count > 1: the same job again, each with its own fence and BO list as a client's submit has */
\tfor (uint32_t k = 0; k < count && ret == kIOReturnSuccess; k++) {
\t\tfor (int i = 0; i < 256; i++) {
\t\t\tw[i] = 0;
\t\t}
\t\tw[4] = (2u << 1) | ((fGpuId >> 28) < 9 ? 1u : 0u) | (1u << 16);  /* WRITE_VALUE, is_64b on Bifrost, index 1 */
\t\tw[8] = (uint32_t)target;                /* payload: address */
\t\tw[9] = (uint32_t)(target >> 32);
\t\tw[10] = 6;                              /* IMMEDIATE_32 */
\t\tw[12] = value;
\t\t__asm__ volatile ("dsb sy" : : : "memory");""",
        "MappleGPU.cpp: reset Bifrost selftest job header per iteration and set is_64b",
    )


def patch_wifi_mt6768(xnu: Path) -> None:
    """Port CONNSYS MT6631 Wi-Fi/BT (MTKPwrap.cpp, MT6631WiFi.cpp, mt6631_wifi.c) to MT6768 (soc1_0 / CONNAC1)."""
    # 1. MTKPwrap.cpp: allow DCXO_CW00 bit 5 (XO_WCN 26 MHz co-clock) and LDO_VCN33_CON0_1 (Wi-Fi VCN33)
    pwrap = xnu / "iokit/Kernel/arm/MTKPwrap.cpp"
    replace_exact(
        pwrap,
        """\t/* CONNSYS with an MT6631 A-die (consys_hw_vcn33_ctrl, co-clock efuse PALDO VCN28) */
\t{ 0x1d1c, 0x0003, 0x03 },       /* LDO_VCN33_CON0_0: RG_LDO_VCN33_EN_0, LP */""",
        """\t/* CONNSYS with an MT6631 A-die (MT6768: DCXO_CW00 XO_WCN + VCN33 BT/Wi-Fi + VCN28) */
\t{ 0x0788, 0x0020, 0x01 },       /* DCXO_CW00: XO_EXTBUF2_EN_M (WCN 26 MHz co-clock) */
\t{ 0x1d1c, 0x0003, 0x03 },       /* LDO_VCN33_CON0_0: RG_LDO_VCN33_EN_0, LP (BT) */
\t{ 0x1d2a, 0x0003, 0x03 },       /* LDO_VCN33_CON0_1: RG_LDO_VCN33_EN_1, LP (Wi-Fi) */""",
        "MTKPwrap.cpp: whitelist MT6768 DCXO_CW00 (0x788) and VCN33_CON0_1 (0x1d2a)",
    )

    # 2. MT6631WiFi.cpp: enforce 16 MiB EMI alignment (INFRA_EMI_MAPPING >> 24) and reserve +16 KiB for BTIF TX AP-DMA
    wcpp = xnu / "iokit/Kernel/arm/MT6631WiFi.cpp"
    replace_exact(
        wcpp,
        """\tif (gMappleWiFiEMI == NULL && PE_parse_boot_argn("mp_wifi_emi", &fixed, sizeof(fixed)) && fixed != 0) {
\t\tif ((fixed & 0xffff) || !mapple_pa_in_hole(fixed) || !mapple_pa_in_hole(fixed + size - 1)) {
\t\t\tIOLog("WIFI: mp_wifi_emi 0x%llx refused: not 64 KiB aligned inside one memory hole\\n", fixed);
\t\t\treturn NULL;
\t\t}""",
        """\tif (gMappleWiFiEMI == NULL && PE_parse_boot_argn("mp_wifi_emi", &fixed, sizeof(fixed)) && fixed != 0) {
\t\tif ((fixed & 0xffffff) || !mapple_pa_in_hole(fixed) || !mapple_pa_in_hole(fixed + size - 1)) {
\t\t\tIOLog("WIFI: mp_wifi_emi 0x%llx refused: not 16 MiB aligned inside one memory hole\\n", fixed);
\t\t\treturn NULL;
\t\t}""",
        "MT6631WiFi.cpp: require 16 MiB alignment for mp_wifi_emi on MT6768",
    )
    replace_exact(
        wcpp,
        """\tif (gMappleWiFiEMI == NULL) {
\t\tstatic const uint64_t masks[] = { 0x00000000ff000000ull, 0x00000000ffff0000ull };
\t\tfor (unsigned i = 0; i < 2 && gMappleWiFiEMI == NULL; i++) {
\t\t\tgMappleWiFiEMI = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task,
\t\t\t    kIODirectionInOut | kIOMemoryPhysicallyContiguous, size, masks[i]);
\t\t}""",
        """\tif (gMappleWiFiEMI == NULL) {
\t\t/* MT6768 INFRA_EMI_MAPPING (0x10001380) takes PA >> 24: EMI MUST be 16 MiB aligned.
\t\t * Prefer < 2 GiB (inside DT alloc-ranges 0x40000000..0xc0000000), fallback < 4 GiB. */
\t\tstatic const uint64_t masks[] = { 0x000000007f000000ull, 0x00000000ff000000ull };
\t\tfor (unsigned i = 0; i < 2 && gMappleWiFiEMI == NULL; i++) {
\t\t\tgMappleWiFiEMI = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task,
\t\t\t    kIODirectionInOut | kIOMemoryPhysicallyContiguous, size, masks[i]);
\t\t}""",
        "MT6631WiFi.cpp: strictly 16 MiB aligned EMI window on MT6768",
    )
    replace_exact(
        wcpp,
        """#define MT6631_WIFI_DMA_SIZE      0x48000""",
        """#define MT6631_WIFI_DMA_SIZE      0x4c000        /* +16 KiB at 0x48000 for BTIF TX AP-DMA vFIFO */""",
        "MT6631WiFi.cpp: expand DMA reserve to 0x4c000 for BTIF TX AP-DMA",
    )

    # 3. bsd/dev/mt6631_wifi.c: full MT6768 (soc1_0 / CONNAC1) adaptation
    wc = xnu / "bsd/dev/mt6631_wifi.c"
    replace_exact(
        wc,
        """/* AP side */
#define SPM_POWERON_CONFIG_EN   0x10006000
#define SPM_CONN_PWR_CON        0x10006304
#define SPM_PWR_STATUS          0x1000616c
#define SPM_PWR_STATUS_2ND      0x10006170
#define CONN_PWR_STA            (1u << 1)
#define PWR_RST_B               (1u << 0)
#define PWR_ISO                 (1u << 1)
#define PWR_ON                  (1u << 2)
#define PWR_ON_2ND              (1u << 3)
#define PWR_CLK_DIS             (1u << 4)
#define INFRA_PROT_SET          0x100012a0
#define INFRA_PROT_CLR          0x100012a4
#define INFRA_PROT_STA1         0x10001228
#define INFRA_PROT1_SET         0x100012a8
#define INFRA_PROT1_CLR         0x100012ac
#define INFRA_PROT1_STA1        0x10001258
#define INFRA_CCIF4_CG_CLR      0x100010c4
#define INFRA_CONN_CCU_CONFIG   0x10001c14
#define INFRA_EMI_MAPPING       0x10001380
#define INFRA_PERI_MAPPING      0x10001388
#define TOPRGU_SWSYSRST         0x10007018
#define GPIO_MODE23             0x10005470
#define GPIO_MODE24             0x10005480
#define IOCFG_LT_DRV            0x10002c00
/* CONN side (only with CONN powered and protection released) */
#define CONN_VERSION            0x180b1010
#define CONN_CFG_ID             0x180b101c
#define CONN_HW_VER             0x18002000
#define CONN_FW_VER             0x18002004
#define CONN_MCU_COM_REG0       0x18002600
#define CONN_MCU_EMI_SLPPROT    0x180c1168
#define CONN_AFE_WBG_PLL_04     0x180b3030
#define CONN_AFE_WBG_GL5_02     0x180b3104
#define CONN_XO_1200            0x180c1200
#define CONN_XO_1204            0x180c1204
#define CONN_XO_1240            0x180c1240
#define CONN_ADIE_CTL           0x180c1500
#define CONN_SPI_STA            0x180c6000
#define CONN_SPI_ADDR           0x180c6050
#define CONN_SPI_WDAT           0x180c6054
#define CONN_SPI_RDAT           0x180c6058
#define CONN_VERSION_VALUE      0x100b0000""",
        """/* AP side (MT6768 / SM-A325M) */
#define SPM_POWERON_CONFIG_EN   0x10006000
#define SPM_CONN_PWR_CON        0x1000632c
#define SPM_PWR_STATUS          0x10006180
#define SPM_PWR_STATUS_2ND      0x10006184
#define SPM_APSRC_6F8           0x100066f8
#define SPM_APSRC_6FC           0x100066fc
#define CONN_PWR_STA            (1u << 1)
#define PWR_RST_B               (1u << 0)
#define PWR_ISO                 (1u << 1)
#define PWR_ON                  (1u << 2)
#define PWR_ON_2ND              (1u << 3)
#define PWR_CLK_DIS             (1u << 4)
#define INFRA_PROT_SET          0x100012a0
#define INFRA_PROT_CLR          0x100012a4
#define INFRA_PROT_STA1         0x10001228
#define INFRA_PROT1_SET         0x100012a8
#define INFRA_PROT1_CLR         0x100012ac
#define INFRA_PROT1_STA1        0x10001258
#define INFRA_EMI_MAPPING       0x10001380
#define INFRA_PERI_MAPPING      0x10001388
#define TOPRGU_SWSYSRST         0x10007018
#define GPIO_MODE23             0x100053e0      /* MT6768 GPIO_MODE14 (A-die interface) */
#define GPIO_MODE24             0x100053f0      /* MT6768 GPIO_MODE15 (A-die interface) */
/* CONN side (only with CONN powered and protection released) */
#define CONN_VERSION            0x180b1010
#define CONN_CFG_ID             0x180b101c
#define CONN_HW_VER             0x18002000
#define CONN_FW_VER             0x18002004
#define CONN_SW_IRQ_REG         0x18002150      /* bit 21 cleared: EMI control HW mode */
#define CONN_MCU_COM_REG0       0x18002600
#define CONN_AFE_WBG_PLL_03     0x180b3034      /* MT6768 RG_WBG_PLL_03: bits 19-20 = 01 */
#define CONN_XO_1200            0x180c1200
#define CONN_XO_1204            0x180c1204
#define CONN_XO_1240            0x180c1240
#define CONN_ADIE_CTL           0x180c1500
#define CONN_TOPPOSE_RESTORE    0x180c1600      /* bit 23 set: toppose_restore_done */
#define CONN_SPI_STA            0x180c6000
#define CONN_SPI_ADDR           0x180c6050
#define CONN_SPI_WDAT           0x180c6054
#define CONN_SPI_RDAT           0x180c6058
#define CONN_VERSION_VALUE      0x10020501      /* MT6768 Talbot SoC 1.0 */
#define PMIC_DCXO_CW00          0x0788          /* bit 5: XO_EXTBUF2_EN_M (WCN 26 MHz) */""",
        "mt6631_wifi.c: MT6768 SPM, pinmux, CONN version, AFE, XO, DCXO register addresses",
    )

    replace_exact(
        wc,
        """\tsay("emi remap 0x%08x, toprgu swsysrst 0x%08x (conn reset bit12 %u), pinmux mode23 0x%08x mode24 0x%08x drv 0x%08x\\n",
\t    rd(INFRA_EMI_MAPPING), rd(TOPRGU_SWSYSRST), !!(rd(TOPRGU_SWSYSRST) & (1u << 12)), rd(GPIO_MODE23),
\t    rd(GPIO_MODE24), rd(IOCFG_LT_DRV));""",
        """\tsay("emi remap 0x%08x, toprgu swsysrst 0x%08x (conn reset bit12 %u), pinmux mode14 0x%08x mode15 0x%08x dcxo_cw00 0x%04x\\n",
\t    rd(INFRA_EMI_MAPPING), rd(TOPRGU_SWSYSRST), !!(rd(TOPRGU_SWSYSRST) & (1u << 12)), rd(GPIO_MODE23),
\t    rd(GPIO_MODE24), pmic(PMIC_DCXO_CW00));""",
        "mt6631_wifi.c: wifi_status show MT6768 pinmux and DCXO_CW00",
    )

    replace_exact(
        wc,
        """\tuint32_t ana18 = pmic(PMIC_VCN18_ANA);
\tif (((ana18 >> 8) & 0xf) != 12) {
\t\tsay("detect: VCN18 voltage select %u is not 1.8 V: refused (no voltage writes here)\\n", (ana18 >> 8) & 0xf);
\t\treturn o;
\t}""",
        """\t/* MT6358 VCN18 is fixed 1.8 V LDO (wifi_detect: no VOSEL, 0x1e8c reads 0). */""",
        "mt6631_wifi.c: MT6358 VCN18 is fixed 1.8 V in wifi_detect",
    )

    replace_exact(
        wc,
        """\tif (((pmic(PMIC_VCN18_ANA) >> 8) & 0xf) != 12) {
\t\tsay("on: VCN18 is not 1.8 V: refused\\n");
\t\treturn o;
\t}""",
        """\t/* MT6358 VCN18 is fixed 1.8 V LDO (wifi_on: no VOSEL, 0x1e8c reads 0). */""",
        "mt6631_wifi.c: MT6358 VCN18 is fixed 1.8 V in wifi_on",
    )

    replace_exact(
        wc,
        """\tuint32_t oldM23 = rd(GPIO_MODE23), oldM24 = rd(GPIO_MODE24), oldDrv = rd(IOCFG_LT_DRV);
\t/* 1. VCN18 (consys_hw_vcn18_ctrl: EN=1 LP=0, SW_OP_EN=1 HW0_OP_EN=1, HW0_OP_CFG=1) */
\tint e = mtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, 0x3);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_CFG, 0x2, 0x2);
\tdelay(300);
\tsay("detect: vcn18 %s, qi %u\\n", e ? "WRITE FAILED" : "on", !!(pmic(PMIC_VCN18_CON1) & 0x8000));
\tif (e) {
\t\tgoto rails;
\t}
\t/* 2. A-die interface pins (set_if_pinmux(1)), 150 us */
\trmw(GPIO_MODE24, 0x77777777, 0x11111111);
\trmw(GPIO_MODE23, 0x70000000, 0x10000000);
\trmw(IOCFG_LT_DRV, 0x07ffffff, 0x1209);
\tdelay(150);
\t/* 3. CONN CPU held in reset (key 0x88, bit 12), SPM register access */
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88001000);
\trmw(SPM_POWERON_CONFIG_EN, 0xffff0001, 0x0b160001);
\t/* 4. MTCMOS on (consys_hw_power_ctrl_mt6781_gen) */
\twr(INFRA_CCIF4_CG_CLR, 1u << 24);
\trmw(SPM_CONN_PWR_CON, PWR_ON, PWR_ON);
\tif (!poll(SPM_PWR_STATUS, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("detect: PWR_STATUS did not come up\\n");
\t\tgoto off;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_ON_2ND, PWR_ON_2ND);
\tif (!poll(SPM_PWR_STATUS_2ND, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("detect: PWR_STATUS_2ND did not come up\\n");
\t\tgoto off;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_CLK_DIS, 0);
\tdelay(1);
\trmw(SPM_CONN_PWR_CON, PWR_ISO, 0);
\trmw(SPM_CONN_PWR_CON, PWR_RST_B, PWR_RST_B);
\tdelay(5000);
\t/* 5. bus protection off, each acknowledged */
\t{
\t\tstatic const struct { uint64_t clr, sta; uint32_t bit; } prot[] = {
\t\t\t{ INFRA_PROT_CLR, INFRA_PROT_STA1, 0x10000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x4000 },
\t\t\t{ INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x40000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x2000 },
\t\t};
\t\tfor (unsigned i = 0; i < sizeof(prot) / sizeof(prot[0]); i++) {
\t\t\twr(prot[i].clr, prot[i].bit);
\t\t\tif (!poll(prot[i].sta, prot[i].bit, 0, 10000)) {
\t\t\t\tsay("detect: bus protection 0x%x did not release: CONN registers not touched\\n", prot[i].bit);
\t\t\t\tgoto off;
\t\t\t}
\t\t}
\t}
\twr(INFRA_CONN_CCU_CONFIG, 0);
\tdelay(10);
\t/* 6. chip id (polling_consys_chipid: 10 x 20 ms) */
\t{
\t\tbool idOK = false;
\t\tfor (int i = 0; i < 10 && !(idOK = rd(CONN_VERSION) == CONN_VERSION_VALUE); i++) {
\t\t\tdelay(20000);
\t\t}
\t\tsay("detect: conn version 0x%08x (%s), cfg id %u, hw 0x%04x, fw 0x%04x\\n", rd(CONN_VERSION),
\t\t    idOK ? "ok" : "UNEXPECTED", rd(CONN_CFG_ID) & 0xf, rd(CONN_HW_VER) & 0xffff, rd(CONN_FW_VER) & 0xffff);
\t\tif (!idOK) {
\t\t\tgoto off;
\t\t}
\t}
\t/* 7. EMI HW mode, AFE, XO (legacy co-clock branch: WMT_SOC.cfg co_clock_flag=1) */
\trmw(CONN_MCU_EMI_SLPPROT, 1u << 0, 1u << 0);
\twr(CONN_AFE_WBG_PLL_04, 0x00040088);
\twr(CONN_AFE_WBG_GL5_02, 0xa8808800);
\trmw(CONN_XO_1240, 1u << 7, 0);
\trmw(CONN_XO_1204, 0xff00, 0x0600);
\trmw(CONN_XO_1200, 0xffff, 0x0708);
\trmw(CONN_XO_1204, 1u << 0, 0);
\t/* 8. A-die id over the RF SPI (consys_adie_chipid_checking_flow) */
\trmw(CONN_ADIE_CTL, 1u << 0, 1u << 0);
\t{
\t\tuint32_t a = adie_read(0xb024), b = adie_read(0xb02c);
\t\tsay("detect: a-die spi 0xb024 -> 0x%08x (id 0x%04x), 0xb02c -> 0x%08x (id 0x%04x): %s\\n", a, a >> 16, b, b >> 16,
\t\t    (a >> 16) == 0x6631 ? "MT6631 (firmware *_soc2_2_*_2_1*)" : (b >> 16) == 0x6635 ?
\t\t    "MT6635 (firmware *_2a_1*; its 1.3 V rail was off, so this read is lucky)" :
\t\t    "no answer (MT6635 needs VS2 1.4625 V + GPIO12 on to answer)");
\t}
\trmw(CONN_ADIE_CTL, 1u << 0, 0);
off:
\to = conn_off(out, len, o);
\twr(GPIO_MODE24, (rd(GPIO_MODE24) & ~0x77777777u) | (oldM24 & 0x77777777u));
\twr(GPIO_MODE23, (rd(GPIO_MODE23) & ~0x70000000u) | (oldM23 & 0x70000000u));
\twr(IOCFG_LT_DRV, (rd(IOCFG_LT_DRV) & ~0x07ffffffu) | (oldDrv & 0x07ffffffu));""",
        """\tuint32_t oldM23 = rd(GPIO_MODE23), oldM24 = rd(GPIO_MODE24);
\t/* 1. VCN18 (consys_hw_vcn18_ctrl: EN=1 LP=0, SW_OP_EN=1 HW0_OP_EN=1, HW0_OP_CFG=1) */
\tint e = mtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, 0x3);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_CFG, 0x2, 0x2);
\tdelay(300);
\tsay("detect: vcn18 %s, qi %u\\n", e ? "WRITE FAILED" : "on", !!(pmic(PMIC_VCN18_CON1) & 0x8000));
\tif (e) {
\t\tgoto rails;
\t}
\t/* 2. MT6768 A-die interface pins (mt6768.h), 150 us */
\trmw(GPIO_MODE24, 0x00000077, 0x00000011);
\trmw(GPIO_MODE23, 0x77777770, 0x11111110);
\tdelay(150);
\t/* 3. CONN CPU held in reset (key 0x88, bit 12), SPM register access */
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88001000);
\trmw(SPM_POWERON_CONFIG_EN, 0xffff0001, 0x0b160001);
\t/* 4. MT6768 MTCMOS on */
\trmw(SPM_CONN_PWR_CON, PWR_ON, PWR_ON);
\tif (!poll(SPM_PWR_STATUS, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("detect: PWR_STATUS did not come up\\n");
\t\tgoto off;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_ON_2ND, PWR_ON_2ND);
\tif (!poll(SPM_PWR_STATUS_2ND, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("detect: PWR_STATUS_2ND did not come up\\n");
\t\tgoto off;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_CLK_DIS, 0);
\tdelay(1);
\trmw(SPM_CONN_PWR_CON, PWR_ISO, 0);
\trmw(SPM_CONN_PWR_CON, PWR_RST_B, PWR_RST_B);
\tdelay(5000);
\trmw(TOPRGU_SWSYSRST, 0xff000200, 0x88000000); /* ap_sw_rst_b clear */
\t/* 5. bus protection off, each acknowledged */
\t{
\t\tstatic const struct { uint64_t clr, sta; uint32_t bit; } prot[] = {
\t\t\t{ INFRA_PROT_CLR, INFRA_PROT_STA1, 0x10000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x4000 },
\t\t\t{ INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x40000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x2000 },
\t\t};
\t\tfor (unsigned i = 0; i < sizeof(prot) / sizeof(prot[0]); i++) {
\t\t\twr(prot[i].clr, prot[i].bit);
\t\t\tif (!poll(prot[i].sta, prot[i].bit, 0, 10000)) {
\t\t\t\tsay("detect: bus protection 0x%x did not release: CONN registers not touched\\n", prot[i].bit);
\t\t\t\tgoto off;
\t\t\t}
\t\t}
\t}
\tdelay(10);
\t/* 6. chip id (polling_consys_chipid: 10 x 20 ms) */
\t{
\t\tbool idOK = false;
\t\tfor (int i = 0; i < 10 && !(idOK = rd(CONN_VERSION) == CONN_VERSION_VALUE); i++) {
\t\t\tdelay(20000);
\t\t}
\t\tsay("detect: conn version 0x%08x (%s), cfg id %u, hw 0x%04x, fw 0x%04x\\n", rd(CONN_VERSION),
\t\t    idOK ? "ok" : "UNEXPECTED", rd(CONN_CFG_ID) & 0xf, rd(CONN_HW_VER) & 0xffff, rd(CONN_FW_VER) & 0xffff);
\t\tif (!idOK) {
\t\t\tgoto off;
\t\t}
\t}
\t/* 7. MT6768 EMI HW mode, AFE, XO */
\trmw(CONN_SW_IRQ_REG, 1u << 21, 0);
\trmw(CONN_AFE_WBG_PLL_03, 0x00180000, 0x00080000);
\trmw(CONN_XO_1240, 1u << 7, 0);
\trmw(CONN_XO_1204, 0xff01, 0x0600);
\trmw(CONN_XO_1200, 0xffff, 0x0708);
\t/* 8. A-die id over the RF SPI */
\trmw(CONN_ADIE_CTL, 1u << 0, 1u << 0);
\t{
\t\tuint32_t a = adie_read(0xb024), b = adie_read(0xb02c);
\t\tsay("detect: a-die spi 0xb024 -> 0x%08x (id 0x%04x), 0xb02c -> 0x%08x (id 0x%04x): %s\\n", a, a >> 16, b, b >> 16,
\t\t    (a >> 16) == 0x6631 ? "MT6631 (soc1_0_1a_1)" : "unexpected A-die");
\t}
\trmw(CONN_ADIE_CTL, 1u << 0, 0);
off:
\to = conn_off(out, len, o);
\twr(GPIO_MODE24, (rd(GPIO_MODE24) & ~0x00000077u) | (oldM24 & 0x00000077u));
\twr(GPIO_MODE23, (rd(GPIO_MODE23) & ~0x77777770u) | (oldM23 & 0x77777770u));""",
        "mt6631_wifi.c: MT6768 wifi_detect sequence",
    )

    replace_exact(
        wc,
        """static struct {
\tbool     on, wmt;
\tvoid    *emi;
\tuint64_t emiPA;
\tuint32_t old18, old18Op, old18Cfg, old33, old33Op, old33Cfg, old28, old28Op, old28Cfg;
\tuint32_t oldM23, oldM24, oldDrv;
} vw;""",
        """static struct {
\tbool     on, wmt;
\tvoid    *emi;
\tuint64_t emiPA;
\tuint32_t old18, old18Op, old18Cfg, old33, old33_1, old33Op, old33Cfg, old28, old28Op, old28Cfg, oldXo;
\tuint32_t oldM23, oldM24;
} vw;""",
        "mt6631_wifi.c: track VCN33_CON0_1 and DCXO_CW00 in struct vw",
    )

    replace_exact(
        wc,
        """\tif (fw_open("soc2_2_ram_mcu_2_1_hdr.bin", &f) != 0) {""",
        """\tif (fw_open("soc1_0_ram_mcu_1a_1_hdr.bin", &f) != 0) {""",
        "mt6631_wifi.c: check soc1_0_ram_mcu_1a_1_hdr.bin in firmware_present",
    )

    replace_exact(
        wc,
        """\tif (err == 0) {
\t\tmemcpy((char *)vw.emi + dateOff, h.date, 16);
\t\tmt6631_wifi_emi_flush(off, body);
\t\tmt6631_wifi_emi_flush(dateOff & ~63u, 64);""",
        """\tif (err == 0) {
\t\tuint8_t dt[16];
\t\tmemcpy(dt, h.date, 15);
\t\tdt[15] = 0;
\t\tmemcpy((char *)vw.emi + dateOff, dt, 16);
\t\tmt6631_wifi_emi_flush(off, body);
\t\tmt6631_wifi_emi_flush(dateOff & ~63u, 64);""",
        "mt6631_wifi.c: 15-char NUL-terminated buildtime in EMI (mt6768.c)",
    )

    replace_exact(
        wc,
        """static void
rails_restore(void)
{
\tmtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, vw.old28 & 0x1);
\tmtk_pwrap_write_regulator(PMIC_VCN28_OP_CFG, 0x2, vw.old28Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN28_OP_EN, 0x2, vw.old28Op & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN33_OP_CFG, 0x2, vw.old33Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN33_OP_EN, 0x3, vw.old33Op & 0x3);
\tmtk_pwrap_write_regulator(PMIC_VCN33_CON0_0, 0x3, vw.old33 & 0x3);
\tdelay(300);
\tmtk_pwrap_write_regulator(PMIC_VCN18_OP_CFG, 0x2, vw.old18Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, vw.old18Op & 0x3);
\tmtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, vw.old18 & 0x3);
}

static void
pinmux_restore(void)
{
\twr(GPIO_MODE24, (rd(GPIO_MODE24) & ~0x77777777u) | (vw.oldM24 & 0x77777777u));
\twr(GPIO_MODE23, (rd(GPIO_MODE23) & ~0x70000000u) | (vw.oldM23 & 0x70000000u));
\twr(IOCFG_LT_DRV, (rd(IOCFG_LT_DRV) & ~0x07ffffffu) | (vw.oldDrv & 0x07ffffffu));
}""",
        """static void
rails_restore(void)
{
\tmtk_pwrap_write_regulator(PMIC_DCXO_CW00, 0x20, vw.oldXo & 0x20);
\tmtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, vw.old28 & 0x1);
\tmtk_pwrap_write_regulator(PMIC_VCN28_OP_CFG, 0x2, vw.old28Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN28_OP_EN, 0x2, vw.old28Op & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN33_OP_CFG, 0x2, vw.old33Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN33_OP_EN, 0x3, vw.old33Op & 0x3);
\tmtk_pwrap_write_regulator(PMIC_VCN33_CON0_1, 0x3, vw.old33_1 & 0x3);
\tmtk_pwrap_write_regulator(PMIC_VCN33_CON0_0, 0x3, vw.old33 & 0x3);
\tdelay(300);
\tmtk_pwrap_write_regulator(PMIC_VCN18_OP_CFG, 0x2, vw.old18Cfg & 0x2);
\tmtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, vw.old18Op & 0x3);
\tmtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, vw.old18 & 0x3);
}

static void
pinmux_restore(void)
{
\twr(GPIO_MODE24, (rd(GPIO_MODE24) & ~0x00000077u) | (vw.oldM24 & 0x00000077u));
\twr(GPIO_MODE23, (rd(GPIO_MODE23) & ~0x77777770u) | (vw.oldM23 & 0x77777770u));
}""",
        "mt6631_wifi.c: MT6768 rails_restore and pinmux_restore",
    )

    replace_exact(
        wc,
        """\tbzero(vw.emi, EMI_SIZE);
\tmt6631_wifi_emi_flush(0, EMI_SIZE);
\tsay("on: EMI window PA 0x%llx (%s aligned), MCU sees it at 0x%08x\\n", vw.emiPA,
\t    (vw.emiPA & 0xffffff) == 0 ? "16 MiB" : "64 KiB", EMI_CONN_BASE);
\tif (load_patch("soc2_2_ram_mcu_2_1_hdr.bin", 0x000000, 0x88320, out, len, &o) ||
\t    load_patch("soc2_2_ram_bt_2_1_hdr.bin", 0x170000, 0x88300, out, len, &o) ||
\t    load_patch("soc2_2_ram_wifi_2_1_hdr.bin", 0x2a0000, 0x88310, out, len, &o)) {
\t\treturn o;
\t}
\trmw(INFRA_EMI_MAPPING, 0xfffff, (uint32_t)(vw.emiPA >> 16));
\trmw(INFRA_PERI_MAPPING, 0xfffff, 0x1000);
\t/* rails: VCN18, then VCN33 at 3.3 V (6631), VCN28 on in co-clock mode (efuse PALDO, HW mode off) */
\tvw.old18 = pmic(PMIC_VCN18_CON0);
\tvw.old18Op = pmic(PMIC_VCN18_OP_EN);
\tvw.old18Cfg = pmic(PMIC_VCN18_OP_CFG);
\tvw.old33 = pmic(PMIC_VCN33_CON0_0);
\tvw.old33Op = pmic(PMIC_VCN33_OP_EN);
\tvw.old33Cfg = pmic(PMIC_VCN33_OP_CFG);
\tvw.old28 = pmic(PMIC_VCN28_CON0);
\tvw.old28Op = pmic(PMIC_VCN28_OP_EN);
\tvw.old28Cfg = pmic(PMIC_VCN28_OP_CFG);
\tvw.oldM23 = rd(GPIO_MODE23);
\tvw.oldM24 = rd(GPIO_MODE24);
\tvw.oldDrv = rd(IOCFG_LT_DRV);
\tint e = mtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, 0x3);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_OP_CFG, 0x2, 0x2);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_ANA, 0x300, 0x100);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_CON0_0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_OP_EN, 0x3, 0x3);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_OP_CFG, 0x2, 0x2);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_OP_EN, 0x2, 0x0);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_OP_CFG, 0x2, 0x0);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, 0x1);
\tdelay(300);
\tsay("on: rails %s: vcn18 qi %u, vcn33 qi %u (ana %04x), vcn28 qi %u\\n", e ? "WRITE FAILED" : "on",
\t    !!(pmic(PMIC_VCN18_CON1) & 0x8000), !!(pmic(PMIC_VCN33_CON1) & 0x8000), pmic(PMIC_VCN33_ANA),
\t    !!(pmic(PMIC_VCN28_CON1) & 0x8000));
\tvw.on = true;           /* from here "wifi off" undoes everything */
\tif (e) {
\t\tgoto fail;
\t}
\trmw(GPIO_MODE24, 0x77777777, 0x11111111);
\trmw(GPIO_MODE23, 0x70000000, 0x10000000);
\trmw(IOCFG_LT_DRV, 0x07ffffff, 0x1209);
\tdelay(150);
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88001000);
\trmw(SPM_POWERON_CONFIG_EN, 0xffff0001, 0x0b160001);
\twr(INFRA_CCIF4_CG_CLR, 1u << 24);
\trmw(SPM_CONN_PWR_CON, PWR_ON, PWR_ON);
\tif (!poll(SPM_PWR_STATUS, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("on: PWR_STATUS did not come up\\n");
\t\tgoto fail;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_ON_2ND, PWR_ON_2ND);
\tif (!poll(SPM_PWR_STATUS_2ND, CONN_PWR_STA, CONN_PWR_STA, 5000)) {
\t\tsay("on: PWR_STATUS_2ND did not come up\\n");
\t\tgoto fail;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_CLK_DIS, 0);
\tdelay(1);
\trmw(SPM_CONN_PWR_CON, PWR_ISO, 0);
\trmw(SPM_CONN_PWR_CON, PWR_RST_B, PWR_RST_B);
\tdelay(5000);
\t{
\t\tstatic const struct { uint64_t clr, sta; uint32_t bit; } prot[] = {
\t\t\t{ INFRA_PROT_CLR, INFRA_PROT_STA1, 0x10000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x4000 },
\t\t\t{ INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x40000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x2000 },
\t\t};
\t\tfor (unsigned i = 0; i < sizeof(prot) / sizeof(prot[0]); i++) {
\t\t\twr(prot[i].clr, prot[i].bit);
\t\t\tif (!poll(prot[i].sta, prot[i].bit, 0, 10000)) {
\t\t\t\tsay("on: bus protection 0x%x did not release\\n", prot[i].bit);
\t\t\t\tgoto fail;
\t\t\t}
\t\t}
\t}
\twr(INFRA_CONN_CCU_CONFIG, 0);
\tdelay(10);
\t{
\t\tbool idOK = false;
\t\tfor (int i = 0; i < 10 && !(idOK = rd(CONN_VERSION) == CONN_VERSION_VALUE); i++) {
\t\t\tdelay(20000);
\t\t}
\t\tif (!idOK) {
\t\t\tsay("on: conn version 0x%08x, not 0x%08x\\n", rd(CONN_VERSION), CONN_VERSION_VALUE);
\t\t\tgoto fail;
\t\t}
\t}
\trmw(CONN_MCU_EMI_SLPPROT, 1u << 0, 1u << 0);           /* 11. EMI HW mode */
\twr(CONN_AFE_WBG_PLL_04, 0x00040088);                    /* 13. AFE */
\twr(CONN_AFE_WBG_GL5_02, 0xa8808800);
\twr(CONN_BT_EMI_ENTRY, EMI_CONN_BASE + 0x170000);        /* 14. EMI entries */
\twr(CONN_WF_EMI_ENTRY, EMI_CONN_BASE + 0x2a0000);
\trmw(CONN_XO_1240, 1u << 7, 0);                          /* 15. XO, legacy co-clock */
\trmw(CONN_XO_1204, 0xff00, 0x0600);
\trmw(CONN_XO_1200, 0xffff, 0x0708);
\trmw(CONN_XO_1204, 1u << 0, 0);
\trmw(CONN_ADIE_SEL, 0x300, 0x100);                       /* 16. A-die = MT6631 */
\twr(CONN_BUSHANGCR, 0x90000002);                         /* 18. bus timeout */
\trmw(CONN_RGU_DLY_CHAIN, 0xf0000, 0x40000);              /* 19. MCU mem PDN delay */
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88000000);           /* 20. CPU out of reset: ROM, then the patches */
\t{
\t\tbool idle = false;
\t\tuint32_t t;
\t\tfor (t = 0; t < 41 && !(idle = rd(CONN_MCU_COM_REG0) == CONN_IDLE_MAGIC); t++) {
\t\t\tdelay(6000);
\t\t}
\t\tsay("on: MCU %s after ~%u ms: com_reg0 0x%08x, pc 0x%08x, dbg 0x%08x\\n", idle ? "IDLE LOOP" :
\t\t    "NOT IDLE", t * 6, rd(CONN_MCU_COM_REG0), rd(CONN_MCU_PC), rd(CONN_MCU_DBG_216C));
\t\tif (!idle) {
\t\t\tsay("on: left on for inspection (wifi status, peek); wifi off to power down. Next suspects: EMI MPU "
\t\t\t    "SMC, patch placement\\n");
\t\t}
\t}""",
        """\tif ((vw.emiPA & 0xffffff) != 0) {
\t\tsay("on: EMI window PA 0x%llx is NOT 16 MiB aligned (required by MT6768 INFRA_EMI_MAPPING)\\n", vw.emiPA);
\t\treturn o;
\t}
\tbzero(vw.emi, EMI_SIZE);
\t/* Dedicated FW log ring header at EMI +0x36500 (mt6768.c: MCU 16K, Wi-Fi 64K, BT 64K, GPS 32K + "EMIFWLOG") */
\t{
\t\tstatic const uint32_t loghdr[8] = {
\t\t\t0x40, 16 * 1024,
\t\t\t0x40 + 16 * 1024 + 64, 64 * 1024,
\t\t\t0x40 + 16 * 1024 + 64 + 64 * 1024 + 64, 64 * 1024,
\t\t\t0x40 + 16 * 1024 + 64 + 2 * (64 * 1024 + 64), 32 * 1024,
\t\t};
\t\tuint8_t *lp = (uint8_t *)vw.emi + 0x36500;
\t\tmemcpy(lp, loghdr, sizeof(loghdr));
\t\tmemcpy(lp + 56, "EMIFWLOG", 8);
\t}
\tmt6631_wifi_emi_flush(0, EMI_SIZE);
\tsay("on: EMI window PA 0x%llx (16 MiB aligned), MCU sees it at 0x%08x\\n", vw.emiPA, EMI_CONN_BASE);
\tif (load_patch("soc1_0_ram_mcu_1a_1_hdr.bin", 0x000000, 0x68320, out, len, &o) ||
\t    load_patch("soc1_0_ram_bt_1a_1_hdr.bin", 0x080000, 0x68300, out, len, &o) ||
\t    load_patch("soc1_0_ram_wifi_1a_1_hdr.bin", 0x140000, 0x68310, out, len, &o)) {
\t\treturn o;
\t}
\trmw(INFRA_EMI_MAPPING, 0xfff, (uint32_t)((vw.emiPA >> 24) & 0xfff));
\trmw(INFRA_PERI_MAPPING, 0x0fff0000, ((0x10003000u >> 24) & 0xfff) << 16);
\t/* rails: VCN18, VCN33 at 3.3 V (both BT EN0 and Wi-Fi EN1), VCN28, and DCXO_CW00 XO_WCN 26 MHz co-clock */
\tvw.old18 = pmic(PMIC_VCN18_CON0);
\tvw.old18Op = pmic(PMIC_VCN18_OP_EN);
\tvw.old18Cfg = pmic(PMIC_VCN18_OP_CFG);
\tvw.old33 = pmic(PMIC_VCN33_CON0_0);
\tvw.old33_1 = pmic(PMIC_VCN33_CON0_1);
\tvw.old33Op = pmic(PMIC_VCN33_OP_EN);
\tvw.old33Cfg = pmic(PMIC_VCN33_OP_CFG);
\tvw.old28 = pmic(PMIC_VCN28_CON0);
\tvw.old28Op = pmic(PMIC_VCN28_OP_EN);
\tvw.old28Cfg = pmic(PMIC_VCN28_OP_CFG);
\tvw.oldXo = pmic(PMIC_DCXO_CW00);
\tvw.oldM23 = rd(GPIO_MODE23);
\tvw.oldM24 = rd(GPIO_MODE24);
\tint e = mtk_pwrap_write_regulator(PMIC_VCN18_OP_EN, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN18_CON0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_OP_EN, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_ANA, 0x300, 0x100);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_CON0_0, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN33_CON0_1, 0x3, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_OP_EN, 0x2, 0x0);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_OP_CFG, 0x2, 0x0);
\te |= mtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, 0x1);
\te |= mtk_pwrap_write_regulator(PMIC_DCXO_CW00, 0x20, 0x20);
\tdelay(2000);
\tsay("on: rails %s: vcn18 qi %u, vcn33 qi %u (ana %04x), vcn28 qi %u, dcxo_cw00 %04x->%04x\\n",
\t    e ? "WRITE FAILED" : "on", !!(pmic(PMIC_VCN18_CON1) & 0x8000), !!(pmic(PMIC_VCN33_CON1) & 0x8000),
\t    pmic(PMIC_VCN33_ANA), !!(pmic(PMIC_VCN28_CON1) & 0x8000), vw.oldXo, pmic(PMIC_DCXO_CW00));
\tvw.on = true;           /* from here "wifi off" undoes everything */
\tif (e) {
\t\tgoto fail;
\t}
\trmw(GPIO_MODE24, 0x00000077, 0x00000011);
\trmw(GPIO_MODE23, 0x77777770, 0x11111110);
\tdelay(150);
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88001000);
\trmw(SPM_POWERON_CONFIG_EN, 0xffff0001, 0x0b160001);
\trmw(SPM_CONN_PWR_CON, PWR_ON, PWR_ON);
\tif (!poll(SPM_PWR_STATUS, CONN_PWR_STA, CONN_PWR_STA, 10000)) {
\t\tsay("on: PWR_STATUS did not come up\\n");
\t\tgoto fail;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_ON_2ND, PWR_ON_2ND);
\tif (!poll(SPM_PWR_STATUS_2ND, CONN_PWR_STA, CONN_PWR_STA, 10000)) {
\t\tsay("on: PWR_STATUS_2ND did not come up\\n");
\t\tgoto fail;
\t}
\trmw(SPM_CONN_PWR_CON, PWR_CLK_DIS, 0);
\tdelay(1);
\trmw(SPM_CONN_PWR_CON, PWR_ISO, 0);
\trmw(SPM_CONN_PWR_CON, PWR_RST_B, PWR_RST_B);
\tdelay(5000);
\trmw(TOPRGU_SWSYSRST, 0xff000200, 0x88000000);           /* ap_sw_rst_b let go */
\t{
\t\tstatic const struct { uint64_t clr, sta; uint32_t bit; } prot[] = {
\t\t\t{ INFRA_PROT_CLR, INFRA_PROT_STA1, 0x10000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x4000 },
\t\t\t{ INFRA_PROT1_CLR, INFRA_PROT1_STA1, 0x40000 }, { INFRA_PROT_CLR, INFRA_PROT_STA1, 0x2000 },
\t\t};
\t\tfor (unsigned i = 0; i < sizeof(prot) / sizeof(prot[0]); i++) {
\t\t\twr(prot[i].clr, prot[i].bit);
\t\t\tif (!poll(prot[i].sta, prot[i].bit, 0, 10000)) {
\t\t\t\tsay("on: bus protection 0x%x did not release\\n", prot[i].bit);
\t\t\t\tgoto fail;
\t\t\t}
\t\t}
\t}
\twr(SPM_APSRC_6F8, 0x5);
\twr(SPM_APSRC_6FC, 0x50505);
\tdelay(10);
\t{
\t\tbool idOK = false;
\t\tfor (int i = 0; i < 10 && !(idOK = rd(CONN_VERSION) == CONN_VERSION_VALUE); i++) {
\t\t\tdelay(20000);
\t\t}
\t\tif (!idOK) {
\t\t\tsay("on: conn version 0x%08x, not 0x%08x\\n", rd(CONN_VERSION), CONN_VERSION_VALUE);
\t\t\tgoto fail;
\t\t}
\t}
\t/* MT6768 post-chipid setup (mt6768.c): AFE, XO co-clock, EMI HW mode, toppose restore */
\trmw(CONN_AFE_WBG_PLL_03, 0x00180000, 0x00080000);
\trmw(CONN_XO_1200, 0xffff, 0x0708);
\trmw(CONN_XO_1204, 0xff01, 0x0600);
\trmw(CONN_SW_IRQ_REG, 1u << 21, 0);
\trmw(CONN_TOPPOSE_RESTORE, 1u << 23, 1u << 23);
\twr(CONN_BUSHANGCR, 0x90000002);
\trmw(TOPRGU_SWSYSRST, 0xff001000, 0x88000000);           /* CPU out of reset */
\t{
\t\tbool idle = false;
\t\tuint32_t t;
\t\tfor (t = 0; t < 50 && !(idle = rd(CONN_MCU_COM_REG0) == CONN_IDLE_MAGIC); t++) {
\t\t\tdelay(10000);
\t\t}
\t\tfor (uint32_t k = 0; idle && k < 100 && rd(CONN_MCU_PC) != 0xb04; k++) {
\t\t\tdelay(10000);
\t\t}
\t\tsay("on: MCU %s after ~%u ms: com_reg0 0x%08x, pc 0x%08x, dbg 0x%08x\\n", idle ? "IDLE LOOP" :
\t\t    "NOT IDLE", t * 10, rd(CONN_MCU_COM_REG0), rd(CONN_MCU_PC), rd(CONN_MCU_DBG_216C));
\t\tif (!idle) {
\t\t\tsay("on: left on for inspection (wifi status, peek); wifi off to power down.\\n");
\t\t}
\t}""",
        "mt6631_wifi.c: MT6768 wifi_on (log ring, soc1_0 patches, >>24 EMI remap, DCXO_CW00, AFE, toppose)",
    )

    # BTIF TX AP-DMA + soc1_0_patch_mcu PDA download + MT6768 WMT init script
    replace_exact(
        wc,
        """#define INFRA_CG0_CLR           0x10001084
#define INFRA_CG0_STA           0x10001090
#define INFRA_CG0_BTIF          (1u << 31)
#define STP_TYPE_WMT            4
#define STP_TYPE_BT             0

static struct {
\tbool    full;
\tuint8_t txseq, txack, rxseq;
\tuint16_t crc[256];
} st;""",
        """#define INFRA_CG0_CLR           0x10001084
#define INFRA_CG0_STA           0x10001090
#define INFRA_CG0_BTIF          (1u << 31)
#define INFRA_CG1_CLR           0x1000108c
#define INFRA_CG1_APDMA         (1u << 18)
#define BTIF_TX_DMA_BASE        0x11000b80
#define OFF_BTIF_TX             0x48000u
#define BTIF_TX_VFF_SZ          8192u
#define LSR_TEMT                (1u << 6)
#define STP_TYPE_WMT            4
#define STP_TYPE_BT             0

static struct {
\tbool     full, dmaOn;
\tuint8_t  txseq, txack, rxseq;
\tuint16_t crc[256];
\tuint8_t *vff;
\tuint32_t vffPA, wpt, wrap;
} st;""",
        "mt6631_wifi.c: BTIF TX AP-DMA defines and state",
    )

    replace_exact(
        wc,
        """static bool
btif_put(const uint8_t *b, uint32_t n)
{
\tfor (uint32_t i = 0; i < n; i++) {
\t\tif (!poll(BTIF_LSR, LSR_THRE, LSR_THRE, 20000)) {
\t\t\treturn false;
\t\t}
\t\twr(BTIF_THR, b[i]);
\t}
\treturn true;
}""",
        """static void
btif_dma_init(void)
{
\tuint64_t pa = 0;
\t(void)mt6631_wifi_dma_alloc(0x4c000, &pa);
\tuint8_t *uva = mt6631_wifi_dma_uncached();
\tst.dmaOn = false;
\tif (pa == 0 || uva == NULL) {
\t\treturn;
\t}
\tst.vff = uva + OFF_BTIF_TX;
\tst.vffPA = (uint32_t)(pa + OFF_BTIF_TX);
\twr(INFRA_CG1_CLR, INFRA_CG1_APDMA);
\tdelay(10);
\trmw(BTIF_TX_DMA_BASE + 0x0c, 1u, 1u);
\t(void)poll(BTIF_TX_DMA_BASE + 0x08, 1u, 0, 2000);
\tif (rd(BTIF_TX_DMA_BASE + 0x08) & 1u) {
\t\trmw(BTIF_TX_DMA_BASE + 0x0c, 2u, 2u);
\t\tdelay(10);
\t\trmw(BTIF_TX_DMA_BASE + 0x0c, 2u, 0);
\t}
\twr(BTIF_TX_DMA_BASE + 0x1c, st.vffPA);
\twr(BTIF_TX_DMA_BASE + 0x54, (uint32_t)((pa + OFF_BTIF_TX) >> 32));
\twr(BTIF_TX_DMA_BASE + 0x24, BTIF_TX_VFF_SZ);
\twr(BTIF_TX_DMA_BASE + 0x2c, 0);
\twr(BTIF_TX_DMA_BASE + 0x30, 0);
\tst.wpt = 0;
\tst.wrap = 0;
\twr(BTIF_TX_DMA_BASE + 0x28, BTIF_TX_VFF_SZ - 7);
\twr(BTIF_TX_DMA_BASE + 0x00, 0);
\twr(BTIF_TX_DMA_BASE + 0x04, 0);
\tst.dmaOn = true;
}

static bool
btif_put(const uint8_t *b, uint32_t n)
{
\tif (st.dmaOn) {
\t\t(void)poll(BTIF_TX_DMA_BASE + 0x14, 1u, 0, 500000);
\t\tuint32_t at = 0;
\t\twhile (at < n) {
\t\t\tuint32_t left = rd(BTIF_TX_DMA_BASE + 0x40);
\t\t\tif (left == 0) {
\t\t\t\tfor (uint32_t t = 0; t < 50000 && (left = rd(BTIF_TX_DMA_BASE + 0x40)) == 0; t++) {
\t\t\t\t\tdelay(10);
\t\t\t\t}
\t\t\t\tif (left == 0) {
\t\t\t\t\treturn false;
\t\t\t\t}
\t\t\t}
\t\t\tuint32_t chunk = MIN(n - at, left);
\t\t\tvolatile uint8_t *dst = st.vff;
\t\t\tif (st.wpt + chunk >= BTIF_TX_VFF_SZ) {
\t\t\t\tuint32_t tail = BTIF_TX_VFF_SZ - st.wpt;
\t\t\t\tfor (uint32_t i = 0; i < tail; i++) dst[st.wpt + i] = b[at + i];
\t\t\t\tfor (uint32_t i = 0; i < chunk - tail; i++) dst[i] = b[at + tail + i];
\t\t\t\tst.wpt = st.wpt + chunk - BTIF_TX_VFF_SZ;
\t\t\t\tst.wrap ^= 0x10000u;
\t\t\t} else {
\t\t\t\tfor (uint32_t i = 0; i < chunk; i++) dst[st.wpt + i] = b[at + i];
\t\t\t\tst.wpt += chunk;
\t\t\t}
\t\t\tat += chunk;
\t\t\t__builtin_arm_dsb(0xf);
\t\t\trmw(BTIF_TX_DMA_BASE + 0x08, 1u, 1u);
\t\t\twr(BTIF_TX_DMA_BASE + 0x2c, st.wpt | st.wrap);
\t\t\tuint32_t valid = rd(BTIF_TX_DMA_BASE + 0x3c);
\t\t\tif (valid > 0 && valid < 8) {
\t\t\t\trmw(BTIF_TX_DMA_BASE + 0x14, 1u, 1u);
\t\t\t}
\t\t}
\t\tfor (uint32_t t = 0; t < 50000 && rd(BTIF_TX_DMA_BASE + 0x3c) != 0; t++) {
\t\t\tuint32_t valid = rd(BTIF_TX_DMA_BASE + 0x3c);
\t\t\tif (valid > 0 && valid < 8) {
\t\t\t\trmw(BTIF_TX_DMA_BASE + 0x14, 1u, 1u);
\t\t\t}
\t\t\tdelay(10);
\t\t}
\t\treturn poll(BTIF_LSR, LSR_TEMT, LSR_TEMT, 500000);
\t}
\tfor (uint32_t i = 0; i < n; i++) {
\t\tif (!poll(BTIF_LSR, LSR_THRE, LSR_THRE, 20000)) {
\t\t\treturn false;
\t\t}
\t\twr(BTIF_THR, b[i]);
\t}
\treturn true;
}""",
        "mt6631_wifi.c: BTIF TX AP-DMA channel 0x11000b80 implementation",
    )

    replace_exact(
        wc,
        """\twr(BTIF_TRI_LVL, 8 | (1u << 4));                /* TX 8, RX 1, loopback off */
\twr(BTIF_DMA_EN, 1u << 2);                       /* PIO both ways, auto reset */
\twr(BTIF_IER, 0);                                /* polled */
\tbtif_wake();""",
        """\twr(BTIF_TRI_LVL, 8 | (1u << 4));                /* TX 8, RX 1, loopback off */
\tbtif_dma_init();
\twr(BTIF_DMA_EN, st.dmaOn ? ((1u << 2) | (1u << 1)) : (1u << 2));
\twr(BTIF_IER, 0);                                /* polled */
\tbtif_wake();
\tdelay(20000);""",
        "mt6631_wifi.c: enable BTIF TX AP-DMA and 20 ms post-wake delay in btif_init",
    )

    replace_exact(
        wc,
        """\t\tdo {                                    /* sync: bit 7 */
\t\t\tb0 = btif_get(ms * 1000);
\t\t} while (b0 >= 0 && !(b0 & 0x80));""",
        """\t\tdo {                                    /* sync: bit 7 set, bit 6 clear */
\t\t\tb0 = btif_get(ms * 1000);
\t\t} while (b0 >= 0 && (b0 == 0x55 || !(b0 & 0x80) || (b0 & 0x40)));""",
        "mt6631_wifi.c: skip 0x55/bit6 noise in stp_recv",
    )

    replace_exact(
        wc,
        """\tfor (int k = 0; k < 4; k++) {
\t\tn = stp_recv(ev, evmax, ms, &type);""",
        """\tfor (int k = 0; k < 12; k++) {
\t\tn = stp_recv(ev, evmax, ms, &type);""",
        "mt6631_wifi.c: tolerate extra WMT power-saving events in wmt_cmd",
    )

    replace_exact(
        wc,
        """/* One packet on STP channel `type` in the current mode (stp_core.c mtk_wcn_stp_send_data, mand / full). */
static bool
stp_send(uint8_t type, const uint8_t *p, uint32_t n)
{
\tuint8_t h[4], c[2] = { 0, 0 };
\th[0] = (uint8_t)(0x80 | (st.txseq << 3) | (st.full ? st.txack : 0));
\th[1] = (uint8_t)((type << 4) | ((n >> 8) & 0xf));
\th[2] = (uint8_t)n;
\th[3] = st.full ? (uint8_t)(h[0] + h[1] + h[2]) : 0;
\tif (st.full) {
\t\tuint16_t crc = crc16(p, n);
\t\tc[0] = (uint8_t)crc;
\t\tc[1] = (uint8_t)(crc >> 8);
\t\tst.txseq = (st.txseq + 1) & 7;
\t}
\treturn btif_put(h, 4) && btif_put(p, n) && btif_put(c, 2);
}""",
        """/* One packet on STP channel `type` sent as ONE contiguous AP-DMA burst (no inter-byte gaps). */
static bool
stp_send(uint8_t type, const uint8_t *p, uint32_t n)
{
\tstatic uint8_t frame[4 + 4096 + 2];
\tif (n > 4096) {
\t\treturn false;
\t}
\tuint8_t *h = frame, *c = frame + 4 + n;
\th[0] = (uint8_t)(0x80 | (st.full ? ((st.txseq << 3) | st.txack) : 0));
\th[1] = (uint8_t)((type << 4) | ((n >> 8) & 0xf));
\th[2] = (uint8_t)n;
\th[3] = st.full ? (uint8_t)(h[0] + h[1] + h[2]) : 0;
\tif (n) {
\t\tmemcpy(frame + 4, p, n);
\t}
\tif (st.full) {
\t\tuint16_t crc = crc16(p, n);
\t\tc[0] = (uint8_t)crc;
\t\tc[1] = (uint8_t)(crc >> 8);
\t\tst.txseq = (st.txseq + 1) & 7;
\t} else {
\t\tc[0] = 0;
\t\tc[1] = 0;
\t}
\treturn btif_put(frame, 6 + n);
}""",
        "mt6631_wifi.c: send full STP frame in one contiguous AP-DMA transfer",
    )

    replace_exact(
        wc,
        """\t\tif (st.full) {
\t\t\tst.txack = (uint8_t)((b0 >> 3) & 7);
\t\t\tst.rxseq = (st.txack + 1) & 7;
\t\t\tstp_ack();
\t\t}""",
        """\t\tif (st.full) {
\t\t\tuint8_t seq = (uint8_t)((b0 >> 3) & 7);
\t\t\tbool dup = (seq != st.rxseq);
\t\t\tif (!dup) {
\t\t\t\tst.txack = seq;
\t\t\t\tst.rxseq = (uint8_t)((seq + 1) & 7);
\t\t\t}
\t\t\tstp_ack();
\t\t\tif (dup) {
\t\t\t\tcontinue;
\t\t\t}
\t\t}""",
        "mt6631_wifi.c: handle duplicate STP frames cleanly in full mode",
    )

    replace_exact(
        wc,
        """\tint rn = wmt_cmd("read 0x80000008", regrd, sizeof(regrd), regrdEv, sizeof(regrdEv), 1000, ev, sizeof(ev), out, len, &o);
\tif (rn < 0) {
\t\treturn o;
\t}""",
        """\tint rn = -1;
\tfor (int tries = 0; tries < 3 && rn < 0; tries++) {
\t\tif (tries > 0) {
\t\t\tbtif_wake();
\t\t\tdelay(20000);
\t\t}
\t\trn = wmt_cmd("read 0x80000008", regrd, sizeof(regrd), regrdEv, sizeof(regrdEv), 1000, ev, sizeof(ev), out, len, &o);
\t}
\tif (rn < 0) {
\t\treturn o;
\t}
\tdelay(10000);""",
        "mt6631_wifi.c: retry initial WMT read 0x80000008 up to 3 times + 10 ms settle",
    )

    replace_exact(
        wc,
        """\tif (rn >= 16) {
\t\tsay("wmt: GEN_HCR 0x%02x%02x%02x%02x (the MCU answers over STP/BTIF)\\n", ev[15], ev[14], ev[13], ev[12]);
\t}
\tstatic const uint8_t qstp[] = { 0x01, 0x04, 0x01, 0x00, 0x04 };
\tstatic const uint8_t qstpDef[] = { 0x02, 0x04, 0x06, 0x00, 0x00, 0x04, 0x11, 0x00, 0x00, 0x00 };
\tstatic const uint8_t qstpFull[] = { 0x02, 0x04, 0x06, 0x00, 0x00, 0x04, 0xdb, 0x0e, 0x68, 0x01 };
\tstatic const uint8_t sstp[] = { 0x01, 0x04, 0x05, 0x00, 0x03, 0xdb, 0x0e, 0x68, 0x01 };
\tstatic const uint8_t sstpEv[] = { 0x02, 0x04, 0x02, 0x00, 0x00, 0x03 };
\t(void)wmt_cmd("query stp", qstp, sizeof(qstp), qstpDef, sizeof(qstpDef), 1000, ev, sizeof(ev), out, len, &o);
\tif (wmt_cmd("set stp full", sstp, sizeof(sstp), sstpEv, sizeof(sstpEv), 1000, ev, sizeof(ev), out, len, &o) < 0) {
\t\treturn o;
\t}
\tst.full = true;                                 /* host side switch, 10 ms */
\tst.txseq = 0;
\tst.txack = 7;
\tdelay(10000);
\tif (wmt_cmd("query stp (full)", qstp, sizeof(qstp), qstpFull, sizeof(qstpFull), 1000, ev, sizeof(ev), out, len,
\t    &o) < 0) {
\t\treturn o;
\t}
\tstatic const uint8_t chipid[] = { 0x01, 0x02, 0x05, 0x00, 0x11, 0x81, 0x67, 0x00, 0x00 };
\tstatic const uint8_t chipidEv[] = { 0x02, 0x02, 0x01, 0x00, 0x00 };
\tif (wmt_cmd("set chip id 0x6781", chipid, sizeof(chipid), chipidEv, sizeof(chipidEv), 1000, ev, sizeof(ev), out,
\t    len, &o) < 0) {
\t\treturn o;
\t}
\tstatic const uint8_t adie[] = { 0x01, 0x02, 0x01, 0x00, 0x12 };
\tstatic const uint8_t adieEv[] = { 0x02, 0x02, 0x05, 0x00, 0x00 };
\t(void)wmt_cmd("query a-die", adie, sizeof(adie), adieEv, sizeof(adieEv), 1000, ev, sizeof(ev), out, len, &o);
\tstatic const uint8_t cal[] = { 0x01, 0x14, 0x01, 0x00, 0x01 };
\t/* MT6781: 02 14 <len 0x22a> 00 01 + calibration data (0130 on the phone) */
\tstatic const uint8_t calEv[] = { 0x02, 0x14 };
\tint c = wmt_cmd("rf calibration", cal, sizeof(cal), calEv, sizeof(calEv), 5000, ev, sizeof(ev), out, len, &o);
\t/* wmt_ic_soc.c: the efuse PALDO (VCN28) goes off after the calibration */
\tmtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, 0x0);
\tvw.wmt = c >= 0;
\tsay("wmt: %s; VCN28 off. Next: wifi fw\\n", c >= 0 ? "done" : "calibration FAILED");""",
        """\tif (rn >= 16) {
\t\tsay("wmt: GEN_HCR 0x%02x%02x%02x%02x (BTIF %s)\\n", ev[15], ev[14], ev[13], ev[12],
\t\t    st.dmaOn ? "AP-DMA TX" : "PIO");
\t}
\t/* MT6768 PDA MCU patch over BTIF TX AP-DMA in mandatory mode, BEFORE STP full mode */
\t{
\t\tstatic uint8_t pbuf[65536];
\t\tstruct fw_file pf;
\t\tint perr = fw_open("soc1_0_patch_mcu_1a_1_hdr.bin", &pf);
\t\tif (perr != 0 || pf.size <= 30 || pf.size > sizeof(pbuf)) {
\t\t\tsay("wmt: soc1_0_patch_mcu_1a_1_hdr.bin error %d (size %u)\\n", perr, perr == 0 ? pf.size : 0);
\t\t\tif (perr == 0) fw_close(&pf);
\t\t\treturn o;
\t\t}
\t\tperr = fw_read(&pf, pbuf, pf.size, 0);
\t\tfw_close(&pf);
\t\tif (perr != 0) {
\t\t\tsay("wmt: soc1_0_patch_mcu_1a_1_hdr.bin read error %d\\n", perr);
\t\t\treturn o;
\t\t}
\t\tuint32_t body = pf.size - 30, target = 0;
\t\tmemcpy(&target, pbuf + 24, 4);
\t\ttarget &= ~0xffu; /* 0x0001c011 -> 0x0001c000 */
\t\t/* Pull BTIF_WAK low before PDA patch so RAM handover does not take a spurious wake IRQ */
\t\trmw(BTIF_WAK, 1, 0);
\t\tdelay(5000);
\t\tuint8_t pda[21] = {
\t\t\t0x01, 0x01, 0x11, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x2f, 0x02, 0x02, 0x00,
\t\t};
\t\tstatic const uint8_t pdaEv[] = { 0x02, 0x01, 0x01, 0x00, 0x00 };
\t\tmemcpy(pda + 5, &target, 4);
\t\tmemcpy(pda + 9, &body, 4);
\t\tif (wmt_cmd("pda cfg", pda, sizeof(pda), pdaEv, sizeof(pdaEv), 1000, ev, sizeof(ev), out, len, &o) < 0) {
\t\t\treturn o;
\t\t}
\t\tuint8_t head[12] = { 0 };
\t\tuint32_t hlen = body + 12;
\t\tmemcpy(head, &hlen, 4);
\t\tbool txok = btif_put(head, sizeof(head));
\t\tfor (uint32_t at = 0; txok && at < body; at += 1000) {
\t\t\ttxok = btif_put(pbuf + 30 + at, MIN(1000u, body - at));
\t\t}
\t\tif (!txok) {
\t\t\tsay("wmt: PDA patch stream failed (LSR 0x%02x)\\n", rd(BTIF_LSR) & 0xff);
\t\t\treturn o;
\t\t}
\t\t/* Write MCU patch buildtime to EMI +0x68330 (15 chars + NUL) before WMT reset */
\t\tuint8_t dt[16];
\t\tmemcpy(dt, pbuf, 15);
\t\tdt[15] = 0;
\t\tmemcpy((char *)vw.emi + 0x68330, dt, 16);
\t\tmt6631_wifi_emi_flush(0x68300, 64);
\t\tdelay(50000);
\t\tsay("wmt: PDA patch %u bytes -> 0x%08x sent (pc 0x%08x)\\n", body, target, rd(CONN_MCU_PC));
\t\tstatic const uint8_t rst[] = { 0x01, 0x07, 0x01, 0x00, 0x04 };
\t\tstatic const uint8_t rstEv[] = { 0x02, 0x07, 0x01, 0x00, 0x00 };
\t\tif (wmt_cmd("wmt reset", rst, sizeof(rst), rstEv, sizeof(rstEv), 2000, ev, sizeof(ev), out, len, &o) < 0) {
\t\t\treturn o;
\t\t}
\t}
\tstatic const uint8_t qstp[] = { 0x01, 0x04, 0x01, 0x00, 0x04 };
\tstatic const uint8_t qstpDef[] = { 0x02, 0x04, 0x06, 0x00, 0x00, 0x04, 0x11, 0x00, 0x00, 0x00 };
\tstatic const uint8_t qstpFull[] = { 0x02, 0x04, 0x06, 0x00, 0x00, 0x04, 0xdb, 0x0e, 0x68, 0x01 };
\tstatic const uint8_t sstp[] = { 0x01, 0x04, 0x05, 0x00, 0x03, 0xdb, 0x0e, 0x68, 0x01 };
\tstatic const uint8_t sstpEv[] = { 0x02, 0x04, 0x02, 0x00, 0x00, 0x03 };
\t(void)wmt_cmd("query stp", qstp, sizeof(qstp), qstpDef, sizeof(qstpDef), 1000, ev, sizeof(ev), out, len, &o);
\tif (wmt_cmd("set stp full", sstp, sizeof(sstp), sstpEv, sizeof(sstpEv), 1000, ev, sizeof(ev), out, len, &o) < 0) {
\t\treturn o;
\t}
\tst.full = true;                                 /* host side switch, 10 ms */
\tst.txseq = 0;
\tst.txack = 7;
\tdelay(10000);
\tif (wmt_cmd("query stp (full)", qstp, sizeof(qstp), qstpFull, sizeof(qstpFull), 1000, ev, sizeof(ev), out, len,
\t    &o) < 0) {
\t\treturn o;
\t}
\tstatic const uint8_t chipid[] = { 0x01, 0x02, 0x05, 0x00, 0x11, 0x68, 0x67, 0x00, 0x00 };
\tstatic const uint8_t chipidEv[] = { 0x02, 0x02, 0x01, 0x00, 0x00 };
\tif (wmt_cmd("set chip id 0x6768", chipid, sizeof(chipid), chipidEv, sizeof(chipidEv), 1000, ev, sizeof(ev), out,
\t    len, &o) < 0) {
\t\treturn o;
\t}
\tstatic const uint8_t adie[] = { 0x01, 0x02, 0x01, 0x00, 0x12 };
\tstatic const uint8_t adieEv[] = { 0x02, 0x02, 0x05, 0x00, 0x00 };
\t(void)wmt_cmd("query a-die", adie, sizeof(adie), adieEv, sizeof(adieEv), 1000, ev, sizeof(ev), out, len, &o);
\tdelay(5000);
\tstatic const uint8_t lte[] = { 0x01, 0x10, 0x02, 0x00, 0x15, 0x01 };
\tstatic const uint8_t coexEv[] = { 0x02, 0x10, 0x01, 0x00, 0x00 };
\t(void)wmt_cmd("lte coex", lte, sizeof(lte), coexEv, sizeof(coexEv), 1000, ev, sizeof(ev), out, len, &o);
\tstatic const uint8_t tssi[] = { 0x01, 0x02, 0x04, 0x00, 0x10, 0x03, 0xc0, 0x0e };
\t(void)wmt_cmd("bt tssi", tssi, sizeof(tssi), chipidEv, sizeof(chipidEv), 1000, ev, sizeof(ev), out, len, &o);
\tstatic const uint8_t cal[] = { 0x01, 0x14, 0x01, 0x00, 0x01 };
\tstatic const uint8_t calEv[] = { 0x02, 0x14 };
\tint c = wmt_cmd("rf calibration", cal, sizeof(cal), calEv, sizeof(calEv), 5000, ev, sizeof(ev), out, len, &o);
\tstatic const uint8_t adj1[] = { 0x01, 0x10, 0x04, 0x00, 0x1e, 0x00, 0x00, 0x00 };
\tstatic const uint8_t adj2[] = { 0x01, 0x10, 0x04, 0x00, 0x1f, 0x00, 0x00, 0x00 };
\tstatic const uint8_t adj3[] = { 0x01, 0x10, 0x04, 0x00, 0x20, 0x00, 0x00, 0x00 };
\tstatic const uint8_t fm[] = { 0x01, 0x05, 0x02, 0x00, 0x02, 0x02 };
\tstatic const uint8_t fmEv[] = { 0x02, 0x05, 0x02, 0x00, 0x00, 0x02 };
\t(void)wmt_cmd("coex bt", adj1, sizeof(adj1), coexEv, sizeof(coexEv), 1000, ev, sizeof(ev), out, len, &o);
\t(void)wmt_cmd("coex opp", adj2, sizeof(adj2), coexEv, sizeof(coexEv), 1000, ev, sizeof(ev), out, len, &o);
\t(void)wmt_cmd("coex ble", adj3, sizeof(adj3), coexEv, sizeof(coexEv), 1000, ev, sizeof(ev), out, len, &o);
\t(void)wmt_cmd("fm strap", fm, sizeof(fm), fmEv, sizeof(fmEv), 1000, ev, sizeof(ev), out, len, &o);
\t/* wmt_ic_soc.c: the efuse PALDO (VCN28) goes off after the calibration */
\tmtk_pwrap_write_regulator(PMIC_VCN28_CON0, 0x1, 0x0);
\tvw.wmt = c >= 0;
\tsay("wmt: %s; VCN28 off. Next: wifi fw\\n", c >= 0 ? "done" : "calibration FAILED");""",
        "mt6631_wifi.c: MT6768 PDA patch download and WMT init sequence",
    )

    # WFDMA DMA_SIZE, preserve BTIF TX vFIFO, WIFI_RAM_CODE filename, and CONNAC1 16-byte RXD
    replace_exact(
        wc,
        """#define DMA_SIZE                0x48000
#define OFF_TX(n)               ((n) == 3 ? 0x0000u : (n) == 15 ? 0x0100u : (n) == 0 ? 0x41000u : 0x0300u)
#define TX0_N                   32              /* data ring: 32 entries at 0x41000, 2 KiB buffers in ring 3's area */
#define TX0_BUF                 0x800
#define RING_LEN(n)             ((n) == 0 ? TX0_N : RING_N)
#define OFF_RX(n)               ((n) == 0 ? 0x0400u : 0x0500u)
#define OFF_TXBUF               0x01000u        /* ring 3 then ring 15 buffers: 16 x 4 KiB each */
#define OFF_RXBUF(n)            ((n) == 1 ? 0x21000u : 0x31000u)
#define WIFI_RAM_CODE           "WIFI_RAM_CODE_soc2_2_2_1.bin"
#define PDA_CHUNK               2048""",
        """#define DMA_SIZE                0x4c000
#define WFDMA_SIZE              0x48000         /* 0x48000..0x4c000 belongs to BTIF TX AP-DMA vFIFO */
#define OFF_TX(n)               ((n) == 3 ? 0x0000u : (n) == 15 ? 0x0100u : (n) == 0 ? 0x41000u : 0x0300u)
#define TX0_N                   32              /* data ring: 32 entries at 0x41000, 2 KiB buffers in ring 3's area */
#define TX0_BUF                 0x800
#define RING_LEN(n)             ((n) == 0 ? TX0_N : RING_N)
#define OFF_RX(n)               ((n) == 0 ? 0x0400u : 0x0500u)
#define OFF_TXBUF               0x01000u        /* ring 3 then ring 15 buffers: 16 x 4 KiB each */
#define OFF_RXBUF(n)            ((n) == 1 ? 0x21000u : 0x31000u)
#define WIFI_RAM_CODE           "WIFI_RAM_CODE_soc1_0_1a_1.bin"
#define PDA_CHUNK               2048
#define RXD_SIZE                16u             /* CONNAC1 (nic_rxd_v1): 16-byte RX descriptor */""",
        "mt6631_wifi.c: WIFI_RAM_CODE_soc1_0_1a_1.bin, WFDMA_SIZE, CONNAC1 RXD_SIZE=16",
    )

    replace_exact(
        wc,
        """\tbzero(wd.va, DMA_SIZE);
\tmt6631_wifi_dma_flush(0, DMA_SIZE);               /* before any descriptor goes in through wd.uva */
\tstatic const uint32_t tx[] = { 0, 1, 3, 15 };
\tfor (unsigned r = 0; r < 2; r++) {
\t\tfor (uint32_t i = 0; i < RING_N; i++) {
\t\t\tuint32_t *d = desc(OFF_RX(r), i);
\t\t\td[0] = (uint32_t)(wd.pa + OFF_RXBUF(r) + BUF_SZ * i);
\t\t\td[1] = (uint32_t)RX_BUF_LEN << 16;
\t\t}
\t}
\tmt6631_wifi_dma_flush(0, DMA_SIZE);""",
        """\tbzero(wd.va, WFDMA_SIZE);
\tmt6631_wifi_dma_flush(0, WFDMA_SIZE);             /* keep BTIF TX vFIFO at 0x48000 untouched */
\tstatic const uint32_t tx[] = { 0, 1, 3, 15 };
\tfor (unsigned r = 0; r < 2; r++) {
\t\tfor (uint32_t i = 0; i < RING_N; i++) {
\t\t\tuint32_t *d = desc(OFF_RX(r), i);
\t\t\td[0] = (uint32_t)(wd.pa + OFF_RXBUF(r) + BUF_SZ * i);
\t\t\td[1] = (uint32_t)RX_BUF_LEN << 16;
\t\t}
\t}
\tmt6631_wifi_dma_flush(0, WFDMA_SIZE);""",
        "mt6631_wifi.c: preserve BTIF TX AP-DMA vFIFO in wfdma_setup",
    )

    # CONNAC1 16-byte RXD offsets across init_cmd, rx_classify, rx_rssi, wifi_cmd, wifi_cap, wifi_event, hw_ch_privilege
    replace_exact(
        wc,
        """\t\tuint8_t *e = ev + 20;
\t\tif (got >= 32 && e[4] == 1 && e[5] == seq) {""",
        """\t\tuint8_t *e = ev + RXD_SIZE;
\t\tif (got >= RXD_SIZE + 12 && e[4] == 1 && e[5] == seq) {""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in init_cmd",
    )

    replace_exact(
        wc,
        """\tif (n < 20) {
\t\treturn 0;
\t}
\tuint16_t ptype = (uint16_t)(b[2] | b[3] << 8);
\tif ((ptype & 0xe00f) == 0xe000) {
\t\treturn n >= 32 ? PKT_EVENT : 0;
\t}
\tuint32_t type = ptype >> 13;
\tif ((ptype & 0xe00f) != 0xe001 && type != 2) {
\t\treturn 0;                               /* TX status, RX vector, MSDU report: not used */
\t}
\tuint32_t off = 20 + ((ptype & (1u << 12)) ? 16 : 0) + ((ptype & (1u << 9)) ? 16 : 0) +""",
        """\tif (n < RXD_SIZE) {
\t\treturn 0;
\t}
\tuint16_t ptype = (uint16_t)(b[2] | b[3] << 8);
\tif ((ptype & 0xe00f) == 0xe000) {
\t\treturn n >= RXD_SIZE + 12 ? PKT_EVENT : 0;
\t}
\tuint32_t type = ptype >> 13;
\tif ((ptype & 0xe00f) != 0xe001 && type != 2) {
\t\treturn 0;                               /* TX status, RX vector, MSDU report: not used */
\t}
\tuint32_t off = RXD_SIZE + ((ptype & (1u << 12)) ? 16 : 0) + ((ptype & (1u << 9)) ? 16 : 0) +""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in rx_classify",
    )

    replace_exact(
        wc,
        """\tuint32_t g3 = 20 + ((ptype & (1u << 12)) ? 16 : 0) + ((ptype & (1u << 9)) ? 16 : 0) + ((ptype & (1u << 10)) ? 8 : 0);""",
        """\tuint32_t g3 = RXD_SIZE + ((ptype & (1u << 12)) ? 16 : 0) + ((ptype & (1u << 9)) ? 16 : 0) + ((ptype & (1u << 10)) ? 8 : 0);""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in rx_rssi",
    )

    replace_exact(
        wc,
        """\t\tuint8_t *e = ev + 20;
\t\tif (eid != 0 && e[4] == eid) {""",
        """\t\tuint8_t *e = ev + RXD_SIZE;
\t\tif (eid != 0 && e[4] == eid) {""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in wifi_cmd",
    )

    replace_exact(
        wc,
        """\tif (n < 36) {
\t\tsay("cap: no NIC_CAPABILITY_V2 event\\n");
\t\treturn o;
\t}
\tuint8_t *b = ev + 20 + 12;
\tuint32_t count = b[0] | b[1] << 8, off = 4, end = (uint32_t)MIN(n, (int)sizeof(ev)) - 32;""",
        """\tif (n < (int)(RXD_SIZE + 16)) {
\t\tsay("cap: no NIC_CAPABILITY_V2 event\\n");
\t\treturn o;
\t}
\tuint8_t *b = ev + RXD_SIZE + 12;
\tuint32_t count = b[0] | b[1] << 8, off = 4, end = (uint32_t)MIN(n, (int)sizeof(ev)) - (RXD_SIZE + 12);""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in wifi_cap",
    )

    replace_exact(
        wc,
        """\tuint8_t act[12] = { 0, 1, 0, 1 };               /* BSS 0 active, AIS, own MAC index 1 */
\tmemcpy(act + 4, wl.mac, 6);
\tact[10] = 4;                                    /* broadcast/multicast WTBL entry */""",
        """\tuint8_t act[12] = { 0, 1, 0, 0 };               /* CONNAC1: BSS 0 active, AIS, own MAC index 0 */
\tmemcpy(act + 4, wl.mac, 6);
\tact[10] = 2;                                    /* CONNAC1: broadcast/multicast WTBL entry 2 */""",
        "mt6631_wifi.c: CONNAC1 OMAC=0 and WTBL_BMC=2 in BSS_ACTIVATE_CTRL",
    )

    replace_exact(
        wc,
        """\tconst uint8_t *e = ev + 20, *d = ev + 32;
\twl.events++;
\tswitch (e[4]) {
\tcase EID_SCAN_DONE:
\t\tif (wl.scanBusy && got >= 38 && d[0] == wl.scanSeq) {""",
        """\tconst uint8_t *e = ev + RXD_SIZE, *d = ev + RXD_SIZE + 12;
\twl.events++;
\tswitch (e[4]) {
\tcase EID_SCAN_DONE:
\t\tif (wl.scanBusy && got >= RXD_SIZE + 18 && d[0] == wl.scanSeq) {""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in wifi_event",
    )

    # CONNAC1 WTBL, TXD, unencrypted EAPOL over TX ring 15, CH_PRIVILEGE event offset, STAREC v1, RSC in hw_key
    replace_exact(
        wc,
        """#define WTBL_AP                 5
#define WTBL_BMC                4""",
        """#define WTBL_AP                 1
#define WTBL_BMC                2""",
        "mt6631_wifi.c: CONNAC1 WTBL_AP=1, WTBL_BMC=2",
    )

    replace_exact(
        wc,
        """\tt[3] = 0x04;                                    /* LMAC, queue 1 = AC_BE (WMM set 0) */
\tt[4] = WTBL_AP;
\tt[5] = type >= 0x600 ? 0x90 : 0x80;             /* long, non-802.11, Ethernet II */
\tt[7] = 0x04;                                    /* PktFt 0, own MAC 1 */""",
        """\tt[3] = 0x00;                                    /* CONNAC1: LMAC AC0 */
\tt[4] = WTBL_AP;
\tt[5] = type >= 0x600 ? 0x90 : 0x80;             /* long, non-802.11, Ethernet II */
\tt[7] = 0x00;                                    /* CONNAC1: PktFt 0, own MAC 0 */""",
        "mt6631_wifi.c: CONNAC1 OMAC=0 and AC0 in tx_data",
    )

    replace_exact(
        wc,
        """\tpkt[7] = 0x06;                                  /* PktFt 2, own MAC 1 */""",
        """\tpkt[7] = 0x02;                                  /* CONNAC1: PktFt 2, own MAC 0 */""",
        "mt6631_wifi.c: CONNAC1 OMAC=0 in tx_mgmt",
    )

    replace_exact(
        wc,
        """static int
hw_tx(const uint8_t *frame, uint32_t len, bool mgmt, bool protect)
{
\tif (mgmt) {
\t\treturn tx_mgmt(frame, len);
\t}
\tbool eapol = len >= 14 && frame[12] == 0x88 && frame[13] == 0x8e;
\tif (eapol && len >= 14 + 7) {
\t\t/* EAPOL-Key with the pairwise bit (4-way M2/M4) never protected; group-key M2 is once the PTK is in */
\t\tuint16_t info = (uint16_t)(frame[14 + 5] << 8 | frame[14 + 6]);
\t\tprotect = tx0.ptk && !(frame[14 + 1] == 3 && (info & 0x0008));
\t} else {
\t\tprotect = protect && tx0.ptk;
\t}
\treturn tx_data(frame, len, protect, eapol);
}""",
        """/* Unencrypted EAPOL (4-way M2/M4) on TX ring 15 as security frame to MCU Q1 at lowest basic rate (CONNAC1) */
static int
tx_eapol_cmd(const uint8_t *eth, uint32_t len)
{
\tstatic uint8_t pkt[32 + 512];
\tif (len < 14 || len > 512) {
\t\treturn -1;
\t}
\tbzero(pkt, 32);
\tput16(pkt + 0, 32 + len);
\tpkt[2] = 0x08;                                  /* (12 + 4) / 2 */
\tpkt[3] = 0x84;                                  /* MCU port, MCU_Q1 */
\tpkt[4] = WTBL_AP;
\tpkt[5] = 0x90;                                  /* long, non-802.11, Ethernet II */
\tpkt[7] = 0x02;                                  /* PktFt 2 (CMD), own MAC 0 */
\tpkt[11] = 0x80;                                 /* fixed rate */
\tput16(pkt + 12, 15u << 11);
\tput16(pkt + 26, rate_code());
\tmemcpy(pkt + 32, eth, len);
\treturn wfdma_tx(15, pkt, 32 + len) ? 0 : -1;
}

static int
hw_tx(const uint8_t *frame, uint32_t len, bool mgmt, bool protect)
{
\tif (mgmt) {
\t\treturn tx_mgmt(frame, len);
\t}
\tbool eapol = len >= 14 && frame[12] == 0x88 && frame[13] == 0x8e;
\tif (eapol && len >= 14 + 7) {
\t\t/* EAPOL-Key with the pairwise bit (4-way M2/M4) never protected; group-key M2 is once the PTK is in */
\t\tuint16_t info = (uint16_t)(frame[14 + 5] << 8 | frame[14 + 6]);
\t\tprotect = tx0.ptk && !(frame[14 + 1] == 3 && (info & 0x0008));
\t\tif (!protect) {
\t\t\treturn tx_eapol_cmd(frame, len);
\t\t}
\t} else {
\t\tprotect = protect && tx0.ptk;
\t}
\treturn tx_data(frame, len, protect, eapol);
}""",
        "mt6631_wifi.c: CONNAC1 unencrypted EAPOL over TX ring 15",
    )

    replace_exact(
        wc,
        """\tif (n >= 32 + 3 && ev[33] == tx0.chToken && ev[34] == 0) {
\t\treturn 0;
\t}
\tprintf("WIFI: channel %u: no grant (%d bytes, token %u/%u, status %u)\\n", sj.ap.ch, n, n > 33 ? ev[33] : 0,
\t    tx0.chToken, n > 34 ? ev[34] : 0);""",
        """\tif (n >= (int)(RXD_SIZE + 12 + 3) && ev[RXD_SIZE + 13] == tx0.chToken && ev[RXD_SIZE + 14] == 0) {
\t\treturn 0;
\t}
\tprintf("WIFI: channel %u: no grant (%d bytes, token %u/%u, status %u)\\n", sj.ap.ch, n,
\t    n > (int)(RXD_SIZE + 13) ? ev[RXD_SIZE + 13] : 0, tx0.chToken,
\t    n > (int)(RXD_SIZE + 14) ? ev[RXD_SIZE + 14] : 0);""",
        "mt6631_wifi.c: CONNAC1 16-byte RXD in hw_ch_privilege",
    )

    replace_exact(
        wc,
        """\tp[54] = WTBL_AP;
\tp[55] = 0xff;""",
        """\tp[23] = 1;                                      /* CONNAC1: CMD_UPDATE_STAREC_VER1 */
\tp[50] = (uint8_t)((sj.ap.rssi + 110) * 2);      /* RCPI */
\tp[54] = WTBL_AP;
\tp[55] = WTBL_BMC;""",
        "mt6631_wifi.c: CONNAC1 STAREC_VER1, RCPI, and WTBL_BMC in hw_sta_rec",
    )

    replace_exact(
        wc,
        """\t(void)rsc;                                      /* gen4m never sends it for CCMP */
\tif (klen != 16 && klen != 32) {
\t\treturn -1;
\t}
\tbzero(p, sizeof(p));
\tp[0] = 1;
\tp[1] = pairwise ? 1 : 0;
\tp[2] = pairwise ? 1 : 0;
\tmemcpy(p + 4, sj.ap.bssid, 6);
\tp[11] = klen == 32 ? 2 : 4;                     /* TKIP (group only) / CCMP */
\tp[12] = keyid;
\tp[13] = (uint8_t)klen;
\tp[14] = pairwise ? WTBL_AP : WTBL_BMC;
\tmemcpy(p + 16, key, klen);""",
        """\tif (klen != 16 && klen != 32) {
\t\treturn -1;
\t}
\tbzero(p, sizeof(p));
\tp[0] = 1;
\tp[1] = pairwise ? 1 : 0;
\tp[2] = pairwise ? 1 : 0;
\tmemcpy(p + 4, sj.ap.bssid, 6);
\tp[11] = klen == 32 ? 2 : 4;                     /* TKIP (group only) / CCMP */
\tp[12] = keyid;
\tp[13] = (uint8_t)klen;
\tp[14] = pairwise ? WTBL_AP : WTBL_BMC;
\tmemcpy(p + 16, key, klen);
\tif (rsc != NULL) {
\t\tmemcpy(p + 48, rsc, 8);
\t}""",
        "mt6631_wifi.c: pass RSC to ADD_REMOVE_KEY for group keys",
    )


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: apply_a32_overlay.py <path/to/xnu>")
    xnu = Path(sys.argv[1]).resolve()
    print(f"Applying SM-A325M (MT6769T) XNU overlay (Hito 3+4+5+6+7) to {xnu}:")
    copy_drivers(xnu)
    patch_build_lists(xnu)
    patch_mtk_ufs(xnu)
    patch_pe_serial(xnu)
    patch_platform_expert(xnu)
    patch_pmic(xnu)
    patch_framebuffer(xnu)
    patch_usb_link(xnu)
    patch_gpu_mt6768(xnu)
    patch_wifi_mt6768(xnu)
    print("Overlay applied cleanly!")


if __name__ == "__main__":
    main()
