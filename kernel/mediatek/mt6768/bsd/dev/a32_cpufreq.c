/*
 * Samsung Galaxy A32 4G (SM-A325M, MT6769T / mt6768) fixed-step CPU frequency raise.
 *
 * Ported from HakiOS SM-A325F (hakios.mtkcpufreq.c) for Open Mapple (xnu-8796.141.3),
 * using MTKPwrap's locked mtk_pwrap_read() / mtk_pwrap_write_regulator() helpers so
 * WACS2 transactions never race with battery/touch/PMIC threads.
 *
 * Boot-arg a32_cpufreq=N:
 *   0  disabled (default; clocks left where LK put them)
 *   1  read-only: log current A75 / A55 clocks and Vproc / VSRAM steps
 *   2  raise A75 cluster (ARMPLL2 / Vproc11) to 2000 MHz @ 1100 mV (step 96)
 *   3  raise A75 cluster to 2000 MHz and A55 cluster (ARMPLL1 / Vproc12) to 1700 MHz @ 975 mV (step 76)
 */
#include <pexpert/arm64/board_config.h>

#if defined(ARM64_BOARD_CONFIG_MT6781)
#include <stdbool.h>
#include <stdint.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);
extern int mtk_pwrap_read(uint32_t addr, uint32_t *value);
extern int mtk_pwrap_write_regulator(uint32_t addr, uint32_t mask, uint32_t value);
extern uint64_t sched_mapple_fast_cpus;

void a32_cpufreq_start(void);

#define APMIXED_PHYS            0x1000c000UL
#define ARMPLL_L_CON1           0x20cu  /* ARMPLL2: 2x Cortex-A75 (CPUs 6..7) */
#define ARMPLL_LL_CON1          0x21cu  /* ARMPLL1: 6x Cortex-A55 (CPUs 0..5) */

#define MT6358_VPROC11_DBG0     0x139eu
#define MT6358_VPROC11_ELR0     0x13a6u
#define MT6358_VPROC12_DBG0     0x141eu
#define MT6358_VPROC12_ELR0     0x1426u
#define MT6358_VSRAM11_TRACK    0x1b64u
#define MT6358_VSRAM12_TRACK    0x1b6cu
#define MT6358_VSRAM11_DBG0     0x1b60u
#define MT6358_VSRAM12_DBG0     0x1ba2u
#define MT6358_VSRAM_CON0       0x1beeu
#define MT6358_VSRAM_CON1       0x1bf0u
#define VOLT_STEP_MAX           99u     /* 1.11875 V ceiling */

static vm_offset_t a32_apmixed;
static uint32_t a32_cpufreq_mode;

static inline uint32_t
cpufreq_rd(uint32_t off)
{
	return *(volatile uint32_t *)(a32_apmixed + off);
}

static inline void
cpufreq_wr(uint32_t off, uint32_t val)
{
	*(volatile uint32_t *)(a32_apmixed + off) = val;
	__asm__ volatile("dsb sy" ::: "memory");
}

static void
a32_udelay(uint32_t us)
{
	uint64_t start, now;
	uint64_t ticks = (uint64_t)us * 13ULL; /* 13 MHz CNTPCT */

	__asm__ volatile("mrs %0, cntpct_el0" : "=r"(start));
	do {
		__asm__ volatile("mrs %0, cntpct_el0" : "=r"(now));
	} while ((now - start) < ticks);
}

static uint32_t
pll_mhz(uint32_t con1)
{
	uint32_t posdiv = 1u << ((con1 >> 24) & 7u);
	return (uint32_t)((uint64_t)(con1 & 0x3fffffu) * 26u / (1u << 14) / posdiv);
}

static uint32_t
pll_dds(uint32_t vco_mhz)
{
	return ((vco_mhz << 14) / 26u) & 0x3fffffu;
}

