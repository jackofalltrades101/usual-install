#!/usr/bin/env python3
"""Build a catalogued AUR package as the logged-in user.

Driven by the installer window. Every step runs as the user, in
~/.cache/usual-install/aur/<pkgbase>; only the two steps that need root go to
usuald, which re-checks them against its own catalogue:

  1. confirm the package still has the maintainer the catalogue vetted
  2. download the AUR snapshot and unpack it
  3. read its dependencies from `makepkg --printsrcinfo`
  4. missing official-repository dependencies -> usuald `aur-deps`
     (dependencies that are themselves AUR packages are refused, not chased)
  5. `makepkg` as the user
  6. the built package -> usuald `install-aur`, behind a password prompt
"""
import os, pathlib, shutil, subprocess, sys, tarfile, urllib.request

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "daemon"))
import aur  # noqa: E402
import client as daemon_client  # noqa: E402

SNAPSHOT_PREFIX = "/cgit/aur.git/snapshot/"


class BuildError(Exception):
    pass


def _run(cmd, cwd, log, env=None, proc_holder=None):
    log("$ " + " ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    if proc_holder is not None:
        proc_holder.append(proc)
    for line in proc.stdout:
        log(line)
    return proc.wait()


def _daemon(op, log, **kw):
    """Run a streaming usuald op; returns (ok, reason)."""
    try:
        for msg in daemon_client.request(op, **kw):
            if msg.get("type") == "log":
                log(msg.get("line", ""))
            elif msg.get("type") == "done":
                return bool(msg.get("ok")), msg.get("reason") or ""
    except daemon_client.DaemonUnavailable:
        return False, "usuald is not running"
    return False, "usuald closed the connection"


def _safe_extract(tar_path, dest):
    with tarfile.open(tar_path) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        root = pathlib.Path(dest).resolve()
        for m in tf.getmembers():
            target = (root / m.name).resolve()
            if not str(target).startswith(str(root) + os.sep) or m.issym() or m.islnk() \
                    or m.isdev():
                raise BuildError(f"unsafe path in snapshot: {m.name}")
        tf.extractall(dest)


def missing_deps(names):
    """Of these dependency names, which are not satisfied on this system?"""
    if not names:
        return []
    r = subprocess.run(["pacman", "-T", "--", *names], capture_output=True, text=True)
    return [aur.dep_name(l) for l in r.stdout.split() if l.strip()]


def in_repos(name):
    r = subprocess.run(["pacman", "-Sp", "--noconfirm", "--print-format", "%n", "--", name],
                       capture_output=True, text=True)
    return r.returncode == 0


def build_and_install(entry, log, status, proc_holder=None):
    """Returns (ok, message). `log` takes text, `status` a short phase label."""
    name, base = entry.get("aur_name", ""), entry.get("pkgbase", "")
    if not (aur.valid_name(name) and aur.valid_name(base)):
        return False, "the catalogue entry is malformed"
    if os.geteuid() == 0:
        return False, "AUR packages are never built as root"

    status("Checking the AUR…")
    try:
        live = aur.current_info(name)
    except Exception as e:  # network, JSON
        return False, f"could not reach the AUR ({e})"
    ok, why = aur.maintainer_unchanged(entry, live)
    if not ok:
        return False, why
    log(f"{name}: maintained by {live.get('Maintainer')}, "
        f"{live.get('NumVotes')} votes, version {live.get('Version')}\n")

    status("Downloading the build script…")
    snap = entry.get("snapshot") or f"{SNAPSHOT_PREFIX}{base}.tar.gz"
    if not snap.startswith(SNAPSHOT_PREFIX):
        return False, f"unexpected snapshot location {snap!r}"
    work = aur.build_root(pathlib.Path.home()) / base
    if work.exists():
        shutil.rmtree(work)
    (work / "out").mkdir(parents=True)
    tgz = work / "snapshot.tar.gz"
    req = urllib.request.Request(aur.AUR + snap, headers={"User-Agent": "usual-install"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tgz, "wb") as f:
        shutil.copyfileobj(r, f)
    try:
        _safe_extract(tgz, work)
    except (tarfile.TarError, BuildError) as e:
        return False, f"bad snapshot: {e}"
    src = work / base
    if not (src / "PKGBUILD").is_file():
        return False, "the snapshot has no PKGBUILD"

    status("Reading dependencies…")
    r = subprocess.run(["makepkg", "--printsrcinfo"], cwd=src, capture_output=True, text=True)
    if r.returncode != 0:
        return False, "makepkg could not read the PKGBUILD"
    info = aur.parse_srcinfo(r.stdout, arch=os.uname().machine)
    wanted = sorted(set(info["depends"]) | set(info["makedepends"]) | {"base-devel"})
    missing = missing_deps(wanted)
    not_in_repos = [d for d in missing if not in_repos(d)]
    if not_in_repos:
        return False, ("it depends on other AUR packages (" + ", ".join(not_in_repos)
                       + "), which Usual Install does not build automatically")
    if missing:
        status("Installing dependencies…")
        log(f"needs from the official repositories: {' '.join(missing)}\n")
        ok, why = _daemon("aur-deps", log, id=entry["id"], deps=missing)
        if not ok:
            return False, f"dependencies: {why}"

    status("Building… (this can take a few minutes)")
    env = dict(os.environ, PKGDEST=str(work / "out"))
    rc = _run(["makepkg", "--force", "--cleanbuild", "--noconfirm", "--nocheck"],
              cwd=src, log=log, env=env, proc_holder=proc_holder)
    if rc != 0:
        return False, f"the build failed (makepkg exited {rc})"
    built = sorted(str(p) for p in (work / "out").iterdir()
                   if p.name.endswith(aur.PKG_SUFFIXES) and "-debug-" not in p.name)
    if not built:
        return False, "the build produced no package"

    status("Waiting for your password…")
    ok, why = _daemon("install-aur", log, id=entry["id"], paths=built)
    if not ok:
        return False, why
    return True, "installed"


def desktop_entry(name):
    """The .desktop file an installed package ships, for the Open button."""
    r = subprocess.run(["pacman", "-Qlq", name], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("/usr/share/applications/") and line.endswith(".desktop"):
            return pathlib.Path(line).stem
    return None
