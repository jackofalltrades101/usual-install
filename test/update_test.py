#!/usr/bin/env python3
"""A bad catalogue must never replace a good one."""
import http.server, json, pathlib, socketserver, subprocess, sys, tempfile, threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "catalogue"))
from update import install_catalogue  # noqa: E402
from sign import sign  # noqa: E402

PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}  {detail}")


def serve(directory):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(directory), **k)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        www, dest = td / "www", td / "dest"
        www.mkdir(); dest.mkdir()
        key, pub = td / "k.key", td / "k.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                       check=True, capture_output=True)
        import os
        os.environ["USUAL_PUBKEY"] = str(pub)
        import sign as sign_mod, importlib
        importlib.reload(sign_mod)
        from update import install_catalogue as inst

        def publish(generated, hosts, signer=key):
            doc = {"schema": 1, "generated": generated, "max_candidates": 8,
                   "hosts": hosts, "domains": {}}
            p = www / "catalogue.json"
            p.write_text(json.dumps(doc))
            if signer:
                sign_mod.sign(p, signer)
            return p

        httpd, base = serve(www)
        url = base + "/catalogue.json"
        try:
            good = {"zoom.us": [{"id": "us.zoom.Zoom", "name": "Zoom", "path": ""}]}
            publish("2026-02-01T00:00:00Z", good)
            ok, msg = inst(url, dest)
            check(ok, "a correctly signed catalogue installs", msg)
            installed = json.loads((dest / "catalogue.json").read_text())
            check(installed["generated"] == "2026-02-01T00:00:00Z", "and is the one served")

            # Tamper after signing.
            p = www / "catalogue.json"
            doc = json.loads(p.read_text())
            doc["hosts"]["zoom.us"] = [{"id": "com.attacker.Evil", "name": "Evil", "path": ""}]
            p.write_text(json.dumps(doc))
            ok, msg = inst(url, dest)
            check(not ok, "a tampered catalogue is rejected", msg)
            still = json.loads((dest / "catalogue.json").read_text())
            check(still["hosts"]["zoom.us"][0]["id"] == "us.zoom.Zoom",
                  "and the good one is still installed")

            # Signed by someone else.
            other = td / "other.key"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519",
                            "-out", str(other)], check=True, capture_output=True)
            publish("2026-03-01T00:00:00Z", good, signer=other)
            ok, msg = inst(url, dest)
            check(not ok, "a catalogue signed by another key is rejected", msg)

            # Rollback.
            publish("2026-01-01T00:00:00Z", good)
            ok, msg = inst(url, dest)
            check(not ok and "older" in msg, "an older catalogue cannot roll back a newer one", msg)

            # A newer one still installs.
            publish("2026-04-01T00:00:00Z", good)
            ok, msg = inst(url, dest)
            check(ok, "a newer catalogue installs", msg)

            # Empty.
            publish("2026-05-01T00:00:00Z", {})
            ok, msg = inst(url, dest)
            check(not ok, "an empty catalogue is rejected", msg)

            # Unreachable server.
            httpd.shutdown(); httpd.server_close()
            ok, msg = inst(url, dest, timeout=3)
            check(not ok, "an unreachable server changes nothing", msg)
            final = json.loads((dest / "catalogue.json").read_text())
            check(final["generated"] == "2026-04-01T00:00:00Z",
                  "the last good catalogue survives every failure")
        finally:
            try: httpd.shutdown()
            except Exception: pass

    print(f"\n{'updates are safe' if not FAIL else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
