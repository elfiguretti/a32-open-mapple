#pragma once
#include <stdint.h>

/* Samsung Galaxy A32 4G (SM-A325M, MT6769T): verified live via fbdiag_a32 v1-v3 */
#define FB_BASE		0x7c720000UL	/* LK atag,videolfb fb_base (s6e3fc3 AMOLED) */
#define FB_W		1080
#define FB_H		2400
#define FB_STRIDE	4352		/* 0x1100 = 1088 * 4 (64-byte aligned!) */

#define RED	0xffff0000U
#define BLUE	0xff0000a0U
#define WHITE	0xffffffffU
#define YELLOW	0xffffff00U
#define GREEN	0xff00ff00U

extern volatile uint32_t *con_fb;

void con_clear(uint32_t bg);
void con_skip(void);
void con_str(const char *label, const char *s);
void con_hex(const char *label, uint64_t v);
void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb);
