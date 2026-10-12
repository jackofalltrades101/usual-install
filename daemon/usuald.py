#!/usr/bin/env python3
"""usuald -- the Usual Install system service.

Owns the signed catalogue, provisions every Chromium-family browser it finds
(and any installed later), and performs privileged installs behind a single
narrow polkit action.

Transport is a Unix socket rather than DBus. The design called for DBus, but
both ends of this conversation are ours, a socket needs no library on a project
that is otherwise dependency-free, and SO_PEERCRED gives exactly the caller
identity polkit needs. Newline-delimited JSON in both directions.

The privileged surface is deliberately tiny. A client may ask to install a
*catalogue entry id*; it may not name an arbitrary package. The daemon
re-resolves that id against its own verified catalogue, so a compromised
session agent still cannot install something the catalogue does not list.

    sudo ./usuald.py --foreground          # run it
    ./usuald.py --self-test                # no root needed
"""
import argparse, json, os, pathlib, shutil, socket, struct, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "resolver"))
sys.path.insert(0, str(HERE.parent / "catalogue"))
sys.path.insert(0, str(HERE.parent / "provision"))

import backends  # noqa: E402
from resolve import resolve, registrable  # noqa: E402
from sign import verify as verify_signature  # noqa: E402

VERSION = "0.2.0"
POLKIT_ACTION = "org.usualinstall.install"

RUN_DIR = pathlib.Path(os.environ.get("USUAL_RUN_DIR", "/run/usual-install"))
SOCKET_PATH = RUN_DIR / "usuald.sock"
CONFIG_PATH = pathlib.Path(os.environ.get("USUAL_CONFIG", "/etc/usual-install/config.json"))
DATA_DIR = pathlib.Path(os.environ.get("USUAL_DATA_DIR", "/var/lib/usual-install"))
CATALOGUE_PATHS = [
    os.environ.get("USUAL_CATALOGUE"),
    str(DATA_DIR / "catalogue.json"),
    "/usr/share/usual-install/catalogue.json",
    str(HERE.parent / "catalogue" / "catalogue.json"),
]

DEFAULT_CONFIG = {
    "enabled": True,
    "backend_order": ["flatpak-user", "native"],
    "provision_interval_seconds": 30,
    "catalogue_url": "https://example.invalid/usual-install/catalogue.json",
    "update_interval_hours": 24,
    "disabled_browsers": [],
    "ignored_domains": [],
    "allow_unsigned_catalogue": False,
}


