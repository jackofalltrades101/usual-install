#!/usr/bin/env python3
"""Package-manager backends.

Phase 1 had one backend, `flatpak --user`, which needs no privilege. This adds
the native repositories, which do. Each backend is a thin, auditable wrapper:
it takes a package name that the caller has already validated against the
signed catalogue, and streams output back.

Nothing here decides *what* to install. That is the resolver's job, and keeping
the decision out of the privileged process is the whole point of the split.

    ./backends.py            # report what this machine has
"""
import os, pathlib, re, shutil, subprocess, sys

PCT = re.compile(r"(\d{1,3})\s*%")


def os_release():
    info = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in pathlib.Path(p).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k] = v.strip().strip('"')
            break
        except OSError:
            continue
    return info


def distro_family():
    """arch | debian | fedora | unknown, from ID and ID_LIKE."""
    info = os_release()
    ids = [info.get("ID", "")] + info.get("ID_LIKE", "").split()
    for i in ids:
        if i in ("arch", "archlinux"):
            return "arch"
        if i in ("debian", "ubuntu"):
            return "debian"
        if i in ("fedora", "rhel", "centos"):
            return "fedora"
    return "unknown"


class Backend:
    """One way of installing a package."""

    name = "none"
    needs_privilege = False

    def available(self):
        return False

    def is_installed(self, pkg):
        return False

    def install_cmd(self, pkg):
        raise NotImplementedError

    def launch_cmd(self, pkg):
        return None

    # Downloaded files this backend can install directly. A vendor's own
    # package is often the only thing that exists for an application -- Discord
    # ships a .deb and nothing else -- and it is the file the user just clicked.
    local_suffixes = ()

    def install_local_cmd(self, path):
        raise NotImplementedError

    def inspect_local(self, path):
        """Return {name, arch, homepage} read out of the package itself.

        Used to refuse a package that cannot work here before touching it:
        Discord's .deb is amd64 and installing it on arm64 only produces a
        confusing failure part-way through.
        """
        return {}

    def update_info(self, pkg):
        """(installed, available) versions, or (None, None) if not known.

        A package installed from a vendor's own .deb only has an upgrade path
        if that package also configured a repository. Many do not, and saying
        so is the point: a vendor package you install by hand is one you keep
        up to date by hand.
        """
        return (None, None)

    def homepage(self, pkg):
        """The homepage the DISTRO records for this package, or None.

        This is the native equivalent of Flathub's declared homepage, and it is
        what makes a native install safe: the package name is only a guess
        until the machine's own package metadata agrees that the package really
        belongs to the vendor whose page the user was on.
        """
        return None


class FlatpakUser(Backend):
    """User-scope Flathub. No root, no polkit, no prompt -- always preferred."""

    name = "flatpak-user"
    needs_privilege = False

    def available(self):
        return shutil.which("flatpak") is not None

    def is_installed(self, pkg):
        return subprocess.run(["flatpak", "--user", "info", pkg],
                              capture_output=True).returncode == 0

    def install_cmd(self, pkg):
        return ["flatpak", "--user", "install", "-y", "--noninteractive", "flathub", pkg]

    def launch_cmd(self, pkg):
        return ["flatpak", "run", pkg]


