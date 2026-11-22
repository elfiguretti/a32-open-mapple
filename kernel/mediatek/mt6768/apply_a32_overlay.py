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
    if new in txt:
        print(f"  [already applied] {label}")
        return
    if old not in txt:
        raise SystemExit(f"ERROR: target snippet not found for {label} in {path}")
    cnt = txt.count(old)
    if cnt != 1:
        raise SystemExit(f"ERROR: expected 1 match for {label} in {path}, found {cnt}")
    path.write_text(txt.replace(old, new, 1), encoding="utf-8", newline="\n")
    print(f"  [ok] {label}")


def copy_drivers(xnu: Path):
    copies = [
        (HERE / "bsd" / "dev" / "mtk_emmc.c", xnu / "bsd" / "dev" / "mtk_emmc.c"),
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
        "bsd/dev/mtk_ufs.c\t\t\tstandard\nbsd/dev/mtk_emmc.c\t\t\tstandard",
        "bsd/conf/files: add bsd/dev/mtk_emmc.c",
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
        '    "     \'IOProviderClass\' = MappleDTNub;"\n'
        '    "     \'IONameMatch\'     = \'stm,stm_ts\';"\n'
        '    "   },"',
        "iokit/KernelConfigTables.cpp: register STMTS (stm,stm_ts)",
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

    # Forward-declare a32_dsi_cmd_flush_now near the top of the file (before AppleCLCD::presentQueued!)
    old_decl = """extern "C" void mtk_ufs_safe_clear(void);                /* bsd/dev/mtk_ufs.c */"""
    new_decl = """extern "C" void mtk_ufs_safe_clear(void);                /* bsd/dev/mtk_ufs.c */
extern "C" void a32_dsi_cmd_flush_now(void);             /* bsd/dev/mtk_ufs.c (SM-A325M DSI CMD) */"""
    replace_exact(p, old_decl, new_decl, "MappleFramebuffer.cpp: declare a32_dsi_cmd_flush_now at top")

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
        "\t{ 0x1c88, 0x0001, 0x01 },       /* LDO_VCAMA2_CON0: RG_LDO_VCAMA2_EN */\n};",
        "MTKPwrap.cpp: whitelist MT6358 VCAMA2 (3.0V touch AVDD)",
    )


