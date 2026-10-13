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
import argparse, json, os, pathlib, shutil, socket, stat, struct, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "resolver"))
sys.path.insert(0, str(HERE.parent / "catalogue"))
sys.path.insert(0, str(HERE.parent / "provision"))

import backends  # noqa: E402
import aur  # noqa: E402
from resolve import resolve, registrable  # noqa: E402
from sign import verify as verify_signature  # noqa: E402

VERSION = "0.2.0"
POLKIT_ACTION = "org.usualinstall.install"
# Installing something built from an AUR script asks for a password every time:
# the equivalent of the UAC prompt a Windows .exe installer shows.
POLKIT_AUR_ACTION = "org.usualinstall.install-aur"
AUR_STAGING = pathlib.Path(os.environ.get("USUAL_AUR_STAGING", "/var/cache/usual-install/aur"))

RUN_DIR = pathlib.Path(os.environ.get("USUAL_RUN_DIR", "/run/usual-install"))
SOCKET_PATH = RUN_DIR / "usuald.sock"
CONFIG_PATH = pathlib.Path(os.environ.get("USUAL_CONFIG", "/etc/usual-install/config.json"))
DATA_DIR = pathlib.Path(os.environ.get("USUAL_DATA_DIR", "/var/lib/usual-install"))
MACHINE_ARCH = os.uname().machine if hasattr(os, "uname") else "x86_64"

