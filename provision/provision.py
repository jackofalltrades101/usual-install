#!/usr/bin/env python3
"""Provision Usual Install into every Chromium-family browser on the machine.

This is the piece that makes "the user never installs an extension" true. In
Phase 1 it runs per-user (no root) so it can be tested without a daemon; the
same table drives usuald's system-wide provisioning in Phase 2.

    ./provision.py keygen          # stable extension id, written into the manifest
    ./provision.py status          # what's installed where
    ./provision.py install         # native messaging host, all detected browsers
    ./provision.py install --preinstall --crx /path/to/sensor.crx
    ./provision.py uninstall

`install --preinstall` exercises the external-extension mechanism, which is the
load-bearing unknown for the whole design: Chromium has been narrowing local
`external_crx` support in favour of `external_update_url`. Test it early.
"""
import argparse, base64, hashlib, json, os, pathlib, shutil, subprocess, sys  # noqa: F401

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
HOST_NAME = "com.usualinstall.host"
SENSOR_MANIFEST = ROOT / "sensor" / "manifest.json"
# Must NOT live inside sensor/ -- Chromium refuses to pack an extension
# directory that contains its own signing key.
KEY_PEM = ROOT / "keys" / "sensor.pem"

# name, binaries, per-user config dir, system extension dirs, system NMH dirs, policy dirs
BROWSERS = [
    ("Chromium", ["chromium", "chromium-browser"], "~/.config/chromium",
     ["/usr/share/chromium/extensions"],
     ["/etc/chromium/native-messaging-hosts"],
     ["/etc/chromium/policies/managed"]),
    ("Google Chrome", ["google-chrome", "google-chrome-stable"], "~/.config/google-chrome",
     ["/opt/google/chrome/extensions", "/usr/share/google-chrome/extensions"],
     ["/etc/opt/chrome/native-messaging-hosts"],
     ["/etc/opt/chrome/policies/managed"]),
    ("Brave", ["brave", "brave-browser"], "~/.config/BraveSoftware/Brave-Browser",
     ["/opt/brave.com/brave/extensions", "/usr/share/brave/extensions"],
     ["/etc/brave/native-messaging-hosts"],
     ["/etc/brave/policies/managed"]),
    ("Vivaldi", ["vivaldi", "vivaldi-stable"], "~/.config/vivaldi",
     ["/opt/vivaldi/extensions"],
     ["/etc/vivaldi/native-messaging-hosts"],
     ["/etc/vivaldi/policies/managed"]),
    ("Microsoft Edge", ["microsoft-edge", "microsoft-edge-stable"], "~/.config/microsoft-edge",
     ["/opt/microsoft/msedge/extensions"],
     ["/etc/opt/edge/native-messaging-hosts"],
     ["/etc/opt/edge/policies/managed"]),
    ("Opera", ["opera"], "~/.config/opera",
     ["/usr/share/opera/extensions"],
     ["/etc/opt/opera/native-messaging-hosts"],
     ["/etc/opt/opera/policies/managed"]),
]


# Snap and Flatpak browsers cannot spawn a native-messaging host without an
# override the user runs themselves. They must be reported, not silently
# skipped -- a browser that looks covered but quietly does nothing is worse
# than one plainly marked unsupported.
# app id -> (display name, config subdirectory inside the sandbox)
FLATPAK_BROWSERS = {
    "com.brave.Browser":     ("Brave", "BraveSoftware/Brave-Browser"),
    "org.chromium.Chromium": ("Chromium", "chromium"),
    "com.google.Chrome":     ("Google Chrome", "google-chrome"),
    "com.microsoft.Edge":    ("Microsoft Edge", "microsoft-edge"),
    "com.vivaldi.Vivaldi":   ("Vivaldi", "vivaldi"),
    "com.opera.Opera":       ("Opera", "opera"),
}

# Snap browsers are a different matter. Strict confinement gives no supported
# way to reach an arbitrary host binary, and unlike Flatpak there is no
# user-grantable permission that changes it. Detected and reported, not fixed.
SNAP_BROWSERS = ("chromium", "brave", "opera", "vivaldi")


