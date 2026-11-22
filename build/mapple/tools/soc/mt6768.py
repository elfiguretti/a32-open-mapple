# MediaTek MT6768 / MT6769T (Helio G80 / G85), for mkadt.py: its blocks, and the names the MediaTek kernel line matches.
# Addresses verified directly from SM-A325M live.dtb (A325MUBSBDYC1) and dumps/iomem.txt.
NAMES = dict(platform='mapple,platform', io='mapple-io', display='mapple,display', fbcon='mapple-fbcon',
             ufs='mapple-emmc', firmware='mapple-shim')
# The root's SoC entry and the cores' compatible follow Open Mapple's MediaTek convention; nothing reads them.
ROOT_COMPAT = 'qcom,sm7150'
CPU_COMPAT = ('qcom,kryo470', 'ARM,v8')
GICD = (0x0c000000, 0x40000)
GICR = (0x0c040000, 0x200000)
TIMEBASE = 13000000
VTIMER_INTID = 27       # DT timer <GIC_PPI 11>, standard virtual timer
CPU_HZ = (1800000000, 2000000000)       # 6x Cortex-A55 @ 1.8 GHz, 2x Cortex-A75 @ 2.0 GHz
BIG_FROM = 6
# eMMC MSDC0 controller on MT6768/MT6769 (msdc@11230000, SPI 0x64)
UFS = ('mediatek,mt6768-msdc0', 0x11230000, 0x10000)
