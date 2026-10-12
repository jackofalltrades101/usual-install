#!/usr/bin/env python3
"""Sign and verify the catalogue.

The catalogue decides what gets installed, which makes it the highest-value
target in the system: anyone who can substitute it can choose the package a
vendor's download page resolves to. It must therefore be signed at build time
and verified before every use.

Ed25519 via the openssl CLI, so the project stays dependency-free.

    ./sign.py keygen --key signing.key            # once, kept offline
    ./sign.py sign   --key signing.key catalogue.json
    ./sign.py verify --pubkey signing.pub catalogue.json
"""
import argparse, pathlib, subprocess, sys

import os
DEFAULT_PUBKEY = pathlib.Path(
    os.environ.get("USUAL_PUBKEY")
    or pathlib.Path(__file__).resolve().parent / "signing.pub")


def _openssl(args, **kw):
    return subprocess.run(["openssl", *args], capture_output=True, **kw)


def sign(path, key):
    """Write <path>.sig. Returns the signature path."""
    sig = pathlib.Path(str(path) + ".sig")
    r = _openssl(["pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                  "-in", str(path), "-out", str(sig)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode().strip())
    return sig


def verify(path, pubkey=None):
    """Return (ok, reason). Never raises -- callers decide what to do."""
    path = pathlib.Path(path)
    pubkey = pathlib.Path(pubkey or DEFAULT_PUBKEY)
    sig = pathlib.Path(str(path) + ".sig")
    if not pubkey.is_file():
        return False, f"no trusted public key at {pubkey}"
    if not sig.is_file():
        return False, f"no signature at {sig}"
    r = _openssl(["pkeyutl", "-verify", "-pubin", "-inkey", str(pubkey), "-rawin",
                  "-in", str(path), "-sigfile", str(sig)])
    if r.returncode != 0:
        return False, "signature does not match (tampered, or signed by another key)"
    return True, "signature ok"


def cmd_keygen(a):
    key = pathlib.Path(a.key)
    if key.exists() and not a.force:
        print(f"{key} exists; --force to replace", file=sys.stderr)
        return 1
    key.parent.mkdir(parents=True, exist_ok=True)
    r = _openssl(["genpkey", "-algorithm", "ed25519", "-out", str(key)])
    if r.returncode != 0:
        print(r.stderr.decode(), file=sys.stderr); return 1
    key.chmod(0o600)
    pub = pathlib.Path(a.pubkey or DEFAULT_PUBKEY)
    r = _openssl(["pkey", "-in", str(key), "-pubout", "-out", str(pub)])
    if r.returncode != 0:
        print(r.stderr.decode(), file=sys.stderr); return 1
    print(f"private key {key}  (keep this offline)")
    print(f"public  key {pub}  (ships with the client)")
    return 0


def cmd_sign(a):
    try:
        print(f"signed -> {sign(a.path, a.key)}")
    except RuntimeError as e:
        print(e, file=sys.stderr); return 1
    return 0


def cmd_verify(a):
    ok, why = verify(a.path, a.pubkey)
    print(("ok   " if ok else "FAIL ") + why)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--key", required=True)
    k.add_argument("--pubkey"); k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keygen)
    g = sub.add_parser("sign"); g.add_argument("path"); g.add_argument("--key", required=True)
    g.set_defaults(func=cmd_sign)
    v = sub.add_parser("verify"); v.add_argument("path"); v.add_argument("--pubkey")
    v.set_defaults(func=cmd_verify)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
