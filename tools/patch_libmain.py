#!/usr/bin/env python3
import sys

def patch_libmain(path):
    with open(path, 'rb') as f:
        data = bytearray(f.read())

    old_pattern = bytes.fromhex('68 06 00 f0 69 06 00 f0 08 11 44 f9 29 15 44 f9 14 01 00 b9 e8 13 40 b9 28 01 00 b9')
    new_pattern = bytes.fromhex('68 06 00 f0 08 11 44 f9 94 d2 9a 52 74 0e a0 72 14 01 00 b9 1f 20 03 d5 1f 20 03 d5')

    pos = data.find(old_pattern)
    if pos != -1:
        data[pos:pos+len(new_pattern)] = new_pattern
        with open(path, 'wb') as f:
            f.write(data)
        print(f'Successfully patched {path} at offset {hex(pos)} to force entry point 0x0073d694')
    else:
        print(f'Warning: pattern not found in {path}')

if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'android/app/src/main/jniLibs/arm64-v8a/libmain.so'
    patch_libmain(target)
