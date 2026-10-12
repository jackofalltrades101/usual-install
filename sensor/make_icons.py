#!/usr/bin/env python3
"""Generate the extension's PNG icons.

Chromium wants PNGs and the project has no image library, so this writes them
directly: a download arrow landing in a package, matching the application icon.
Small enough to be obvious, and it keeps the build dependency-free.
"""
import pathlib, struct, sys, zlib

FG = (0x2D, 0x7F, 0xF9, 255)      # the blue used for the primary action
MUTED = (0x2D, 0x7F, 0xF9, 150)


def glyph(size):
    """RGBA pixels for one icon, drawn from proportions rather than a bitmap."""
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    u = size / 16.0

    def rect(x0, y0, x1, y1, c):
        for y in range(max(0, int(y0)), min(size, int(y1))):
            for x in range(max(0, int(x0)), min(size, int(x1))):
                px[y][x] = c

    t = max(1, round(u))                       # stroke thickness
    # box
    bx0, bx1, by0, by1 = 1.5 * u, 14.5 * u, 9.5 * u, 14.5 * u
    rect(bx0, by0, bx1, by0 + t, FG)
    rect(bx0, by1 - t, bx1, by1, FG)
    rect(bx0, by0, bx0 + t, by1, FG)
    rect(bx1 - t, by0, bx1, by1, FG)
    # shaft
    rect(8 * u - t / 2, 1.5 * u, 8 * u + t / 2 + (t % 2), 7.5 * u, FG)
    # arrowhead
    span = 3.2 * u
    for i in range(int(span)):
        y = 7.5 * u - span + i
        # Widest at the top, converging to the tip at the bottom: a download
        # arrow, not a Y.
        half = (span - i) * 0.85
        rect(8 * u - half - t / 2, y, 8 * u - half + t / 2 + (t % 2), y + 1, FG)
        rect(8 * u + half - t / 2, y, 8 * u + half + t / 2 + (t % 2), y + 1, FG)
    # seam
    rect(bx0 + t, (by0 + by1) / 2, bx1 - t, (by0 + by1) / 2 + max(1, t // 2), MUTED)
    return px


def write_png(path, px):
    size = len(px)
    raw = b"".join(b"\x00" + b"".join(struct.pack("4B", *p) for p in row) for row in px)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    return len(png)


def main():
    out = pathlib.Path(__file__).resolve().parent / "icons"
    out.mkdir(exist_ok=True)
    for size in (16, 32, 48, 128):
        n = write_png(out / f"icon{size}.png", glyph(size))
        print(f"  icons/icon{size}.png  {n} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
