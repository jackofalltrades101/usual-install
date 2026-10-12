#!/usr/bin/env python3
"""Domain -> package resolution for Usual Install.

The single place a package is chosen. Deliberately dependency-free so the
session agent, the tests, and any future daemon all share one implementation.

Resolution is most-specific-first:

    1. exact hostname + longest matching path prefix
    2. exact hostname (root)
    3. registrable domain

A bucket larger than `max_candidates` is shared hosting -- github.com,
sourceforge.net, apps.gnome.org at its root -- and never resolves. A bucket
with one entry installs directly. A bucket with a few entries becomes a
picker in the installer window, which is the correct outcome for
jetbrains.com/idea/ (Community vs Ultimate) or steampowered.com
(Steam vs Steam Link).

Run directly to execute the self-tests:  ./resolve.py --self-test
"""
import re
from urllib.parse import urlparse

# Vendor sites routinely prefix paths with a locale, so the page the user is on
# is /en-US/firefox/new/ while the app declared /firefox/ as its homepage.
# 61% of catalogue entries carry a path, so this is not an edge case.
LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")

# A reverse-DNS-looking token: starts with a letter, has two or more dots, and
# has letters after the final dot. Matches "eu.jumplink.Learn6502" but not a
# version string like "7.5.2" or "libreoffice-7.5.2".
REVERSE_DNS = re.compile(r"^[A-Za-z][\w-]*(?:\.[\w-]+){1,}\.[\w-]*[A-Za-z][\w-]*$")

TWO_LABEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "co.nz", "com.br", "co.in", "co.za", "com.mx", "com.tr",
}

# A download is only a candidate for interception if it looks like something a
# user would otherwise have to install by hand. Derived from the filename, so a
# PDF or a screenshot from a vendor's own domain is left alone.
INSTALLER_SUFFIXES = (
    ".deb", ".rpm", ".appimage", ".flatpakref", ".flatpak", ".snap",
    ".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".txz", ".run", ".bin",
    ".pkg.tar.zst", ".pkg.tar.xz", ".sh", ".zip",
)

# A Windows installer means the vendor offers no Linux build on this page. That
# is the trigger for the compatibility-layer path, never for a native install.
WINDOWS_SUFFIXES = (".exe", ".msi")


