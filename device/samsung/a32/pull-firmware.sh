#!/bin/bash
# Pull SM-A325M (a32) CONNSYS MT6631 (soc1_0_1a_1) firmware off YOUR rooted phone into fw-connsys/.
# Nothing of the vendor's is ever kept in git. Read-only on the phone.
set -e
S=${MAPPLE_SERIAL:?set MAPPLE_SERIAL to the serial of your phone, as adb devices shows it}
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE}"
TMP=/data/local/tmp/mapple-fw

CONNSYS="WIFI_RAM_CODE_soc1_0_1a_1.bin soc1_0_patch_mcu_1a_1_hdr.bin
	soc1_0_ram_bt_1a_1_hdr.bin soc1_0_ram_mcu_1a_1_hdr.bin
	soc1_0_ram_wifi_1a_1_hdr.bin WMT_SOC.cfg BT_FW.cfg wifi.cfg"

adb -s "$S" shell su <<EOF
set -e
rm -rf $TMP; mkdir -p $TMP/connsys
for f in $(echo $CONNSYS); do
    [ -f /vendor/firmware/\$f ] && cp /vendor/firmware/\$f $TMP/connsys/
done
chmod 644 $TMP/connsys/*
EOF

mkdir -p "$OUT/fw-connsys"
adb -s "$S" pull "$TMP/connsys/." "$OUT/fw-connsys/" >/dev/null
echo "rm -rf $TMP" | adb -s "$S" shell su
ls -l "$OUT/fw-connsys"
