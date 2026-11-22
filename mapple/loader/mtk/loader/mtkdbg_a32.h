/*
 * Debug output for bare-metal code on Samsung Galaxy A32 4G (SM-A325M, MT6769T / mt6768)
 * running with MMU off. Adapted from Open Mapple / HakiOS mtkdbg.h with verified addresses
 * from SM-A325M live.dtb and /proc/iomem:
 *
 *  - ramoops console: 0x4d09f000 (size 0x40000), pmsg: 0x4d0e0000 (size 0x10000)
 *  - LK framebuffer:  0x7c720000 (size 0x22e0000, 1080x2400, s6e3fc3 AMOLED DSI CMD)
 *  - OVL0:            0x1400b000, OVL0_2L: 0x1400c000, DSI0: 0x14014000, MUTEX: 0x14016000
 *  - TOPRGU watchdog: 0x10007000
 *
 * Build with -mgeneral-regs-only -mstrict-align -fno-builtin.
 */
#ifndef MTKDBG_A32_H
#define MTKDBG_A32_H

#include <stdint.h>
#include "font8x16.h"

#define RAMOOPS_CONSOLE      0x4d09f000ULL
#define RAMOOPS_CONSOLE_SIZE 0x40000u
#define RAMOOPS_SIG          0x43474244u /* "DBGC" */
#define RAMOOPS_PMSG         0x4d0e0000ULL
#define RAMOOPS_PMSG_SIZE    0x10000u

#define TOPRGU_BASE          0x10007000ULL
#define TOPRGU_MODE          (TOPRGU_BASE + 0x00)
#define TOPRGU_LENGTH        (TOPRGU_BASE + 0x04)
#define TOPRGU_RESTART       (TOPRGU_BASE + 0x08)
#define TOPRGU_STATUS        (TOPRGU_BASE + 0x0c)
#define TOPRGU_SWRST         (TOPRGU_BASE + 0x14)
#define TOPRGU_MODE_KEY      0x22000000u
#define TOPRGU_RESTART_KEY   0x1971u
#define TOPRGU_SWRST_KEY     0x1209u

/* SM-A325M LK framebuffer (verified live by fbdiag_a32: OVL0 L0=0x7c720000, PITCH=0x1100=4352) */
#define LK_FB_BASE           0x7c720000ULL
#define LK_FB_SIZE           0x22e0000ULL
#define LK_FB_PITCH          4352u /* 0x1100 = 1088 * 4 (64-byte aligned!) */
#define LK_FB_PITCH_MAX      8192u
#define PANEL_W              1080u
#define PANEL_H              2400u

/* MT6768/MT6769 display blocks from SM-A325M live.dtb */
#define MMSYS_CONFIG_BASE    0x14000000ULL
#define DISP_MUTEX_BASE      0x14001000ULL /* disp_mutex0@14001000 (verified in live.dts!) */
#define OVL0_BASE            0x1400b000ULL
#define OVL0_2L_BASE         0x1400c000ULL
#define RDMA0_BASE           0x1400d000ULL
#define DSI0_BASE            0x14014000ULL
#define DISP_MUTEX_EN        (DISP_MUTEX_BASE + 0x20)
#define DISP_MUTEX_RUN       (DISP_MUTEX_BASE + 0x24)

#define OVL_EN               0x00c
#define OVL_ROI_SIZE         0x020
#define OVL_SRC_CON          0x02c
#define OVL_L0_CON           0x030
#define OVL_L0_SRC_SIZE      0x038
#define OVL_L0_OFFSET        0x03c
#define OVL_L0_PITCH         0x044
#define OVL_L0_ADDR          0xf40

