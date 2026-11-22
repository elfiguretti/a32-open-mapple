/*
 * Samsung Galaxy A32 4G (SM-A325M, MT6769T): STMicroelectronics FTS5CU56A
 * touchscreen driver, subclass of MappleHIDTouch.
 *
 * Matches /arm-io/touch ("stm,stm_ts") published directly as an IOPlatformDevice
 * under /arm-io (without MTKI2C/APDMA/EINT).
 *
 * Uses open-drain bit-bang I2C directly on GPIO89 (SCL) and GPIO90 (SDA) with
 * IOCFG_0 pull-ups (verified on SM-A325F/SM-A325M):
 *   - Power rails:
 *       * tsp_avdd_ldo (3.0V): MT6358 VCAMA2 (ANA_CON0 0x1e0c = 0x0c00, OP_EN 0x1c8a = 1, CON0 0x1c88 = 1)
 *       * tsp_io_ldo   (1.8V): GPIO 4 output HIGH
 *   - I2C 7-bit address: 0x49 (~100 kHz bit-bang, repeated START for register reads)
 *   - IRQ line: GPIO 10 (active low while FIFO is non-empty, checked via DIN)
 *   - Flash-resident firmware (verified live: id=535448 fw=02.54.00.16)
 *   - Coordinate event layout (stm_ts_event_coordinate, 16 bytes via 0x60):
 *       b[0]: eid[1:0] (0 = coordinate, 1 = status), tid[5:2] (0..9), tchsta[7:6] (1=ENTER, 2=MOVE, 3=LEAVE)
 *       b[1]: x[11:4]
 *       b[2]: y[11:4]
 *       b[3]: x[3:0] in bits [7:4], y[3:0] in bits [3:0]
 *       b[7]: left_event[4:0] (remaining queued events in IC FIFO)
 */
#include <pexpert/arm64/board_config.h>

#if defined(MT6781)
#include <IOKit/IOLib.h>
#include <IOKit/IOWorkLoop.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <mach/mach_time.h>
#include <pexpert/pexpert.h>
#include "MappleHIDTouch.h"

extern "C" vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern "C" int mtk_pwrap_write_regulator(uint32_t addr, uint32_t mask, uint32_t value);
extern "C" kern_return_t thread_terminate(thread_t thread);

#define A32_GPIO_PHYS           0x10005000UL
#define A32_IOCFG0_PHYS         0x10002000UL

#define A32_TSP_IO_GPIO         4
#define A32_TSP_IRQ_GPIO        10
#define A32_PIN_SCL             89
#define A32_PIN_SDA             90

#define GPIO_DIR_SET(bank)      (0x004u + (bank) * 0x10u)
#define GPIO_DIR_CLR(bank)      (0x008u + (bank) * 0x10u)
#define GPIO_DOUT_SET(bank)     (0x104u + (bank) * 0x10u)
#define GPIO_DOUT_CLR(bank)     (0x108u + (bank) * 0x10u)
#define GPIO_DIN(bank)          (0x200u + (bank) * 0x10u)
#define GPIO_MODE_REG(pin)      (0x300u + ((pin) / 8u) * 0x10u)

#define IOCFG_PD                0x50u
#define IOCFG_PU                0x60u
#define IOCFG_BIT_SCL           23u
#define IOCFG_BIT_SDA           25u

#define MT6358_VCAMA2_CON0      0x1c88u
#define MT6358_VCAMA2_OP_EN     0x1c8au
#define MT6358_VCAMA2_ANA_CON0  0x1e0cu

#define STM_CMD_SENSE_ON        0x10
#define STM_CMD_READ_ID         0x22
#define STM_CMD_READ_PANEL_INFO 0x23
#define STM_CMD_READ_FW_VER     0x24
#define STM_CMD_READ_ONE_EVENT  0x60
#define STM_CMD_CLEAR_ALL_EVENT 0x62

#define STM_EVENT_SIZE          16
#define STM_EID_COORDINATE      0x00
#define STM_EID_STATUS          0x01
#define STM_STATUS_ERROR        0x01
#define STM_STATUS_INFO         0x02
#define STM_ERROR_QUEUE_FULL    0x01
#define STM_INFO_READY          0x00
#define STM_READY_RESET         0x10

#define STM_TCHSTA_ENTER        1
#define STM_TCHSTA_MOVE         2
#define STM_TCHSTA_LEAVE        3

#define STM_MAX_FINGERS         MAPPLE_TOUCH_FINGERS
#define STM_MAX_DRAIN           16u

