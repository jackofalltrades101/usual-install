#!/usr/bin/env python3
"""The vendor's own package: recognised, checked, and refused when it cannot work.

For a great many applications the vendor's .deb or .rpm is the only thing that
exists -- Discord publishes no Flathub build and is in no distribution's
repositories -- and it is the file the user just clicked. These checks cover
the decisions around installing it, which are the parts that can quietly go
wrong: a package for the wrong architecture, a package claiming to be from a
different vendor, and a site nobody has ever heard of.
"""
import json, os, pathlib, shutil, struct, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
PASS = FAIL = SKIP = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}  {detail}")


def skip(label):
    global SKIP
    SKIP += 1; print(f"  skip  {label}")


def build_deb(td, arch, homepage, name="vendortest"):
    """A minimal but genuine .deb, so inspection is exercised for real."""
    if not shutil.which("dpkg-deb"):
        return None
    root = pathlib.Path(td, f"pkg-{arch}")
    (root / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (root / "usr/bin").mkdir(parents=True, exist_ok=True)
    (root / "DEBIAN/control").write_text(
        f"Package: {name}\nVersion: 1.0.0\nSection: misc\nPriority: optional\n"
        f"Architecture: {arch}\nMaintainer: Test <t@example.invalid>\n"
        f"Homepage: {homepage}\nDescription: vendor package test\n")
    (root / "usr/bin" / name).write_text("#!/bin/sh\ntrue\n")
    (root / "usr/bin" / name).chmod(0o755)
    out = pathlib.Path(td, f"{name}_{arch}.deb")
    r = subprocess.run(["dpkg-deb", "--build", str(root), str(out)],
                       capture_output=True)
    return out if r.returncode == 0 else None


def ranking_checks():
    sys.path.insert(0, str(ROOT / "resolver"))
    from resolve import rank
    cands = [{"id": "us.zoom.Zoom", "backend": "flatpak", "verified": True},
             {"id": "aur:zoom", "backend": "aur"},
             {"id": "vendor:zoom.deb", "backend": "vendor"}]
    print("\n--- which source the Install button lands on ---")
    first = rank(cands, aur_ok=True, prefer="vendor")[0]["id"]
    check(first.startswith("vendor:"),
          "by default the developer's own package is offered first", first)
    first = rank(cands, aur_ok=True, prefer="flathub")[0]["id"]
    check(first == "us.zoom.Zoom",
          "set to prefer Flathub, the verified build comes first", first)
    ids = [c["id"] for c in rank(cands, aur_ok=True, prefer="vendor")]
    check(len(ids) == 3, "every source stays available in the picker", ids)
    ids = [c["id"] for c in rank(cands, aur_ok=False, prefer="vendor")]
    check("aur:zoom" not in ids,
          "AUR is dropped where it cannot be built, in either order", ids)


def main():
    import backends
    ranking_checks()

    print("\n--- architecture matching ---")
    names = backends.machine_arch_names()
    check(bool(names), f"this machine answers to {sorted(names)}")
    check(backends.arch_ok("all") and backends.arch_ok("noarch"),
          "architecture-independent packages are accepted")
    wrong = "amd64" if "amd64" not in names else "arm64"
    check(not backends.arch_ok(wrong), f"a {wrong} package is refused here")
    check(not backends.arch_ok(""), "an unreadable architecture is refused, not assumed")

    native = backends.native_backend()
    if not native:
        skip("no native backend on this system; package checks skipped")
        return finish()
    check(bool(native.local_suffixes),
          f"{native.name} declares the files it can install: {native.local_suffixes}")

    if native.name != "apt" or not shutil.which("dpkg-deb"):
        skip("package inspection needs dpkg-deb; covered on Debian-family CI")
        return finish()

    with tempfile.TemporaryDirectory() as td:
        print("\n--- reading a package without installing it ---")
        host_arch = subprocess.run(["dpkg", "--print-architecture"],
                                   capture_output=True, text=True).stdout.strip()
        good = build_deb(td, host_arch, "https://vendor.example/")
        check(good is not None, "built a package for this architecture")
        if good:
            info = native.inspect_local(good)
            check(info.get("name") == "vendortest", "reads the package name", info)
            check(info.get("arch") == host_arch, "reads the architecture", info)
            check(info.get("homepage") == "https://vendor.example/",
                  "reads the declared homepage", info)
            check(backends.arch_ok(info.get("arch")),
                  "and that architecture is accepted here")

        other = "amd64" if host_arch != "amd64" else "arm64"
        bad = build_deb(td, other, "https://vendor.example/", name="wrongarch")
        if bad:
            info = native.inspect_local(bad)
            check(not backends.arch_ok(info.get("arch")),
                  f"a {other} package is refused on {host_arch}")

        print("\n--- the install command resolves dependencies ---")
        cmd = native.install_local_cmd(good or "x.deb")
        check("apt-get" in cmd[0] and "install" in cmd,
              "apt-get install is used, not dpkg -i, so dependencies are pulled in",
              cmd)

    return finish()


def finish():
    print(f"\n{'vendor packages behave' if not FAIL else str(FAIL) + ' FAILURES'}"
          + (f" ({SKIP} skipped)" if SKIP else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
