/*
 * fbdiag_a32 v2 (Hito 2b): Safe bare-metal AMOLED DSI CMD flush & display probe
 * for Samsung Galaxy A32 4G (SM-A325M, MT6769T / mt6768, S6E3FC3 AMOLED).
 *
 * Verified from Run 1:
 *  - EL1, MMU off, CNTFRQ=13MHz, dtb=0x4bc80000
 *  - OVL0 (0x1400b000): EN=1, ROI=2400x1080, SRC_CON=0x09 (L0=0x7c720000 pitch=4352, L3=0x7d116000)
 *  - DSI0 (0x14014000): MODE=0 (CMD_MODE), DISP_MUTEX (0x14016000)
 *
 * Run 2:
 *  1) Logs MUTEX0..5, MMSYS CG, RDMA0, DSI0 registers as LK leaves them.
 *  2) Disables OVL0 L3 overlay (SRC_CON=1), draws bright full-screen test bars + big text to 0x7c720000.
 *  3) Triggers a MT6768 DSI CMD frame update (DSI_CMDQ=0x002c3909, MUTEX_EN=1, DSI_START 0->1)
 *     so the S6E3FC3 AMOLED panel pulls the new frame into its GRAM!
 *  4) Holds the screen for 8 seconds so the user can see it live, then chains straight back to Android.
 */
#include <stdint.h>
#include "mtkdbg_a32.h"

#define FBDIAG_SECONDS 8
#define CHAIN_OFFSET   0x40000u
#define KERNEL_PA      0x40080000ULL
#define STUB_PA        0x4a000000ULL

/* MT6768 DSI0 registers (verified from drivers/misc/mediatek/video/mt6768/dispsys/ddp_reg_dsi.h) */
#define DSI_START      (DSI0_BASE + 0x000)
#define DSI_STA        (DSI0_BASE + 0x004)
#define DSI_INTSTA     (DSI0_BASE + 0x00c)
#define DSI_COM_CTRL   (DSI0_BASE + 0x010)
#define DSI_MODE_CTRL  (DSI0_BASE + 0x014)
#define DSI_CMDQ_SIZE  (DSI0_BASE + 0x060)
#define DSI_CMDQ_DATA0 (DSI0_BASE + 0x200)

/* MT6768 DISP_MUTEX registers (verified from ddp_reg_mutex.h & ddp_mutex.c) */
#define MUTEX_EN(n)    (DISP_MUTEX_BASE + 0x020 + 0x20u * (n))
#define MUTEX_RST(n)   (DISP_MUTEX_BASE + 0x028 + 0x20u * (n))
#define MUTEX_SOF(n)   (DISP_MUTEX_BASE + 0x02c + 0x20u * (n))
#define MUTEX_MOD0(n)  (DISP_MUTEX_BASE + 0x030 + 0x20u * (n))

/* MT6768 primary path modules: OVL0(7) | RDMA0(9) | COLOR0(11) | CCORR0(12) | AAL0(13) | GAMMA0(14) | DITHER0(15) | DSI0(16) */
#define MT6768_PRIMARY_MOD0 ((1u << 7) | (1u << 9) | (1u << 11) | (1u << 12) | (1u << 13) | (1u << 14) | (1u << 15) | (1u << 16))

extern char _start[];
extern char chain_stub[], chain_stub_end[];
static uint64_t chain_dtb;

static void bar(uint32_t y, uint32_t h, uint32_t color, const char *label)
{
	fb_rect(0, y, PANEL_W, h, color);
	uint32_t saved_y = fbc.y, saved_x = fbc.x, saved_bg = fbc.bg, saved_fg = fbc.fg;
	uint32_t saved_scale = fbc.scale;
	fbc.x = 32;
	fbc.y = y + (h > 48 ? (h - 48) / 2 : 8);
	fbc.scale = 3;
	fbc.bg = 0xff000000u;
	fbc.fg = 0xffffffffu;
	while (*label)
		fbcon_putc(*label++);
	fbc.x = saved_x;
	fbc.y = saved_y;
	fbc.bg = saved_bg;
	fbc.fg = saved_fg;
	fbc.scale = saved_scale;
}

