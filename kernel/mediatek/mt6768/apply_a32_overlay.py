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
extern "C" void a32_dsi_cmd_flush_now(void);             /* bsd/dev/mtk_ufs.c (SM-A325M DSI CMD) */
extern "C" int a32_dsi_set_brightness(uint32_t level);   /* bsd/dev/mtk_ufs.c (SM-A325M S6E3FC3 DCS) */
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
    new_pres = """\tbool ok = present(recs + (size_t)pick * MAPPLE_FB_SWAP_REC_SIZE, force, jobs);
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
\t\t\ta32_touch_display_power(off ? 0 : 1);"""
    replace_exact(p, old_pwr, new_pwr, "MappleFramebuffer.cpp: notify STMTS on display power change")


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
        '\t\t}',
        "usb_link.c: add chown and fpatch commands for live rootfs repair",
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


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: apply_a32_overlay.py <path/to/xnu>")
    xnu = Path(sys.argv[1]).resolve()
    print(f"Applying SM-A325M (MT6769T) XNU overlay (Hito 3+4+5) to {xnu}:")
    copy_drivers(xnu)
    patch_build_lists(xnu)
    patch_mtk_ufs(xnu)
    patch_pe_serial(xnu)
    patch_platform_expert(xnu)
    patch_pmic(xnu)
    patch_framebuffer(xnu)
    patch_usb_link(xnu)
    patch_gpu_mt6768(xnu)
    print("Overlay applied cleanly!")


if __name__ == "__main__":
    main()