class STMTS : public MappleHIDTouch {
	OSDeclareDefaultStructors(STMTS);
public:
	virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	virtual void free(void) APPLE_KEXT_OVERRIDE;
	virtual IOWorkLoop *getWorkLoop(void) const APPLE_KEXT_OVERRIDE;
protected:
	virtual OSString *getProduct() APPLE_KEXT_OVERRIDE { return OSString::withCString("FTS5CU56A"); }
private:
	static void pollThreadEntry(void *param, wait_result_t wr);

	inline uint32_t rd(vm_offset_t base, uint32_t off) const
	{
		return *(volatile uint32_t *)(base + off);
	}
	inline void wr(vm_offset_t base, uint32_t off, uint32_t v) const
	{
		*(volatile uint32_t *)(base + off) = v;
		__asm__ volatile("dsb sy" ::: "memory");
	}
	inline void pinLow(uint32_t pin) const
	{
		uint32_t bank = pin / 32u, bit = 1u << (pin % 32u);
		wr(fGpio, GPIO_DOUT_CLR(bank), bit);
		wr(fGpio, GPIO_DIR_SET(bank), bit);
	}
	inline void pinRelease(uint32_t pin) const
	{
		uint32_t bank = pin / 32u, bit = 1u << (pin % 32u);
		wr(fGpio, GPIO_DOUT_SET(bank), bit);
		wr(fGpio, GPIO_DIR_CLR(bank), bit);
	}
	inline bool pinValue(uint32_t pin) const
	{
		return (rd(fGpio, GPIO_DIN(pin / 32u)) & (1u << (pin % 32u))) != 0;
	}
	inline void i2cHalf(void) const
	{
		IODelay(4);
	}

	void pinsToGpio(void);
	void powerOn(void);
	void i2cStart(void);
	void i2cStop(void);
	void i2cRecover(void);
	bool i2cWriteByte(uint8_t value);
	uint8_t i2cReadByte(bool ack);
	bool ftsReadOnce(uint8_t cmd, uint8_t *buf, uint32_t len);
	bool ftsWriteOnce(uint8_t cmd, const uint8_t *buf, uint32_t len);
	bool ftsRead(uint8_t cmd, uint8_t *buf, uint32_t len);
	bool ftsWrite(uint8_t cmd, const uint8_t *buf, uint32_t len);
	void handleEvent(const uint8_t *ev);
	void releaseContact(uint8_t id);
	void releaseAll(void);
	void pollLoop(void);

	IOWorkLoop *fWorkLoop;
	vm_offset_t fGpio;
	vm_offset_t fIocfg;
	uint8_t fAddr;
	uint32_t fMaxX, fMaxY;
	uint32_t fPanelW, fPanelH;
	uint32_t fActiveMask;
	int fLastX[STM_MAX_FINGERS];
	int fLastY[STM_MAX_FINGERS];
	uint32_t fEvents;
	uint32_t fReads;
	uint32_t fErrors;
	uint32_t fLogCount;
	volatile bool fStop;
	volatile bool fPollDone;
};

OSDefineMetaClassAndStructors(STMTS, MappleHIDTouch);

static uint32_t
stmDtU32(IOService *provider, const char *key, uint32_t dflt)
{
	OSData *d = OSDynamicCast(OSData, provider->getProperty(key));
	return (d != NULL && d->getLength() >= 4) ? *(const uint32_t *)d->getBytesNoCopy() : dflt;
}

IOWorkLoop *
STMTS::getWorkLoop(void) const
{
	return fWorkLoop;
}

