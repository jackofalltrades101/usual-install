#!/usr/bin/env python3
"""The catalogue must be unusable if it has been altered.

Signing is only worth anything if a bad signature actually stops an install,
so this checks the agent's behaviour, not just the verify function.
"""
import json, os, pathlib, shutil, struct, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "catalogue"))
from sign import sign, verify  # noqa: E402

AGENT = ROOT / "session" / "usual-session"
PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}  {detail}")


def ask(catalogue_path, pubkey, allow_unsigned=False):
    """Ask the agent to resolve zoom.us with this catalogue; return the action."""
    # This checks the agent's own catalogue handling, which only runs when no
    # daemon is available -- usuald has its own verified copy and would answer
    # instead, hiding whatever the local file says.
    env = dict(os.environ, USUAL_CATALOGUE=str(catalogue_path),
               USUAL_DRY_RUN="1", USUAL_PUBKEY=str(pubkey),
               USUAL_SOCKET="/nonexistent/usuald.sock")
    if allow_unsigned:
        env["USUAL_ALLOW_UNSIGNED"] = "1"
    else:
        env.pop("USUAL_ALLOW_UNSIGNED", None)
    msg = json.dumps({"type": "download", "pageUrl": "https://zoom.us/download",
                      "downloadUrl": "https://zoom.us/x.deb",
                      "filename": "zoom_amd64.deb", "mime": "", "bytes": 1}).encode()
    payload = struct.pack("@I", len(msg)) + msg
    p = subprocess.run([sys.executable, str(AGENT)], input=payload,
                       capture_output=True, env=env, timeout=120)
    out = p.stdout
    if len(out) < 4:
        return None, p.stderr.decode()[-200:]
    (n,) = struct.unpack("@I", out[:4])
    return json.loads(out[4:4 + n].decode()), ""


def main():
    src = ROOT / "catalogue" / "catalogue.json"
    if not src.is_file():
        print("build the catalogue first"); return 1

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        key, pub = td / "k.key", td / "k.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                       check=True, capture_output=True)

        good = td / "catalogue.json"
        shutil.copy(src, good)
        sign(good, key)

        print("\n--- verify() unit behaviour ---")
        ok, why = verify(good, pub)
        check(ok, "a correctly signed catalogue verifies", why)

        tampered = td / "tampered.json"
        data = json.loads(good.read_text())
        # The attack this defends against: repoint a real vendor domain at an
        # attacker's package.
        data["hosts"]["zoom.us"] = [{"id": "com.attacker.Evil", "name": "Evil",
                                     "summary": "", "backend": "flatpak",
                                     "remote": "flathub", "path": ""}]
        tampered.write_text(json.dumps(data))
        shutil.copy(str(good) + ".sig", str(tampered) + ".sig")
        ok, why = verify(tampered, pub)
        check(not ok, "an altered catalogue fails verification", why)

        unsigned = td / "unsigned.json"
        shutil.copy(src, unsigned)
        ok, why = verify(unsigned, pub)
        check(not ok, "a catalogue with no signature fails verification", why)

        print("\n--- agent behaviour (what actually protects the user) ---")
        reply, err = ask(good, pub)
        check(reply and reply.get("action") == "handled",
              "signed catalogue resolves normally", f"{reply} {err}")

        reply, err = ask(tampered, pub)
        check(reply and reply.get("action") == "ignore",
              "TAMPERED catalogue is refused, nothing is installed", f"{reply} {err}")
        if reply and reply.get("action") == "handled":
            print(f"        !! agent would have installed {reply.get('candidates')}")

        reply, err = ask(unsigned, pub)
        check(reply and reply.get("action") == "ignore",
              "unsigned catalogue is refused by default", f"{reply} {err}")

        reply, err = ask(unsigned, pub, allow_unsigned=True)
        check(reply and reply.get("action") == "handled",
              "USUAL_ALLOW_UNSIGNED=1 permits it for development", f"{reply} {err}")

    print(f"\n{'signing enforced' if not FAIL else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