class Pacman(Backend):
    name = "pacman"
    needs_privilege = True

    def available(self):
        return shutil.which("pacman") is not None

    def is_installed(self, pkg):
        return subprocess.run(["pacman", "-Q", pkg], capture_output=True).returncode == 0

    def install_cmd(self, pkg):
        return ["pacman", "-S", "--noconfirm", "--needed", pkg]

    def update_info(self, pkg):
        q = subprocess.run(["pacman", "-Q", pkg], capture_output=True, text=True)
        installed = q.stdout.split()[1] if q.returncode == 0 and q.stdout.split() else None
        u = subprocess.run(["pacman", "-Qu", pkg], capture_output=True, text=True)
        available = installed
        if u.returncode == 0 and u.stdout.strip():
            parts = u.stdout.split()
            if len(parts) >= 4:
                available = parts[-1]
        return (installed, available)

    local_suffixes = (".pkg.tar.zst", ".pkg.tar.xz")

    def install_local_cmd(self, path):
        return ["pacman", "-U", "--noconfirm", str(path)]

    def inspect_local(self, path):
        try:
            out = subprocess.run(["bsdtar", "-xOf", str(path), ".PKGINFO"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        info = {}
        for line in out.splitlines():
            if "=" not in line:
                continue
            k, v = (x.strip() for x in line.split("=", 1))
            if k == "pkgname": info["name"] = v
            elif k == "arch": info["arch"] = v
            elif k == "url": info["homepage"] = v
        return info

    def homepage(self, pkg):
        r = subprocess.run(["pacman", "-Si", pkg], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if line.startswith("URL"):
                return line.split(":", 1)[1].strip()
        return None


class Apt(Backend):
    name = "apt"
    needs_privilege = True

    def available(self):
        return shutil.which("apt-get") is not None

    def is_installed(self, pkg):
        r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg],
                           capture_output=True, text=True)
        return r.returncode == 0 and "install ok installed" in r.stdout

    def install_cmd(self, pkg):
        return ["apt-get", "install", "-y", "--no-install-recommends", pkg]

    def env(self):
        return dict(os.environ, DEBIAN_FRONTEND="noninteractive")

    def update_info(self, pkg):
        r = subprocess.run(["apt-cache", "policy", pkg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return (None, None)
        inst = cand = None
        from_repo = False
        for line in r.stdout.splitlines():
            t = line.strip()
            if t.startswith("Installed:"):
                inst = t.split(":", 1)[1].strip()
            elif t.startswith("Candidate:"):
                cand = t.split(":", 1)[1].strip()
            elif "://" in t:
                # apt reports a Candidate for anything installed, including a
                # .deb that configured no repository. Only a remote source in
                # the version table means there is somewhere to update from.
                from_repo = True
        none = ("(none)", "")
        installed = None if inst in none else inst
        available = None if (cand in none or not from_repo) else cand
        return (installed, available)

    local_suffixes = (".deb",)

    def install_local_cmd(self, path):
        # apt resolves the package's dependencies; dpkg -i would not.
        return ["apt-get", "install", "-y", "--no-install-recommends", str(path)]

    def inspect_local(self, path):
        try:
            out = subprocess.run(
                ["dpkg-deb", "-f", str(path), "Package", "Architecture", "Homepage"],
                capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        info = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            kl = k.lower()
            if kl == "package": info["name"] = v
            elif kl == "architecture": info["arch"] = v
            elif kl == "homepage": info["homepage"] = v
        return info

    def homepage(self, pkg):
        r = subprocess.run(["apt-cache", "show", pkg], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if line.lower().startswith("homepage:"):
                return line.split(":", 1)[1].strip()
        return None


class Dnf(Backend):
    name = "dnf"
    needs_privilege = True

    def available(self):
        return shutil.which("dnf") is not None or shutil.which("dnf5") is not None

    def is_installed(self, pkg):
        return subprocess.run(["rpm", "-q", pkg], capture_output=True).returncode == 0

    def install_cmd(self, pkg):
        dnf = shutil.which("dnf5") or shutil.which("dnf")
        return [dnf, "install", "-y", pkg]

    def update_info(self, pkg):
        dnf = shutil.which("dnf5") or shutil.which("dnf")
        inst = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg],
                              capture_output=True, text=True)
        installed = inst.stdout.strip() if inst.returncode == 0 else None
        up = subprocess.run([dnf, "-q", "list", "--upgrades", pkg],
                            capture_output=True, text=True)
        available = None
        for line in up.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].split(".")[0] == pkg:
                available = parts[1]
        return (installed, available or installed)

    local_suffixes = (".rpm",)

    def install_local_cmd(self, path):
        dnf = shutil.which("dnf5") or shutil.which("dnf")
        return [dnf, "install", "-y", str(path)]

    def inspect_local(self, path):
        try:
            out = subprocess.run(
                ["rpm", "-qp", "--qf", "%{NAME}\\n%{ARCH}\\n%{URL}\\n", str(path)],
                capture_output=True, text=True, timeout=30).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return {}
        info = {}
        if len(out) > 0: info["name"] = out[0].strip()
        if len(out) > 1: info["arch"] = out[1].strip()
        if len(out) > 2 and out[2].strip() not in ("(none)", ""):
            info["homepage"] = out[2].strip()
        return info

    def homepage(self, pkg):
        dnf = shutil.which("dnf5") or shutil.which("dnf")
        r = subprocess.run([dnf, "info", pkg], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            low = line.lower()
            if low.startswith("url") or low.startswith("source"):
                if ":" in line:
                    v = line.split(":", 1)[1].strip()
                    if v.startswith("http"):
                        return v
        return None


# What each packaging system calls "runs anywhere".
ANY_ARCH = {"all", "noarch", "any"}


def machine_arch_names():
    """The names this machine answers to, across packaging systems.

    dpkg says amd64 where rpm says x86_64, and a package built for the wrong
    one installs into a broken state rather than failing cleanly.
    """
    uname = os.uname().machine if hasattr(os, "uname") else ""
    names = {uname}
    try:
        dpkg = subprocess.run(["dpkg", "--print-architecture"],
                              capture_output=True, text=True, timeout=10)
        if dpkg.returncode == 0:
            names.add(dpkg.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    equivalents = {"x86_64": "amd64", "amd64": "x86_64",
                   "aarch64": "arm64", "arm64": "aarch64",
                   "armv7l": "armhf", "armhf": "armv7l"}
    for n in list(names):
        if n in equivalents:
            names.add(equivalents[n])
    return {n for n in names if n}


def arch_ok(pkg_arch):
    """True when a package of this architecture can run on this machine."""
    a = (pkg_arch or "").strip().lower()
    if not a:
        return False          # unknown is not the same as fine
    return a in ANY_ARCH or a in machine_arch_names()


ALL = [FlatpakUser(), Pacman(), Apt(), Dnf()]
NATIVE_FOR_FAMILY = {"arch": "pacman", "debian": "apt", "fedora": "dnf"}


def by_name(name):
    for b in ALL:
        if b.name == name:
            return b
    return None


def native_backend():
    """The right native backend for this distro, if it is installed."""
    want = NATIVE_FOR_FAMILY.get(distro_family())
    b = by_name(want) if want else None
    return b if (b and b.available()) else None


def available_backends():
    return [b for b in ALL if b.available()]


def run_streaming(cmd, env=None):
    """Yield (kind, payload) tuples: ('log', line) and ('progress', pct).

    Returns the exit code. Package managers differ wildly in how they report
    progress, so a percentage is parsed when one appears and the log is always
    streamed -- a build log is better than a lying progress bar.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
    except FileNotFoundError:
        yield ("log", f"*** {cmd[0]} not found\n")
        return 127
    for line in proc.stdout:
        yield ("log", line)
        m = PCT.search(line)
        if m:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                yield ("progress", pct)
    return proc.wait()


def main():
    import term
    fam = distro_family()
    info = os_release()
    term.heading("Install methods on this computer")
    term.row("Distribution", f"{info.get('PRETTY_NAME', '?')} ({fam} family)")
    term.section("Methods")
    for b in ALL:
        mark = term.good("available") if b.available() else term.muted("not installed")
        priv = "asks for a password" if b.needs_privilege else "no password"
        term.row(b.name, f"{mark}  {term.muted(priv)}")
    nb = native_backend()
    term.section("System packages")
    term.row("Uses", nb.name if nb else term.muted("none on this system"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