void
STMTS::pinsToGpio(void)
{
	/* 1.8V I/O rail on GPIO4: level HIGH before direction output, function 0. */
	uint32_t ioBit = 1u << (A32_TSP_IO_GPIO % 32u);
	wr(fGpio, GPIO_DOUT_SET(A32_TSP_IO_GPIO / 32u), ioBit);
	wr(fGpio, GPIO_DIR_SET(A32_TSP_IO_GPIO / 32u), ioBit);
	uint32_t mode = rd(fGpio, GPIO_MODE_REG(A32_TSP_IO_GPIO));
	mode &= ~(0xfu << ((A32_TSP_IO_GPIO % 8u) * 4u));
	wr(fGpio, GPIO_MODE_REG(A32_TSP_IO_GPIO), mode);

	/* Ensure IRQ GPIO10 is input, function 0. */
	uint32_t irqBit = 1u << (A32_TSP_IRQ_GPIO % 32u);
	wr(fGpio, GPIO_DIR_CLR(A32_TSP_IRQ_GPIO / 32u), irqBit);
	mode = rd(fGpio, GPIO_MODE_REG(A32_TSP_IRQ_GPIO));
	mode &= ~(0xfu << ((A32_TSP_IRQ_GPIO % 8u) * 4u));
	wr(fGpio, GPIO_MODE_REG(A32_TSP_IRQ_GPIO), mode);

	/* GPIO89 (SCL) and GPIO90 (SDA) to GPIO function 0 with open-drain pull-ups. */
	mode = rd(fGpio, GPIO_MODE_REG(A32_PIN_SCL));
	mode &= ~(0xfu << ((A32_PIN_SCL % 8u) * 4u));
	mode &= ~(0xfu << ((A32_PIN_SDA % 8u) * 4u));
	wr(fGpio, GPIO_MODE_REG(A32_PIN_SCL), mode);

	uint32_t pd = rd(fIocfg, IOCFG_PD);
	pd &= ~((1u << IOCFG_BIT_SCL) | (1u << IOCFG_BIT_SDA));
	wr(fIocfg, IOCFG_PD, pd);

	uint32_t pu = rd(fIocfg, IOCFG_PU);
	pu |= (1u << IOCFG_BIT_SCL) | (1u << IOCFG_BIT_SDA);
	wr(fIocfg, IOCFG_PU, pu);

	pinRelease(A32_PIN_SCL);
	pinRelease(A32_PIN_SDA);
}

void
STMTS::powerOn(void)
{
	/* Enable 3.0V AVDD on MT6358 VCAMA2 (wait up to 5 s if MTKPwrap is still attaching). */
	int prc = -1;
	for (int i = 0; i < 100; i++) {
		prc = mtk_pwrap_write_regulator(MT6358_VCAMA2_ANA_CON0, 0x0f00, 0x0c00);
		if (prc != -1) {
			break;
		}
		IOSleep(50);
	}
	if (prc == 0) {
		(void)mtk_pwrap_write_regulator(MT6358_VCAMA2_OP_EN, 0x0001, 0x0001);
		(void)mtk_pwrap_write_regulator(MT6358_VCAMA2_CON0, 0x0001, 0x0001);
	} else {
		IOLog("TOUCH: FTS5CU56A VCAMA2 power-on failed (%d)\n", prc);
	}

	/* Allow FTS5CU56A internal MCU to boot its flash firmware. */
	IOSleep(50);
}

void
STMTS::i2cStart(void)
{
	pinRelease(A32_PIN_SDA);
	pinRelease(A32_PIN_SCL);
	i2cHalf();
	pinLow(A32_PIN_SDA);
	i2cHalf();
	pinLow(A32_PIN_SCL);
	i2cHalf();
}

void
STMTS::i2cStop(void)
{
	pinLow(A32_PIN_SDA);
	i2cHalf();
	pinRelease(A32_PIN_SCL);
	i2cHalf();
	pinRelease(A32_PIN_SDA);
	i2cHalf();
}

void
STMTS::i2cRecover(void)
{
	pinRelease(A32_PIN_SDA);
	for (unsigned i = 0; i < 9; i++) {
		pinRelease(A32_PIN_SCL);
		i2cHalf();
		pinLow(A32_PIN_SCL);
		i2cHalf();
	}
	i2cStop();
}

bool
STMTS::i2cWriteByte(uint8_t value)
{
	for (unsigned i = 0; i < 8; i++) {
		if (value & 0x80u) {
			pinRelease(A32_PIN_SDA);
		} else {
			pinLow(A32_PIN_SDA);
		}
		i2cHalf();
		pinRelease(A32_PIN_SCL);
		i2cHalf();
		pinLow(A32_PIN_SCL);
		i2cHalf();
		value <<= 1;
	}
	pinRelease(A32_PIN_SDA);
	i2cHalf();
	pinRelease(A32_PIN_SCL);
	i2cHalf();
	bool ack = !pinValue(A32_PIN_SDA);
	pinLow(A32_PIN_SCL);
	i2cHalf();
	return ack;
}

