"""Outline of a PNG (which cells are non-transparent), to compare a printer's plate preview with 3MF plate previews.

Bambu renders both the same way (same camera, transparent background); only the filament colour differs, so the
overlap of the outlines identifies the plate. Standard library only.
"""
import struct
import zlib
from pathlib import Path


def alpha_mask(path_or_bytes, size=48):
    """Set of (x, y) cells on a size x size grid that are covered (alpha > 16, or non-white without alpha)."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else Path(path_or_bytes).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w = 8, b"", None
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace:
                raise ValueError("only 8-bit, non-interlaced PNGs")
            bpp = {6: 4, 2: 3, 4: 2, 0: 1}[ctype]
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + n
    if w is None:
        raise ValueError("no IHDR")
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    cells = set()
    for y in range(0, h, 2):
        row = rows[y]
        for x in range(0, w, 2):
            on = row[x * 4 + 3] > 16 if bpp == 4 else min(row[x * bpp:(x + 1) * bpp]) < 235
            if on:
                cells.add((x * size // w, y * size // h))
    return cells


def iou(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0
