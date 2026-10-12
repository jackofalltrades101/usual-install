#!/usr/bin/env python3
"""Generate the Windows-compatibility catalogue.

Usual Install's native catalogue comes from Flathub, where every app declares
its own homepage -- so it can be keyed by domain. No compatibility source does
that, so this catalogue is keyed by *name* and matched against the page title,
the download filename, and the domain label. That is a weaker signal, which is
why `compat/match.py` sets a high confidence bar and why the common answer is
"not supported".

Sources, and what each is actually worth:

  bottles   36 curated entries with grades and recommended runner builds.
            Machine-readable, reliable, tiny. Mostly game launchers.
  lutris    Searchable JSON API covering games and some Windows applications.
            Has install scripts but no compatibility grade, so entries are
            admitted only as "has a working community install script".
  protondb  Games only, keyed by Steam AppID. A vendor download page is not
            where Steam games come from, so this is off by default.
  winehq    The real Windows-application compatibility database, thousands of
            apps with exactly the grades we want -- and NO API, HTML only.
            Scraping a volunteer-run site per install would be both rude and a
            privacy leak, so it is not implemented. This is the ceiling on
            coverage for the whole feature.

    ./build_compat.py -o compat.json
"""
import argparse, json, pathlib, re, sys, time, urllib.parse, urllib.request

BOTTLES_INDEX = "https://raw.githubusercontent.com/bottlesdevs/programs/main/index.yml"
LUTRIS_SEARCH = "https://lutris.net/api/games?search={q}&format=json"
UA = {"User-Agent": "usual-install/0.1 (+catalogue builder)"}


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- bottles ----

def parse_bottles_index(text):
    """Minimal parser for the subset of YAML this file uses.

    Top-level keys with a flat scalar mapping and one list field. Avoids a
    PyYAML dependency; the whole project is deliberately dependency-free.
    """
    entries = []
    current = None
    list_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^[\w.\-]+:\s*$", raw):                 # top-level slug
            if current:
                entries.append(current)
            current = {"slug": raw.split(":")[0].strip(), "runners": []}
            list_key = None
            continue
        if current is None:
            continue
        m = re.match(r"^\s+([A-Za-z]\w*):\s*(.*)$", raw)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                list_key = key
            else:
                list_key = None
                current[key.lower()] = val.strip("'\"")
            continue
        m = re.match(r"^\s+-\s*(.+)$", raw)
        if m and list_key:
            current.setdefault(list_key.lower(), []).append(m.group(1).strip())
    if current:
        entries.append(current)

    out = []
    for e in entries:
        if not e.get("name"):
            continue
        out.append({
            "name": e["name"],
            "slug": e["slug"],
            "grade": (e.get("grade") or "").lower() or None,
            "category": (e.get("category") or "software").lower(),
            "runners": e.get("runners", []),
            "source": "bottles",
        })
    return out


# ----------------------------------------------------------------- lutris ----

def lutris_lookup(names, pause=0.5):
    """Confirm a community install script exists. No grade, so no auto-offer."""
    found = []
    for n in names:
        try:
            data = json.loads(get(LUTRIS_SEARCH.format(q=urllib.parse.quote(n)), timeout=30))
        except Exception as e:
            sys.stderr.write(f"  lutris {n!r}: {e}\n")
            continue
        for g in data.get("results", []):
            if not any(p.get("name") == "Windows" for p in g.get("platforms", [])):
                continue
            found.append({
                "name": g["name"],
                "slug": g["slug"],
                "grade": None,               # Lutris publishes no grade
                "category": "games",
                "runners": [],
                "source": "lutris",
            })
        time.sleep(pause)
    return found


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="compat.json")
    ap.add_argument("--from-file", help="local bottles index.yml instead of fetching")
    ap.add_argument("--lutris", metavar="NAME", action="append", default=[],
                    help="also look up this name on Lutris (repeatable)")
    args = ap.parse_args()

    if args.from_file:
        text = pathlib.Path(args.from_file).read_text()
    else:
        sys.stderr.write(f"fetching {BOTTLES_INDEX}\n")
        text = get(BOTTLES_INDEX).decode("utf-8")

    entries = parse_bottles_index(text)
    sources = ["bottles"]

    if args.lutris:
        sys.stderr.write(f"querying lutris for {len(args.lutris)} name(s)\n")
        extra = lutris_lookup(args.lutris)
        entries += extra
        if extra:
            sources.append("lutris")

    graded = [e for e in entries if e["grade"]]
    offerable = [e for e in graded if e["grade"] in ("platinum", "gold")]

    out = pathlib.Path(args.out)
    out.write_text(json.dumps({
        "schema": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
        "notes": {
            "winehq": "not included: appdb.winehq.org has no API, HTML only. "
                      "This is the main limit on coverage.",
            "protondb": "not included: games only, keyed by Steam AppID.",
        },
        "entries": entries,
    }, indent=1, sort_keys=True))

    sys.stderr.write(
        f"\n{out}  ({out.stat().st_size/1024:.0f} KB)\n"
        f"  {len(entries)} entries, {len(graded)} graded\n"
        f"  {len(offerable)} are platinum/gold and could ever be offered\n"
        f"  coverage is small because WineHQ has no API; see --help\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
