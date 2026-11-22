/*
 * Samsung Galaxy A32 4G (SM-A325M, MT6769T): STMicroelectronics FTS5CU56A
 * touchscreen driver, subclass of MappleHIDTouch.
 *
 * Matches /arm-io/i2c6/touch ("stm,stm_ts"), published as a MappleDTNub child
 * of MTKI2C (bus 6 @ 0x1100d000, APDMA @ 0x11000600).
 *
 * Hardware and protocol (SM-A325M live DT and Samsung stm_core.c/stm_fn.c):
 *   - Power rails:
 *       * tsp_avdd_ldo (3.0V): MT6358 VCAMA2 (ANA_CON0 0x1e0c = 0x0c00, CON0 0x1c88 bit 0 = 1)
 *       * tsp_io_ldo   (1.8V): GPIO 4 output HIGH (fixed_regulator@0)
 *   - I2C 7-bit address: 0x49 on bus 6 (400 kHz)
 *   - IRQ: EINT/GPIO 10, active low while the FIFO is non-empty
 *   - Flash-resident firmware (fts5cu56a_a32.bin, verified live: id=535448 fw=02.54.00.16)
 *   - Commands:
 *       0x22 READ_DEVICE_ID   (3 bytes -> 53 54 48)
 *       0x24 READ_FW_VERSION  (4 bytes -> 02 54 00 16)
 *       0x62 CLEAR_ALL_EVENT  (0 bytes)
 *       0x10 SENSE_ON         (0 bytes)
 *       0x60 READ_ONE_EVENT   (16 bytes in one I2C transaction)
 *       0x61 READ_ALL_EVENT   (16 bytes per remaining event in the FIFO)
 *   - Coordinate event layout (stm_ts_event_coordinate, 16 bytes):
 *       b[0]: eid[1:0] (0 = coordinate), tid[5:2] (0..9), tchsta[7:6] (1=ENTER, 2=MOVE, 3=LEAVE)
 *       b[1]: x[11:4]
 *       b[2]: y[11:4]
 *       b[3]: x[3:0] in bits [7:4], y[3:0] in bits [3:0]
 *       b[7]: left_event[4:0] (remaining queued events in IC FIFO)
 */
#include <pexpert/arm64/board_config.h>

#if defined(MT6781)
#include <IOKit/IOLib.h>
#include <IOKit/IOWorkLoop.h>
#include <IOKit/IOInterruptEventSource.h>
#include <IOKit/IOTimerEventSource.h>
#include <pexpert/pexpert.h>
#include "MappleHIDTouch.h"
#include "../MTKIO.h"

extern "C" int mtk_pwrap_write_regulator(uint32_t addr, uint32_t mask, uint32_t value);
extern "C" int mapple_gpio_set(uint32_t pin, int high);
extern "C" int mapple_gpio_get(uint32_t pin, int *high);

#define STM_CMD_SENSE_ON        0x10
#define STM_CMD_READ_ID         0x22
#define STM_CMD_READ_FW_VER     0x24
#define STM_CMD_TOUCH_TYPE      0x30
#define STM_CMD_OP_MODE         0x31
#define STM_CMD_READ_ONE_EVENT  0x60
#define STM_CMD_READ_ALL_EVENT  0x61
#define STM_CMD_CLEAR_ALL_EVENT 0x62
#define STM_CMD_SCAN_MODE       0xa0

#define STM_TOUCH_TYPE_DEFAULT  0x61  /* Touch, palm and wet */
#define STM_OP_MODE_NORMAL      0x00
#define STM_SCAN_MODE_DEFAULT   0x01  /* Mutual and self sensing */
#define STM_ECHO_EVENT          0x43
#define STM_ECHO_STATUS         0x01

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
#define STM_MAX_EVENTS_PER_IRQ  32
#define STM_FIFO_WATCH_MS       80

/* SM-A325M touch power rails (verified from live.dts + mt6358-regulator.c) */
#define A32_TSP_IO_GPIO         4
#define A32_TSP_IRQ_GPIO        10
#define MT6358_VCAMA2_ANA_CON0  0x1e0c
#define MT6358_LDO_VCAMA2_CON0  0x1c88