def flatpak_browsers():
    """Installed Flatpak browsers. These ARE provisionable -- see provision_flatpak."""
    out = []
    for app_id, (name, cfg) in FLATPAK_BROWSERS.items():
        for root in ("/var/lib/flatpak/app", os.path.expanduser("~/.local/share/flatpak/app")):
            if pathlib.Path(root, app_id).exists():
                out.append({"app_id": app_id, "name": name, "config": cfg,
                            "base": pathlib.Path(os.path.expanduser(f"~/.var/app/{app_id}"))})
                break
    return out


def snap_browsers():
    """Installed Snap browsers. These are NOT provisionable."""
    return [s.title() for s in SNAP_BROWSERS
            if pathlib.Path(f"/snap/{s}/current").exists()]


def provision_flatpak(ext_id, crx=None):
    """Make a Flatpak browser able to reach the session agent.

    Three pieces, none of which needs root:

      1. `flatpak override --user --talk-name=org.freedesktop.Flatpak`, which
         lets the sandboxed browser spawn something on the host at all.
      2. A wrapper inside the sandbox that re-enters the host via
         flatpak-spawn. The browser can only execute paths it can see, so the
         manifest cannot point at the real agent directly.
      3. The host manifest (and optionally the preinstall manifest) in the
         sandbox's view of ~/.config.

    Flatpak does NOT rewrite ~/.config and ~/.local/share inside the sandbox.
    It sets XDG_CONFIG_HOME and XDG_DATA_HOME to ~/.var/app/<id>/{config,data}
    at their real absolute paths, and those are what the sandbox can see. So
    the manifest must point at the ~/.var/app/... path, not at ~/.local/share:
    getting this wrong is why an earlier attempt left the browser unable to
    read its own CRX while the manifest itself looked correct.
    """
    results = []
    agent = ROOT / "session" / "usual-session"
    for b in flatpak_browsers():
        app_id, base = b["app_id"], b["base"]
        try:
            r = subprocess.run(
                ["flatpak", "override", "--user",
                 "--talk-name=org.freedesktop.Flatpak", app_id],
                capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                results.append((b["name"], False, r.stderr.strip() or "override failed"))
                continue
        except (OSError, subprocess.SubprocessError) as e:
            results.append((b["name"], False, str(e)))
            continue

        wrapper_dir = base / "data" / "usual-install"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper = wrapper_dir / "host-wrapper.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            "# Re-enter the host from inside the browser's sandbox.\n"
            f'exec flatpak-spawn --host "{agent}" "$@"\n')
        wrapper.chmod(0o755)
        # XDG_DATA_HOME inside the sandbox is this same absolute path.
        inside = wrapper

        nmh = base / "config" / b["config"] / "NativeMessagingHosts"
        nmh.mkdir(parents=True, exist_ok=True)
        (nmh / f"{HOST_NAME}.json").write_text(json.dumps({
            "name": HOST_NAME,
            "description": "Usual Install session agent",
            "path": str(inside),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{ext_id}/"],
        }, indent=2) + "\n")

        if crx:
            # The CRX has to be readable from inside the sandbox too, so copy it
            # into the app's data directory and reference the path the browser
            # will see rather than the one on the host.
            inside_crx = wrapper_dir / "sensor.crx"
            shutil.copy(crx, inside_crx)
            ext_dir = base / "config" / b["config"] / "External Extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            version = json.loads(SENSOR_MANIFEST.read_text())["version"]
            (ext_dir / f"{ext_id}.json").write_text(json.dumps(
                {"external_crx": str(inside_crx), "external_version": version},
                indent=2) + "\n")

        results.append((b["name"], True, str(nmh)))
    return results


