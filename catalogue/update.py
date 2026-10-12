#!/usr/bin/env python3
"""Fetch and install a new catalogue.

Run by a systemd timer. The important property is that a bad download can
never replace a good catalogue: the signature is checked *before* anything is
moved into place, and the move itself is atomic, so a crash mid-update leaves
the previous catalogue intact rather than a half-written file.

    ./update.py --url https://example/catalogue.json --dest /var/lib/usual-install
    ./update.py --check            # report what is installed, change nothing
"""
import argparse, json, os, pathlib, shutil, sys, tempfile, time, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sign import verify  # noqa: E402

UA = {"User-Agent": "usual-install/0.2"}
DEFAULT_DEST = pathlib.Path(os.environ.get("USUAL_DATA_DIR", "/var/lib/usual-install"))


def fetch(url, dest, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        dest.write_bytes(r.read())
    return dest


def install_catalogue(url, dest_dir, name="catalogue.json", timeout=120):
    """Download, verify, then atomically replace. Returns (ok, message)."""
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / name

    with tempfile.TemporaryDirectory(dir=str(dest_dir)) as td:
        td = pathlib.Path(td)
        staged = td / name
        try:
            fetch(url, staged, timeout)
            fetch(url + ".sig", pathlib.Path(str(staged) + ".sig"), timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"download failed: {e}"

        ok, why = verify(staged)
        if not ok:
            return False, f"rejected: {why}"

        try:
            data = json.loads(staged.read_text())
        except ValueError as e:
            return False, f"rejected: not valid JSON ({e})"
        if not data.get("hosts"):
            return False, "rejected: catalogue contains no entries"

        # Never downgrade: an attacker who can serve an old signed catalogue
        # could otherwise roll back a correction.
        if final.is_file():
            try:
                current = json.loads(final.read_text()).get("generated", "")
                if current and data.get("generated", "") < current:
                    return False, (f"rejected: offered {data.get('generated')} is older "
                                   f"than installed {current}")
            except (OSError, ValueError):
                pass

        # Replace the signature first so the pair is never mismatched for long,
        # then the catalogue itself; both are atomic renames on the same mount.
        shutil.copy(str(staged) + ".sig", str(final) + ".sig.new")
        os.replace(str(final) + ".sig.new", str(final) + ".sig")
        shutil.copy(staged, str(final) + ".new")
        os.replace(str(final) + ".new", final)

    return True, f"installed {data.get('generated')} ({len(data.get('hosts', {}))} hostnames)"


def report(dest_dir, name="catalogue.json"):
    final = pathlib.Path(dest_dir) / name
    if not final.is_file():
        print(f"{final}: not installed")
        return 1
    ok, why = verify(final)
    try:
        data = json.loads(final.read_text())
    except ValueError:
        print(f"{final}: unreadable")
        return 1
    age = (time.time() - final.stat().st_mtime) / 3600
    print(f"{final}")
    print(f"  generated   {data.get('generated')}")
    print(f"  entries     {len(data.get('hosts', {}))} hostnames, "
          f"{len(data.get('domains', {}))} domains")
    print(f"  signature   {'ok' if ok else 'FAILED — ' + why}")
    print(f"  age         {age:.1f} h")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--dest", default=str(DEFAULT_DEST))
    ap.add_argument("--name", default="catalogue.json")
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    a = ap.parse_args()

    if a.check or not a.url:
        return report(a.dest, a.name)

    ok, msg = install_catalogue(a.url, a.dest, a.name)
    print(("ok: " if ok else "failed: ") + msg, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
