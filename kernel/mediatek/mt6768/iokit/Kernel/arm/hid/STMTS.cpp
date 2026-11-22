/*
 * Samsung Galaxy A32 4G (SM-A325M, MT6769T): STMicroelectronics FTS5CU56A
 * touchscreen driver, subclass of MappleHIDTouch.
 *
 * Matches /arm-io/i2c6/touch ("stm,stm_ts"), published as a MappleDTNub child
 * of MTKI2C (bus 6 @ 0x1100d000).
 *
 * Protocol verified directly against Samsung SM-A325M kernel source
 * (drivers/input/sec_input/stm/stm_reg.h, stm_dev.h, stm_core.c) and live.dts:
 *   - I2C 7-bit address: 0x49
 *   - IRQ: EINT/GPIO 10, falling edge
 *   - Flash-resident firmware (fts5cu56a_a32.bin, sec,regulator_boot_on):
 *     no firmware download required at boot.
 *   - Commands:
 *       0x22 READ_DEVICE_ID   (3 bytes)
 *       0x24 READ_FW_VERSION  (4 bytes)
 *       0x62 CLEAR_ALL_EVENT  (0 bytes)
 *       0x10 SENSE_ON         (0 bytes)
 *       0x60 READ_ONE_EVENT   (16-byte hardware packet; all coordinate fields
 *                              and left_event sit in bytes 0..7, which fits
 *                              MTKI2C::kFIFOSize = 8 directly!).
 *   - Coordinate event layout (stm_event_coordinate, 8 bytes):
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
#include <pexpert/pexpert.h>
#include "MappleHIDTouch.h"
#include "../MTKIO.h"

#define STM_CMD_SENSE_ON        0x10
#define STM_CMD_READ_ID         0x22
#define STM_CMD_READ_FW_VER     0x24
#define STM_CMD_READ_ONE_EVENT  0x60
#define STM_CMD_CLEAR_ALL_EVENT 0x62

#define STM_EID_COORDINATE      0x00
#define STM_EID_STATUS          0x01
#define STM_EID_NONE            0x00

#define STM_TCHSTA_ENTER        1
#define STM_TCHSTA_MOVE         2
#define STM_TCHSTA_LEAVE        3

#define STM_MAX_FINGERS         MAPPLE_TOUCH_FINGERS
#define STM_MAX_EVENTS_PER_IRQ  32

class STMTS : public MappleHIDTouch {
	OSDeclareDefaultStructors(STMTS);
public:
	virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	virtual void free(void) APPLE_KEXT_OVERRIDE;
	virtual IOWorkLoop *getWorkLoop(void) const APPLE_KEXT_OVERRIDE;
protected:
	virtual OSString *getManufacturer() APPLE_KEXT_OVERRIDE { return OSString::withCString("STMicroelectronics"); }
	virtual OSString *getProduct() APPLE_KEXT_OVERRIDE { return OSString::withCString("FTS5CU56A"); }
private:
	static void interruptOccurred(OSObject *owner, IOInterruptEventSource *src, int count);
	void handleEvent(void);
	void releaseAll(void);
	IOReturn sendCmd(uint8_t cmd);
	IOReturn readCmd(uint8_t cmd, uint8_t *buf, uint32_t len);

	MTKI2C *fBus;
	IOWorkLoop *fWorkLoop;
	IOInterruptEventSource *fInterrupt;
	uint8_t fAddr;
	uint32_t fMaxX, fMaxY;
	uint32_t fPanelW, fPanelH;
	uint32_t fActiveMask;
	uint32_t fErrors;
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

IOReturn
STMTS::sendCmd(uint8_t cmd)
{
	return fBus->transfer(fAddr, &cmd, 1, NULL, 0);
}

IOReturn
STMTS::readCmd(uint8_t cmd, uint8_t *buf, uint32_t len)
{
	return fBus->transfer(fAddr, &cmd, 1, buf, len);
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

	uint8_t id[3] = { 0 };
	uint8_t fw[4] = { 0 };
	IOReturn kr = kIOReturnIOError;
	for (int attempt = 0; attempt < 3; attempt++) {
		kr = readCmd(STM_CMD_READ_ID, id, sizeof(id));
		if (kr == kIOReturnSuccess) {
			break;
		}
		IOSleep(20);
	}
	if (kr != kIOReturnSuccess) {
		IOLog("TOUCH: FTS5CU56A at 0x%02x did not answer (kr=0x%x)\n", fAddr, kr);
		return false;
	}
	(void)readCmd(STM_CMD_READ_FW_VER, fw, sizeof(fw));
	IOLog("TOUCH: FTS5CU56A at 0x%02x id=%02x%02x%02x fw=%02x.%02x.%02x.%02x\n",
	    fAddr, id[0], id[1], id[2], fw[0], fw[1], fw[2], fw[3]);

	(void)sendCmd(STM_CMD_CLEAR_ALL_EVENT);
	IOSleep(5);
	(void)sendCmd(STM_CMD_SENSE_ON);

	if (!MappleHIDTouch::start(provider)) {
		return false;
	}
	fInterrupt = IOInterruptEventSource::interruptEventSource(this, &STMTS::interruptOccurred, provider, 0);
	if (fInterrupt == NULL || fWorkLoop->addEventSource(fInterrupt) != kIOReturnSuccess) {
		IOLog("TOUCH: STMTS failed to attach EINT interrupt\n");
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
		if (fWorkLoop != NULL) {
			fWorkLoop->removeEventSource(fInterrupt);
		}
	}
	OSSafeReleaseNULL(fInterrupt);
	MappleHIDTouch::free();
	OSSafeReleaseNULL(fWorkLoop);
}

void
STMTS::interruptOccurred(OSObject *owner, IOInterruptEventSource *src __unused, int count __unused)
{
	STMTS *self = OSDynamicCast(STMTS, owner);
	if (self != NULL) {
		self->handleEvent();
	}
}

void
STMTS::releaseAll(void)
{
	for (int id = 0; id < STM_MAX_FINGERS; id++) {
		if (fActiveMask & (1u << id)) {
			touch(id, false, 0, 0);
		}
	}
	fActiveMask = 0;
}

void
STMTS::handleEvent(void)
{
	for (uint32_t iter = 0; iter < STM_MAX_EVENTS_PER_IRQ; iter++) {
		uint8_t ev[8] = { 0 };
		if (readCmd(STM_CMD_READ_ONE_EVENT, ev, sizeof(ev)) != kIOReturnSuccess) {
			if (++fErrors <= 8) {
				IOLog("TOUCH: FTS5CU56A event read error #%u\n", fErrors);
			}
			break;
		}
		if (ev[0] == 0 && ev[1] == 0 && ev[2] == 0 && ev[3] == 0) {
			break;
		}
		uint8_t eid = ev[0] & 0x03u;
		uint8_t left = ev[7] & 0x1fu;

		if (eid == STM_EID_COORDINATE) {
			uint8_t tid = (ev[0] >> 2) & 0x0fu;
			uint8_t tchsta = (ev[0] >> 6) & 0x03u;
			uint32_t raw_x = ((uint32_t)ev[1] << 4) | ((uint32_t)ev[3] >> 4);
			uint32_t raw_y = ((uint32_t)ev[2] << 4) | ((uint32_t)ev[3] & 0x0fu);

			if (tid < STM_MAX_FINGERS) {
				if (raw_x >= fMaxX) {
					raw_x = fMaxX - 1;
				}
				if (raw_y >= fMaxY) {
					raw_y = fMaxY - 1;
				}
				int px = (int)((raw_x * fPanelW) / fMaxX);
				int py = (int)((raw_y * fPanelH) / fMaxY);

				if (tchsta == STM_TCHSTA_ENTER || tchsta == STM_TCHSTA_MOVE) {
					fActiveMask |= (1u << tid);
					touch((int)tid, true, px, py);
				} else if (tchsta == STM_TCHSTA_LEAVE) {
					fActiveMask &= ~(1u << tid);
					touch((int)tid, false, px, py);
				}
			}
		} else if (eid == STM_EID_STATUS) {
			/* Controller reset / status notification: clear stuck contacts and re-arm sense if needed. */
			if (ev[1] == 0x10 && fActiveMask != 0) {
				releaseAll();
			}
		}
		if (left == 0) {
			break;
		}
	}
}
#endif /* MT6781 */