def log(*a):
    print(f"[usuald]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------- config ----

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


# ------------------------------------------------------------ catalogue ----

class Catalogue:
    """The verified catalogue, reloaded when the file changes."""

    def __init__(self, allow_unsigned=False):
        self.allow_unsigned = allow_unsigned
        self.data = {"hosts": {}, "domains": {}, "max_candidates": 8}
        self.path = None
        self.mtime = 0
        self.verified = False
        self.reload()

    def reload(self):
        for p in CATALOGUE_PATHS:
            if not p:
                continue
            path = pathlib.Path(p)
            if not path.is_file():
                continue
            ok, why = verify_signature(path)
            if not ok and not self.allow_unsigned:
                log(f"REFUSING {path}: {why}")
                continue
            try:
                data = json.loads(path.read_text())
            except ValueError as e:
                log(f"unreadable catalogue {path}: {e}")
                continue
            self.data, self.path, self.verified = data, path, ok
            self.mtime = path.stat().st_mtime
            log(f"catalogue {path} verified={ok} hosts={len(data.get('hosts', {}))}")
            return True
        log("no usable catalogue; every request will resolve to nothing")
        return False

    def maybe_reload(self):
        if self.path and self.path.is_file():
            m = self.path.stat().st_mtime
            if m != self.mtime:
                self.reload()

    def entry_for(self, app_id):
        """Find a catalogue entry by id. The only way a package name enters."""
        for bucket in (self.data.get("hosts", {}), self.data.get("domains", {})):
            for entries in bucket.values():
                for e in entries:
                    if e.get("id") == app_id:
                        return e
        return None


# --------------------------------------------------------------- polkit ----

def polkit_allows(pid, uid, action=POLKIT_ACTION):
    """Ask polkit whether this caller may perform a privileged install.

    Uses pkcheck rather than a polkit library, keeping the dependency list
    empty. When polkit is absent we fail closed for non-root callers: a missing
    authority is not permission.
    """
    if uid == 0:
        return True, "caller is root"
    pkcheck = shutil.which("pkcheck")
    if not pkcheck:
        return False, "polkit not installed; refusing privileged install"
    try:
        start = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        proc_arg = f"{pid},{start}"
    except (OSError, IndexError):
        proc_arg = str(pid)
    r = subprocess.run([pkcheck, "--action-id", action, "--process", proc_arg,
                        "--allow-user-interaction"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return True, "authorized by polkit"
    return False, (r.stderr.strip() or "polkit denied the request")


# ---------------------------------------------------------------- server ----

class Daemon:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.catalogue = Catalogue(self.config.get("allow_unsigned_catalogue", False))
        self.install_lock = threading.Lock()
        self.history = []
        self.provisioned = {}
        self.stop_event = threading.Event()

    # -- operations ---------------------------------------------------------

    def op_status(self, req, peer, send):
        import provision as prov
        send({"type": "done", "ok": True, "status": {
            "version": VERSION,
            "enabled": self.config.get("enabled", True),
            "catalogue": {
                "path": str(self.catalogue.path) if self.catalogue.path else None,
                "verified": self.catalogue.verified,
                "generated": self.catalogue.data.get("generated"),
                "hosts": len(self.catalogue.data.get("hosts", {})),
            },
            "backends": [b.name for b in backends.available_backends()],
            "native_backend": (backends.native_backend().name
                               if backends.native_backend() else None),
            "distro": backends.distro_family(),
            "browsers": [b["name"] for b in prov.detected()],
            "flatpak_browsers": [b["name"] for b in prov.flatpak_browsers()],
            "snap_browsers": prov.snap_browsers(),
            "history": self.history[-20:],
        }})

    def op_resolve(self, req, peer, send):
        self.catalogue.maybe_reload()
        if not self.config.get("enabled", True):
            send({"type": "done", "ok": False, "reason": "disabled in configuration"})
            return
        url = req.get("pageUrl") or ""
        for d in self.config.get("ignored_domains", []):
            if d and d in url:
                send({"type": "done", "ok": False, "reason": f"{d} is on the ignore list"})
                return
        candidates, reason = resolve(url, self.catalogue.data)
        send({"type": "done", "ok": bool(candidates),
              "candidates": candidates, "reason": reason})

    def op_install(self, req, peer, send):
        """Install a catalogue entry. The client never names a package."""
        app_id = req.get("id") or ""
        entry = self.catalogue.entry_for(app_id)
        if not entry:
            send({"type": "done", "ok": False,
                  "reason": f"{app_id!r} is not in the verified catalogue"})
            return

        # A native package name is a guess until the machine's own package
        # metadata agrees it belongs to the vendor the user was actually
        # visiting. This is the same principle the Flathub catalogue rests on,
        # applied locally and at install time.
        native_pkg = entry.get("native_hint")
        want_domain = (req.get("domain") or "").lower()

        backend = self.pick_backend(req.get("backend"))
        if not backend:
            if req.get("backend") == "flatpak-user":
                send({"type": "done", "ok": False,
                      "reason": "flatpak --user belongs to the session agent, "
                                "not the daemon"})
            else:
                send({"type": "done", "ok": False,
                      "reason": f"no native backend on this system "
                                f"({backends.distro_family()})"})
            return

        if backend.needs_privilege:
            if not native_pkg:
                send({"type": "done", "ok": False,
                      "reason": f"no native package is known for {app_id}"})
                return
            if not want_domain:
                send({"type": "done", "ok": False,
                      "reason": "a native install must say which domain it came from"})
                return
            declared = backend.homepage(native_pkg)
            if not declared:
                send({"type": "done", "ok": False,
                      "reason": f"{native_pkg!r} is not in the {backend.name} "
                                f"repositories, or declares no homepage"})
                return
            from urllib.parse import urlparse
            got = registrable(urlparse(declared).hostname or "")
            if got != registrable(want_domain):
                send({"type": "done", "ok": False,
                      "reason": f"refusing {native_pkg!r}: it declares {got or declared!r}, "
                                f"not {registrable(want_domain)!r}"})
                return
            send({"type": "log",
                  "line": f"{native_pkg} declares {declared} — matches "
                          f"{registrable(want_domain)}\n"})
            app_id = native_pkg          # from here on, install the native name

            allowed, why = polkit_allows(peer["pid"], peer["uid"])
            if not allowed:
                send({"type": "done", "ok": False, "reason": why})
                return
            send({"type": "log", "line": f"authorized: {why}\n"})

        if not self.install_lock.acquire(blocking=False):
            send({"type": "done", "ok": False, "reason": "another install is in progress"})
            return
        try:
            if backend.is_installed(app_id):
                send({"type": "done", "ok": True, "reason": "already installed"})
                return
            cmd = backend.install_cmd(app_id)
            env = backend.env() if hasattr(backend, "env") else None
            send({"type": "log", "line": "$ " + " ".join(cmd) + "\n"})
            gen = backends.run_streaming(cmd, env=env)
            rc = 0
            try:
                while True:
                    kind, payload = next(gen)
                    send({"type": kind, ("line" if kind == "log" else "pct"): payload})
            except StopIteration as stop:
                rc = stop.value or 0
            self.history.append({"id": app_id, "backend": backend.name,
                                 "rc": rc, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               time.gmtime())})
            send({"type": "done", "ok": rc == 0, "rc": rc,
                  "reason": "installed" if rc == 0 else f"{backend.name} exited {rc}"})
        finally:
            self.install_lock.release()

    def pick_backend(self, requested=None):
        """Native backends only.

        `flatpak --user` installs into the *calling user's* flatpak, so running
        it from a root daemon would silently install into root's. That backend
        therefore stays with the session agent, and the daemon exists only for
        the installs that genuinely need privilege.
        """
        if requested in (None, "native", "auto"):
            return backends.native_backend()
        if requested == "flatpak-user":
            return None
        b = backends.by_name(requested)
        if b and b.available() and b.needs_privilege:
            return b
        return None

    def op_provision(self, req, peer, send):
        n = self.provision_once(force=True)
        send({"type": "done", "ok": True, "reason": f"provisioned {n} location(s)"})

    def op_config_get(self, req, peer, send):
        send({"type": "done", "ok": True, "config": self.config})

    def op_config_set(self, req, peer, send):
        if peer["uid"] != 0:
            allowed, why = polkit_allows(peer["pid"], peer["uid"],
                                         "org.usualinstall.configure")
            if not allowed:
                send({"type": "done", "ok": False, "reason": why})
                return
        updates = req.get("config") or {}
        unknown = [k for k in updates if k not in DEFAULT_CONFIG]
        if unknown:
            send({"type": "done", "ok": False, "reason": f"unknown keys: {unknown}"})
            return
        self.config.update(updates)
        try:
            save_config(self.config)
        except OSError as e:
            send({"type": "done", "ok": False, "reason": str(e)})
            return
        send({"type": "done", "ok": True, "config": self.config})

    OPS = {"status": op_status, "resolve": op_resolve, "install": op_install,
           "provision": op_provision, "config-get": op_config_get,
           "config-set": op_config_set}

    # -- provisioning -------------------------------------------------------

    def provision_once(self, force=False):
        """Write browser manifests for anything not already covered.

        Polling rather than inotify: a new browser appearing is not
        time-critical, polling a handful of directories costs nothing, and it
        has no failure modes to debug. The design said inotify; this is the
        same promise kept more simply.
        """
        import provision as prov
        ext_id = prov.current_extension_id()
        if not ext_id:
            return 0
        wrote = 0
        disabled = set(self.config.get("disabled_browsers", []))
        crx = next((p for p in (DATA_DIR / "sensor.crx",
                                pathlib.Path("/usr/share/usual-install/sensor.crx"),
                                HERE.parent / "sensor.crx") if p.is_file()), None)
        version = None
        try:
            version = json.loads((HERE.parent / "sensor" / "manifest.json")
                                 .read_text())["version"]
        except (OSError, ValueError, KeyError):
            pass

        for b in prov.detected():
            if b["name"] in disabled:
                continue
            manifest = prov.host_manifest(ext_id)
            for d in b["system_nmh"]:
                target = d / f"{prov.HOST_NAME}.json"
                if not force and target.exists():
                    continue
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(manifest, indent=2) + "\n")
                    if self.provisioned.get(b["name"]) != str(target):
                        log(f"provisioned {b['name']} -> {target}")
                    self.provisioned[b["name"]] = str(target)
                    wrote += 1
                except OSError as e:
                    log(f"could not provision {b['name']}: {e}")

            # The preinstall manifest is what makes "the user never installs an
            # extension" true; without it the native host is there but nothing
            # ever talks to it.
            if crx and version:
                for d in b["system_ext"]:
                    target = d / f"{ext_id}.json"
                    if not force and target.exists():
                        continue
                    try:
                        d.mkdir(parents=True, exist_ok=True)
                        target.write_text(json.dumps(
                            {"external_crx": str(crx),
                             "external_version": version}, indent=2) + "\n")
                        log(f"preinstalled sensor for {b['name']} -> {target}")
                        wrote += 1
                    except OSError as e:
                        log(f"could not preinstall for {b['name']}: {e}")
        return wrote

    def provision_loop(self):
        interval = max(5, int(self.config.get("provision_interval_seconds", 30)))
        while not self.stop_event.is_set():
            try:
                self.provision_once()
            except Exception as e:      # a watcher must never take the daemon down
                log(f"provision loop error: {e!r}")
            self.stop_event.wait(interval)

    # -- connection handling ------------------------------------------------

    def handle(self, conn):
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", creds)
        except (OSError, AttributeError):
            pid, uid, gid = -1, -1, -1
        peer = {"pid": pid, "uid": uid, "gid": gid}

        f = conn.makefile("rwb")

        def send(obj):
            f.write((json.dumps(obj) + "\n").encode())
            f.flush()

        try:
            line = f.readline()
            if not line:
                return
            req = json.loads(line.decode())
            op = self.OPS.get(req.get("op"))
            if not op:
                send({"type": "done", "ok": False,
                      "reason": f"unknown op {req.get('op')!r}"})
                return
            op(self, req, peer, send)
        except Exception as e:
            log(f"request failed: {e!r}")
            try:
                send({"type": "done", "ok": False, "reason": "internal error"})
            except OSError:
                pass
        finally:
            try:
                f.close()
            except OSError:
                pass
            conn.close()

    def serve(self):
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        # Access control is polkit's job, not the filesystem's: any local user
        # may ask, and the daemon decides what they are allowed to have.
        os.chmod(SOCKET_PATH, 0o666)
        srv.listen(16)
        log(f"listening on {SOCKET_PATH} (version {VERSION})")

        threading.Thread(target=self.provision_loop, daemon=True).start()

        try:
            while not self.stop_event.is_set():
                conn, _ = srv.accept()
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            srv.close()
            if SOCKET_PATH.exists():
                SOCKET_PATH.unlink()
            log("stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--foreground", action="store_true", help="(default; systemd Type=simple)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        from selftest import run as run_selftest
        return run_selftest()
    if os.geteuid() != 0:
        log("not running as root: privileged installs and system provisioning "
            "will be unavailable")
    Daemon().serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
