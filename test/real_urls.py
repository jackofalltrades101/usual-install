#!/usr/bin/env python3
"""Resolve realistic vendor download URLs against the real catalogue.

Not unit tests -- this is the honesty check. It prints what Usual Install would
actually do on pages a user visits, including the cases it declines.
"""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "resolver"))
from resolve import resolve, looks_installable

cat = json.loads((ROOT / "catalogue" / "catalogue.json").read_text())

SHOULD_INSTALL = [
    "https://zoom.us/download",
    "https://slack.com/downloads/linux",
    "https://discord.com/download",
    "https://www.spotify.com/us/download/linux/",
    "https://obsidian.md/download",
    "https://signal.org/download/",
    "https://desktop.telegram.org/",
    "https://www.blender.org/download/",
    "https://www.libreoffice.org/download/download-libreoffice/",
    "https://www.audacityteam.org/download/",
    "https://krita.org/en/download/",
    "https://www.inkscape.org/release/",
    "https://apps.gnome.org/Loupe/",
    "https://kdenlive.org/en/download/",
    "https://www.jetbrains.com/pycharm/download/",
    # Locale-prefixed paths must match the same as unprefixed ones.
    "https://www.mozilla.org/en-US/firefox/new/",
    "https://www.mozilla.org/de/firefox/new/",
    # Path precision: the generic page and the sub-product page differ.
    "https://store.steampowered.com/about/",
    "https://store.steampowered.com/steamlink/about/",
]
SHOULD_PICK = [
    "https://www.jetbrains.com/idea/download/",
    "https://vscodium.com/",
]
SHOULD_DECLINE = [
    "https://github.com/obsidianmd/obsidian-releases/releases",
    "https://sourceforge.net/projects/sevenzip/files/",
    "https://apps.gnome.org/",
    "https://www.kernel.org/",
    "https://news.ycombinator.com/",
    "https://en.wikipedia.org/wiki/Zoom",
]

def show(url):
    got, reason = resolve(url, cat)
    names = ", ".join(f"{c['name']} ({c['id']})" for c in got[:3])
    if len(got) > 3: names += f", +{len(got)-3} more"
    return len(got), names, reason

fails = 0
for label, urls, check in [
    ("INSTALLS DIRECTLY (expect exactly 1)", SHOULD_INSTALL, lambda n: n == 1),
    ("SHOWS A PICKER (expect 2+)", SHOULD_PICK, lambda n: n >= 2),
    ("DECLINES, download proceeds (expect 0)", SHOULD_DECLINE, lambda n: n == 0),
]:
    print(f"\n=== {label} ===")
    for url in urls:
        n, names, reason = show(url)
        ok = check(n)
        fails += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {url}")
        print(f"       -> {names or '(nothing)'}   [{reason}]")

# Pages the design cannot serve, listed rather than hidden. These are Linux app
# directories, not vendor download pages -- the app declares its own domain as
# its homepage, so the directory page has no catalogue entry. They already have
# their own install buttons, so declining is defensible, but it is a real
# inconsistency: apps.gnome.org/Loupe/ resolves and apps.kde.org/kdenlive/ does not.
KNOWN_GAPS = [
    "https://apps.kde.org/kdenlive/",
    "https://flathub.org/apps/org.kde.kdenlive",
]
print(f"\n=== KNOWN GAPS (informational, not failures) ===")
for url in KNOWN_GAPS:
    n, names, reason = show(url)
    print(f"  {'resolves' if n else 'declines'}  {url}")
    print(f"       -> {names or '(nothing)'}   [{reason}]")

print(f"\n=== filename gate ===")
for f in ["zoom_amd64.deb", "Obsidian-1.5.3.AppImage", "slack-desktop.rpm",
          "pricing.pdf", "logo.svg", "release-notes.html"]:
    print(f"  {'intercept' if looks_installable(f) else 'ignore   '}  {f}")

print(f"\n{'ALL EXPECTATIONS MET' if not fails else str(fails) + ' UNMET'}")
sys.exit(1 if fails else 0)