static inline uint32_t rd32(uint64_t a) { return *(volatile uint32_t *)a; }
static inline void wr32(uint64_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

static inline uint64_t dbg_ticks(void)
{
	uint64_t v;
	__asm__ volatile("isb; mrs %0, cntpct_el0" : "=r"(v));
	return v;
}

static inline uint64_t dbg_freq(void)
{
	uint64_t f;
	__asm__ volatile("mrs %0, cntfrq_el0" : "=r"(f));
	return f ? f : 13000000u;
}

static inline void dbg_delay_ms(uint32_t ms)
{
	uint64_t end = dbg_ticks() + (dbg_freq() / 1000u) * ms;
	while (dbg_ticks() < end) {
	}
}

static inline void wdt_disable(void)
{
	wr32(TOPRGU_RESTART, TOPRGU_RESTART_KEY);
	wr32(TOPRGU_MODE, TOPRGU_MODE_KEY);
}

static inline void __attribute__((noreturn)) wdt_reset(void)
{
	wr32(TOPRGU_SWRST, TOPRGU_SWRST_KEY);
	for (;;) {
		__asm__ volatile("wfe");
	}
}

struct ramoops_hdr {
	uint32_t sig;
	uint32_t start;
	uint32_t size;
};

static uint64_t plog_base __attribute__((unused)) = RAMOOPS_CONSOLE;
static uint32_t plog_zone __attribute__((unused)) = RAMOOPS_CONSOLE_SIZE;
#define PLOG_HDR  ((volatile struct ramoops_hdr *)plog_base)
#define PLOG_DATA ((volatile char *)(plog_base + sizeof(struct ramoops_hdr)))
#define PLOG_CAP  (plog_zone - sizeof(struct ramoops_hdr))

static inline void plog_reset(void)
{
	PLOG_HDR->sig = RAMOOPS_SIG;
	PLOG_HDR->start = 0;
	PLOG_HDR->size = 0;
}

static inline void plog_putc(char c)
{
	uint32_t at = PLOG_HDR->start, size = PLOG_HDR->size;
	if (PLOG_HDR->sig != RAMOOPS_SIG || at >= PLOG_CAP || size > PLOG_CAP) {
		plog_reset();
		at = size = 0;
	}
	PLOG_DATA[at] = c;
	if (++at == PLOG_CAP)
		at = 0;
	if (size < PLOG_CAP)
		size++;
	PLOG_HDR->start = at;
	PLOG_HDR->size = size;
}

struct fbcon {
	uint64_t base;
	uint32_t pitch, w, h;
	uint32_t x, y, top, scale;
	uint32_t fg, bg;
};

static struct fbcon fbc;

static inline void fb_px(uint32_t x, uint32_t y, uint32_t c)
{
	*(volatile uint32_t *)(fbc.base + (uint64_t)y * fbc.pitch + (uint64_t)x * 4) = c;
}

static inline void fb_rect(uint32_t x0, uint32_t y0, uint32_t w, uint32_t h, uint32_t c)
{
	if (!fbc.base)
		return;
	for (uint32_t y = y0; y < y0 + h && y < fbc.h; y++) {
		for (uint32_t x = x0; x < x0 + w && x < fbc.w; x++) {
			fb_px(x, y, c);
		}
	}
}

static inline void fbcon_init(uint64_t base, uint32_t pitch, uint32_t top, uint32_t scale)
{
	fbc.base = base;
	fbc.pitch = pitch;
	fbc.w = PANEL_W;
	fbc.h = PANEL_H;
	fbc.x = 0;
	fbc.y = fbc.top = top;
	fbc.scale = scale;
	fbc.fg = 0xffffffffu;
	fbc.bg = 0xff000000u;
}

static inline void fbcon_newline(void)
{
	uint32_t lh = 16 * fbc.scale;
	fbc.x = 0;
	fbc.y += lh;
	if (fbc.y + lh > fbc.h)
		fbc.y = fbc.top;
	fb_rect(0, fbc.y, fbc.w, lh, fbc.bg);
}

static inline void fbcon_putc(char c)
{
	uint32_t cw = 8 * fbc.scale;
	if (!fbc.base)
		return;
	if (c == '\n') {
		fbcon_newline();
		return;
	}
	if ((unsigned char)c < 0x20 || (unsigned char)c > 0x7e)
		c = '?';
	if (fbc.x + cw > fbc.w)
		fbcon_newline();
	const unsigned char *g = font8x16[(unsigned char)c - 0x20];
	for (uint32_t row = 0; row < 16 * fbc.scale; row++) {
		unsigned char bits = g[row / fbc.scale];
		for (uint32_t col = 0; col < cw; col++) {
			fb_px(fbc.x + col, fbc.y + row, (bits & (0x80 >> (col / fbc.scale))) ? fbc.fg : fbc.bg);
		}
	}
	fbc.x += cw;
}

static inline void dbg_putc(char c)
{
	plog_putc(c);
	fbcon_putc(c);
}

static inline void dbg_puts(const char *s)
{
	while (*s)
		dbg_putc(*s++);
}

static inline void dbg_hex(uint64_t v, int digits)
{
	static const char hex[] = "0123456789abcdef";
	for (int i = (digits - 1) * 4; i >= 0; i -= 4)
		dbg_putc(hex[(v >> i) & 0xf]);
}

static inline void dbg_dec(uint64_t v)
{
	char buf[21];
	int n = 0;
	do {
		buf[n++] = (char)('0' + v % 10);
		v /= 10;
	} while (v);
	while (n)
		dbg_putc(buf[--n]);
}

#endif /* MTKDBG_A32_H */
