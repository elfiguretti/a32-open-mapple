# Samsung Galaxy A32 4G (SM-A325M, MT6769T / mt6768) device tree values for build/mapple/tools/mkadt.py
# (paired with tools/soc/mt6768.py). All physical addresses verified from SM-A325M live.dtb and iomem.txt.

# 4 GB RAM (0x40000000-0x13fffffff). Samsung reserves RKP/RKP_NS/KASLR at 0xd0000000-0xd1401000 and
# dramc-rk1 at 0x13ffff000, leaving a clean 1740 MiB contiguous top bank for XNU (16 KiB page aligned,
# ending on the 32 MiB boundary at 0x13e000000).
RAM = (0xd1404000, 0x13e000000 - 0xd1404000)

# LK's framebuffer: verified live by fbdiag_a32 (2026-11-21):
# OVL0 L0 ADDR=0x7c720000, PITCH=0x1100 (4352 bytes = 1088*4, 64B-aligned!), SZ=1080x2400, s6e3fc3 AMOLED DSI CMD.
FB = dict(base=0x7c720000, width=1080, height=2400, stride=4352)
FB['size'] = (FB['stride'] * FB['height'] + 0x3fff) & ~0x3fff

ECID = 0x4133324D00000001  # 'A32M' + 1, synthetic
PRODUCT_NAME = 'Galaxy A32 4G'
DISPLAY_GAMUT = 'sRGB'
# serial-number is overwritten at boot by the shim with LK's androidboot.serialno.
ABOUT = dict(serial='MAPPLE0000000000', model='SM-A325M', region='PSN', regulatory='SM-A325M')


def devices(a, ph, phandle, gic_ph, ecid, flags):
    """/arm-io devices for SM-A325M (MT6769T / mt6768), verified against live.dtb."""
    node, cstr, u32, spi, io, wifi_mac = a.node, a.cstr, a.u32, a.spi, a.io, a.wifi_mac
    IRQ_EDGE_RISING, IRQ_EDGE_FALLING, IRQ_LEVEL_HIGH = a.IRQ_EDGE_RISING, a.IRQ_EDGE_FALLING, a.IRQ_LEVEL_HIGH
    HID_CSMR_POWER, HID_CSMR_VOLUME_UP, HID_CSMR_VOLUME_DOWN = a.HID_CSMR_POWER, a.HID_CSMR_VOLUME_UP, a.HID_CSMR_VOLUME_DOWN
    eint_ph = next(phandle)

    return [
        # Keypad @ 0x10010000 (SPI 0x49): in live.dtb kpd-hw-init-map bit 1 = 0x73 (KEY_VOLUMEUP).
        # Power (PWRKEY) and Volume Down (HOMEKEY / kpd-sw-rstkey=0x72) sit on the MT6358 PMIC.
        node('keypad', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-keypad')),
            ('reg', io(0x10010000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x49, IRQ_EDGE_RISING)),
            ('keymap', u32(1, HID_CSMR_VOLUME_UP)),
        ]),
        # EINT controller @ 0x1000b000 (SPI 0xba, same block as MT6781).
        node('eint', [
            ('AAPL,phandle', u32(eint_ph)),
            ('compatible', cstr('mediatek,mt6768-eint')),
            ('reg', io(0x1000b000, 0x1000)),
            ('interrupt-controller', b''),
            ('#interrupt-cells', u32(2)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0xba, IRQ_LEVEL_HIGH)),
        ]),
        # PWRAP @ 0x1000d000 -> MT6358 PMIC (EINT 144 / 0x90): PWRKEY = power, HOMEKEY = volume down.
        node('pwrap', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-pwrap')),
            ('reg', io(0x1000d000, 0x1000)),
        ], [node('pmic', [
            ph(),
            ('compatible', cstr('mediatek,mt6358-pmic')),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(144, IRQ_LEVEL_HIGH)),
        ], [node('keys', [
            ph(),
            ('compatible', cstr('mediatek,mt6358-keys')),
            ('keymap', u32(0, HID_CSMR_POWER, 1, HID_CSMR_VOLUME_DOWN)),
        ])])]),
        # GPIO controller @ 0x10005000.
        node('gpio', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-gpio')),
            ('reg', io(0x10005000, 0x1000)),
        ]),
        # I2C6 @ 0x1100d000 (DMA @ 0x11000600, SPI 0x94) hosting STMicroelectronics FTS5CU56A touchscreen @ 0x49.
        node('i2c6', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-i2c')),
            ('reg', io(0x1100d000, 0x1000) + io(0x11000600, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x94, IRQ_LEVEL_HIGH)),
            ('bus-id', u32(6)),
            ('clock-frequency', u32(400000)),
            ('clock-div', u32(5)),
        ]),
        # STMicroelectronics FTS5CU56A touch (stm,stm_ts) on i2c6 @ 0x49, IRQ GPIO/EINT 10, coords 4096x4096.
        node('touch', [
            ph(),
            ('compatible', cstr('stm,stm_ts')),
            ('bus-id', u32(6)),
            ('i2c-addr', u32(0x49)),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(10, IRQ_EDGE_FALLING)),
            ('max-x', u32(4095)),
            ('max-y', u32(4095)),
            ('panel-size', u32(FB['width'], FB['height'])),
        ]),
        # eMMC MSDC0 @ 0x11230000 (SPI 0x64), 128 GB SAMSUNG DV6DAB.
        node('emmc', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-msdc0')),
            ('reg', io(0x11230000, 0x10000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x64, IRQ_LEVEL_HIGH)),
        ]),
        # Mali-G52 MC2 (Bifrost): mali@13040000, mfg_cfg@13000000, spm@10006000, topckgen@10000000,
        # apmixed@1000c000, infracfg_ao@10001000; IRQs SPI 0x112 (GPU), 0x113 (MMU), 0x114 (JOB).
        node('gpu', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-mali')),
            ('reg', io(0x13040000, 0x4000) + io(0x13000000, 0x1000) + io(0x10006000, 0x1000) +
                    io(0x10000000, 0x1000) + io(0x1000c000, 0x1000) + io(0x10001000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x112, IRQ_LEVEL_HIGH) + spi(0x113, IRQ_LEVEL_HIGH) + spi(0x114, IRQ_LEVEL_HIGH)),
        ]),
        # CONNSYS MT6631 Wi-Fi/BT (consys@18002000, btif@1100c000).
        node('wlan', [
            ph(),
            ('device_type', cstr('wlan')),
            ('compatible', cstr('mediatek,mt6768-wlan')),
            ('local-mac-address', wifi_mac(ecid)),
            ('wifi-vendor', cstr('Murata')),
        ]),
    ]
