#!/usr/bin/env python3
"""Host capability detection and runner selection.

Decides whether a Windows application can run acceptably on *this* machine and
which compatibility layer to use. Everything here is local -- no network, no
guessing from the catalogue.

The bar is deliberately high. A Silver or Bronze rating means "runs with
problems", which in practice means a user who thinks Usual Install is broken.
Those resolve to "not supported" and the plain download proceeds.

    ./runners.py            # report this machine
    ./runners.py --self-test
"""
import argparse, glob, json, os, pathlib, shutil, subprocess, sys

# WineHQ / Bottles use the same five-step vocabulary.
GRADE_RANK = {"platinum": 4, "gold": 3, "silver": 2, "bronze": 1, "garbage": 0}
# Only these are offered. Anything else is "not supported".
ACCEPTABLE_GRADES = {"platinum", "gold"}


def detect_host():
    """What this machine can actually run.

    USUAL_FAKE_HOST (a JSON object) overrides detection, so the decision logic
    can be tested on a machine that is not the target -- an arm64 Mac, say,
    which correctly reports that nothing is supported.
    """
    fake = os.environ.get("USUAL_FAKE_HOST")
    if fake:
        return json.loads(fake)

    arch = os.uname().machine if hasattr(os, "uname") else "unknown"

    # Vulkan is what DXVK/VKD3D need; without it you get slow GL fallbacks.
    icd = glob.glob("/usr/share/vulkan/icd.d/*.json") + \
          glob.glob("/etc/vulkan/icd.d/*.json")
    vulkan = bool(icd) or shutil.which("vulkaninfo") is not None

    gpu = "unknown"
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout.lower()
        for vendor, key in (("nvidia", "nvidia"), ("amd", "amd/ati"), ("intel", "intel")):
            if key in out or vendor in out:
                gpu = vendor
                break
    except (OSError, subprocess.SubprocessError):
        for p in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                v = pathlib.Path(p).read_text().strip()
                gpu = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}.get(v, "unknown")
                break
            except OSError:
                pass

    flatpak = shutil.which("flatpak") is not None
    flatpak_apps = set()
    if flatpak:
        try:
            out = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                                 capture_output=True, text=True, timeout=15).stdout
            flatpak_apps = {l.strip() for l in out.splitlines() if l.strip()}
        except (OSError, subprocess.SubprocessError):
            pass

    return {
        "arch": arch,
        "vulkan": vulkan,
        "gpu": gpu,
        "flatpak": flatpak,
        "wine": shutil.which("wine") is not None,
        "bottles": ("com.usebottles.bottles" in flatpak_apps) or shutil.which("bottles") is not None,
        "lutris": ("net.lutris.Lutris" in flatpak_apps) or shutil.which("lutris") is not None,
        "steam": ("com.valvesoftware.Steam" in flatpak_apps) or shutil.which("steam") is not None,
    }


def choose_runner(entry, host):
    """Return (plan, reason). plan is None when nothing good is available.

    `entry` is a compat catalogue entry: name, grade, category, optional runners.
    """
    grade = (entry.get("grade") or "").lower()

    # 1. The compatibility rating has to be good enough to be worth offering.
    if grade not in ACCEPTABLE_GRADES:
        if grade in GRADE_RANK:
            return None, f"rated {grade} — runs with known problems, not offered"
        return None, "no compatibility rating available"

    # 2. x86 only. box64/FEX exist on ARM but not at a quality worth offering.
    if host.get("arch") not in ("x86_64", "amd64"):
        return None, f"{host.get('arch')} is not supported by any x86 compatibility layer"

    category = (entry.get("category") or "software").lower()

    # 3. Games want a Proton-derived runner; applications want a managed prefix.
    #    Bottles handles both well and installs rootless from Flathub, which is
    #    the same mechanism Usual Install already uses for native apps.
    if host.get("bottles"):
        plan = {"runner": "bottles", "setup": "use-existing"}
    elif host.get("lutris") and category == "games":
        plan = {"runner": "lutris", "setup": "use-existing"}
    elif host.get("flatpak"):
        plan = {"runner": "bottles", "setup": "install-flatpak",
                "package": "com.usebottles.bottles"}
    elif host.get("wine"):
        plan = {"runner": "wine", "setup": "use-existing"}
    else:
        return None, "no compatibility layer available and no flatpak to install one"

    notes = []
    if not host.get("vulkan"):
        notes.append("no Vulkan driver found — DXVK will fall back to OpenGL and may be slow")
    if host.get("gpu") == "unknown":
        notes.append("GPU could not be identified")
    if entry.get("runners"):
        plan["suggested_runner_build"] = entry["runners"][-1]

    plan["grade"] = grade
    plan["category"] = category
    plan["notes"] = notes
    return plan, f"{grade} rating, running via {plan['runner']}"


# --------------------------------------------------------------------------- #

def _self_test():
    gold_app = {"name": "FL Studio", "grade": "Gold", "category": "Software"}
    silver = {"name": "Ableton Live", "grade": "Silver", "category": "Software"}
    ungraded = {"name": "Whatever", "grade": None, "category": "Software"}
    plat_game = {"name": "Epic Games Store", "grade": "Platinum", "category": "Games"}

    x86 = {"arch": "x86_64", "vulkan": True, "gpu": "amd", "flatpak": True,
           "wine": False, "bottles": True, "lutris": False, "steam": True}
    bare = dict(x86, bottles=False, lutris=False, wine=False)
    nothing = dict(bare, flatpak=False)
    arm = dict(x86, arch="aarch64")
    novulkan = dict(x86, vulkan=False)

    cases = [
        (gold_app, x86, True, "gold app, bottles present"),
        (plat_game, x86, True, "platinum game"),
        (silver, x86, False, "silver is not offered"),
        (ungraded, x86, False, "no rating is not offered"),
        (gold_app, arm, False, "arm is not supported"),
        (gold_app, bare, True, "installs bottles from flathub"),
        (gold_app, nothing, False, "no layer and no flatpak"),
        (gold_app, novulkan, True, "offered, with a warning"),
    ]
    fails = 0
    for entry, host, want_plan, label in cases:
        plan, reason = choose_runner(entry, host)
        ok = bool(plan) == want_plan
        fails += not ok
        detail = (f"{plan['runner']}/{plan['setup']}" if plan else "not supported")
        print(f"  {'ok  ' if ok else 'FAIL'}  {label:38} {detail:26} [{reason}]")

    plan, _ = choose_runner(gold_app, novulkan)
    if not (plan and plan["notes"]):
        print("  FAIL  missing Vulkan should produce a warning note"); fails += 1
    else:
        print(f"  ok    warning surfaced: {plan['notes'][0]}")

    plan, _ = choose_runner(gold_app, bare)
    if not (plan and plan["setup"] == "install-flatpak"):
        print("  FAIL  should plan a flatpak install of Bottles"); fails += 1
    else:
        print(f"  ok    plans rootless install of {plan['package']}")

    print(f"\n{'all passed' if not fails else str(fails) + ' FAILURES'}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return 1 if _self_test() else 0
    host = detect_host()
    if a.json:
        print(json.dumps(host, indent=2))
    else:
        print("this machine:")
        for k, v in host.items():
            print(f"  {k:10} {v}")
        print("\nacceptable grades:", ", ".join(sorted(ACCEPTABLE_GRADES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
