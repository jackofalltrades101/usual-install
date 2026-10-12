"""Terminal output in the same voice as the windows.

Colour only when writing to a terminal and NO_COLOR is unset; piped output stays
plain, so scripts and tests that read it are unaffected. The colours are the
windows' own: Breeze blue for headings, Mint green for success, Breeze orange
and red for warnings and failures.
"""
import os, sys

_RGB = {"blue": (61, 174, 233), "green": (47, 138, 70), "orange": (246, 116, 0),
        "red": (218, 68, 83)}


def _on(stream):
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _paint(text, colour=None, bold=False, dim=False, stream=sys.stdout):
    if not _on(stream):
        return text
    codes = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if colour:
        codes.append("38;2;%d;%d;%d" % _RGB[colour])
    return f"\033[{';'.join(codes)}m{text}\033[0m" if codes else text


def heading(text, stream=sys.stdout):
    print(_paint(text, "blue", bold=True, stream=stream), file=stream)


def section(text, stream=sys.stdout):
    print(file=stream)
    print(_paint(text, bold=True, stream=stream), file=stream)


def row(key, value, width=16, stream=sys.stdout):
    print(f"  {_paint(key.rjust(width), dim=True, stream=stream)}  {value}", file=stream)


def good(text, stream=sys.stdout):
    return _paint(text, "green", stream=stream)


def warn(text, stream=sys.stdout):
    return _paint(text, "orange", stream=stream)


def bad(text, stream=sys.stdout):
    return _paint(text, "red", stream=stream)


def muted(text, stream=sys.stdout):
    return _paint(text, dim=True, stream=stream)


def result(ok, text, stream=sys.stdout):
    mark = good("✓", stream) if ok else bad("✕", stream)
    print(f"{mark} {text}", file=stream)
