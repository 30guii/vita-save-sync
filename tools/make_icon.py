"""Gera ui/icon.ico (o anel azul do logótipo) sem bibliotecas extra."""
import math
import os
import struct
import zlib

SIZES = (16, 32, 48, 256)


def ring_png(n: int) -> bytes:
    rows = []
    c = (n - 1) / 2
    r_out, r_in = n * 0.46, n * 0.20
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            dx, dy = x - c, y - c
            d = math.hypot(dx, dy)
            # anti-aliasing simples nas duas bordas do anel
            a = max(0.0, min(1.0, r_out - d + 0.5)) * max(0.0, min(1.0, d - r_in + 0.5))
            t = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
            mix = 0.5 + 0.5 * math.cos(t * 2 * math.pi)      # azul -> ciano à volta do anel
            rr = int(47 + (63 - 47) * mix)
            gg = int(123 + (214 - 123) * mix)
            bb = 255
            # fundo escuro arredondado por trás
            bg = max(0.0, min(1.0, n * 0.5 - d + 0.5))
            R = int(rr * a + 7 * (1 - a))
            G = int(gg * a + 11 * (1 - a))
            B = int(bb * a + 20 * (1 - a))
            A = int(255 * max(a, bg))
            row += bytes((R, G, B, A))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main(out):
    pngs = [ring_png(n) for n in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, body = b"", b""
    for n, png in zip(SIZES, pngs):
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    with open(out, "wb") as f:
        f.write(header + entries + body)


if __name__ == "__main__":
    main(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "icon.ico"))