def detected():
    """Browsers that appear to exist on this machine."""
    out = []
    for name, bins, user_cfg, sys_ext, sys_nmh, pol in BROWSERS:
        cfg = pathlib.Path(os.path.expanduser(user_cfg))
        on_path = next((b for b in bins if shutil.which(b)), None)
        installed_systemwide = any(pathlib.Path(p).parent.exists() for p in sys_ext)
        if cfg.exists() or on_path or installed_systemwide:
            out.append({
                "name": name, "binary": on_path, "config": cfg,
                "user_nmh": cfg / "NativeMessagingHosts",
                "user_ext": cfg / "External Extensions",
                "system_nmh": [pathlib.Path(p) for p in sys_nmh],
                "system_ext": [pathlib.Path(p) for p in sys_ext],
                "policies": [pathlib.Path(p) for p in pol],
                "profile_exists": cfg.exists(),
            })
    return out


# --------------------------------------------------------------------------- #

def extension_id_from_key(pem_path):
    """Chromium derives the id from SHA256 of the DER public key, hex mapped a-p."""
    der = subprocess.run(
        ["openssl", "rsa", "-in", str(pem_path), "-pubout", "-outform", "DER"],
        capture_output=True, check=True).stdout
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest), base64.b64encode(der).decode()


def cmd_keygen(args):
    if KEY_PEM.exists() and not args.force:
        print(f"{KEY_PEM} exists; use --force to replace "
              f"(this changes the extension id)")
    else:
        KEY_PEM.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "genrsa", "-out", str(KEY_PEM), "2048"],
                       check=True, capture_output=True)
        KEY_PEM.chmod(0o600)
        print(f"wrote {KEY_PEM}")

    ext_id, pubkey_b64 = extension_id_from_key(KEY_PEM)
    manifest = json.loads(SENSOR_MANIFEST.read_text())
    manifest["key"] = pubkey_b64
    SENSOR_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "sensor" / "extension-id").write_text(ext_id + "\n")

    print(f"extension id: {ext_id}")
    print(f"pinned into {SENSOR_MANIFEST} via \"key\" — the id is now stable "
          f"across unpacked loads and packed crx builds")
    return 0


def current_extension_id():
    f = ROOT / "sensor" / "extension-id"
    if f.is_file():
        return f.read_text().strip()
    return None


# --------------------------------------------------------------------------- #

def host_manifest(ext_id):
    return {
        "name": HOST_NAME,
        "description": "Usual Install session agent",
        "path": str(ROOT / "session" / "usual-session"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }


def cmd_install(args):
    ext_id = args.extension_id or current_extension_id()
    if not ext_id:
        print("no extension id. run: ./provision.py keygen", file=sys.stderr)
        return 1

    agent = ROOT / "session" / "usual-session"
    if not agent.is_file():
        print(f"missing {agent}", file=sys.stderr)
        return 1
    agent.chmod(0o755)

    manifest = host_manifest(ext_id)
    wrote = 0
    for b in detected():
        target_dirs = b["system_nmh"] if args.system else [b["user_nmh"]]
        for d in target_dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{HOST_NAME}.json").write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  native host  -> {d / (HOST_NAME + '.json')}")
                wrote += 1
            except PermissionError:
                print(f"  native host  !! {d} (needs root; rerun with sudo or drop --system)")

        if args.preinstall:
            if not args.crx:
                print("  preinstall   !! --preinstall needs --crx /path/to/sensor.crx")
                continue
            crx = pathlib.Path(args.crx).resolve()
            version = json.loads(SENSOR_MANIFEST.read_text())["version"]
            entry = {"external_crx": str(crx), "external_version": version}
            ext_dirs = b["system_ext"] if args.system else [b["user_ext"]]
            for d in ext_dirs:
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f"{ext_id}.json").write_text(json.dumps(entry, indent=2) + "\n")
                    print(f"  preinstall   -> {d / (ext_id + '.json')}")
                except PermissionError:
                    print(f"  preinstall   !! {d} (needs root)")

    if args.flatpak:
        for name, ok_, detail in provision_flatpak(ext_id, args.crx):
            print(f"  flatpak      {'->' if ok_ else '!!'} {name}: {detail}")
            wrote += 1 if ok_ else 0
    elif flatpak_browsers():
        print(f"  flatpak      .. {len(flatpak_browsers())} flatpak browser(s) "
              f"skipped; rerun with --flatpak")
    for n in snap_browsers():
        print(f"  snap         !! {n} cannot be supported; install a non-snap build")

    if not wrote:
        print("\nno browsers detected", file=sys.stderr)
        return 1
    print(f"\nprovisioned {wrote} location(s). Restart the browser to pick it up.")
    return 0


