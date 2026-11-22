#!/usr/bin/env python3
"""
Deterministic, self-verifying XNU source overlay for Samsung Galaxy A32 4G (SM-A325M,
MT6769T / mt6768, S6E3FC3 Super AMOLED DSI CMD) on top of Open Mapple's
xnu_kernel_mediatek_common + xnu_kernel_mediatek_mt6781.

Applies 3 minimal, silicon-verified changes:
  1) bsd/dev/mtk_ufs.c:
     - Adds a32_console_hw_init() and a32_dsi_cmd_flush_now() mapping ramoops (0x4d09f000),
       OVL0 (0x1400b000), DISP_MUTEX (0x14001000), and DSI0 (0x14014000) via ml_io_map().
     - Mirrors all console output from mapple_log_append() into ramoops (survives warm reset
       -> /sys/fs/pstore/console-ramoops-2) and triggers the 7-write MT6768 DSI CMD flush on
       newlines (rate-limited) so XNU boot logs scroll live on the S6E3FC3 AMOLED!
  2) iokit/Kernel/arm/MapplePlatformExpert.cpp:
     - Calls a32_console_hw_init() at the very start of MapplePlatformExpert::start() so early
       Mach + IOKit + BSD boot logs immediately flush to ramoops and the AMOLED panel.
  3) iokit/Kernel/arm/MappleFramebuffer.cpp:
     - Updates DISP_OVL0_PA/OVL0_2L/RDMA0/DSI0/SMI_LARB1/CCORR0 to verified MT6768 bases.
     - Calls a32_dsi_cmd_flush_now() whenever AppleCLCD::presentQueued() presents a frame.
"""
import sys
from pathlib import Path


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


def patch_mtk_ufs(xnu: Path):
    p = xnu / "bsd" / "dev" / "mtk_ufs.c"
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

    new = """/* ---- SM-A325M (MT6769T): live ramoops console + S6E3FC3 DSI CMD AMOLED flush ---- */
#define A32_RAMOOPS_PA       0x4d09f000UL
#define A32_RAMOOPS_SIZE     0x40000U
#define A32_RAMOOPS_SIG      0x43474244U /* "DBGC" */
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

void a32_dsi_cmd_flush_now(void);
void
a32_dsi_cmd_flush_now(void)
{
\tif (!a32_hw_ready || a32_dsi0_va == 0 || a32_mutex_va == 0 || a32_ovl0_va == 0) {
\t\treturn;
\t}
\t__asm__ volatile("dsb sy" ::: "memory");
\tfor (uint32_t t = 0; t < 50000; t++) {
\t\tif (!(a32_io_rd32(a32_dsi0_va, 0x004) & (1u << 31))) {
\t\t\tbreak;
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
\t\tif (a32_hw_ready) {
\t\t\ta32_ramoops_putc_raw(ch);
\t\t}
\t\tif (ch == '\\n') {
\t\t\tsaw_nl = 1;
\t\t}
\t}
\tif (a32_hw_ready && saw_nl) {
\t\t/* Rate-limit console AMOLED refreshes to ~25 Hz (13 MHz cntpct -> 520000 ticks ~ 40 ms) */
\t\tuint64_t now;
\t\t__asm__ volatile("mrs %0, cntpct_el0" : "=r"(now));
\t\tif (now - a32_last_flush_ticks >= 520000ULL) {
\t\t\ta32_last_flush_ticks = now;
\t\t\ta32_dsi_cmd_flush_now();
\t\t}
\t}
}"""
    replace_exact(p, old, new, "mtk_ufs.c: a32 ramoops + DSI CMD live console")


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
    old_pa = """#define DISP_OVL0_PA            0x14005000ull
#define DISP_OVL0_2L_PA         0x14006000ull
#define SMI_LARB0_PA            0x14003000ull
#define SMI_LARB1_PA            0x14004000ull"""
    new_pa = """/* SM-A325M (MT6769T / mt6768) verified display MMIO addresses */
extern "C" void a32_dsi_cmd_flush_now(void);
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
    print(f"Applying SM-A325M (MT6769T) XNU overlay to {xnu}:")
    patch_mtk_ufs(xnu)
    patch_platform_expert(xnu)
    patch_framebuffer(xnu)
    print("Overlay applied cleanly!")


if __name__ == "__main__":
    main()
