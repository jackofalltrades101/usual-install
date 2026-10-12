#!/usr/bin/env python3
"""AUR support shared by the session agent (which builds) and usuald (which installs).

The AUR is where an Arch beginner gets stuck, so Usual Install handles it --
but it is also arbitrary code, so the rules are stricter than for Flathub or
the official repositories:

  * Only packages in the signed catalogue are offered. The catalogue takes an
    AUR package only if it is maintained, not flagged out of date, not a VCS
    (-git) build, has enough votes, and declares the vendor's site as its URL.
  * The maintainer is re-checked at install time. A package that changed hands
    since the catalogue was built is refused, which is how AUR takeovers work.
  * makepkg runs as the user, never as root, in a directory the user owns.
  * Installing the built package goes through its own polkit action that asks
    for a password every time -- the Linux equivalent of the Windows UAC prompt
    an .exe installer shows. usuald copies the file somewhere the user cannot
    touch before inspecting it, then checks it is the package the catalogue
    named.
  * Dependencies must come from the official repositories. Chains of AUR
    packages are refused rather than built silently.

    ./aur.py --self-test
"""
import json, os, pathlib, re, subprocess, sys, urllib.parse, urllib.request

AUR = "https://aur.archlinux.org"
RPC_INFO = AUR + "/rpc/v5/info?arg[]={name}"
VCS_SUFFIXES = ("-git", "-hg", "-svn", "-bzr", "-darcs", "-fossil", "-cvs")
# Package names as pacman allows them; anything else is never passed to a command.
NAME_RE = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]*$")
PKG_SUFFIXES = (".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz", ".pkg.tar")
DEP_FIELDS = ("depends", "makedepends", "checkdepends")


def valid_name(name):
    return bool(name) and len(name) <= 128 and NAME_RE.match(name) is not None


def dep_name(dep):
    """'python>=3.11: for scripts' -> 'python'. Drops version and description."""
    dep = dep.split(":", 1)[0].strip()
    return re.split(r"[<>=]", dep, maxsplit=1)[0].strip()


def parse_srcinfo(text, arch="x86_64"):
    """Dependencies from `makepkg --printsrcinfo`, merged across pkgbase and pkgnames.

    Returns {"depends": [...], "makedepends": [...], "checkdepends": [...]},
    each a sorted list of bare names, including the <field>_<arch> variants for
    this machine's architecture.
    """
    out = {f: set() for f in DEP_FIELDS}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, val = (s.strip() for s in line.split("=", 1))
        for f in DEP_FIELDS:
            if key == f or key == f"{f}_{arch}":
                name = dep_name(val)
                if name:
                    out[f].add(name)
    return {f: sorted(v) for f, v in out.items()}


def build_root(home):
    return pathlib.Path(home) / ".cache" / "usual-install" / "aur"


def validate_built_path(path, home, pkgbase, uid=None):
    """Is this a package file the session agent legitimately built?

    It must be a regular file, named like a pacman package, owned by the caller
    when a uid is given, and sit directly inside the caller's own build
    directory for this package base, with no symlink anywhere between the home
    directory and the file. Returns (ok, reason).
    """
    if not valid_name(pkgbase):
        return False, f"bad package base {pkgbase!r}"
    p = pathlib.Path(path)
    if not p.is_absolute():
        return False, "path must be absolute"
    if not p.name.endswith(PKG_SUFFIXES):
        return False, f"{p.name} is not a package file"
    home = pathlib.Path(home)
    cur = home
    for part in (".cache", "usual-install", "aur", pkgbase, "out", p.name):
        cur = cur / part
        if cur.is_symlink():
            return False, f"refusing a symlink at {cur}"
    want = (build_root(home) / pkgbase / "out").resolve()
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return False, "no such file"
    if real.parent != want or real.name != p.name:
        return False, f"{real} is outside {want}"
    if not real.is_file():
        return False, "not a regular file"
    if uid is not None and real.stat().st_uid != uid:
        return False, "the package file is not owned by the caller"
    return True, "ok"


