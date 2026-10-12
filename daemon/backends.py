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
    fam = distro_family()
    info = os_release()
    print(f"distro : {info.get('PRETTY_NAME', '?')}  (family: {fam})")
    print("backends:")
    for b in ALL:
        mark = "available" if b.available() else "-"
        priv = "needs root" if b.needs_privilege else "rootless"
        print(f"  {b.name:14} {mark:10} {priv}")
    nb = native_backend()
    print(f"\nnative backend for this machine: {nb.name if nb else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
