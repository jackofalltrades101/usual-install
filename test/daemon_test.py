#!/usr/bin/env python3
"""Exercise usuald over its real socket.

Runs the daemon unprivileged in a temporary directory, so this needs no root
and no system changes. What it checks is mostly the privileged surface: that
the daemon will not install something absent from its verified catalogue, will
not accept an arbitrary package name, and refuses rather than guesses.
"""
import json, os, pathlib, shutil, signal, socket, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "catalogue"))

PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}  {detail}")


def _finish():
    print(f"\n{'daemon verified' if not FAIL else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


def privileged_possible():
    """Can this process actually pass a polkit check?

    Running as root it can. Otherwise polkit must be installed, and in a
    container there is usually no authority to answer, so the daemon correctly
    refuses. That refusal is the right behaviour, not a failure, so the
    assertions that need a *successful* privileged operation are skipped
    rather than reported as broken.
    """
    import shutil, subprocess as sp
    if os.geteuid() == 0:
        return True
    pkcheck = shutil.which("pkcheck")
    if not pkcheck:
        return False
    # Registration is not enough: the action must also be *authorisable* here.
    # Over SSH polkit sees an inactive session and demands a password that no
    # agent can answer, so ask it the same question the daemon asks rather
    # than inferring from what is installed.
    try:
        r = sp.run([pkcheck, "--action-id", "org.usualinstall.configure",
                    "--process", str(os.getpid())],
                   capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, sp.SubprocessError):
        return False


def main():
    import client
    from sign import sign

    cat_src = ROOT / "catalogue" / "catalogue.json"
    if not cat_src.is_file():
        print("build the catalogue first"); return 1

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        rundir, datadir = td / "run", td / "data"
        rundir.mkdir(); datadir.mkdir()
        cfg = td / "config.json"
        cfg.write_text(json.dumps({"enabled": True, "provision_interval_seconds": 3600}))

        cat = datadir / "catalogue.json"
        data = json.loads(cat_src.read_text())
        # A synthetic entry whose native_hint is a package that genuinely
        # exists on the test machine and declares a real homepage, so the
        # match path can be exercised and not only the refusals.
        data["hosts"]["curl.se"] = [{
            "id": "se.curl.Curl", "name": "curl", "summary": "",
            "backend": "flatpak", "remote": "flathub",
            "native_hint": "curl", "path": "",
        }]
        data["domains"]["curl.se"] = list(data["hosts"]["curl.se"])
        cat.write_text(json.dumps(data))
        key = td / "k.key"; pub = td / "k.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
                       check=True, capture_output=True)
        sign(cat, key)

        env = dict(os.environ,
                   USUAL_RUN_DIR=str(rundir), USUAL_CONFIG=str(cfg),
                   USUAL_DATA_DIR=str(datadir), USUAL_PUBKEY=str(pub))
        env.pop("USUAL_CATALOGUE", None)
        client.SOCKET_PATH = rundir / "usuald.sock"

        proc = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "usuald.py")],
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                if client.SOCKET_PATH.exists():
                    break
                if proc.poll() is not None:
                    print("daemon exited early:\n" + (proc.stdout.read() or ""))
                    return 1
                time.sleep(0.1)
            check(client.SOCKET_PATH.exists(), "daemon came up and bound its socket")

            print("\n--- status ---")
            done, _ = client.call("status")
            st = done.get("status", {})
            check(done.get("ok"), "status replies", done)
            check(st.get("catalogue", {}).get("verified") is True,
                  "daemon reports a VERIFIED catalogue", st.get("catalogue"))
            print(f"        distro={st.get('distro')} backends={st.get('backends')} "
                  f"native={st.get('native_backend')}")

            print("\n--- resolution happens in the daemon, against its own catalogue ---")
            done, _ = client.call("resolve", pageUrl="https://zoom.us/download")
            check(done.get("ok") and done["candidates"][0]["id"] == "us.zoom.Zoom",
                  "zoom.us resolves to us.zoom.Zoom", done.get("reason"))
            done, _ = client.call("resolve", pageUrl="https://github.com/a/b/releases")
            check(not done.get("ok"), "a forge still resolves to nothing", done.get("reason"))

            print("\n--- the privileged surface refuses what it should ---")
            done, _ = client.call("install", id="com.attacker.Evil")
            check(not done.get("ok") and "not in the verified catalogue" in done.get("reason", ""),
                  "refuses an id absent from the catalogue", done.get("reason"))

            done, _ = client.call("install", id="; rm -rf /")
            check(not done.get("ok"), "refuses a shell-injection-shaped id", done.get("reason"))

            done, _ = client.call("install", id="us.zoom.Zoom", backend="flatpak-user")
            check(not done.get("ok") and "session agent" in done.get("reason", ""),
                  "refuses to run flatpak --user as the daemon", done.get("reason"))

            print("\n--- native installs must match the vendor's own domain ---")
            done, _ = client.call("install", id="us.zoom.Zoom", backend="native")
            check(not done.get("ok"), "native install without a domain is refused",
                  done.get("reason"))
            done, _ = client.call("install", id="us.zoom.Zoom", backend="native",
                                  domain="attacker.example")
            check(not done.get("ok"), "native install refused when the domain disagrees",
                  done.get("reason"))
            print(f"        reason: {done.get('reason')}")

            # Needs package metadata present; a slim image may have none.
            sys.path.insert(0, str(ROOT / "daemon"))
            import backends as _b
            nb = _b.native_backend()
            if not (nb and nb.homepage("curl")):
                print("  skip  package metadata unavailable; "
                      "run `apt-get update` (or equivalent) to exercise the match path")
                raise SystemExit(_finish())

            done, logs = client.call("install", id="se.curl.Curl", backend="native",
                                     domain="curl.se")
            matched = any("matches curl.se" in l for l in logs)
            check(matched, "native install proceeds when the package's own homepage agrees",
                  f"{done.get('reason')} logs={logs}")
            if matched:
                print(f"        {[l.strip() for l in logs if 'declares' in l]}")
            if privileged_possible():
                check(done.get("ok"), "and the install completes", done.get("reason"))
            else:
                reason = (done.get("reason") or "").lower()
                refused_by_authority = any(w in reason for w in
                                           ("polkit", "authoris", "authoriz",
                                            "authentic"))
                check(not done.get("ok") and refused_by_authority,
                      "and without an authority to authorise it, fails closed",
                      done.get("reason"))

            done, _ = client.call("install", id="se.curl.Curl", backend="native",
                                  domain="totally-different.example")
            check(not done.get("ok") and "refusing" in done.get("reason", ""),
                  "but is refused for the same package under a different domain",
                  done.get("reason"))
            print(f"        {done.get('reason')}")

            print("\n--- configuration ---")
            done, _ = client.call("config-get")
            check(done.get("ok") and "backend_order" in done.get("config", {}),
                  "config-get returns the live configuration")
            done, _ = client.call("config-set", config={"not_a_real_key": 1})
            check(not done.get("ok"), "config-set rejects unknown keys", done.get("reason"))
            done, _ = client.call("config-set", config={"enabled": False})
            if privileged_possible():
                check(done.get("ok") and done["config"]["enabled"] is False,
                      "config-set applies a known key", done.get("reason"))
                done, _ = client.call("resolve", pageUrl="https://zoom.us/download")
                check(not done.get("ok") and "disabled" in done.get("reason", ""),
                      "disabling the daemon stops resolution", done.get("reason"))
                client.call("config-set", config={"enabled": True})
            else:
                check(not done.get("ok"),
                      "config-set is refused without an authority to authorise it",
                      done.get("reason"))

            print("\n--- an unverifiable catalogue is not used ---")
            cat.write_text(json.dumps({"hosts": {"zoom.us": [
                {"id": "com.attacker.Evil", "name": "Evil", "path": ""}]},
                "domains": {}, "max_candidates": 8}))
            done, _ = client.call("resolve", pageUrl="https://zoom.us/download")
            bad = done.get("ok") and done["candidates"][0]["id"] == "com.attacker.Evil"
            check(not bad, "a tampered catalogue never reaches resolution",
                  done.get("candidates"))

            print("\n--- unknown operations ---")
            done, _ = client.call("definitely-not-an-op")
            check(not done.get("ok"), "unknown op is rejected", done.get("reason"))
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"\n{'daemon verified' if not FAIL else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
