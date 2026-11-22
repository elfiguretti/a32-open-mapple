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
#define A32_IFR2_CG_CLR      0x084U      /* bit 11 = CLK_IFR_I2C_AP */
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

void a32_dsi_cmd_flush_mode(int force);
void
a32_dsi_cmd_flush_mode(int force)
{
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0 || a32_ovl0_va == 0) {
\t\treturn;
\t}
\t__asm__ volatile("dsb sy" ::: "memory");
\tif (a32_io_rd32(a32_dsi0_va, 0x004) & (1u << 31)) {
\t\tif (!force) {
\t\t\treturn; /* mid-frame: never tear the AMOLED panel */
\t\t}
\t\tuint64_t deadline = a32_cntpct_now() + 455000ULL; /* 35 ms @ 13 MHz */
\t\twhile (a32_io_rd32(a32_dsi0_va, 0x004) & (1u << 31)) {
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

\t/* Ensure MT6768 infracfg_ao clock gates for I2C6 (touch) and MSDC0 (eMMC) are open */
\tvm_offset_t infra = ml_io_map(A32_INFRACFG_PA, 0x1000);
\tif (infra != 0) {
\t\ta32_io_wr32(infra, A32_IFR2_CG_CLR, (1u << 11));
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
    patch_framebuffer(xnu)
    print("Overlay applied cleanly!")


if __name__ == "__main__":
    main()
