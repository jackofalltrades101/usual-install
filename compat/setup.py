#!/usr/bin/env python3
"""Set up a Windows application to run through a compatibility layer.

Called with the downloaded installer and a plan from runners.choose_runner().
Yields log lines so the installer window can stream progress.

Scope is honest: Bottles (installed rootless from Flathub when absent) and
plain Wine. Lutris and Proton are detected and preferred where they already
exist, but their per-title install scripts are not driven from here -- that is
a per-game format, not something to fake.
"""
import os, pathlib, shutil, subprocess

PREFIX_ROOT = pathlib.Path(
    os.environ.get("USUAL_PREFIX_ROOT",
                   os.path.expanduser("~/.local/share/usual-install/prefixes")))
BOTTLES_ID = "com.usebottles.bottles"
DRY_RUN = os.environ.get("USUAL_DRY_RUN") == "1"


def _run(cmd, env=None, proc_holder=None):
    """Run a command, yielding its output lines.

    proc_holder, when given, receives the live Popen under "proc" so the
    caller can terminate it if the user closes the window mid-setup.
    """
    yield "$ " + " ".join(cmd) + "\n"
    if DRY_RUN:
        yield "[dry run] skipped\n"
        return 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        if proc_holder is not None:
            proc_holder["proc"] = proc
    except FileNotFoundError:
        yield f"*** {cmd[0]} not found\n"
        return 127
    for line in proc.stdout:
        yield line
    return proc.wait()


def _drain(gen):
    """Run a _run generator to completion, collecting lines and the exit code."""
    rc = 0
    try:
        while True:
            yield next(gen)
    except StopIteration as stop:
        rc = stop.value if stop.value is not None else 0
    return rc


def ensure_runner(plan, proc_holder=None):
    """Install the chosen runner if it isn't present. Yields log lines."""
    if plan["setup"] != "install-flatpak":
        yield f"using existing {plan['runner']}\n"
        return 0
    yield f"installing {plan['package']} from Flathub (user scope, no root)\n"
    rc = yield from _drain(_run(
        ["flatpak", "--user", "install", "-y", "--noninteractive", "flathub", plan["package"]],
        proc_holder=proc_holder))
    return rc


def bottles_cli():
    """How to reach bottles-cli, flatpak or native."""
    if DRY_RUN:
        return ["bottles-cli"]
    if shutil.which("bottles-cli"):
        return ["bottles-cli"]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "--command=bottles-cli", BOTTLES_ID]
    return None


def run_installer(entry, plan, exe_path, proc_holder=None):
    """Create a prefix and run the Windows installer inside it.

    Yields log lines; returns 0 on success.
    """
    slug = entry.get("slug") or entry["name"].lower().replace(" ", "-")
    exe_path = str(exe_path)

    if plan["runner"] == "bottles":
        cli = bottles_cli()
        if not cli:
            yield "*** bottles-cli not reachable\n"
            return 1
        yield f"creating bottle {slug!r}\n"
        rc = yield from _drain(_run(cli + [
            "new", "--bottle-name", slug,
            "--environment", "gaming" if plan.get("category") == "games" else "application",
        ], proc_holder=proc_holder))
        if rc not in (0, None):
            yield f"*** creating the bottle failed ({rc})\n"
            return rc
        yield f"running {pathlib.Path(exe_path).name} in the bottle\n"
        rc = yield from _drain(_run(cli + ["run", "-b", slug, "-e", exe_path],
                                    proc_holder=proc_holder))
        return rc

    if plan["runner"] == "wine":
        prefix = PREFIX_ROOT / slug
        yield f"using WINEPREFIX {prefix}\n"
        if not DRY_RUN:
            prefix.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, WINEPREFIX=str(prefix), WINEDLLOVERRIDES="mscoree,mshtml=")
        rc = yield from _drain(_run(["wine", "wineboot", "--init"], env=env,
                                    proc_holder=proc_holder))
        if rc not in (0, None):
            yield f"*** wineboot failed ({rc})\n"
            return rc
        rc = yield from _drain(_run(["wine", exe_path], env=env, proc_holder=proc_holder))
        return rc

    yield f"*** no setup path implemented for runner {plan['runner']!r}\n"
    return 1


def full_setup(entry, plan, exe_path, proc_holder=None):
    """ensure_runner + run_installer. Yields log lines; returns exit code."""
    rc = yield from _drain(ensure_runner(plan, proc_holder=proc_holder))
    if rc not in (0, None):
        yield f"*** installing the runner failed ({rc})\n"
        return rc
    for note in plan.get("notes", []):
        yield f"note: {note}\n"
    rc = yield from _drain(run_installer(entry, plan, exe_path, proc_holder=proc_holder))
    return rc


if __name__ == "__main__":
    import argparse, json, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", required=True, help="JSON compat entry")
    ap.add_argument("--plan", required=True, help="JSON plan from runners.choose_runner")
    ap.add_argument("--exe", required=True)
    a = ap.parse_args()
    gen = full_setup(json.loads(a.entry), json.loads(a.plan), a.exe)
    rc = 0
    try:
        while True:
            sys.stdout.write(next(gen))
    except StopIteration as stop:
        rc = stop.value or 0
    sys.exit(rc)