static bool
cluster_raise(const char *name, uint32_t con1_at, uint32_t elr, uint32_t dbg,
    uint32_t vsram_con, uint32_t vsram_dbg, uint32_t track, uint32_t mhz,
    uint32_t posdiv_sel, uint32_t step, uint32_t *final_mhz)
{
	uint32_t con1 = cpufreq_rd(con1_at);
	uint32_t vproc = 0, vsram = 0, tracking = 0;

	if (mtk_pwrap_read(dbg, &vproc) != 0 ||
	    mtk_pwrap_read(vsram_dbg, &vsram) != 0 ||
	    mtk_pwrap_read(track, &tracking) != 0) {
		printf("a32_cpufreq: %s: PMIC read failed; left as is\n", name);
		return false;
	}
	vproc &= 0x7fu;
	vsram = (vsram >> 8) & 0x7fu;
	uint32_t cur_mhz = pll_mhz(con1);
	printf("a32_cpufreq: %s at %u MHz (CON1 %#x), Vproc step %u (%u mV), VSRAM step %u (%u mV), tracking %#x\n",
	    name, cur_mhz, con1, vproc, 500u + vproc * 25u / 4u, vsram, 500u + vsram * 25u / 4u, tracking);
	if (final_mhz != NULL) {
		*final_mhz = cur_mhz;
	}
	if (step == 0) {
		return true;
	}

	/* Only modify clocks/rails when they match LK's verified boot state (~1086-1175 MHz, posdiv /2, 0.8 V). */
	if (((con1 >> 24) & 7u) != 1u || cur_mhz < 1000u || cur_mhz > 1250u || vproc < 44u || vproc > 52u) {
		printf("a32_cpufreq: %s not in expected LK state; left as is\n", name);
		return false;
	}

	uint32_t want_sram = (step + 16u > VOLT_STEP_MAX) ? VOLT_STEP_MAX : (step + 16u);
	if (!(tracking & 1u)) {
		if (mtk_pwrap_write_regulator(vsram_con, 0x007fu, want_sram) != 0) {
			printf("a32_cpufreq: %s: VSRAM write failed; left as is\n", name);
			return false;
		}
	}
	if (mtk_pwrap_write_regulator(elr, 0x007fu, step) != 0) {
		printf("a32_cpufreq: %s: Vproc write failed; left as is\n", name);
		return false;
	}
	delay_for_interval(1, NSEC_PER_MSEC);

	if (mtk_pwrap_read(dbg, &vproc) != 0 || (vproc & 0x7fu) != step) {
		printf("a32_cpufreq: %s: Vproc readback %u != %u; PLL left as is\n",
		    name, vproc & 0x7fu, step);
		return false;
	}

	uint32_t posdiv = 1u << posdiv_sel;
	cpufreq_wr(con1_at, (cpufreq_rd(con1_at) & ~0x3fffffu) | pll_dds(mhz * posdiv) | (1u << 31));
	a32_udelay(20);
	cpufreq_wr(con1_at, (cpufreq_rd(con1_at) & ~(7u << 24)) | (posdiv_sel << 24));
	a32_udelay(2);

	con1 = cpufreq_rd(con1_at);
	(void)mtk_pwrap_read(vsram_dbg, &vsram);
	uint32_t now_mhz = pll_mhz(con1);
	if (final_mhz != NULL) {
		*final_mhz = now_mhz;
	}
	printf("a32_cpufreq: %s now at %u MHz (CON1 %#x), Vproc step %u (%u mV), VSRAM step %u (%u mV)\n",
	    name, now_mhz, con1, step, 500u + step * 25u / 4u,
	    (vsram >> 8) & 0x7fu, 500u + ((vsram >> 8) & 0x7fu) * 25u / 4u);
	return true;
}

static void
a32_cpufreq_thread(__unused void *param, __unused wait_result_t wr)
{
	a32_apmixed = ml_io_map(APMIXED_PHYS, 0x1000);
	if (a32_apmixed == 0) {
		printf("a32_cpufreq: failed to map APMIXED\n");
		thread_terminate(current_thread());
		return;
	}

	uint32_t a75_mhz = 0;
	if (cluster_raise("A75s", ARMPLL_L_CON1, MT6358_VPROC11_ELR0, MT6358_VPROC11_DBG0,
	    MT6358_VSRAM_CON0, MT6358_VSRAM11_DBG0, MT6358_VSRAM11_TRACK,
	    2000u, 0u, a32_cpufreq_mode >= 2u ? 96u : 0u, &a75_mhz)) {
		if (a75_mhz >= 1950u) {
			/* CPUs 6 and 7 are Cortex-A75 in SM-A325M live.dts. */
			sched_mapple_fast_cpus = (1ULL << 6) | (1ULL << 7);
			printf("a32_cpufreq: sched_mapple_fast_cpus = 0x%llx\n", sched_mapple_fast_cpus);
		}
		(void)cluster_raise("A55s", ARMPLL_LL_CON1, MT6358_VPROC12_ELR0, MT6358_VPROC12_DBG0,
		    MT6358_VSRAM_CON1, MT6358_VSRAM12_DBG0, MT6358_VSRAM12_TRACK,
		    1700u, 1u, a32_cpufreq_mode >= 3u ? 76u : 0u, NULL);
	}

	thread_terminate(current_thread());
}

void
a32_cpufreq_start(void)
{
	uint32_t mode = 0;
	if (!PE_parse_boot_argn("a32_cpufreq", &mode, sizeof(mode)) || mode == 0) {
		return;
	}
	a32_cpufreq_mode = mode;

	thread_t thread = NULL;
	if (kernel_thread_start(a32_cpufreq_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}
#endif /* ARM64_BOARD_CONFIG_MT6781 */
