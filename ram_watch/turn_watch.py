from pathlib import Path
import struct

files = [
    "turn0_filtered.bin",
    "turn1_filtered.bin",
    "turn2_filtered.bin"
]

data = [Path(f).read_bytes() for f in files]

min_len = min(len(d) for d in data)


candidates = []

for i in range(0, min_len, 4):  # u32 aligned
    vals = [
        struct.unpack_from("<I", d, i)[0]
        for d in data
    ]

    if len(set(vals)) == 3:  # all different across turns
        candidates.append((i, vals))

for off, vals in candidates:
    print(hex(off), ":", [hex(v) for v in vals])