static void __attribute__((noreturn)) chain_android(void)
{
	const volatile uint32_t *img = (const volatile uint32_t *)((uint64_t)_start + CHAIN_OFFSET);
	if (img[14] != 0x644d5241u) {
		dbg_puts("ERROR: no Linux Image at CHAIN_OFFSET -> warm reset\n");
		dbg_delay_ms(3000);
		wdt_reset();
	}
	const volatile uint32_t *hdr = (const volatile uint32_t *)_start;
	uint64_t image_size = ((uint64_t)hdr[11] << 32) | hdr[10];
	image_size = (image_size + 7ULL) & ~7ULL;
	dbg_puts("chaining to Android kernel, ");
	dbg_dec(image_size);
	dbg_puts(" bytes\n");

	volatile uint32_t *dst = (volatile uint32_t *)STUB_PA;
	const volatile uint32_t *src = (const volatile uint32_t *)chain_stub;
	for (uint64_t i = 0; i < (uint64_t)(chain_stub_end - chain_stub) / 4; i++)
		dst[i] = src[i];
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
	void (*stub)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t) = (void *)STUB_PA;
	stub(chain_dtb, KERNEL_PA, (uint64_t)img, image_size, KERNEL_PA);
	__builtin_unreachable();
}

void fbdiag_fault(uint64_t vec, uint64_t esr, uint64_t elr, uint64_t far)
{
	fbc.fg = 0xffff4040u;
	dbg_puts("\n*** FAULT vec=");
	dbg_hex(vec, 3);
	dbg_puts(" ESR=");
	dbg_hex(esr, 8);
	dbg_puts(" ELR=");
	dbg_hex(elr, 10);
	dbg_puts(" FAR=");
	dbg_hex(far, 10);
	dbg_puts("\n");
	dbg_delay_ms(3000);
	chain_android();
}

static void dsi_cmd_flush(uint32_t mutex_id)
{
	__asm__ volatile("dsb sy" ::: "memory");
	/* Ensure DSI0 is enabled and not busy */
	for (uint32_t t = 0; t < 100000; t++) {
		if (!(rd32(DSI_STA) & (1u << 31)))
			break;
	}
	/* Clear pending DSI int status, queue DCS 0x2C (WRITE_MEMORY_START) in CMDQ0 */
	wr32(DSI_INTSTA, 0);
	wr32(DSI_CMDQ_DATA0, 0x002c3909u);
	wr32(DSI_CMDQ_SIZE, 1u);
	/* Trigger MUTEX + DSI_START 0->1 (exact MT6768 ddp_dsi.c CMD_MODE sequence) */
	wr32(MUTEX_EN(mutex_id), 1u);
	wr32(DSI_START, 0u);
	wr32(DSI_START, 1u);
	__asm__ volatile("dsb sy" ::: "memory");
}

