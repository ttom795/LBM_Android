#!/usr/bin/env python3
"""Converts prebuilt libmain.so into libd3db.so for resolving d3dx9 and bink symbols."""
import sys

def make_d3db(src, dst):
    with open(src, "rb") as f:
        d = bytearray(f.read())

    patches = [
        (b"libmain.so\x00", b"libd3db.so\x00"),
    ]

    for old_s, new_s in patches:
        idx = d.find(old_s)
        if idx != -1:
            d[idx:idx+len(new_s)] = new_s
            print(f"Patched {old_s} -> {new_s} at {hex(idx)}")
        else:
            print(f"Warning: {old_s} not found")

    with open(dst, "wb") as f:
        f.write(d)
    print(f"Successfully generated {dst}")

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/libs/libmain.so"
    dst = sys.argv[2] if len(sys.argv) > 2 else "android/app/src/main/jniLibs/arm64-v8a/libd3db.so"
    make_d3db(src, dst)