def registrable(host):
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in TWO_LABEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def norm_host(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def norm_path(path):
    if not path:
        return ""
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    return path


def looks_installable(filename, url=""):
    """True if this download is plausibly something we could replace."""
    name = (filename or "").lower()
    if not name:
        name = norm_path(urlparse(url).path).rsplit("/", 1)[-1].lower()
    return name.endswith(INSTALLER_SUFFIXES)


def strip_locale(path):
    """Drop up to two leading locale segments: /en-US/firefox/new -> /firefox/new."""
    parts = [p for p in path.split("/") if p]
    dropped = 0
    while parts and dropped < 2 and LOCALE_SEGMENT.match(parts[0]):
        parts.pop(0)
        dropped += 1
    return ("/" + "/".join(parts)) if parts else ""


def path_names_the_app(entry):
    """True when the homepage path identifies the app itself.

    flathub.org/apps/eu.jumplink.Learn6502 is a per-app page in a directory, so
    a *different* path on flathub.org is a different app and must never fall
    back to this entry. libreoffice.org/discover/libreoffice is a vendor
    section, so /download/... on the same host is still LibreOffice.

    Without this guard, flathub.org/apps/org.kde.kdenlive resolved to
    "Learn 6502 Assembly" -- a wrong-app match, the worst failure mode here.
    """
    path = entry.get("path", "")
    if not path:
        return False
    app_id = (entry.get("id") or "").lower()
    base_id = app_id[:-8] if app_id.endswith(".desktop") else app_id
    for seg in (s for s in path.split("/") if s):
        low = seg.lower()
        if base_id and low in (app_id, base_id):
            return True
        if seg.count(".") >= 2 and REVERSE_DNS.match(seg):
            return True
    return False


def looks_windows(filename, url=""):
    """True if this download is a Windows installer."""
    name = (filename or "").lower()
    if not name:
        name = norm_path(urlparse(url).path).rsplit("/", 1)[-1].lower()
    return name.endswith(WINDOWS_SUFFIXES)


def _path_prefix_matches(entry_path, page_path):
    """Does the app's homepage path cover the page the user is on?"""
    if not entry_path:
        return False
    return page_path == entry_path or page_path.startswith(entry_path + "/")


def resolve(page_url, catalogue):
    """Return (candidates, reason). candidates is a possibly-empty list."""
    try:
        parsed = urlparse(page_url)
    except ValueError:
        return [], "unparseable page url"
    host = norm_host(parsed.hostname)
    if not host:
        return [], "no hostname"

    limit = catalogue.get("max_candidates", 8)
    page_path = norm_path(parsed.path)

    host_bucket = catalogue.get("hosts", {}).get(host, [])

    # 1. hostname + longest matching path prefix, tried against the path as-is
    #    and with any locale prefix removed.
    forms = [page_path]
    delocalised = strip_locale(page_path)
    if delocalised != page_path:
        forms.append(delocalised)
    matched = [e for e in host_bucket
               if any(_path_prefix_matches(e.get("path", ""), f) for f in forms)]
    if matched:
        longest = max(len(e["path"]) for e in matched)
        best = _dedupe([e for e in matched if len(e["path"]) == longest])
        if len(best) <= limit:
            return best, f"hostname+path {host}{best[0]['path']}"
        return [], f"{len(best)} apps share {host}{best[0]['path']}; shared hosting"

    # 2. hostname root
    if host_bucket:
        rooted = _dedupe([e for e in host_bucket if not e.get("path")])
        if rooted and len(rooted) <= limit:
            return rooted, f"hostname {host}"
        # Last resort on this host: an app declares it as home but under a
        # path that does not cover this page. Only safe for entries whose path
        # is a vendor section, never for per-app directory pages.
        loose = _dedupe([e for e in host_bucket if not path_names_the_app(e)])
        if loose and len(loose) <= limit:
            return loose, f"hostname {host} (unmatched path)"

    # 3. registrable domain
    dom = registrable(host)
    dom_bucket = _dedupe([e for e in catalogue.get("domains", {}).get(dom, [])
                          if not path_names_the_app(e)])
    if not dom_bucket:
        return [], f"no catalogue entry for {dom}"
    if len(dom_bucket) > limit:
        return [], f"{len(dom_bucket)} apps share {dom}; shared hosting, not a vendor page"
    return dom_bucket, f"domain {dom}"


def _dedupe(entries):
    out = {}
    for e in entries:
        out.setdefault(e["id"], e)
    return sorted(out.values(), key=lambda e: e["id"])


# --------------------------------------------------------------------------- #

def _self_test():
    cat = {
        "max_candidates": 8,
        "hosts": {
            "zoom.us": [{"id": "us.zoom.Zoom", "name": "Zoom", "path": ""}],
            "jetbrains.com": [
                {"id": "com.jetbrains.IntelliJ-IDEA-Community", "name": "IntelliJ CE", "path": "/idea"},
                {"id": "com.jetbrains.IntelliJ-IDEA-Ultimate", "name": "IntelliJ U", "path": "/idea"},
                {"id": "com.jetbrains.PyCharm", "name": "PyCharm", "path": "/pycharm"},
            ],
            "apps.gnome.org": [
                {"id": f"org.gnome.App{i}", "name": f"App{i}", "path": f"/App{i}"} for i in range(40)
            ],
            "github.com": [{"id": f"io.github.p{i}", "name": f"p{i}", "path": ""} for i in range(900)],
            # Regression: a directory host whose single entry names the app in
            # its path. Any other path here is a different app, not this one.
            "flathub.org": [{"id": "eu.jumplink.Learn6502", "name": "Learn 6502",
                             "path": "/apps/eu.jumplink.Learn6502"}],
            # A vendor section path, by contrast, should still cover the host.
            "libreoffice.org": [{"id": "org.libreoffice.LibreOffice", "name": "LibreOffice",
                                 "path": "/discover/libreoffice"}],
        },
        "domains": {
            "zoom.us": [{"id": "us.zoom.Zoom", "name": "Zoom"}],
            "jetbrains.com": [{"id": f"com.jetbrains.P{i}", "name": f"P{i}"} for i in range(10)],
            "gnome.org": [{"id": f"org.gnome.App{i}", "name": f"App{i}"} for i in range(40)],
            "github.com": [{"id": f"io.github.p{i}", "name": f"p{i}"} for i in range(900)],
            "flathub.org": [{"id": "eu.jumplink.Learn6502", "name": "Learn 6502",
                             "path": "/apps/eu.jumplink.Learn6502"}],
            "libreoffice.org": [{"id": "org.libreoffice.LibreOffice", "name": "LibreOffice",
                                 "path": "/discover/libreoffice"}],
        },
    }

    cases = [
        ("https://zoom.us/download", 1, "exact vendor domain"),
        ("https://www.zoom.us/download?os=linux", 1, "www stripped, query ignored"),
        ("https://jetbrains.com/pycharm/download/", 1, "path picks one product"),
        ("https://www.jetbrains.com/idea/download/", 2, "genuine ambiguity -> picker"),
        ("https://apps.gnome.org/App7/", 1, "umbrella domain recovered by path"),
        ("https://apps.gnome.org/", 0, "umbrella root is shared hosting"),
        ("https://github.com/someone/project/releases", 0, "forge never resolves"),
        ("https://example.invalid/download", 0, "unknown domain is a no-op"),
        ("https://www.mozilla.org/en-US/firefox/new/", 0, "locale prefix (no entry here)"),
        # Wrong-app regression: this resolved to "Learn 6502 Assembly" before
        # path_names_the_app existed.
        ("https://flathub.org/apps/org.kde.kdenlive", 0, "directory page, different app"),
        ("https://flathub.org/apps/eu.jumplink.Learn6502", 1, "directory page, exact app"),
        ("https://www.libreoffice.org/download/download-libreoffice/", 1,
         "vendor section path still covers the host"),
    ]
    failures = 0
    for url, expected, label in cases:
        got, reason = resolve(url, cat)
        ok = len(got) == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label:38} {len(got)} (want {expected})  [{reason}]")

    print()
    for name, want_win in [("PhotoshopSetup.exe", True), ("office.msi", True),
                           ("zoom_amd64.deb", False), ("notes.pdf", False)]:
        ok = looks_windows(name) == want_win
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  windows({name!r:24}) -> {looks_windows(name)}")

    files = [
        ("zoom_amd64.deb", True), ("Obsidian.AppImage", True),
        ("app.tar.gz", True), ("whitepaper.pdf", False),
        ("screenshot.png", False), ("", False),
    ]
    for name, expected in files:
        ok = looks_installable(name) == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  installable({name!r:24}) -> {looks_installable(name)}")

    print(f"\n{'all passed' if not failures else str(failures) + ' FAILURES'}")
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if _self_test() else 0)
