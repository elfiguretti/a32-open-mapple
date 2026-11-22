/*
 * Verified on SM-A325M (MT6769T / S6E3FC3 Super AMOLED DSI CMD) in fbdiag_a32 v3:
 *  - OVL0 (0x1400b000): L0 @ 0x7c720000, pitch=0x1100 (4352), SRC_CON=1 hides LK L3 overlay
 *  - DISP_MUTEX0 (0x14001000): MOD0=0x00090380 already configured by LK
 *  - DSI0 (0x14014000): CMD_MODE=0, CMDQ0=0x002c3909 (DCS WRITE_MEMORY_START)
 *  - RAMOOPS console: 0x4d09f000 (size 0x40000, sig "DBGC") -> /sys/fs/pstore/console-ramoops-2
 */
#pragma once
#include <stdint.h>

#define A32_OVL0_SRC_CON     0x1400b02cUL
#define A32_MUTEX0_EN        0x14001020UL
#define A32_DSI0_START       0x14014000UL
#define A32_DSI0_STA         0x14014004UL
#define A32_DSI0_INTSTA      0x1401400cUL
#define A32_DSI0_CMDQ_SIZE   0x14014060UL
#define A32_DSI0_CMDQ_DATA0  0x14014200UL

#define A32_RAMOOPS_BASE     0x4d09f000UL
#define A32_RAMOOPS_SIZE     0x40000U
#define A32_RAMOOPS_SIG      0x43474244U /* "DBGC" */

struct a32_ramoops_hdr {
	uint32_t sig;
	uint32_t start;
	uint32_t size;
};

static inline void a32_wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t a32_rd32(uintptr_t a) { return *(volatile uint32_t *)a; }

static inline void a32_dsi_flush(void)
{
	__asm__ volatile("dsb sy" ::: "memory");
	for (uint32_t t = 0; t < 50000; t++) {
		if (!(a32_rd32(A32_DSI0_STA) & (1u << 31)))
			break;
	}
	a32_wr32(A32_OVL0_SRC_CON, 1u);
	a32_wr32(A32_DSI0_INTSTA, 0u);
	a32_wr32(A32_DSI0_CMDQ_DATA0, 0x002c3909u);
	a32_wr32(A32_DSI0_CMDQ_SIZE, 1u);
	a32_wr32(A32_MUTEX0_EN, 1u);
	a32_wr32(A32_DSI0_START, 0u);
	a32_wr32(A32_DSI0_START, 1u);
	__asm__ volatile("dsb sy" ::: "memory");
}

static inline void a32_plog_reset(void)
{
	volatile struct a32_ramoops_hdr *h = (volatile struct a32_ramoops_hdr *)A32_RAMOOPS_BASE;
	h->sig = A32_RAMOOPS_SIG;
	h->start = 0;
	h->size = 0;
}

static inline void a32_plog_putc(char c)
{
	volatile struct a32_ramoops_hdr *h = (volatile struct a32_ramoops_hdr *)A32_RAMOOPS_BASE;
	volatile char *data = (volatile char *)(A32_RAMOOPS_BASE + sizeof(*h));
	uint32_t cap = A32_RAMOOPS_SIZE - (uint32_t)sizeof(*h);
	uint32_t at = h->start, sz = h->size;
	if (h->sig != A32_RAMOOPS_SIG || at >= cap || sz > cap) {
		a32_plog_reset();
		at = sz = 0;
	}
	data[at] = c;
	if (++at == cap)
		at = 0;
	if (sz < cap)
		sz++;
	h->start = at;
	h->size = sz;
}

static inline void a32_plog_puts(const char *s)
{
	while (*s)
		a32_plog_putc(*s++);
}