# Flathub publishes a separate application set per architecture, and they are
# not the same: Zoom, Slack and Spotify have no aarch64 build at all. Shipping
# one x86_64 catalogue everywhere means an ARM user is offered applications
# that cannot be installed, and the failure reads as a broken tool.
CATALOGUE_PATHS = [
    os.environ.get("USUAL_CATALOGUE"),
    str(DATA_DIR / f"catalogue-{MACHINE_ARCH}.json"),
    f"/usr/share/usual-install/catalogue-{MACHINE_ARCH}.json",
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
            built_for = (data.get("source") or {}).get("arch")
            log(f"catalogue {path} verified={ok} hosts={len(data.get('hosts', {}))}"
                + (f" arch={built_for}" if built_for else ""))
            if built_for and built_for != MACHINE_ARCH:
                log(f"WARNING: this catalogue was built for {built_for} but this "
                    f"machine is {MACHINE_ARCH}. Applications it lists may have no "
                    f"build for this architecture and will fail to install.")
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
        # Flatpak browsers are per user: report the caller's own.
        fp_state = []
        try:
            import pwd
            if peer.get("uid", -1) >= 0:
                home = pwd.getpwuid(peer["uid"]).pw_dir
                fp_state = prov.flatpak_state(home, prov.current_extension_id() or "",
                                              with_crx=prov.find_crx() is not None)
        except (KeyError, OSError):
            pass
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
            "flatpak_browsers": [s["name"] for s in fp_state],
            "flatpak_ready": [s["name"] for s in fp_state if s["ready"]],
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
        usable, dropped = self.usable_here(candidates)
        if not usable and dropped:
            # We know this application and we know it cannot be installed here.
            # Staying silent would leave the user downloading a package that
            # will not install either -- Discord's own .deb is amd64 only, so
            # on arm64 the download is useless and nothing says so.
            name = self.display_name(dropped[0], url)
            send({"type": "done", "ok": False, "candidates": [],
                  "reason": reason,
                  "unavailable": {
                      "name": name,
                      "why": f"{name} is only packaged for Arch Linux. There is no "
                             f"{backends.distro_family()} package and no "
                             f"{MACHINE_ARCH} build on Flathub."}})
            return
        send({"type": "done", "ok": bool(usable),
              "candidates": usable, "reason": reason})

    @staticmethod
    def display_name(candidate, page_url):
        """A name worth showing a person.

        AUR entries carry packaging names like `discord_arch_electron`, which
        is not what the user came looking for. The site they are on is a better
        answer, so fall back to its domain label when the candidate name looks
        like a package rather than a product.
        """
        name = (candidate.get("name") or "").strip()
        looks_like_a_package = (not name) or any(ch in name for ch in "_-") \
            or name.islower()
        if looks_like_a_package:
            from urllib.parse import urlparse
            label = registrable(urlparse(page_url).hostname or "").split(".")[0]
            if label:
                return label[:1].upper() + label[1:]
        return name or candidate.get("id", "This application")

    def usable_here(self, candidates):
        """Drop candidates this machine could never install.

        The catalogue is shared across distributions, so it lists AUR packages
        that only mean anything on Arch. Offering one on Debian produces a
        window for something that cannot possibly install, which is worse than
        offering nothing at all.
        """
        if backends.distro_family() == "arch":
            return candidates, []
        kept, dropped = [], []
        for c in candidates:
            if str(c.get("id", "")).startswith("aur:") or c.get("backend") == "aur":
                dropped.append(c)
            else:
                kept.append(c)
        if dropped:
            log(f"dropped {len(dropped)} AUR candidate(s): not usable on "
                f"{backends.distro_family()}")
        return kept, dropped

    def op_install(self, req, peer, send):
        """Install a catalogue entry. The client never names a package."""
        app_id = req.get("id") or ""
        if (app_id.startswith("aur:") and backends.distro_family() != "arch"):
            send({"type": "done", "ok": False,
                  "reason": f"AUR packages are only usable on Arch; this is "
                            f"{backends.distro_family()}"})
            return
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

    def op_install_file(self, req, peer, send):
        """Install a package the vendor served directly.

        For a great many applications the vendor's own .deb or .rpm is the only
        thing that exists -- Discord publishes no Flathub build and is in no
        distribution's repositories -- and it is precisely the file the user
        just clicked. Installing it is what the tool is for; leaving it in
        Downloads is the behaviour we set out to replace.

        This is the least constrained thing the daemon does, so it is fenced:
        the file must belong to the caller, be a package format this system
        actually uses, be built for this architecture, and be authorised
        through polkit like any other privileged install.
        """
        raw = req.get("path") or ""
        domain = (req.get("domain") or "").lower()
        path = pathlib.Path(raw)

        try:
            real = path.resolve(strict=True)
        except OSError:
            send({"type": "done", "ok": False, "reason": f"no such file: {raw}"})
            return
        try:
            st = real.lstat()
        except OSError as e:
            send({"type": "done", "ok": False, "reason": str(e)})
            return
        if not stat.S_ISREG(st.st_mode):
            send({"type": "done", "ok": False, "reason": "not a regular file"})
            return
        # The caller must own what it is asking us to install, so one user
        # cannot talk the daemon into installing another user's file.
        if peer["uid"] not in (0, st.st_uid):
            send({"type": "done", "ok": False,
                  "reason": "that file does not belong to you"})
            return

        backend = next((b for b in backends.ALL
                        if b.needs_privilege and b.available()
                        and any(str(real).lower().endswith(sfx)
                                for sfx in b.local_suffixes)), None)
        if not backend:
            send({"type": "done", "ok": False,
                  "reason": f"{real.name} is not a package format "
                            f"{backends.distro_family()} installs"})
            return

        info = backend.inspect_local(real)
        if not info:
            send({"type": "done", "ok": False,
                  "reason": f"{real.name} is not a readable {backend.name} package"})
            return
        if not backends.arch_ok(info.get("arch")):
            send({"type": "done", "ok": False,
                  "reason": f"{real.name} is built for {info.get('arch')}, "
                            f"and this machine is {MACHINE_ARCH}"})
            return

        declared = info.get("homepage")
        if declared and domain:
            from urllib.parse import urlparse
            got = registrable(urlparse(declared).hostname or "")
            if got and got != registrable(domain):
                send({"type": "done", "ok": False,
                      "reason": f"refusing {info.get('name')}: it declares "
                                f"{got!r}, not {registrable(domain)!r}"})
                return
            send({"type": "log",
                  "line": f"{info.get('name')} declares {declared} — matches "
                          f"{registrable(domain)}\n"})
        else:
            send({"type": "log",
                  "line": f"{info.get('name')} declares no homepage; trusting "
                          f"{domain or 'the download'} it came from\n"})

        allowed, why = polkit_allows(peer["pid"], peer["uid"])
        if not allowed:
            send({"type": "done", "ok": False, "reason": why})
            return
        send({"type": "log", "line": f"authorized: {why}\n"})

        if not self.install_lock.acquire(blocking=False):
            send({"type": "done", "ok": False, "reason": "another install is in progress"})
            return
        try:
            cmd = backend.install_local_cmd(real)
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
            self.history.append({"id": info.get("name") or real.name,
                                 "backend": backend.name, "rc": rc,
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            send({"type": "done", "ok": rc == 0, "rc": rc,
                  "reason": "installed" if rc == 0 else f"{backend.name} exited {rc}"})
        finally:
            self.install_lock.release()

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

    # ---- AUR ------------------------------------------------------------
    #
    # The session agent builds AUR packages as the user; the daemon only ever
    # (a) installs official-repository dependencies for a catalogued AUR entry
    # and (b) installs the package the user built, after checking it is the
    # one the catalogue names. Neither accepts anything outside the catalogue.

    def _aur_entry(self, req, send):
        self.catalogue.maybe_reload()
        entry = self.catalogue.entry_for(req.get("id") or "")
        if not entry or entry.get("backend") != "aur":
            send({"type": "done", "ok": False,
                  "reason": f"{req.get('id')!r} is not an AUR package in the verified catalogue"})
            return None
        if not aur.can_build():
            send({"type": "done", "ok": False, "reason": "this system cannot build AUR packages"})
            return None
        return entry

    def _stream(self, cmd, send):
        send({"type": "log", "line": "$ " + " ".join(cmd) + "\n"})
        gen = backends.run_streaming(cmd)
        rc = 0
        try:
            while True:
                kind, payload = next(gen)
                send({"type": kind, ("line" if kind == "log" else "pct"): payload})
        except StopIteration as stop:
            rc = stop.value or 0
        return rc

    def op_aur_deps(self, req, peer, send):
        """Install official-repository dependencies of a catalogued AUR package."""
        entry = self._aur_entry(req, send)
        if not entry:
            return
        deps = req.get("deps") or []
        if not isinstance(deps, list) or not deps or len(deps) > 200:
            send({"type": "done", "ok": False, "reason": "no dependencies to install"})
            return
        for d in deps:
            if not isinstance(d, str) or not aur.valid_name(d):
                send({"type": "done", "ok": False, "reason": f"bad dependency name {d!r}"})
                return
            # -Sp resolves providers (java-runtime -> a JRE) and touches nothing.
            r = subprocess.run(["pacman", "-Sp", "--noconfirm", "--print-format", "%n", "--", d],
                               capture_output=True, text=True)
            if r.returncode != 0:
                send({"type": "done", "ok": False,
                      "reason": f"{d} is not in the official repositories"})
                return
        allowed, why = polkit_allows(peer["pid"], peer["uid"])
        if not allowed:
            send({"type": "done", "ok": False, "reason": why})
            return
        if not self.install_lock.acquire(blocking=False):
            send({"type": "done", "ok": False, "reason": "another install is in progress"})
            return
        try:
            rc = self._stream(["pacman", "-S", "--needed", "--asdeps", "--noconfirm", "--", *deps],
                              send)
            send({"type": "done", "ok": rc == 0,
                  "reason": "dependencies installed" if rc == 0 else f"pacman exited {rc}"})
        finally:
            self.install_lock.release()

    def op_install_aur(self, req, peer, send):
        """Install the package the caller built for a catalogued AUR entry."""
        import pwd, tempfile
        entry = self._aur_entry(req, send)
        if not entry:
            return
        if peer["uid"] < 0:
            send({"type": "done", "ok": False, "reason": "unknown caller"})
            return
        paths = req.get("paths") or []
        if not isinstance(paths, list) or not paths or len(paths) > 10:
            send({"type": "done", "ok": False, "reason": "no built package given"})
            return
        home = pwd.getpwuid(peer["uid"]).pw_dir
        for path in paths:
            ok, why = aur.validate_built_path(str(path), home, entry["pkgbase"], uid=peer["uid"])
            if not ok:
                send({"type": "done", "ok": False, "reason": f"refusing {path}: {why}"})
                return

        allowed, why = polkit_allows(peer["pid"], peer["uid"], action=POLKIT_AUR_ACTION)
        if not allowed:
            send({"type": "done", "ok": False, "reason": why})
            return
        if not self.install_lock.acquire(blocking=False):
            send({"type": "done", "ok": False, "reason": "another install is in progress"})
            return
        try:
            # Copy first, then inspect the copy: the user can change their own
            # files at any moment, but not these.
            AUR_STAGING.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=AUR_STAGING) as stage:
                chosen = None
                for path in paths:
                    src = pathlib.Path(path)
                    dst = pathlib.Path(stage) / src.name
                    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as fin, open(dst, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    name = aur.package_name_of(dst)
                    if name == entry["aur_name"]:
                        chosen = dst
                if not chosen:
                    send({"type": "done", "ok": False,
                          "reason": f"none of the built files is {entry['aur_name']}"})
                    return
                send({"type": "log", "line": f"verified {chosen.name} is "
                                             f"{entry['aur_name']}\n"})
                rc = self._stream(["pacman", "-U", "--noconfirm", "--", str(chosen)], send)
            self.history.append({"id": entry["id"], "backend": "aur", "rc": rc,
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            send({"type": "done", "ok": rc == 0, "rc": rc,
                  "reason": "installed" if rc == 0 else f"pacman exited {rc}"})
        finally:
            self.install_lock.release()

    OPS = {"status": op_status, "resolve": op_resolve, "install": op_install,
           "install-file": op_install_file,
           "provision": op_provision, "config-get": op_config_get,
           "config-set": op_config_set, "aur-deps": op_aur_deps,
           "install-aur": op_install_aur}

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
        if crx is None:
            # Writing a preinstall manifest that names a missing CRX leaves the
            # browser silently unable to install the sensor, which looks exactly
            # like the product not working.
            log("no sensor.crx found; browsers will not be given the extension")
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
                entry = json.dumps({"external_crx": str(crx),
                                    "external_version": version}, indent=2) + "\n"
                # Deliberately NOT written to the system-wide extensions
                # directory. Debian's Chromium treats entries there as unpacked
                # extension directories, and the resulting error aborts its
                # external-extension provider before the per-profile manifest is
                # ever read -- so writing it does not merely fail, it actively
                # prevents the sensor from installing at all. The per-profile
                # location is honoured by every browser tested (Chromium, Brave,
                # Vivaldi) and is what makes the mechanism work.
                self.clear_stale_system_manifests(b, ext_id)
                wrote += self.preinstall_per_user(b, ext_id, entry, force)

            # The host manifest has to reach the profile too, or the sensor
            # loads, registers its listeners, and silently cannot call out.
            wrote += self.write_per_user(
                b, "NativeMessagingHosts", f"{prov.HOST_NAME}.json",
                json.dumps(manifest, indent=2) + "\n", force)
        return wrote

    def clear_stale_system_manifests(self, browser, ext_id):
        """Remove a system-wide manifest an older version of us may have left.

        Leaving one behind would keep the browser from reading the per-profile
        manifest, so an upgrade must clean up after the previous behaviour.
        """
        for d in browser["system_ext"]:
            target = d / f"{ext_id}.json"
            if target.exists():
                try:
                    target.unlink()
                    log(f"removed stale system manifest {target}")
                except OSError:
                    pass

    def preinstall_per_user(self, browser, ext_id, entry, force):
        """Write the preinstall manifest into each existing user profile."""
        return self.write_per_user(browser, "External Extensions",
                                   f"{ext_id}.json", entry, force)

    def write_per_user(self, browser, subdir, filename, content, force):
        """Write one file into each existing user's profile for this browser.

        Both of the files a browser needs from us live here, and neither works
        reliably from the system-wide location:

          External Extensions/   Debian's Chromium treats entries in the
                                 system directory as unpacked extension
                                 directories and errors on the JSON.
          NativeMessagingHosts/  Brave does not read the system directory at
                                 all. The manifest sat in
                                 /etc/brave/native-messaging-hosts/ while the
                                 browser looked in the profile, so every
                                 download was let through and nothing was
                                 logged -- the product appeared to do nothing.

        Note the spelling difference: the system directories are
        `native-messaging-hosts`, the per-profile one is `NativeMessagingHosts`.
        """
        wrote = 0
        rel = browser["config"].relative_to(pathlib.Path(os.path.expanduser("~"))) \
            if str(browser["config"]).startswith(os.path.expanduser("~")) else None
        if rel is None:
            return 0
        seen = set()
        for home in pathlib.Path("/home").glob("*"):
            if not home.is_dir():
                continue
            real = home.resolve()
            if real in seen:          # /home/x -> /home/y symlinks are common
                continue
            seen.add(real)
            profile = real / rel
            # Only write into a profile that already exists. Creating one as
            # root leaves the whole profile directory root-owned, and the
            # browser -- which runs as the user -- then cannot use it at all.
            # That is far worse than waiting: a profile appears the moment
            # someone runs the browser, and the next poll provisions it.
            if not profile.is_dir():
                continue
            d = profile / subdir
            target = d / filename
            if not force and target.exists():
                continue
            try:
                st = home.stat()
                d.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                # The browser runs as that user, so the files must be theirs.
                os.chown(d, st.st_uid, st.st_gid)
                os.chown(target, st.st_uid, st.st_gid)
                log(f"provisioned {browser['name']} ({home.name}) -> {target}")
                wrote += 1
            except OSError as e:
                log(f"could not provision {browser['name']} in {home}: {e}")
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