void fbdiag_main(uint64_t dtb)
{
	wdt_disable();
	plog_reset();
	chain_dtb = dtb;

	uint32_t orig_src_con = rd32(OVL0_BASE + OVL_SRC_CON);
	uint64_t fb = LK_FB_BASE;
	uint32_t pitch = LK_FB_PITCH;
	uint32_t l0_addr = rd32(OVL0_BASE + OVL_L0_ADDR);
	uint32_t l0_pitch = rd32(OVL0_BASE + OVL_L0_PITCH) & 0xffffu;
	if (l0_addr >= LK_FB_BASE && l0_addr < LK_FB_BASE + LK_FB_SIZE && l0_pitch >= PANEL_W * 4 && l0_pitch <= LK_FB_PITCH_MAX) {
		fb = l0_addr;
		pitch = l0_pitch;
	}

	fbcon_init(fb, pitch, 900, 2);

	dbg_puts("== fbdiag_a32 v2 (DSI CMD flush test) ==\n");
	dbg_puts("fb=");
	dbg_hex(fb, 8);
	dbg_puts(" pitch=");
	dbg_dec(pitch);
	dbg_puts(" orig_src_con=");
	dbg_hex(orig_src_con, 4);
	dbg_putc('\n');

	/* Log MMSYS clock gates, RDMA0, DSI0, and MUTEX0..5 BEFORE touching anything */
	dbg_puts("MMSYS CG0=");
	dbg_hex(rd32(MMSYS_CONFIG_BASE + 0x100), 8);
	dbg_puts(" CG1=");
	dbg_hex(rd32(MMSYS_CONFIG_BASE + 0x110), 8);
	dbg_puts(" MISC=");
	dbg_hex(rd32(MMSYS_CONFIG_BASE + 0x0f0), 8);
	dbg_putc('\n');

	dbg_puts("RDMA0 GCON=");
	dbg_hex(rd32(RDMA0_BASE + 0x010), 8);
	dbg_puts(" SZ0=");
	dbg_hex(rd32(RDMA0_BASE + 0x014), 8);
	dbg_puts(" SZ1=");
	dbg_hex(rd32(RDMA0_BASE + 0x018), 8);
	dbg_putc('\n');

	dbg_puts("DSI0 START=");
	dbg_hex(rd32(DSI_START), 8);
	dbg_puts(" STA=");
	dbg_hex(rd32(DSI_STA), 8);
	dbg_puts(" INTSTA=");
	dbg_hex(rd32(DSI_INTSTA), 8);
	dbg_puts(" COM=");
	dbg_hex(rd32(DSI_COM_CTRL), 8);
	dbg_puts(" MODE=");
	dbg_hex(rd32(DSI_MODE_CTRL), 8);
	dbg_puts(" CMDQ0=");
	dbg_hex(rd32(DSI_CMDQ_DATA0), 8);
	dbg_putc('\n');

	int active_mutex = -1;
	for (uint32_t m = 0; m < 6; m++) {
		uint32_t en = rd32(MUTEX_EN(m));
		uint32_t sof = rd32(MUTEX_SOF(m));
		uint32_t mod0 = rd32(MUTEX_MOD0(m));
		dbg_puts("MUTEX");
		dbg_dec(m);
		dbg_puts(": EN=");
		dbg_hex(en, 2);
		dbg_puts(" SOF=");
		dbg_hex(sof, 4);
		dbg_puts(" MOD0=");
		dbg_hex(mod0, 8);
		dbg_putc('\n');
		if (active_mutex < 0 && mod0 != 0)
			active_mutex = (int)m;
	}

	/* Turn off LK's L3 overlay so L0 (0x7c720000) is on top */
	wr32(OVL0_BASE + OVL_SRC_CON, 0x00000001u);

	/* Fill background and draw bright unmistakable test bars */
	fb_rect(0, 0, PANEL_W, PANEL_H, 0xff101020u);
	bar(120, 140, 0xffff0000u, "A32 OPEN MAPPLE: RED");
	bar(280, 140, 0xff00ff00u, "A32 OPEN MAPPLE: GREEN");
	bar(440, 140, 0xff0040ffu, "A32 OPEN MAPPLE: BLUE");
	bar(600, 140, 0xffffffffu, "A32 OPEN MAPPLE: WHITE");
	bar(760, 100, 0xffffff00u, "SM-A325M AMOLED LIVE!");

	/* Configure MUTEX if LK didn't leave one configured */
	uint32_t mid = (active_mutex >= 0) ? (uint32_t)active_mutex : 0u;
	if (active_mutex < 0) {
		wr32(MUTEX_MOD0(mid), MT6768_PRIMARY_MOD0);
		wr32(MUTEX_SOF(mid), 0u); /* single-mode software trigger */
	}

	dbg_puts("Flushing DSI CMD via MUTEX");
	dbg_dec(mid);
	dbg_puts(" MOD0=");
	dbg_hex(rd32(MUTEX_MOD0(mid)), 8);
	dbg_putc('\n');

	dsi_cmd_flush(mid);
	dbg_delay_ms(50);

	dbg_puts("AFTER FLUSH: DSI_STA=");
	dbg_hex(rd32(DSI_STA), 8);
	dbg_puts(" DSI_INTSTA=");
	dbg_hex(rd32(DSI_INTSTA), 8);
	dbg_puts(" MUTEX_EN=");
	dbg_hex(rd32(MUTEX_EN(mid)), 8);
	dbg_puts(" OVL_SRC_CON=");
	dbg_hex(rd32(OVL0_BASE + OVL_SRC_CON), 4);
	dbg_putc('\n');

	/* Re-flush once per second during countdown so the live countdown ticks on the AMOLED! */
	for (int s = FBDIAG_SECONDS; s > 0; s--) {
		dbg_puts("Android in ");
		dbg_dec(s);
		dbg_puts(" s\n");
		dsi_cmd_flush(mid);
		dbg_delay_ms(1000);
	}

	/* Restore original OVL0 SRC_CON before handing back to Android */
	wr32(OVL0_BASE + OVL_SRC_CON, orig_src_con);
	chain_android();
}
