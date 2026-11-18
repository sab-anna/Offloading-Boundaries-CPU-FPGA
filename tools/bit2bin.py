#!/usr/bin/env python3
"""bit2bin.py <in.bit> <out.bit.bin>

Converts a Vivado .bit file into the .bin layout loaded by xmutil / fpga_manager on the KV260:
the .bit header is removed and the bytes of every 32-bit word are swapped.
For the four-lane design this reproduces, byte for byte, the .bin file used in the measurements
(same layout as `bootgen -arch zynqmp -process_bitstream bin`).
"""
import struct
import sys


def bit_payload(data: bytes) -> bytes:
    j = 2 + struct.unpack('>H', data[0:2])[0] + 2       # skip the initial field and the 'a' key length
    while True:
        key = chr(data[j])
        j += 1
        if key == 'e':                                    # 'e' = configuration data, 32-bit length
            n = struct.unpack('>I', data[j:j + 4])[0]
            return data[j + 4:j + 4 + n]
        j += 2 + struct.unpack('>H', data[j:j + 2])[0]    # 'a'..'d' = design, part, date, time


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    payload = bit_payload(open(sys.argv[1], 'rb').read())
    swapped = b''.join(payload[i:i + 4][::-1] for i in range(0, len(payload), 4))
    with open(sys.argv[2], 'wb') as f:
        f.write(swapped)
    print(f'{sys.argv[2]}: {len(swapped)} bytes')


if __name__ == '__main__':
    main()