uint8_t
STMTS::i2cReadByte(bool ack)
{
	uint8_t value = 0;
	pinRelease(A32_PIN_SDA);
	for (unsigned i = 0; i < 8; i++) {
		i2cHalf();
		pinRelease(A32_PIN_SCL);
		i2cHalf();
		value = (uint8_t)((value << 1) | (pinValue(A32_PIN_SDA) ? 1u : 0u));
		pinLow(A32_PIN_SCL);
		i2cHalf();
	}
	if (ack) {
		pinLow(A32_PIN_SDA);
	} else {
		pinRelease(A32_PIN_SDA);
	}
	i2cHalf();
	pinRelease(A32_PIN_SCL);
	i2cHalf();
	pinLow(A32_PIN_SCL);
	i2cHalf();
	pinRelease(A32_PIN_SDA);
	return value;
}

bool
STMTS::ftsReadOnce(uint8_t cmd, uint8_t *buf, uint32_t len)
{
	uint8_t wrAddr = (uint8_t)(fAddr << 1);
	uint8_t rdAddr = (uint8_t)(wrAddr | 1u);

	i2cStart();
	if (!i2cWriteByte(wrAddr) || !i2cWriteByte(cmd)) {
		i2cStop();
		return false;
	}
	i2cStart();
	if (!i2cWriteByte(rdAddr)) {
		i2cStop();
		return false;
	}
	for (uint32_t i = 0; i < len; i++) {
		buf[i] = i2cReadByte(i + 1 < len);
	}
	i2cStop();
	return true;
}

bool
STMTS::ftsWriteOnce(uint8_t cmd, const uint8_t *buf, uint32_t len)
{
	uint8_t wrAddr = (uint8_t)(fAddr << 1);

	i2cStart();
	if (!i2cWriteByte(wrAddr) || !i2cWriteByte(cmd)) {
		i2cStop();
		return false;
	}
	for (uint32_t i = 0; i < len; i++) {
		if (!i2cWriteByte(buf[i])) {
			i2cStop();
			return false;
		}
	}
	i2cStop();
	return true;
}

bool
STMTS::ftsRead(uint8_t cmd, uint8_t *buf, uint32_t len)
{
	for (int attempt = 0; attempt < 3; attempt++) {
		if (ftsReadOnce(cmd, buf, len)) {
			return true;
		}
		i2cRecover();
	}
	return false;
}

bool
STMTS::ftsWrite(uint8_t cmd, const uint8_t *buf, uint32_t len)
{
	for (int attempt = 0; attempt < 3; attempt++) {
		if (ftsWriteOnce(cmd, buf, len)) {
			return true;
		}
		i2cRecover();
	}
	return false;
}

