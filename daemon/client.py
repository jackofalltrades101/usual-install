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


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    op = sys.argv[1]
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
                print()
                print(json.dumps(msg, indent=2))
                return 0 if msg.get("ok") else 1
    except DaemonUnavailable as e:
        print(f"usuald is not running: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