class STMTS : public MappleHIDTouch {
	OSDeclareDefaultStructors(STMTS);
public:
	virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	virtual void free(void) APPLE_KEXT_OVERRIDE;
	virtual IOWorkLoop *getWorkLoop(void) const APPLE_KEXT_OVERRIDE;
protected:
	virtual OSString *getProduct() APPLE_KEXT_OVERRIDE { return OSString::withCString("FTS5CU56A"); }
private:
	enum class ReadMode { Interrupt, Watch, ConfirmLoss };
	static void interruptOccurred(OSObject *owner, IOInterruptEventSource *src, int count);
	static void retryInterrupt(OSObject *owner, IOTimerEventSource *timer);
	static void watchActiveContacts(OSObject *owner, IOTimerEventSource *timer);
	void powerOn(void);
	bool startScanning(void);
	void readEvents(ReadMode mode);
	void handleEvent(const uint8_t *ev);
	void releaseContact(uint8_t id);
	void releaseAll(void);
	bool sendCmd(const uint8_t *cmd, uint32_t len);
	IOReturn readCmd(uint8_t cmd, uint8_t *buf, uint32_t len);
	IOReturn readBatch(uint8_t (&events)[STM_MAX_EVENTS_PER_IRQ][STM_EVENT_SIZE], uint32_t &count);