def patch_i2c_dma(xnu: Path):
    """Add MT6768 APDMA support to MTKI2C for rlen > 8 (16-byte FTS5CU56A touch events)."""
    h = xnu / "iokit" / "Kernel" / "arm" / "MTKIO.h"
    replace_exact(
        h,
        "#include <IOKit/IOMemoryDescriptor.h>",
        "#include <IOKit/IOMemoryDescriptor.h>\n#include <IOKit/IOBufferMemoryDescriptor.h>",
        "MTKIO.h: include IOBufferMemoryDescriptor.h",
    )
    replace_exact(
        h,
        "\tuint32_t                fErrors;\n};",
        "\tuint32_t                fErrors;\n"
        "\tvm_offset_t             fDmaRegs;\n"
        "\tIOBufferMemoryDescriptor *fDmaDesc;\n"
        "\tIOMemoryMap            *fDmaMap;\n"
        "\tvolatile uint8_t       *fDmaBuf;\n"
        "\tuint64_t                fDmaPa;\n"
        "\tbool                    fReportedDmaPoll;\n"
        "\tbool                    fTransferActive;\n};",
        "MTKIO.h: add APDMA fields to MTKI2C",
    )

    c = xnu / "iokit" / "Kernel" / "arm" / "MTKIO.cpp"
    replace_exact(
        c,
        '#include "MTKIO.h"',
        '#include "MTKIO.h"\n#include <mach/mach_time.h>\n'
        'extern "C" vm_offset_t ml_io_map(vm_offset_t, vm_size_t);\n'
        'static constexpr uint32_t kA32ApdmaRxOffset = 64;\n'
        'static constexpr uint32_t kA32ApdmaMaxRead = 512;\n'
        'static constexpr uint32_t kA32ApdmaIntFlag = 0x00;\n'
        'static constexpr uint32_t kA32ApdmaEnable = 0x08;\n'
        'static constexpr uint32_t kA32ApdmaRxAddress = 0x20;\n'
        'static constexpr uint32_t kA32ApdmaDone = 0x03;',
        "MTKIO.cpp: declare ml_io_map for APDMA",
    )
    replace_exact(
        c,
        "\tinitHW();\n\tfInterrupt->enable();",
        "\tuint32_t dmaPa = dtU32(provider, \"dma-base\", 0);\n"
        "\tif (dmaPa != 0) {\n"
        "\t\tfDmaRegs = ml_io_map((vm_offset_t)dmaPa, 0x80);\n"
        "\t\tfDmaDesc = IOBufferMemoryDescriptor::inTaskWithPhysicalMask(kernel_task,\n"
        "\t\t    kIODirectionInOut | kIOMemoryPhysicallyContiguous, 4096, 0xfffffffffffff000ull);\n"
        "\t\tif (fDmaRegs != 0 && fDmaDesc != NULL) {\n"
        "\t\t\tfDmaPa = fDmaDesc->getPhysicalSegment(0, NULL, kIOMemoryMapperNone);\n"
        "\t\t\tbzero(fDmaDesc->getBytesNoCopy(), 4096);\n"
        "\t\t\tfDmaDesc->performOperation(kIOMemoryIncoherentIOFlush, 0, 4096);\n"
        "\t\t\tfDmaMap = fDmaDesc->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapWriteCombineCache, 0, 0);\n"
        "\t\t\tif (fDmaMap != NULL) {\n"
        "\t\t\t\tfDmaBuf = (volatile uint8_t *)fDmaMap->getVirtualAddress();\n"
        "\t\t\t\tIOLog(\"I2C: bus %u APDMA @ 0x%x buf pa 0x%llx\\n\", fBusId, dmaPa, fDmaPa);\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "\tinitHW();\n\tfInterrupt->enable();",
        "MTKIO.cpp: map APDMA and coherent buffer in MTKI2C::start",
    )
    replace_exact(
        c,
        "\tOSSafeReleaseNULL(fMap);\n\tOSSafeReleaseNULL(fWorkLoop);",
        "\tOSSafeReleaseNULL(fDmaMap);\n\tOSSafeReleaseNULL(fDmaDesc);\n\tOSSafeReleaseNULL(fMap);\n\tOSSafeReleaseNULL(fWorkLoop);",
        "MTKIO.cpp: release APDMA resources in MTKI2C::free",
    )
    replace_exact(
        c,
        "\tif (addr > 0x7f || wlen == 0 || wlen > kFIFOSize || rlen > kFIFOSize || (rlen && rbuf == NULL)) {\n"
        "\t\treturn kIOReturnBadArgument;\n"
        "\t}",
        "\t/* FTS5CU56A can return 31 queued 16-byte events in a single read. */\n"
        "\tuint32_t maxR = (fDmaRegs != 0 && fDmaBuf != NULL) ? kA32ApdmaMaxRead : (uint32_t)kFIFOSize;\n"
        "\tif (addr > 0x7f || wlen == 0 || wlen > kFIFOSize || rlen > maxR || (rlen && rbuf == NULL)) {\n"
        "\t\treturn kIOReturnBadArgument;\n"
        "\t}",
        "MTKIO.cpp: allow up to 512-byte reads when APDMA is configured",
    )
    replace_exact(
        c,
        "\t\treturn transferGated(addr, wbuf, wlen, rbuf, rlen);",
        "\t\t/* commandSleep(&fDone) drops the gate while waiting for the I2C IRQ. */\n"
        "\t\twhile (fTransferActive) {\n"
        "\t\t\tfGate->commandSleep(&fTransferActive, THREAD_UNINT);\n"
        "\t\t}\n"
        "\t\tfTransferActive = true;\n"
        "\t\tIOReturn kr = transferGated(addr, wbuf, wlen, rbuf, rlen);\n"
        "\t\tfTransferActive = false;\n"
        "\t\tfGate->commandWakeup(&fTransferActive);\n"
        "\t\treturn kr;",
        "MTKIO.cpp: serialize transfers while waiting for the hardware IRQ",
    )
    replace_exact(
        c,
        "\tbool wrrd = rlen != 0;\n"
        "\n"
        "\twr16(I2C_HW_TIMEOUT, fHWTimeout);\n"
        "\twr16(I2C_CLOCK_DIV, (uint16_t)(((fClockDiv - 1) << 8) | (fClockDiv - 1)));\n"
        "\tuint16_t control = CONTROL_ACKERR_DET_EN | CONTROL_CLK_EXT_EN;\n"
        "\tif (wrrd) {\n"
        "\t\tcontrol |= CONTROL_DIR_CHANGE | CONTROL_RS;\n"
        "\t}\n"
        "\twr16(I2C_CONTROL, control);",
        "\tbool wrrd = rlen != 0;\n"
        "\tbool useDma = wrrd && rlen > kFIFOSize && fDmaRegs != 0 && fDmaBuf != NULL;\n"
        "\n"
        "\twr16(I2C_HW_TIMEOUT, fHWTimeout);\n"
        "\twr16(I2C_CLOCK_DIV, (uint16_t)(((fClockDiv - 1) << 8) | (fClockDiv - 1)));\n"
        "\tuint16_t control = CONTROL_ACKERR_DET_EN | CONTROL_CLK_EXT_EN;\n"
        "\tif (wrrd) {\n"
        "\t\tcontrol |= CONTROL_DIR_CHANGE | CONTROL_RS;\n"
        "\t}\n"
        "\tif (useDma) {\n"
        "\t\t/* MT6768 i2c-mtk.c enables DMA, DMA ACK and async handshaking together. */\n"
        "\t\tcontrol |= (1u << 2) | (1u << 8) | (1u << 9);\n"
        "\t}\n"
        "\twr16(I2C_CONTROL, control);",
        "MTKIO.cpp: set I2C_CONTROL_DMA_EN for rlen > 8",
    )
    replace_exact(
        c,
        "\tfor (uint32_t i = 0; i < wlen; i++) {\n"
        "\t\t*(volatile uint8_t *)(fRegs + I2C_DATA_PORT) = wbuf[i];\n"
        "\t}",
        "\tif (useDma) {\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x0c) = 1; /* DMA warm RST */\n"
        "\t\tIODelay(5);\n"
        "\t\tfor (uint32_t i = 0; i < wlen; i++) {\n"
        "\t\t\tfDmaBuf[i] = wbuf[i];\n"
        "\t\t}\n"
        "\t\t/* Keep the TX opcode out of the RX slot if a read fails to fill it. */\n"
        "\t\tfor (uint32_t i = 0; i < rlen; i++) {\n"
        "\t\t\tfDmaBuf[kA32ApdmaRxOffset + i] = 0;\n"
        "\t\t}\n"
        "\t\tuint64_t rxPa = fDmaPa + kA32ApdmaRxOffset;\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + kA32ApdmaIntFlag) = 0;\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x18) = 0; /* CON = WRRD */\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x1c) = (uint32_t)fDmaPa;\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + kA32ApdmaRxAddress) = (uint32_t)rxPa;\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x54) = (uint32_t)(fDmaPa >> 32);\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x58) = (uint32_t)(rxPa >> 32);\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x24) = wlen;\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + 0x28) = rlen;\n"
        "\t\tOSMemoryBarrier();\n"
        "\t\t*(volatile uint32_t *)(fDmaRegs + kA32ApdmaEnable) = 1;\n"
        "\t} else {\n"
        "\t\tfor (uint32_t i = 0; i < wlen; i++) {\n"
        "\t\t\t*(volatile uint8_t *)(fRegs + I2C_DATA_PORT) = wbuf[i];\n"
        "\t\t}\n"
        "\t}",
        "MTKIO.cpp: program APDMA for WRRD transactions with rlen > 8",
    )
    replace_exact(
        c,
        "\twhile (!fDone) {\n"
        "\t\tif (fGate->commandSleep(&fDone, deadline, THREAD_UNINT) == THREAD_TIMED_OUT) {\n"
        "\t\t\tbreak;\n"
        "\t\t}\n"
        "\t}",
        "\twhile (!fDone) {\n"
        "\t\tAbsoluteTime wake = deadline;\n"
        "\t\tif (useDma) {\n"
        "\t\t\tclock_interval_to_deadline(2, kMillisecondScale, &wake);\n"
        "\t\t\tif (wake > deadline) {\n"
        "\t\t\t\twake = deadline;\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t\tif (fGate->commandSleep(&fDone, wake, THREAD_UNINT) != THREAD_TIMED_OUT || fDone) {\n"
        "\t\t\tcontinue;\n"
        "\t\t}\n"
        "\t\tif (useDma) {\n"
        "\t\t\tuint16_t pending = rd16(I2C_INTR_STAT) & (INTR_TRANSAC_COMP | INTR_ERRORS);\n"
        "\t\t\tif (pending != 0) {\n"
        "\t\t\t\twr16(I2C_INTR_STAT, pending);\n"
        "\t\t\t\tfIrqStat = pending;\n"
        "\t\t\t\tfDone = true;\n"
        "\t\t\t\tbreak;\n"
        "\t\t\t}\n"
        "\t\t\t/* On A32, a completed WRRD can leave START and INTR_STAT both zero. */\n"
        "\t\t\tconst volatile uint32_t *dma = (const volatile uint32_t *)fDmaRegs;\n"
        "\t\t\tuint32_t rxEnd = (uint32_t)(fDmaPa + kA32ApdmaRxOffset + rlen);\n"
        "\t\t\tif (rd16(I2C_START) == 0 && dma[kA32ApdmaEnable / 4] == 0 &&\n"
        "\t\t\t    (dma[kA32ApdmaIntFlag / 4] & kA32ApdmaDone) == kA32ApdmaDone &&\n"
        "\t\t\t    dma[kA32ApdmaRxAddress / 4] == rxEnd) {\n"
        "\t\t\t\tfDone = true;\n"
        "\t\t\t\tif (!fReportedDmaPoll) {\n"
        "\t\t\t\t\tfReportedDmaPoll = true;\n"
        "\t\t\t\t\tIOLog(\"I2C: bus %u confirmed APDMA completion without MCU IRQ\\n\", fBusId);\n"
        "\t\t\t\t}\n"
        "\t\t\t\tbreak;\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t\tif (mach_absolute_time() >= deadline) {\n"
        "\t\t\tbreak;\n"
        "\t\t}\n"
        "\t}",
        "MTKIO.cpp: confirm completed APDMA transfers if the controller IRQ is missed",
    )
    replace_exact(
        c,
        "\tfor (uint32_t i = 0; i < rlen; i++) {\n"
        "\t\trbuf[i] = *(volatile uint8_t *)(fRegs + I2C_DATA_PORT);\n"
        "\t}",
        "\tif (useDma) {\n"
        "\t\tvolatile uint32_t *dmaEn = (volatile uint32_t *)(fDmaRegs + kA32ApdmaEnable);\n"
        "\t\tfor (unsigned int i = 0; i < 100 && *dmaEn != 0; i++) {\n"
        "\t\t\tIODelay(1);\n"
        "\t\t}\n"
        "\t\tif (*dmaEn != 0) {\n"
        "\t\t\treturn kIOReturnTimeout;\n"
        "\t\t}\n"
        "\t\tOSMemoryBarrier();\n"
        "\t\tfor (uint32_t i = 0; i < rlen; i++) {\n"
        "\t\t\trbuf[i] = fDmaBuf[kA32ApdmaRxOffset + i];\n"
        "\t\t}\n"
        "\t} else {\n"
        "\t\tfor (uint32_t i = 0; i < rlen; i++) {\n"
        "\t\t\trbuf[i] = *(volatile uint8_t *)(fRegs + I2C_DATA_PORT);\n"
        "\t\t}\n"
        "\t}",
        "MTKIO.cpp: wait for APDMA completion and copy the RX buffer back to caller",
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
    patch_i2c_dma(xnu)
    patch_framebuffer(xnu)
    patch_usb_link(xnu)
    print("Overlay applied cleanly!")


if __name__ == "__main__":
    main()