def cmd_uninstall(args):
    ext_id = current_extension_id()
    removed = 0
    for b in detected():
        for d in [b["user_nmh"]] + b["system_nmh"]:
            f = d / f"{HOST_NAME}.json"
            if f.exists():
                try:
                    f.unlink(); print(f"  removed {f}"); removed += 1
                except PermissionError:
                    print(f"  !! {f} (needs root)")
        if ext_id:
            for d in [b["user_ext"]] + b["system_ext"]:
                f = d / f"{ext_id}.json"
                if f.exists():
                    try:
                        f.unlink(); print(f"  removed {f}"); removed += 1
                    except PermissionError:
                        print(f"  !! {f} (needs root)")
    print(f"removed {removed} file(s)")
    return 0


def cmd_status(args):
    ext_id = current_extension_id()
    print(f"extension id : {ext_id or '(none — run keygen)'}")
    print(f"session agent: {ROOT / 'session' / 'usual-session'}")
    cat = ROOT / "catalogue" / "catalogue.json"
    if cat.is_file():
        data = json.loads(cat.read_text())
        print(f"catalogue    : {len(data.get('hosts', {}))} hostnames, "
              f"{len(data.get('domains', {}))} domains, generated {data.get('generated')}")
    else:
        print("catalogue    : missing — run catalogue/build_catalogue.py")

    fp = flatpak_browsers()
    if fp:
        print(f"\n{len(fp)} flatpak browser(s) — provisionable with: install --flatpak")
        for b in fp:
            live = (b["base"] / "config" / b["config"] /
                    "NativeMessagingHosts" / f"{HOST_NAME}.json").exists()
            print(f"  {b['name']:16} {b['app_id']:24} "
                  f"{'provisioned' if live else 'not provisioned'}")
    snaps = snap_browsers()
    if snaps:
        print(f"\n{len(snaps)} snap browser(s) — NOT supportable:")
        for n in snaps:
            print(f"  {n:16} snap confinement gives no way to reach a host binary.")
            print(f"                   Install a non-snap build to use Usual Install.")

    found = detected()
    print(f"\n{len(found)} browser(s) detected:")
    for b in found:
        user_host = (b["user_nmh"] / f"{HOST_NAME}.json").exists()
        sys_host = any((d / f"{HOST_NAME}.json").exists() for d in b["system_nmh"])
        pre = bool(ext_id) and (
            (b["user_ext"] / f"{ext_id}.json").exists()
            or any((d / f"{ext_id}.json").exists() for d in b["system_ext"]))
        flags = []
        if user_host: flags.append("native host (user)")
        if sys_host: flags.append("native host (system)")
        if pre: flags.append("preinstall")
        if not b["profile_exists"]: flags.append("never launched")
        print(f"  {b['name']:16} {b['binary'] or '(no binary on PATH)':26} "
              f"{', '.join(flags) or 'not provisioned'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="generate a stable extension id")
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keygen)

    i = sub.add_parser("install", help="write native host (and optionally preinstall) manifests")
    i.add_argument("--system", action="store_true", help="system-wide instead of per-user (needs root)")
    i.add_argument("--preinstall", action="store_true", help="also write the external extension manifest")
    i.add_argument("--flatpak", action="store_true",
                   help="also provision Flatpak browsers (grants the talk-name "
                        "override and installs a flatpak-spawn wrapper; no root)")
    i.add_argument("--crx", help="packed sensor.crx, required with --preinstall")
    i.add_argument("--extension-id")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove everything we wrote")
    u.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("status", help="show what is provisioned")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
