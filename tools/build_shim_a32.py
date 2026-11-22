#!/usr/bin/env python3
"""
Cross-platform builder for shim-a32.Image (SM-A325M Open Mapple boot shim).
Works on Windows (with tools/zig/zig.exe) and macOS/Linux (with clang + ld.lld or zig).

Usage:
  python3 build_shim_a32.py --ref-shim <path/to/mapple_loader/shim> --out <path/to/shim-a32.Image> [--check-abs <path/to/check-abs.py>]
"""
import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def find_toolchain():
    # Prefer local zig.exe on Windows, or clang + ld.lld on macOS/Linux
    zig_local = list((Path(__file__).resolve().parents[2] / "tools" / "zig").rglob("zig.exe"))
    if zig_local:
        z = str(zig_local[0])
        return [z, "cc", "-target", "aarch64-freestanding-none"], [z, "ld.lld"]
    if shutil.which("clang") and shutil.which("ld.lld"):
        return ["clang", "--target=aarch64-none-elf"], ["ld.lld"]
    if shutil.which("zig"):
        return ["zig", "cc", "-target", "aarch64-freestanding-none"], ["zig", "ld.lld"]
    raise SystemExit("No ARM64 compiler found (need zig or clang + ld.lld)")


def run(cmd):
    print("+", " ".join(str(x) for x in cmd), flush=True)
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-shim", required=True, help="Path to upstream mapple_loader/shim")
    ap.add_argument("--port-shim", default=str(Path(__file__).resolve().parents[1] / "mapple" / "loader" / "shim"))
    ap.add_argument("--out", required=True, help="Output Image path")
    ap.add_argument("--check-abs", default="", help="Optional path to mapple_build/tools/check-abs.py")
    ap.add_argument("--extra-cflag", action="append", default=[])
    args = ap.parse_args()

    ref = Path(args.ref_shim).resolve()
    port = Path(args.port_shim).resolve()
    out_img = Path(args.out).resolve()
    obj_dir = out_img.parent / "shim_a32_obj"
    obj_dir.mkdir(parents=True, exist_ok=True)

    cc, lld = find_toolchain()
    cflags = [
        *cc,
        "-O2", "-g0", "-ffreestanding", "-nostdlib", "-fno-pic",
        "-fno-asynchronous-unwind-tables", "-fno-unwind-tables",
        "-mgeneral-regs-only", "-mstrict-align", "-fno-stack-protector",
        "-Wall", "-Wextra",
        "-DMT6781_SHIM", "-DA32",
        f"-I{port}",
        f"-I{ref}",
        *args.extra_cflag,
    ]

    # Use our A32 mt6768.c & console.c, plus upstream start.S, fdt.c, holes.c, identity.c, loader.c
    sources = [
        ref / "start.S",
        port / "mt6768.c",
        port / "console.c",
        ref / "fdt.c",
        ref / "holes.c",
        ref / "identity.c",
        ref / "loader.c",
    ]
    objs = []
    c_objs = []
    for src in sources:
        o = obj_dir / (src.stem + ".o")
        run([*cflags, "-c", str(src), "-o", str(o)])
        objs.append(o)
        if src.suffix == ".c":
            c_objs.append(o)

    if args.check_abs and Path(args.check_abs).exists():
        run([sys.executable, args.check_abs, *[str(o) for o in c_objs]])

    elf_out = obj_dir / "shim-a32.elf"
    run([*lld, "-T", str(ref / "shim.ld"), "--build-id=none", "-N", *[str(o) for o in objs], "-o", str(elf_out)])
    run([*lld, "-T", str(ref / "shim.ld"), "--build-id=none", "-N", *[str(o) for o in objs], "--oformat=binary", "-o", str(out_img)])

    hdr = out_img.read_bytes()[:64]
    img_size = struct.unpack_from("<Q", hdr, 16)[0]
    with open(out_img, "r+b") as f:
        f.truncate(img_size)
    print(f"Built {out_img} ({out_img.stat().st_size} bytes, arm64 header image_size=0x{img_size:x})")


if __name__ == "__main__":
    main()
