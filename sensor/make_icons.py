#!/usr/bin/env python3
"""Generate the extension's PNG icons.

Chromium wants PNGs and the project has no image library, so this draws the
application icon's shapes directly and writes the PNGs itself, keeping the
build dependency-free.
"""
import pathlib, struct, sys, zlib

# Same shapes and colours as packaging/usual-install.svg, in its 64-unit space:
# a Breeze blue tile, a white download arrow, a Mint green tray.
TILE = (0x3D, 0xAE, 0xE9)
ARROW = (0xFF, 0xFF, 0xFF)
TRAY = (0xB5, 0xE8, 0xA3)
SUPERSAMPLE = 6


def _in_round_rect(x, y, x0, y0, x1, y1, r, top=True, bottom=True):
    """Inside a rectangle whose top and/or bottom corners are rounded by r."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    dx = max(x0 + r - x, 0, x - (x1 - r))
    if y < y0 + r:
        dy = (y0 + r - y) if top else 0
    elif y > y1 - r:
        dy = (y - (y1 - r)) if bottom else 0
    else:
        dy = 0
    return dx * dx + dy * dy <= r * r


def _in_triangle(x, y, a, b, c):
    def side(p, q):
        return (x - q[0]) * (p[1] - q[1]) - (p[0] - q[0]) * (y - q[1])
    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def colour_at(x, y):
    """Colour at a point in the icon's 64-unit space, or None if transparent."""
    if not _in_round_rect(x, y, 4, 4, 60, 60, 12):
        return None
    if 28 <= x <= 36 and 11 <= y <= 30:
        return ARROW
    if _in_triangle(x, y, (17, 27), (47, 27), (32, 42)):
        return ARROW
    if _in_round_rect(x, y, 13, 37, 51, 53, 3, top=False) and not (19 < x < 45 and y < 46):
        return TRAY
    return TILE


def glyph(size):
    """RGBA pixels for one icon: the vector shapes, supersampled for smooth edges."""
    n = SUPERSAMPLE
    scale = 64.0 / size
    px = []
    for py in range(size):
        row = []
        for qx in range(size):
            r = g = b = a = 0
            for sy in range(n):
                for sx in range(n):
                    c = colour_at((qx + (sx + 0.5) / n) * scale, (py + (sy + 0.5) / n) * scale)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 1
            if a:
                row.append((r // a, g // a, b // a, round(255 * a / (n * n))))
            else:
                row.append((0, 0, 0, 0))
        px.append(row)
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