bool
STMTS::start(IOService *provider)
{
	uint32_t touch_en = 1;
	PE_parse_boot_argn("mp_touch", &touch_en, sizeof(touch_en));
	if (touch_en == 0) {
		IOLog("TOUCH: FTS5CU56A disabled by mp_touch=0\n");
		return false;
	}

	fWorkLoop = IOWorkLoop::workLoop();
	fAddr = (uint8_t)stmDtU32(provider, "i2c-address", 0x49);
	fMaxX = stmDtU32(provider, "max-x", 4095);
	fMaxY = stmDtU32(provider, "max-y", 4095);
	fPanelW = 1080;
	fPanelH = 2400;
	OSData *ps = OSDynamicCast(OSData, provider->getProperty("panel-size"));
	if (ps != NULL && ps->getLength() >= 8) {
		const uint32_t *dims = (const uint32_t *)ps->getBytesNoCopy();
		if (dims[0] != 0 && dims[1] != 0) {
			fPanelW = dims[0];
			fPanelH = dims[1];
		}
	}
	fGpio = ml_io_map(A32_GPIO_PHYS, 0x1000);
	fIocfg = ml_io_map(A32_IOCFG0_PHYS, 0x1000);
	if (fWorkLoop == NULL || fGpio == 0 || fIocfg == 0 || fMaxX == 0 || fMaxY == 0) {
		IOLog("TOUCH: STMTS missing work loop, MMIO map, or bounds\n");
		return false;
	}

	pinsToGpio();
	IOSleep(2);
	powerOn();

	IOLog("TOUCH: FTS5CU56A pins mode11=%08x pu=%08x pd=%08x idle scl=%d sda=%d irq=%d\n",
	    rd(fGpio, GPIO_MODE_REG(A32_PIN_SCL)), rd(fIocfg, IOCFG_PU), rd(fIocfg, IOCFG_PD),
	    pinValue(A32_PIN_SCL) ? 1 : 0, pinValue(A32_PIN_SDA) ? 1 : 0, pinValue(A32_TSP_IRQ_GPIO) ? 1 : 0);

	i2cRecover();

	uint8_t id[5] = { 0 };
	bool idOk = false;
	for (int attempt = 0; attempt < 5; attempt++) {
		if (ftsRead(STM_CMD_READ_ID, id, sizeof(id)) && id[0] == 0x53 && id[1] == 0x54 && id[2] == 0x48) {
			idOk = true;
			break;
		}
		IOSleep(30);
	}
	if (!idOk) {
		IOLog("TOUCH: FTS5CU56A at 0x%02x probe failed (id=%02x%02x%02x)\n",
		    fAddr, id[0], id[1], id[2]);
		return false;
	}

	uint8_t fw[9] = { 0 };
	(void)ftsRead(STM_CMD_READ_FW_VER, fw, sizeof(fw));
	uint8_t panel[11] = { 0 };
	if (ftsRead(STM_CMD_READ_PANEL_INFO, panel, sizeof(panel))) {
		uint32_t rx = ((uint32_t)panel[0] << 8) | panel[1];
		uint32_t ry = ((uint32_t)panel[2] << 8) | panel[3];
		if (rx > 0 && rx < 0xffffu) {
			fMaxX = rx;
		}
		if (ry > 0 && ry < 0xffffu) {
			fMaxY = ry;
		}
		IOLog("TOUCH: FTS5CU56A at 0x%02x id=%02x%02x%02x fw=%02x.%02x.%02x.%02x panel=%ux%u\n",
		    fAddr, id[0], id[1], id[2], fw[0], fw[1], fw[2], fw[3], rx, ry);
	} else {
		IOLog("TOUCH: FTS5CU56A at 0x%02x id=%02x%02x%02x fw=%02x.%02x.%02x.%02x\n",
		    fAddr, id[0], id[1], id[2], fw[0], fw[1], fw[2], fw[3]);
	}

	if (!ftsWrite(STM_CMD_SENSE_ON, NULL, 0)) {
		IOLog("TOUCH: FTS5CU56A SENSE_ON failed\n");
		return false;
	}
	IOSleep(30);
	(void)ftsWrite(STM_CMD_CLEAR_ALL_EVENT, NULL, 0);

	if (!MappleHIDTouch::start(provider)) {
		return false;
	}

	fStop = false;
	fPollDone = false;
	thread_t thread = NULL;
	if (kernel_thread_start(&STMTS::pollThreadEntry, this, &thread) != KERN_SUCCESS) {
		IOLog("TOUCH: STMTS failed to start polling thread\n");
		return false;
	}
	thread_deallocate(thread);
	IOLog("TOUCH: FTS5CU56A on (bit-bang GPIO%u/%u), coords %ux%u -> panel %ux%u\n",
	    A32_PIN_SCL, A32_PIN_SDA, fMaxX, fMaxY, fPanelW, fPanelH);
	return true;
}

void
STMTS::free(void)
{
	fStop = true;
	for (int i = 0; i < 20 && !fPollDone; i++) {
		IOSleep(5);
	}
	MappleHIDTouch::free();
	OSSafeReleaseNULL(fWorkLoop);
}

void
STMTS::pollThreadEntry(void *param, wait_result_t wr __unused)
{
	static_cast<STMTS *>(param)->pollLoop();
}

void
STMTS::releaseContact(uint8_t id)
{
	fActiveMask &= ~(1u << id);
	touch(id, false, fLastX[id], fLastY[id]);
}

void
STMTS::releaseAll(void)
{
	for (uint8_t id = 0; id < STM_MAX_FINGERS; id++) {
		if (fActiveMask & (1u << id)) {
			releaseContact(id);
		}
	}
}

