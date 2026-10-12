#!/usr/bin/env python3
"""Talk to usuald.

Every caller treats the daemon as optional. Phase 1 worked without it and must
keep working: if the socket is absent, the session agent falls back to local
resolution and a user-scope flatpak install. The daemon adds native-repo
installs and system-wide provisioning; it is not a prerequisite for the
product to function.

    ./client.py status
    ./client.py resolve https://zoom.us/download
    ./client.py install us.zoom.Zoom
    ./client.py install us.zoom.Zoom native zoom.us
    ./client.py refresh        # set up browsers again, Flatpak ones included
"""
import json, os, pathlib, socket, sys

SOCKET_PATH = pathlib.Path(
    os.environ.get("USUAL_SOCKET", "/run/usual-install/usuald.sock"))
TIMEOUT = 600


class DaemonUnavailable(Exception):
    pass


def available():
    return SOCKET_PATH.exists()


def request(op, timeout=TIMEOUT, **kw):
    """Send one request, yielding each streamed message.

    The final message always has type "done". Raises DaemonUnavailable when the
    daemon is not running, which callers are expected to handle rather than
    treat as fatal.
    """
    if not SOCKET_PATH.exists():
        raise DaemonUnavailable(f"no socket at {SOCKET_PATH}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET_PATH))
    except OSError as e:
        raise DaemonUnavailable(str(e))
    f = s.makefile("rwb")
    try:
        f.write((json.dumps({"op": op, **kw}) + "\n").encode())
        f.flush()
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line.decode())
    finally:
        try:
            f.close()
        except OSError:
            pass
        s.close()


def call(op, **kw):
    """Collect a whole exchange: (final_done_message, [log lines])."""
    logs, final = [], {"type": "done", "ok": False, "reason": "no reply"}
    for msg in request(op, **kw):
        if msg.get("type") == "log":
            logs.append(msg.get("line", ""))
        elif msg.get("type") == "done":
            final = msg
    return final, logs


def refresh_browsers():
    """Set up every browser again. Returns [(ok, message)].

    Two halves: ordinary browsers are provisioned by usuald (system-wide, so
    it needs the service); this user's Flatpak browsers are set up right here,
    as the user, since that needs no privilege. Shared by `usualctl refresh`
    and the settings window's Refresh browsers button.
    """
    out = []
    try:
        done, _ = call("provision")
        out.append((bool(done.get("ok")), "Browsers: " + (done.get("reason") or "checked")))
    except DaemonUnavailable:
        out.append((False, "Browsers: the Usual Install service is not running "
                           "(start it with: sudo systemctl enable --now usuald)"))
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "provision"))
    import provision as prov
    ext_id = prov.current_extension_id()
    if not ext_id:
        out.append((False, "Flatpak browsers: this installation has no extension id"))
        return out
    results = prov.provision_flatpak(ext_id, prov.find_crx())
    if not results:
        out.append((True, "Flatpak browsers: none installed"))
    for name, ok, detail in results:
        out.append((ok, f"{name} (Flatpak): " + ("set up" if ok else detail)))
    return out


def _print_status(st):
    import term
    term.heading("Usual Install service")
    term.row("Version", st.get("version"))
    term.row("Distribution", st.get("distro"))
    term.row("System packages", st.get("native_backend") or term.muted("none on this system"))
    term.row("Install methods", ", ".join(st.get("backends", [])) or term.muted("none"))
    cat = st.get("catalogue", {})
    term.section("App catalogue")
    term.row("Last updated", cat.get("generated") or "unknown")
    term.row("Covers", f"{cat.get('hosts', 0)} websites")
    term.row("Signature", term.good("valid") if cat.get("verified")
             else term.bad("did not verify, so it is not in use"))
    term.section("Browsers")
    for name in st.get("browsers", []):
        term.row(name, term.good("set up"))
    ready = set(st.get("flatpak_ready", []))
    for name in st.get("flatpak_browsers", []):
        term.row(f"{name} (Flatpak)", term.good("set up") if name in ready
                 else term.warn("not set up; run: usualctl refresh"))
    for name in st.get("snap_browsers", []):
        term.row(f"{name} (Snap)", term.bad("cannot be supported"))


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    as_json = "--json" in sys.argv or not sys.stdout.isatty()
    sys.argv = [a for a in sys.argv if a != "--json"]
    op = sys.argv[1]
    if op == "refresh":
        import term
        results = refresh_browsers()
        if as_json:
            print(json.dumps([{"ok": ok, "message": m} for ok, m in results], indent=2))
        else:
            for ok, m in results:
                term.result(ok, m)
            print("Restart your browsers to pick up any changes.")
        return 0 if all(ok for ok, _ in results) else 1
    kw = {}
    if op == "resolve" and len(sys.argv) > 2:
        kw["pageUrl"] = sys.argv[2]
    if op == "install" and len(sys.argv) > 2:
        kw["id"] = sys.argv[2]
        if len(sys.argv) > 3:
            kw["backend"] = sys.argv[3]
        # A native install is refused without the domain it came from, so the
        # CLI has to be able to supply it.
        if len(sys.argv) > 4:
            kw["domain"] = sys.argv[4]
    try:
        for msg in request(op, **kw):
            if msg.get("type") == "log":
                sys.stdout.write(msg.get("line", ""))
            elif msg.get("type") == "progress":
                sys.stdout.write(f"\r  {msg.get('pct')}%")
                sys.stdout.flush()
            else:
                if as_json:
                    print()
                    print(json.dumps(msg, indent=2))
                elif op == "status" and msg.get("ok"):
                    _print_status(msg.get("status", {}))
                elif op == "resolve":
                    import term
                    for c in msg.get("candidates", []):
                        term.row(c.get("name", ""), c.get("id", ""))
                    term.result(bool(msg.get("ok")), msg.get("reason", ""))
                else:
                    import term
                    print()
                    term.result(bool(msg.get("ok")), msg.get("reason", ""))
                return 0 if msg.get("ok") else 1
    except DaemonUnavailable as e:
        import term
        term.result(False, f"The Usual Install service is not running ({e}). "
                           f"Start it with: sudo systemctl enable --now usuald",
                    stream=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
