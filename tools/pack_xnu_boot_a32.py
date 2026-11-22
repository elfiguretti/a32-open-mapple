#!/usr/bin/env python3
"""
Pack shim-a32.Image (kernel, gzip) + xnu-a32.bin (ramdisk, MAPPLEPL payload) + stock DTB
into a Samsung Galaxy A32 4G (SM-A325M) header v2 boot.img and Odin/samloader .tar!

Usage:
  python3 pack_xnu_boot_a32.py \
      --stock-boot e:/a32-open-mapple/backup/magisk/inspect/boot.img \
      --shim       e:/a32-open-mapple/out/hito3/shim-a32.Image \
      --payload    e:/a32-open-mapple/out/hito3/xnu-a32.bin \
      --out-tar    e:/a32-open-mapple/out/xnu_a32_boot.tar
"""
import argparse
import gzip
import hashlib
import struct
import tarfile
from pathlib import Path

BOOT_PARTITION = 32 << 20


def pad(b: bytes, n: int) -> bytes:
    return b + b"\x00" * (-len(b) % n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock-boot", required=True, help="Backup of SM-A325M boot.img (header v2, source of stock DTB)")
    ap.add_argument("--shim", required=True, help="Compiled shim-a32.Image")
    ap.add_argument("--payload", required=True, help="MAPPLEPL payload (xnu-a32.bin)")
    ap.add_argument("--out-img", default="", help="Optional output boot.img path")
    ap.add_argument("--out-tar", required=True, help="Output Odin/samloader .tar path")
    args = ap.parse_args()

    d = Path(args.stock_boot).read_bytes()
    assert d[:8] == b"ANDROID!", "stock-boot is not an Android boot image"
    ksize, _, rsize, _, ssize, _, _, page, hver = struct.unpack_from("<9I", d, 8)
    assert hver == 2, f"expected header v2, got v{hver}"
    rdo_size = struct.unpack_from("<I", d, 1632)[0]
    dtb_size = struct.unpack_from("<I", d, 1648)[0]

    def pages(n: int) -> int:
        return (n + page - 1) // page * page

    off = page + pages(ksize) + pages(rsize) + pages(ssize) + pages(rdo_size)
    dtb = d[off : off + dtb_size]
    assert len(dtb) == dtb_size and dtb[:4] in (b"\xd0\x0d\xfe\xed", b"\xd7\xb7\xab\x1e"), (
        f"failed to extract valid FDT/DT_TABLE from stock-boot (got {dtb[:4].hex()})"
    )

    shim_raw = Path(args.shim).read_bytes()
    assert shim_raw[0x38:0x3C] == b"ARM\x64", "shim is not a valid arm64 Image"
    new_kernel = gzip.compress(shim_raw, 9, mtime=0)

    new_ramdisk = Path(args.payload).read_bytes()
    assert new_ramdisk[:8] == b"MAPPLEPL", "payload does not start with MAPPLEPL magic"

    hdr = bytearray(d[:page])
    struct.pack_into("<I", hdr, 8, len(new_kernel))
    struct.pack_into("<I", hdr, 16, len(new_ramdisk))
    struct.pack_into("<I", hdr, 24, 0)     # second_size = 0
    struct.pack_into("<I", hdr, 1632, 0)   # recovery_dtbo_size = 0

    sha = hashlib.sha1()
    for part in (new_kernel, new_ramdisk, b"", b"", dtb):
        sha.update(part)
        sha.update(struct.pack("<I", len(part)))
    hdr[576:608] = pad(sha.digest(), 32)

    img = bytes(hdr) + pad(new_kernel, page) + pad(new_ramdisk, page) + pad(dtb, page)
    if len(img) > BOOT_PARTITION:
        raise SystemExit(
            f"boot.img is {len(img)} bytes (> {BOOT_PARTITION} bytes partition limit by {len(img) - BOOT_PARTITION} bytes)"
        )

    out_tar = Path(args.out_tar).resolve()
    out_img = Path(args.out_img).resolve() if args.out_img else out_tar.with_suffix(".img")
    out_img.parent.mkdir(parents=True, exist_ok=True)
    out_img.write_bytes(img)

    with tarfile.open(out_tar, "w") as tf:
        tf.add(out_img, arcname="boot.img")

    print(
        f"Packed {out_tar} ({out_tar.stat().st_size / (1024*1024):.2f} MiB) | "
        f"boot.img={len(img)} B (shim.gz={len(new_kernel)} B, MAPPLEPL={len(new_ramdisk)} B, dtb={len(dtb)} B)"
    )


if __name__ == "__main__":
    main()