void
STMTS::pollLoop(void)
{
	uint32_t pollUs = 4000;
	uint32_t pollAll = 0;
	(void)PE_parse_boot_argn("a32_touch_poll_us", &pollUs, sizeof(pollUs));
	(void)PE_parse_boot_argn("a32_touch_poll_all", &pollAll, sizeof(pollAll));
	if (pollUs < 1000u || pollUs > 50000u) {
		pollUs = 4000u;
	}

	uint32_t statsEvery = (60u * 1000000u) / pollUs;
	if (statsEvery == 0) {
		statsEvery = 1;
	}
	uint64_t lastReadNs = 0;
	const uint64_t watchIntervalNs = 40ULL * NSEC_PER_MSEC;
	uint32_t lastLoggedReads = 0;
	uint32_t statsLogged = 0;

	for (uint32_t tick = 0; !fStop; tick++) {
		uint64_t nowNs = 0;
		absolutetime_to_nanoseconds(mach_absolute_time(), &nowNs);

		bool irqAsserted = !pinValue(A32_TSP_IRQ_GPIO);
		bool shouldRead = (pollAll != 0) || irqAsserted || (tick < 4u) ||
		    (fActiveMask != 0 && (nowNs - lastReadNs) >= watchIntervalNs);

		if (shouldRead) {
			lastReadNs = nowNs;
			uint8_t ev[STM_EVENT_SIZE];
			for (uint32_t i = 0; i < STM_MAX_DRAIN; i++) {
				if (!ftsRead(STM_CMD_READ_ONE_EVENT, ev, sizeof(ev))) {
					if (++fErrors <= 8) {
						IOLog("TOUCH: FTS5CU56A bit-bang read failed (errors=%u)\n", fErrors);
					}
					releaseAll();
					break;
				}
				fReads++;
				if (ev[0] == 0) {
					break;
				}
				handleEvent(ev);
				if ((ev[7] & 0x1fu) == 0) {
					break;
				}
			}
		}

		if (tick != 0 && (tick % statsEvery) == 0 && (fReads != lastLoggedReads || statsLogged < 3u)) {
			lastLoggedReads = fReads;
			statsLogged++;
			IOLog("TOUCH: FTS5CU56A stats events=%u reads=%u errors=%u active=0x%x\n",
			    fEvents, fReads, fErrors, fActiveMask);
		}

		delay_for_interval(pollUs, NSEC_PER_USEC);
	}

	fPollDone = true;
	thread_terminate(current_thread());
}

void
STMTS::handleEvent(const uint8_t *ev)
{
	uint8_t eid = ev[0] & 0x03u;
	if (eid == STM_EID_STATUS) {
		uint8_t type = (ev[0] >> 2) & 0x0fu;
		if (type == STM_STATUS_ERROR && ev[1] == STM_ERROR_QUEUE_FULL) {
			releaseAll();
		} else if (type == STM_STATUS_INFO && ev[1] == STM_INFO_READY && ev[2] == STM_READY_RESET) {
			IOLog("TOUCH: FTS5CU56A reset, restarting scan\n");
			releaseAll();
			(void)ftsWrite(STM_CMD_SENSE_ON, NULL, 0);
			IOSleep(30);
			(void)ftsWrite(STM_CMD_CLEAR_ALL_EVENT, NULL, 0);
		}
		return;
	}
	if (eid != STM_EID_COORDINATE) {
		return;
	}

	uint8_t tid = (ev[0] >> 2) & 0x0fu;
	uint8_t state = ev[0] >> 6;
	if (tid >= STM_MAX_FINGERS || state == 0) {
		return;
	}
	fEvents++;

	uint32_t rawX = ((uint32_t)ev[1] << 4) | (ev[3] >> 4);
	uint32_t rawY = ((uint32_t)ev[2] << 4) | (ev[3] & 0x0fu);
	if (rawX > fMaxX) {
		rawX = fMaxX;
	}
	if (rawY > fMaxY) {
		rawY = fMaxY;
	}
	int x = (int)((uint64_t)rawX * (fPanelW - 1) / fMaxX);
	int y = (int)((uint64_t)rawY * (fPanelH - 1) / fMaxY);
	if (x < 0) {
		x = 0;
	} else if (x >= (int)fPanelW) {
		x = (int)fPanelW - 1;
	}
	if (y < 0) {
		y = 0;
	} else if (y >= (int)fPanelH) {
		y = (int)fPanelH - 1;
	}

	bool down = state != STM_TCHSTA_LEAVE;
	/* Some FTS release records omit the position. */
	if (!down && rawX == 0 && rawY == 0 && (fActiveMask & (1u << tid))) {
		x = fLastX[tid];
		y = fLastY[tid];
	}
	if (state != STM_TCHSTA_MOVE && fLogCount < 8) {
		fLogCount++;
		IOLog("TOUCH: FTS5CU56A %s tid=%u (%d,%d)\n", down ? "down" : "up", tid, x, y);
	}
	/* A second ENTER on the same slot starts a new contact after a lost LEAVE. */
	if (state == STM_TCHSTA_ENTER && (fActiveMask & (1u << tid))) {
		IOLog("TOUCH: FTS5CU56A restarting active contact %u\n", tid);
		releaseContact(tid);
	}
	if (down) {
		fActiveMask |= 1u << tid;
		fLastX[tid] = x;
		fLastY[tid] = y;
	} else {
		fActiveMask &= ~(1u << tid);
	}
	touch(tid, down, x, y);
}
#endif /* MT6781 */
