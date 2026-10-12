#!/usr/bin/env python3
"""Exercise compat/setup.py against real subprocesses, with stub runners.

A genuine end-to-end compatibility install needs x86_64 hardware and a real
Windows installer, which this machine is not and does not have. What *can* be
tested here is our own orchestration: that the right commands are issued in the
right order, that a failing step aborts the rest, and that the window can
cancel a running child. Stub executables stand in for bottles-cli and wine, so
these are real processes -- not the dry-run path.
"""
import os, pathlib, stat, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compat"))

PASS = FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok    {label}")
    else:
        FAIL += 1; print(f"  FAIL  {label}  {detail}")


def stub(dirpath, name, exit_code=0):
    """A real executable that records how it was called."""
    p = pathlib.Path(dirpath) / name
    log = pathlib.Path(dirpath) / f"{name}.calls"
    p.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'echo "stub output from $0"\n'
        f"exit {exit_code}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log


def drain(gen):
    lines, rc = [], 0
    try:
        while True:
            lines.append(next(gen))
    except StopIteration as stop:
        rc = stop.value or 0
    return lines, rc


def main():
    import importlib
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        bindir = td / "bin"; bindir.mkdir()
        prefixes = td / "prefixes"

        os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
        os.environ["USUAL_PREFIX_ROOT"] = str(prefixes)
        os.environ.pop("USUAL_DRY_RUN", None)
        import setup as cs
        importlib.reload(cs)

        entry = {"name": "FL Studio", "slug": "flstudio", "grade": "gold",
                 "category": "software"}
        exe = td / "flstudio_win64_21.exe"; exe.write_text("MZ fake installer")

        print("\n--- bottles: existing runner ---")
        calls = stub(bindir, "bottles-cli")
        plan = {"runner": "bottles", "setup": "use-existing", "grade": "gold",
                "category": "software", "notes": ["no Vulkan driver found"]}
        lines, rc = drain(cs.full_setup(entry, plan, exe))
        got = calls.read_text().strip().splitlines() if calls.exists() else []
        check(rc == 0, "setup succeeds", f"rc={rc}")
        check(any(c.startswith("new --bottle-name flstudio") for c in got),
              "creates a bottle named for the app", got)
        check(any("--environment application" in c for c in got),
              "application environment for non-game software", got)
        check(any(f"run -b flstudio -e {exe}" in c for c in got),
              "runs the downloaded installer inside the bottle", got)
        check(any("no Vulkan" in l for l in lines), "surfaces plan warnings in the log")

        print("\n--- bottles: game gets the gaming environment ---")
        calls2 = stub(bindir, "bottles-cli")
        game = dict(entry, name="Epic Games Store", slug="epic", category="games")
        drain(cs.full_setup(game, dict(plan, category="games"), exe))
        got2 = calls2.read_text().strip().splitlines() if calls2.exists() else []
        check(any("--environment gaming" in c for c in got2),
              "gaming environment for games", got2)

        print("\n--- a failing step aborts the rest ---")
        stub(bindir, "bottles-cli", exit_code=3)
        lines, rc = drain(cs.full_setup(entry, plan, exe))
        check(rc == 3, "non-zero exit is propagated", f"rc={rc}")
        check(not any(" run -b " in l for l in lines),
              "does not run the installer after bottle creation failed")

        print("\n--- wine path creates an isolated prefix ---")
        wcalls = stub(bindir, "wine")
        wplan = {"runner": "wine", "setup": "use-existing", "grade": "gold",
                 "category": "software", "notes": []}
        lines, rc = drain(cs.full_setup(entry, wplan, exe))
        wgot = wcalls.read_text().strip().splitlines() if wcalls.exists() else []
        check(rc == 0, "wine setup succeeds", f"rc={rc}")
        check(any("wineboot --init" in c for c in wgot), "initialises the prefix", wgot)
        check((prefixes / "flstudio").is_dir(),
              "per-application prefix is created, not the default one")
        check(any(str(exe) in c for c in wgot), "runs the installer under wine", wgot)

        print("\n--- the window can stop a running child ---")
        slow = bindir / "bottles-cli"
        slow.write_text("#!/bin/sh\nsleep 60\n")
        slow.chmod(0o755)
        holder = {}
        gen = cs.full_setup(entry, plan, exe, proc_holder=holder)
        next(gen); next(gen)               # start ensure_runner / first command
        for _ in range(4):
            if holder.get("proc"):
                break
            try: next(gen)
            except StopIteration: break
        proc = holder.get("proc")
        check(proc is not None, "the live subprocess is exposed to the window")
        if proc:
            proc.terminate(); proc.wait(timeout=10)
            check(proc.poll() is not None, "terminate() actually stops it")

    print(f"\n{'orchestration verified' if not FAIL else str(FAIL) + ' FAILURES'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
