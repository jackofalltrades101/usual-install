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
AUR_META = "https://aur.archlinux.org/packages-meta-v1.json.gz"
AUR_MIN_VOTES = 10
AUR_VCS = ("-git", "-hg", "-svn", "-bzr", "-darcs", "-fossil", "-cvs")
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


def fetch_aur():
    sys.stderr.write(f"fetching {AUR_META}\n")
    req = urllib.request.Request(AUR_META, headers={"User-Agent": "usual-install"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    sys.stderr.write(f"  {len(raw)/1e6:.1f} MB compressed\n")
    return json.loads(gzip.decompress(raw))


def aur_entries(meta, min_votes=AUR_MIN_VOTES):
    """Vetted AUR packages, keyed like Flathub entries by their declared URL.

    The AUR's URL field is set by whoever maintains the package, so the bar is
    higher than for Flathub: maintained, not flagged out of date, not a VCS
    build, at least `min_votes` votes. Where several packages claim the same
    site and path (minecraft-launcher and a fork, say), only the most-voted is
    kept: this is "the AUR's package for that site", not a search result.
    """
    best = {}
    stats = collections.Counter()
    for p in meta:
        name, url = p.get("Name") or "", p.get("URL") or ""
        if not p.get("Maintainer"):
            stats["orphaned"] += 1; continue
        if p.get("OutOfDate"):
            stats["out_of_date"] += 1; continue
        if name.endswith(AUR_VCS):
            stats["vcs"] += 1; continue
        if (p.get("NumVotes") or 0) < min_votes:
            stats["few_votes"] += 1; continue
        try:
            parsed = urlparse(url)
        except ValueError:
            stats["bad_url"] += 1; continue
        if not parsed.hostname or parsed.scheme not in ("http", "https"):
            stats["bad_url"] += 1; continue
        key = (norm_host(parsed.hostname), norm_path(parsed.path))
        if key in best and best[key]["votes"] >= p["NumVotes"]:
            continue
        best[key] = {
            "id": f"aur:{name}",
            "name": name,
            "summary": p.get("Description") or "",
            "backend": "aur",
            "aur_name": name,
            "pkgbase": p.get("PackageBase") or name,
            "maintainer": p["Maintainer"],
            "votes": p["NumVotes"],
            "updated": p.get("LastModified"),
            "snapshot": p.get("URLPath") or "",
            "host": key[0],
            "path": key[1],
            "registrable": registrable(parsed.hostname),
        }
    stats["kept"] = len(best)
    return list(best.values()), stats


def merge_aur(hosts, domains, entries):
    for e in entries:
        host, dom = e.pop("host"), e.pop("registrable")
        # Show the product's real name, not the package name: if exactly one
        # Flathub app claims the same site and path, the AUR entry is the same
        # program ("Minecraft", not "minecraft-launcher").
        same = {x["name"] for x in hosts.get(host, [])
                if x.get("backend") == "flatpak" and x.get("path", "") == e["path"]}
        if len(same) == 1:
            e["name"] = same.pop()
        hosts.setdefault(host, []).append(e)
        domains.setdefault(dom, []).append(e)
    key = lambda e: (e.get("path", ""), e["id"])
    for bucket in (hosts, domains):
        for k in bucket:
            bucket[k] = sorted(bucket[k], key=key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="catalogue.json")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--remote", default="flathub")
    ap.add_argument("--from-file", help="use a local appstream.xml instead of fetching")
    ap.add_argument("--sign-key", help="Ed25519 private key; writes <out>.sig alongside")
    ap.add_argument("--no-aur", action="store_true",
                    help="leave AUR packages out of the catalogue")
    ap.add_argument("--aur-from-file",
                    help="use a local packages-meta-v1.json instead of fetching")
    ap.add_argument("--max-candidates", type=int, default=8,
                    help="buckets larger than this are treated as shared hosting "
                         "and never resolved (github.com, sourceforge.net, ...)")
    args = ap.parse_args()

    if args.from_file:
        xml_bytes = pathlib.Path(args.from_file).read_bytes()
    else:
        xml_bytes = fetch(args.arch)

    hosts, domains, stats = build(xml_bytes, args.remote)
    aur_stats = None
    if not args.no_aur:
        if args.aur_from_file:
            meta = json.loads(pathlib.Path(args.aur_from_file).read_text())
        else:
            meta = fetch_aur()
        entries, aur_stats = aur_entries(meta)
        merge_aur(hosts, domains, entries)
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
    if aur_stats is not None:
        sys.stderr.write(
            f"  AUR: kept {aur_stats['kept']} (dropped {aur_stats['few_votes']} with "
            f"<{AUR_MIN_VOTES} votes, {aur_stats['vcs']} VCS, {aur_stats['orphaned']} "
            f"orphaned, {aur_stats['out_of_date']} out of date)\n")


if __name__ == "__main__":
    main()