def package_name_of(pkgfile):
    """The Name field of a built package, read by pacman itself."""
    r = subprocess.run(["pacman", "-Qip", str(pkgfile)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("Name"):
            return line.split(":", 1)[1].strip()
    return None


def current_info(name, timeout=20):
    """Live AUR metadata for one package, or None."""
    url = RPC_INFO.format(name=urllib.parse.quote(name))
    req = urllib.request.Request(url, headers={"User-Agent": "usual-install"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    results = data.get("results") or []
    return results[0] if results else None


def maintainer_unchanged(entry, live):
    """Refuse a package whose maintainer changed since the catalogue was signed."""
    if not live:
        return False, f"{entry.get('aur_name')} is no longer in the AUR"
    if live.get("Maintainer") != entry.get("maintainer"):
        return False, (f"{entry.get('aur_name')} is now maintained by "
                       f"{live.get('Maintainer') or 'nobody'}, not "
                       f"{entry.get('maintainer')} as when it was vetted")
    if live.get("OutOfDate"):
        return False, f"{entry.get('aur_name')} has been flagged out of date"
    return True, "maintainer unchanged"


def can_build():
    """Is this an Arch-family machine that could build AUR packages at all?"""
    from shutil import which
    return which("pacman") is not None and which("makepkg") is not None


# --------------------------------------------------------------------------- #

def _self_test():
    import tempfile
    ok = True

    def check(cond, label):
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
        ok = ok and cond

    check(dep_name("python>=3.11: for scripts") == "python", "dep_name strips version and note")
    check(dep_name("java-runtime=17") == "java-runtime", "dep_name strips =version")
    check(valid_name("minecraft-launcher") and valid_name("lib32-gcc-libs"), "valid names accepted")
    check(not valid_name("foo;rm -rf /") and not valid_name("-x") and not valid_name(""),
          "hostile names rejected")

    info = parse_srcinfo("""pkgbase = minecraft-launcher
	pkgver = 2.2.1
	makedepends = tar
	depends = java-runtime>=17
	depends = gtk3
	depends_x86_64 = lib32-glibc
	depends_aarch64 = only-on-arm
pkgname = minecraft-launcher
	depends = libsecret
""")
    check(info["depends"] == ["gtk3", "java-runtime", "lib32-glibc", "libsecret"],
          "srcinfo: depends merged, arch-specific kept, other arch dropped")
    check(info["makedepends"] == ["tar"], "srcinfo: makedepends")

    with tempfile.TemporaryDirectory() as home:
        out = build_root(home) / "minecraft-launcher" / "out"
        out.mkdir(parents=True)
        good = out / "minecraft-launcher-2.2.1-1-x86_64.pkg.tar.zst"
        good.write_bytes(b"x")
        check(validate_built_path(str(good), home, "minecraft-launcher")[0],
              "built package in the right place is accepted")
        check(not validate_built_path(str(good), home, "other-base")[0],
              "same file under a different package base is refused")
        link = out / "evil-1-1-x86_64.pkg.tar.zst"
        link.symlink_to("/etc/shadow")
        check(not validate_built_path(str(link), home, "minecraft-launcher")[0],
              "symlink is refused")
        outside = pathlib.Path(home) / "x-1-1-any.pkg.tar.zst"
        outside.write_bytes(b"x")
        check(not validate_built_path(str(outside), home, "minecraft-launcher")[0],
              "file outside the build directory is refused")
        check(not validate_built_path(str(good), home, "minecraft-launcher",
                                      uid=os.getuid() + 1)[0],
              "file owned by someone else is refused")
        elsewhere = pathlib.Path(home) / "elsewhere"; elsewhere.mkdir()
        (elsewhere / "y-1-1-any.pkg.tar.zst").write_bytes(b"x")
        lb = build_root(home) / "linked-base"; lb.mkdir()
        (lb / "out").symlink_to(elsewhere)
        check(not validate_built_path(str(lb / "out" / "y-1-1-any.pkg.tar.zst"),
                                      home, "linked-base")[0],
              "symlinked build directory is refused")
        dotdot = str(out / ".." / ".." / "x-1-1-any.pkg.tar.zst")
        check(not validate_built_path(dotdot, home, "minecraft-launcher")[0],
              "../ traversal is refused")
        txt = out / "notes.txt"; txt.write_text("x")
        check(not validate_built_path(str(txt), home, "minecraft-launcher")[0],
              "non-package file is refused")

    entry = {"aur_name": "minecraft-launcher", "maintainer": "alice"}
    check(maintainer_unchanged(entry, {"Maintainer": "alice", "OutOfDate": None})[0],
          "same maintainer passes")
    check(not maintainer_unchanged(entry, {"Maintainer": "mallory", "OutOfDate": None})[0],
          "changed maintainer is refused")
    check(not maintainer_unchanged(entry, {"Maintainer": None, "OutOfDate": None})[0],
          "orphaned package is refused")
    check(not maintainer_unchanged(entry, None)[0], "deleted package is refused")

    print("\nall passed" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
