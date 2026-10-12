#!/usr/bin/env python3
"""Generate the Usual Install catalogue from Flathub AppStream metadata.

No hand-maintained data. Every entry is derived from what the vendor declared
as <url type="homepage"> in their own AppStream file.

    ./build_catalogue.py -o catalogue.json
"""
import argparse, collections, gzip, io, json, pathlib, sys, time, urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

APPSTREAM = "https://dl.flathub.org/repo/appstream/{arch}/appstream.xml.gz"
APP_TYPES = {"desktop-application", "console-application", "desktop"}

# Registrable-domain suffixes needing three labels. A real deployment should use
# the Public Suffix List; this covers the common cases without a dependency.
TWO_LABEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "co.nz", "com.br", "co.in", "co.za", "com.mx", "com.tr",
}


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
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def norm_path(path):
    """Normalise to a comparable prefix: leading slash, collapsed, no trailing slash."""
    if not path:
        return ""
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    return path


def fetch(arch):
    url = APPSTREAM.format(arch=arch)
    sys.stderr.write(f"fetching {url}\n")
    with urllib.request.urlopen(url, timeout=300) as r:
        raw = r.read()
    sys.stderr.write(f"  {len(raw)/1e6:.1f} MB compressed\n")
    return gzip.decompress(raw)


XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def native_hint(app_id):
    """Likely distro package name for a Flathub id: org.inkscape.Inkscape -> inkscape."""
    last = app_id.split(".")[-1]
    if last.lower() in ("desktop", "client", "app") and app_id.count(".") >= 2:
        last = app_id.split(".")[-2]
    return last.lower()


def text_of(component, tag):
    """Text of the *unlocalised* element.

    AppStream ships one <name>/<summary> per translation -- Loupe has 59 -- and
    ElementTree's find() returns whichever comes first in document order, which
    is alphabetical by language code. That made the installer window announce
    "عارض الصور" to every user. The canonical English string is the one with no
    xml:lang attribute.
    """
    chosen = None
    for el in component.findall(tag):
        if el.text is None:
            continue
        if el.get(XML_LANG) is None:
            chosen = el
            break
        if chosen is None:
            chosen = el          # fall back to any translation if none is plain
    if chosen is None or not chosen.text:
        return None
    return " ".join(chosen.text.split())


VERIFIED_KEY = "flathub::verification::verified"


def is_verified(component):
    """Whether Flathub has verified this app's publisher as its developer.

    Flathub records it as <custom><value key="flathub::verification::verified">
    true</value></custom> (GNOME Software reads the same key). Unverified apps
    are often community repackages of a vendor's own program -- Minecraft's is --
    and the installer window must say so rather than imply the vendor ships it.
    """
    for v in component.iter("value"):
        if v.get("key") == VERIFIED_KEY:
            return (v.text or "").strip().lower() == "true"
    return False


def build(xml_bytes, remote):
    root = ET.parse(io.BytesIO(xml_bytes)).getroot()
    hosts = collections.defaultdict(list)
    domains = collections.defaultdict(list)
    stats = collections.Counter()

    for comp in root.findall("component"):
        stats["components"] += 1
        if comp.get("type") not in APP_TYPES:
            stats["skipped_not_app"] += 1
            continue
        app_id = text_of(comp, "id")
        home = text_of(comp, "url[@type='homepage']")
        if not app_id or not home:
            stats["skipped_no_homepage"] += 1
            continue
        try:
            parsed = urlparse(home)
        except ValueError:
            stats["skipped_bad_url"] += 1
            continue
        if not parsed.hostname:
            stats["skipped_bad_url"] += 1
            continue

        entry = {
            "id": app_id,
            "name": text_of(comp, "name") or app_id,
            "summary": text_of(comp, "summary") or "",
            "backend": "flatpak",
            "remote": remote,
            "verified": is_verified(comp),
            # A *guess* at the distro package name, never trusted on its own.
            # Before installing it, usuald asks the local package manager what
            # homepage that package declares and requires it to match the
            # domain the user was actually on.
            "native_hint": native_hint(app_id),
        }
        host = norm_host(parsed.hostname)
        entry["path"] = norm_path(parsed.path)
        hosts[host].append(entry)
        # The domain index carries the path too, so resolution step 3 can apply
        # the same path_names_the_app guard the hostname steps use.
        domains[registrable(parsed.hostname)].append(entry)
        stats["indexed"] += 1

    # Dedupe by app id within each bucket; several AppStream components can share one.
    def dedupe(bucket):
        out = {}
        for e in bucket:
            out.setdefault((e["id"], e.get("path", "")), e)
        return sorted(out.values(), key=lambda e: (e.get("path", ""), e["id"]))

    return (
        {h: dedupe(v) for h, v in hosts.items()},
        {d: dedupe(v) for d, v in domains.items()},
        stats,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="catalogue.json")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--remote", default="flathub")
    ap.add_argument("--from-file", help="use a local appstream.xml instead of fetching")
    ap.add_argument("--sign-key", help="Ed25519 private key; writes <out>.sig alongside")
    ap.add_argument("--max-candidates", type=int, default=8,
                    help="buckets larger than this are treated as shared hosting "
                         "and never resolved (github.com, sourceforge.net, ...)")
    args = ap.parse_args()

    if args.from_file:
        xml_bytes = pathlib.Path(args.from_file).read_bytes()
    else:
        xml_bytes = fetch(args.arch)

    hosts, domains, stats = build(xml_bytes, args.remote)
    catalogue = {
        "schema": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"appstream": APPSTREAM.format(arch=args.arch), "arch": args.arch},
        "max_candidates": args.max_candidates,
        "hosts": hosts,
        "domains": domains,
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(catalogue, indent=1, sort_keys=True))

    if args.sign_key:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from sign import sign as sign_file
        sys.stderr.write(f"signed -> {sign_file(out, args.sign_key)}\n")

    unamb = sum(1 for v in domains.values() if len(v) == 1)
    sys.stderr.write(
        f"\n{out}  ({out.stat().st_size/1e6:.1f} MB)\n"
        f"  indexed {stats['indexed']} apps\n"
        f"  {len(hosts)} hostnames, {len(domains)} registrable domains\n"
        f"  {unamb} domains resolve to exactly one app "
        f"({100*unamb/max(len(domains),1):.1f}%)\n"
        f"  skipped: {stats['skipped_no_homepage']} without homepage, "
        f"{stats['skipped_not_app']} non-application\n"
    )


if __name__ == "__main__":
    main()
