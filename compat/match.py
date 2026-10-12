#!/usr/bin/env python3
"""Match a download to a compatibility catalogue entry, conservatively.

The native catalogue is keyed by domain because Flathub apps declare their
homepage. No compatibility source does, so this has to work from weaker
signals: the page title, the download filename, and the domain label.

Weak signals plus a low bar produce wrong-app matches, which are worse than no
feature at all -- so the bar here is high and deliberately rejects more than it
accepts. "Not supported" is the correct and expected answer most of the time.

    ./match.py --self-test
"""
import argparse, json, pathlib, re, sys

# Words that carry no identity -- they appear in nearly every installer name.
NOISE = {
    "setup", "install", "installer", "download", "downloader", "win", "win32",
    "win64", "windows", "x64", "x86", "amd64", "32bit", "64bit", "bit",
    "free", "trial", "latest", "full", "offline", "online", "web", "exe",
    "msi", "app", "application", "software", "program", "the", "for", "and",
    "version", "release", "final", "portable", "edition",
}
VERSIONISH = re.compile(r"^v?\d+([._-]\d+)*[a-z]?$")


def tokens(text):
    """Identity-bearing lowercase tokens."""
    parts = re.split(r"[^A-Za-z0-9]+", (text or "").lower())
    out = []
    for p in parts:
        if not p or p in NOISE or VERSIONISH.match(p):
            continue
        out.append(p)
    return out


def squash(text):
    """Lowercase alphanumerics only: 'FL Studio' -> 'flstudio'."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def domain_label(host):
    """Best-guess product word from a hostname: www.flstudio.com -> flstudio."""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p not in ("com", "org", "net", "io", "co", "app")]
    return parts[0] if parts else ""


def query_signals(page_title="", filename="", host=""):
    """The things we are allowed to infer a product name from."""
    stem = re.sub(r"\.(exe|msi|zip)$", "", (filename or ""), flags=re.I)
    return {
        "title": page_title or "",
        "filename": stem,
        "domain": domain_label(host),
    }


def score_entry(entry, signals):
    """Return (confident, why). Confident means safe to offer."""
    name = entry.get("name") or ""
    n_sq = squash(name)
    n_tok = set(tokens(name))
    if len(n_sq) < 4:
        return False, "catalogue name too short to match safely"

    for label, text in signals.items():
        if not text:
            continue
        q_sq = squash(text)
        q_tok = set(tokens(text))

        # 1. Exact squashed equality -- "FL Studio" vs "flstudio".
        if q_sq and q_sq == n_sq:
            return True, f"exact name match in {label}"

        # 2. The full product name appears inside the signal, and is long
        #    enough that the containment is not a coincidence.
        if len(n_sq) >= 6 and n_sq in q_sq:
            return True, f"name contained in {label}"

        # 3. Every identity-bearing word of the name is present, and the name
        #    has at least two such words. Single-word names must go through
        #    rule 1 or 2 -- "Steam" must not match "steamy-photo-editor".
        if len(n_tok) >= 2 and n_tok and n_tok <= q_tok:
            return True, f"all {len(n_tok)} name words present in {label}"

    return False, "no confident name match"


def match(catalogue, page_title="", filename="", host=""):
    """Return (entry, why) or (None, why)."""
    signals = query_signals(page_title, filename, host)
    hits = []
    for entry in catalogue.get("entries", []):
        ok, why = score_entry(entry, signals)
        if ok:
            hits.append((entry, why))

    if not hits:
        return None, "no compatibility entry matched this download"
    if len(hits) > 1:
        # Ambiguity here means our signals were too weak to tell two products
        # apart. Guessing would risk setting up the wrong application.
        names = ", ".join(e["name"] for e, _ in hits[:4])
        return None, f"ambiguous between {len(hits)} entries ({names}); refusing to guess"
    return hits[0]


# --------------------------------------------------------------------------- #

def _self_test():
    cat = {"entries": [
        {"name": "FL Studio", "grade": "gold", "category": "software"},
        {"name": "Ableton Live", "grade": "silver", "category": "software"},
        {"name": "Epic Games Store", "grade": "platinum", "category": "games"},
        {"name": "Steam", "grade": "bronze", "category": "games"},
        {"name": "Evernote", "grade": "silver", "category": "software"},
        {"name": "Autodesk Fusion", "grade": "bronze", "category": "software"},
    ]}

    cases = [
        # title, filename, host, expected name or None, label
        ("FL Studio 21 — Download", "flstudio_win64_21.0.3.exe", "image-line.com",
         "FL Studio", "title and filename agree"),
        ("", "flstudio_win64.exe", "image-line.com", "FL Studio", "filename alone"),
        ("Download Ableton Live 12", "ableton_live_12.exe", "ableton.com",
         "Ableton Live", "two-word name, all words present"),
        ("Epic Games Store", "EpicInstaller-16.exe", "epicgames.com",
         "Epic Games Store", "title exact"),
        ("", "", "evernote.com", "Evernote", "domain label alone"),
        # Things that must NOT match.
        ("Steamy Photo Editor", "steamy-photo-editor.exe", "steamyphoto.com",
         None, "short name must not match a superstring"),
        ("Download our app", "setup.exe", "example.com",
         None, "generic installer name"),
        ("Photoshop", "PhotoshopSetup.exe", "adobe.com",
         None, "not in the catalogue at all"),
        ("", "installer_win64_v2.1.exe", "unknown-vendor.com",
         None, "pure noise tokens"),
    ]

    fails = 0
    for title, fname, host, want, label in cases:
        entry, why = match(cat, title, fname, host)
        got = entry["name"] if entry else None
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label:44} -> {str(got):18} [{why}]")

    # Ambiguity must refuse rather than pick.
    amb = {"entries": [{"name": "Office Pro", "grade": "gold", "category": "software"},
                       {"name": "Office Pro Plus", "grade": "gold", "category": "software"}]}
    entry, why = match(amb, "Office Pro Plus 2024", "officeproplus.exe", "example.com")
    if entry is None and "ambiguous" in why:
        print(f"  ok    ambiguity refused                           -> None               [{why}]")
    else:
        print(f"  FAIL  ambiguity should refuse, got {entry}"); fails += 1

    print(f"\n{'all passed' if not fails else str(fails) + ' FAILURES'}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--catalogue", default=str(pathlib.Path(__file__).parent / "compat.json"))
    ap.add_argument("--title", default="")
    ap.add_argument("--filename", default="")
    ap.add_argument("--host", default="")
    a = ap.parse_args()
    if a.self_test:
        return 1 if _self_test() else 0
    cat = json.loads(pathlib.Path(a.catalogue).read_text())
    entry, why = match(cat, a.title, a.filename, a.host)
    print(json.dumps({"entry": entry, "why": why}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
