# Samsung Galaxy A32 4G (SM-A325M, MT6769T / mt6768) device tree values for build/mapple/tools/mkadt.py
# (paired with tools/soc/mt6768.py). All physical addresses verified from SM-A325M live.dtb and iomem.txt.

# 4 GB RAM (0x40000000-0x13fffffff). Samsung reserves RKP/RKP_NS/KASLR at 0xd0000000-0xd1401000 and
# dramc-rk1 at 0x13ffff000. XNU's 16 KiB bootstrap page tables in start.s use 32 MiB L2 blocks
# (ARM_TT_L2_SIZE = 0x2000000), so phys_base MUST be 32 MiB aligned -> 0xd2000000..0x13e000000 (1728 MiB).
RAM = (0xd2000000, 0x13e000000 - 0xd2000000)

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
    """/arm-io devices for SM-A325M (MT6769T / mt6768), verified against live.dtb.

    Compatible strings match xnu_kernel_mediatek_common/iokit/KernelConfigTables.cpp
    because MT6768/MT6769T shares the same Keypad, EINT, PWRAP, MT6358 PMIC, GPIO,
    I2C v2, and CONNSYS MT6631 register layouts as MT6781.
    """
    node, cstr, u32, spi, io, wifi_mac = a.node, a.cstr, a.u32, a.spi, a.io, a.wifi_mac
    IRQ_EDGE_RISING, IRQ_LEVEL_HIGH = a.IRQ_EDGE_RISING, a.IRQ_LEVEL_HIGH
    HID_CSMR_POWER, HID_CSMR_VOLUME_UP, HID_CSMR_VOLUME_DOWN = a.HID_CSMR_POWER, a.HID_CSMR_VOLUME_UP, a.HID_CSMR_VOLUME_DOWN
    eint_ph = next(phandle)

    return [
        # Keypad @ 0x10010000 (SPI 0x49): in live.dtb kpd-hw-init-map bit 1 = 0x73 (KEY_VOLUMEUP).
        # Power (PWRKEY) and Volume Down (HOMEKEY / kpd-sw-rstkey=0x72) sit on the MT6358 PMIC.
        node('keypad', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-keypad')),
            ('reg', io(0x10010000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x49, IRQ_EDGE_RISING)),
            ('keymap', u32(1, HID_CSMR_VOLUME_UP)),
        ]),
        # EINT controller @ 0x1000b000 (SPI 0xba, same block as MT6781).
        node('eint', [
            ('AAPL,phandle', u32(eint_ph)),
            ('compatible', cstr('mediatek,mt6781-eint')),
            ('reg', io(0x1000b000, 0x1000)),
            ('interrupt-controller', b''),
            ('#interrupt-cells', u32(2)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0xba, IRQ_LEVEL_HIGH)),
        ]),
        # PWRAP @ 0x1000d000 -> MT6358 PMIC (EINT 144 / 0x90): PWRKEY = power, HOMEKEY = volume down.
        node('pwrap', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-pwrap')),
            ('reg', io(0x1000d000, 0x1000)),
        ], [node('pmic', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-pmic')),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(144, IRQ_LEVEL_HIGH)),
        ], [node('keys', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-keys')),
            ('keymap', u32(0, HID_CSMR_POWER, 1, HID_CSMR_VOLUME_DOWN)),
        ])])]),
        # GPIO controller @ 0x10005000 (pin 4 = tsp_io_ldo fixed_regulator@0 for FTS5CU56A touch).
        node('gpio', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-gpio')),
            ('reg', io(0x10005000, 0x1000)),
            ('output-pins', u32(4)),
        ]),
        # STMicroelectronics FTS5CU56A touch (iokit/Kernel/arm/hid/STMTS.cpp):
        # Direct GPIO89/90 open-drain bit-bang I2C @ 0x49, GPIO10 active-low IRQ, max_coords = 4095x4095.
        node('touch', [
            ph(),
            ('compatible', cstr('stm,stm_ts')),
            ('i2c-address', u32(0x49)),
            ('max-x', u32(4095)),
            ('max-y', u32(4095)),
            ('panel-size', u32(FB['width'], FB['height'])),
        ]),
        # eMMC MSDC0 @ 0x11230000 (SPI 0x64), 128 GB SAMSUNG DV6DAB (bsd/dev/mtk_emmc.c).
        node('emmc', [
            ph(),
            ('compatible', cstr('mediatek,mt6768-msdc0')),
            ('reg', io(0x11230000, 0x10000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x64, IRQ_LEVEL_HIGH)),
        ]),
        # Mali-G52 MC2 (Bifrost): mali@13040000, mfg_cfg@13000000, spm@10006000, topckgen@10000000,
        # apmixed@1000c000, infracfg_ao@10001000; IRQs SPI 0x112 (GPU), 0x113 (MMU), 0x114 (JOB).
        # MappleGPU.cpp stays idle unless boot-arg mp_gpu=1|2 is explicitly passed.
        node('gpu', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-mali')),
            ('reg', io(0x13040000, 0x4000) + io(0x13000000, 0x1000) + io(0x10006000, 0x1000) +
                    io(0x10000000, 0x1000) + io(0x1000c000, 0x1000) + io(0x10001000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x112, IRQ_LEVEL_HIGH) + spi(0x113, IRQ_LEVEL_HIGH) + spi(0x114, IRQ_LEVEL_HIGH)),
        ]),
        # CONNSYS MT6631 Wi-Fi/BT (consys@18002000, btif@1100c000).
        node('wlan', [
            ph(),
            ('device_type', cstr('wlan')),
            ('compatible', cstr('mediatek,mt6781-wlan')),
            ('local-mac-address', wifi_mac(ecid)),
            ('wifi-vendor', cstr('Murata')),
        ]),
    ]