	MTKI2C *fBus;
	IOWorkLoop *fWorkLoop;
	IOInterruptEventSource *fInterrupt;
	IOTimerEventSource *fRetryTimer;
	IOTimerEventSource *fWatchTimer;
	uint8_t fAddr;
	uint32_t fMaxX, fMaxY;
	uint32_t fPanelW, fPanelH;
	uint32_t fActiveMask;
	int fLastX[STM_MAX_FINGERS];
	int fLastY[STM_MAX_FINGERS];
	uint32_t fErrors;
	uint32_t fLogCount;
	bool fSawInterrupt;
	bool fConfirmLostEvent;
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

bool
STMTS::sendCmd(const uint8_t *cmd, uint32_t len)
{
	IOReturn kr = fBus->transfer(fAddr, cmd, len, NULL, 0);
	if (kr != kIOReturnSuccess) {
		IOLog("TOUCH: FTS5CU56A command 0x%02x failed (0x%x)\n", cmd[0], kr);
	}
	return kr == kIOReturnSuccess;
}

IOReturn
STMTS::readCmd(uint8_t cmd, uint8_t *buf, uint32_t len)
{
	return fBus->transfer(fAddr, &cmd, 1, buf, len);
}

IOReturn
STMTS::readBatch(uint8_t (&events)[STM_MAX_EVENTS_PER_IRQ][STM_EVENT_SIZE], uint32_t &count)
{
	count = 0;
	IOReturn kr = readCmd(STM_CMD_READ_ONE_EVENT, events[0], STM_EVENT_SIZE);
	if (kr != kIOReturnSuccess || events[0][0] == 0) {
		return kr;
	}
	count = 1 + (events[0][7] & 0x1fu);
	if (count > 1) {
		kr = readCmd(STM_CMD_READ_ALL_EVENT, events[1], (count - 1) * STM_EVENT_SIZE);
		if (kr == kIOReturnSuccess) {
			for (uint32_t i = 1; i < count; i++) {
				if (events[i][0] == 0) {
					return kIOReturnIOError;
				}
			}
		}
	}
	return kr;
}

void
STMTS::powerOn(void)
{
	/* 1) Enable 3.0V AVDD on MT6358 VCAMA2 (wait up to 5 s if MTKPwrap is still attaching) */
	int prc = -1;
	for (int i = 0; i < 100; i++) {
		prc = mtk_pwrap_write_regulator(MT6358_VCAMA2_ANA_CON0, 0x0f00, 0x0c00);
		if (prc != -1) {
			break;
		}
		IOSleep(50);
	}
	if (prc == 0) {
		(void)mtk_pwrap_write_regulator(MT6358_LDO_VCAMA2_CON0, 0x0001, 0x0001);
	} else {
		IOLog("TOUCH: FTS5CU56A VCAMA2 power-on failed (%d)\n", prc);
	}

	/* 2) Enable 1.8V I/O LDO on GPIO 4 (wait for MTKGPIO if needed) */
	(void)MTKGPIO::wait();
	int grc = mapple_gpio_set(A32_TSP_IO_GPIO, 1);
	if (grc != 0) {
		IOLog("TOUCH: FTS5CU56A GPIO %u power-on failed (%d)\n", A32_TSP_IO_GPIO, grc);
	}

	/* Allow FTS5CU56A internal MCU to boot its flash firmware */
	IOSleep(100);
}

bool
STMTS::startScanning(void)
{
	const uint8_t clear[] = { STM_CMD_CLEAR_ALL_EVENT };
	const uint8_t normal[] = { STM_CMD_OP_MODE, STM_OP_MODE_NORMAL };
	const uint8_t touchType[] = { STM_CMD_TOUCH_TYPE, STM_TOUCH_TYPE_DEFAULT, 0 };
	const uint8_t senseOn[] = { STM_CMD_SENSE_ON };
	const uint8_t scan[] = { STM_CMD_SCAN_MODE, 0, STM_SCAN_MODE_DEFAULT };
	if (!sendCmd(clear, sizeof(clear)) || !sendCmd(normal, sizeof(normal))) {
		return false;
	}
	IOSleep(5);
	if (!sendCmd(touchType, sizeof(touchType))) {
		return false;
	}
	IOSleep(10);
	if (!sendCmd(senseOn, sizeof(senseOn)) || !sendCmd(scan, sizeof(scan))) {
		return false;
	}
	IOSleep(20);

	// Consume the command echoes before arming EINT10.
	uint8_t events[STM_MAX_EVENTS_PER_IRQ][STM_EVENT_SIZE];
	for (int attempt = 0; attempt < 12; attempt++) {
		uint32_t count;
		IOReturn kr = readBatch(events, count);
		if (kr != kIOReturnSuccess) {
			IOLog("TOUCH: FTS5CU56A scan echo read failed (0x%x)\n", kr);
			break;
		}
		for (uint32_t i = 0; i < count; i++) {
			const uint8_t *ev = events[i];
			if (ev[0] == STM_ECHO_EVENT && ev[1] == STM_ECHO_STATUS &&
			    ev[2] == scan[0] && ev[3] == scan[1] && ev[4] == scan[2]) {
				IOLog("TOUCH: FTS5CU56A scan mode acknowledged\n");
				return true;
			}
		}
		IOSleep(5);
	}
	IOLog("TOUCH: FTS5CU56A scan mode echo missing\n");
	return true;
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
	fBus = OSDynamicCast(MTKI2C, provider->getProvider());
	fWorkLoop = IOWorkLoop::workLoop();
	fAddr = (uint8_t)stmDtU32(provider, "i2c-address", 0x49);
	fMaxX = stmDtU32(provider, "max-x", 4096);
	fMaxY = stmDtU32(provider, "max-y", 4096);
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
	if (fBus == NULL || fWorkLoop == NULL || fMaxX == 0 || fMaxY == 0) {
		IOLog("TOUCH: STMTS missing I2C bus, work loop, or bounds\n");
		return false;
	}

	powerOn();

	uint8_t id[3] = { 0 };
	uint8_t fw[4] = { 0 };
	IOReturn kr = kIOReturnIOError;
	for (int attempt = 0; attempt < 5; attempt++) {
		kr = readCmd(STM_CMD_READ_ID, id, sizeof(id));
		if (kr == kIOReturnSuccess && id[0] == 0x53 && id[1] == 0x54 && id[2] == 0x48) {
			break;
		}
		IOSleep(30);
	}
	if (kr != kIOReturnSuccess || id[0] != 0x53 || id[1] != 0x54 || id[2] != 0x48) {
		IOLog("TOUCH: FTS5CU56A at 0x%02x probe failed (kr=0x%x id=%02x%02x%02x)\n",
		    fAddr, kr, id[0], id[1], id[2]);
		return false;
	}
	(void)readCmd(STM_CMD_READ_FW_VER, fw, sizeof(fw));
	IOLog("TOUCH: FTS5CU56A at 0x%02x id=%02x%02x%02x fw=%02x.%02x.%02x.%02x\n",
	    fAddr, id[0], id[1], id[2], fw[0], fw[1], fw[2], fw[3]);

	if (!startScanning()) {
		return false;
	}
	fInterrupt = IOInterruptEventSource::interruptEventSource(this, &STMTS::interruptOccurred, provider, 0);
	fRetryTimer = IOTimerEventSource::timerEventSource(this, &STMTS::retryInterrupt);
	fWatchTimer = IOTimerEventSource::timerEventSource(this, &STMTS::watchActiveContacts);
	if (fInterrupt == NULL || fRetryTimer == NULL || fWatchTimer == NULL ||
	    fWorkLoop->addEventSource(fInterrupt) != kIOReturnSuccess ||
	    fWorkLoop->addEventSource(fRetryTimer) != kIOReturnSuccess ||
	    fWorkLoop->addEventSource(fWatchTimer) != kIOReturnSuccess) {
		IOLog("TOUCH: STMTS failed to attach EINT interrupt\n");
		return false;
	}
	if (!MappleHIDTouch::start(provider)) {
		return false;
	}
	fInterrupt->enable();
	IOLog("TOUCH: FTS5CU56A on, coords %ux%u -> panel %ux%u\n", fMaxX, fMaxY, fPanelW, fPanelH);
	return true;
}

void
STMTS::free(void)
{
	if (fInterrupt != NULL) {
		fInterrupt->disable();
		fWorkLoop->removeEventSource(fInterrupt);
	}
	if (fRetryTimer != NULL) {
		fRetryTimer->cancelTimeout();
		fWorkLoop->removeEventSource(fRetryTimer);
	}
	if (fWatchTimer != NULL) {
		fWatchTimer->cancelTimeout();
		fWorkLoop->removeEventSource(fWatchTimer);
	}
	OSSafeReleaseNULL(fInterrupt);
	OSSafeReleaseNULL(fRetryTimer);
	OSSafeReleaseNULL(fWatchTimer);
	MappleHIDTouch::free();
	OSSafeReleaseNULL(fWorkLoop);
}

void
STMTS::interruptOccurred(OSObject *owner, IOInterruptEventSource *src __unused, int count __unused)
{
	STMTS *self = static_cast<STMTS *>(owner);
	if (!self->fSawInterrupt) {
		self->fSawInterrupt = true;
		IOLog("TOUCH: FTS5CU56A first EINT10 interrupt\n");
	}
	self->readEvents(ReadMode::Interrupt);
}

void
STMTS::retryInterrupt(OSObject *owner, IOTimerEventSource *timer __unused)
{
	STMTS *self = static_cast<STMTS *>(owner);
	if (self->fConfirmLostEvent) {
		self->fConfirmLostEvent = false;
		self->readEvents(ReadMode::ConfirmLoss);
	}
	self->fInterrupt->enable();
}

void
STMTS::watchActiveContacts(OSObject *owner, IOTimerEventSource *timer)
{
	STMTS *self = static_cast<STMTS *>(owner);
	if (self->fActiveMask == 0) {
		return;
	}
	// A quiet FTS FIFO does not tell us whether a stationary finger is still down.
	self->readEvents(ReadMode::Watch);
	if (self->fActiveMask != 0) {
		timer->setTimeoutMS(STM_FIFO_WATCH_MS);
	}
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
	fWatchTimer->cancelTimeout();
}

void
STMTS::readEvents(ReadMode mode)
{
	int irqLevel;
	bool asserted = mode != ReadMode::ConfirmLoss && fActiveMask != 0 &&
	    mapple_gpio_get(A32_TSP_IRQ_GPIO, &irqLevel) == 0 && irqLevel == 0;
	uint8_t events[STM_MAX_EVENTS_PER_IRQ][STM_EVENT_SIZE];
	uint32_t count;
	IOReturn kr = readBatch(events, count);
	if (kr != kIOReturnSuccess || count == 0) {
		if (mode == ReadMode::Watch && (kr == kIOReturnSuccess || !asserted)) {
			return;
		}
		if (++fErrors <= 8) {
			IOLog("TOUCH: FTS5CU56A EINT10 %s (0x%x)\n",
			    kr == kIOReturnSuccess ? "with empty FIFO" : "event read failed", kr);
		}
		if (mode == ReadMode::ConfirmLoss && fActiveMask != 0) {
			IOLog("TOUCH: FTS5CU56A missing FIFO record, releasing active contacts\n");
		}
		if (kr != kIOReturnSuccess || mode == ReadMode::ConfirmLoss) {
			releaseAll();
		}
		if (mode != ReadMode::Interrupt) {
			return;
		}
		// An already deasserted GPIO can leave a harmless stale IRQ. Only check
		// for a lost record if the read deasserted an IRQ that signalled data.
		fConfirmLostEvent = kr == kIOReturnSuccess && asserted &&
		    mapple_gpio_get(A32_TSP_IRQ_GPIO, &irqLevel) == 0 && irqLevel != 0;
		// Keep a stuck active-low line from immediately retriggering on an empty or failed read.
		fInterrupt->disable();
		fRetryTimer->setTimeoutMS(fConfirmLostEvent ? 4 : 20);
		return;
	}
	for (uint32_t i = 0; i < count; i++) {
		handleEvent(events[i]);
	}
	if (fActiveMask == 0) {
		fWatchTimer->cancelTimeout();
	} else if (mode != ReadMode::Watch) {
		fWatchTimer->setTimeoutMS(STM_FIFO_WATCH_MS);
	}
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
			(void)startScanning();
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
	uint32_t rawX = ((uint32_t)ev[1] << 4) | (ev[3] >> 4);
	uint32_t rawY = ((uint32_t)ev[2] << 4) | (ev[3] & 0x0fu);
	if (rawX >= fMaxX) {
		rawX = fMaxX - 1;
	}
	if (rawY >= fMaxY) {
		rawY = fMaxY - 1;
	}
	int x = (int)(rawX * fPanelW / fMaxX);
	int y = (int)(rawY * fPanelH / fMaxY);
	bool down = state != STM_TCHSTA_LEAVE;
	if (state != STM_TCHSTA_MOVE && fLogCount < 8) {
		fLogCount++;
		IOLog("TOUCH: FTS5CU56A %s tid=%u (%d,%d)\n", down ? "down" : "up", tid, x, y);
	}
	// A second ENTER on the same slot starts a new contact after a lost LEAVE.
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
