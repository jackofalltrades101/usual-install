#!/usr/bin/env python3
"""Drive usual-session over the real native-messaging protocol.

Verifies the wire format and the decision logic together, without a browser.
This is the piece that catches a framing mistake, which is otherwise invisible
until Chromium silently drops the connection.
"""
import json, os, pathlib, struct, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / "session" / "usual-session"

# A machine where compatibility layers are actually viable, so the decision
# logic is exercised rather than short-circuited by this host's architecture.
FAKE_X86 = json.dumps({
    "arch": "x86_64", "vulkan": True, "gpu": "amd", "flatpak": True,
    "wine": False, "bottles": True, "lutris": False, "steam": True,
})

CASES = [
    # page url, filename, expect handled?
    ("https://zoom.us/download", "zoom_amd64.deb", True),
    ("https://obsidian.md/download", "Obsidian-1.5.3.AppImage", True),
    ("https://www.jetbrains.com/idea/download/", "ideaIU.tar.gz", True),
    ("https://zoom.us/pricing", "whitepaper.pdf", False),
    ("https://github.com/x/y/releases", "app.AppImage", False),
    ("https://news.ycombinator.com/", "thing.tar.gz", False),
    ("https://flathub.org/apps/org.kde.kdenlive", "x.flatpakref", False),
]


def talk(messages, extra_env=None):
    env = dict(os.environ, USUAL_DRY_RUN="1", **(extra_env or {}))
    payload = b""
    for m in messages:
        body = json.dumps(m).encode()
        payload += struct.pack("@I", len(body)) + body
    proc = subprocess.run([sys.executable, str(AGENT)], input=payload,
                          capture_output=True, env=env, timeout=60)
    out, replies = proc.stdout, []
    i = 0
    while i + 4 <= len(out):
        (n,) = struct.unpack("@I", out[i:i + 4])
        i += 4
        replies.append(json.loads(out[i:i + n].decode()))
        i += n
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode())
    return replies


# Windows installers: page title, filename, host, expected action.
# "compat"  -> let the download finish, then set up a compatibility layer
# "ignore"  -> say nothing or say not-supported; the plain download proceeds
COMPAT_CASES = [
    ("FL Studio 21 — Download", "flstudio_win64_21.exe", "https://image-line.com/download",
     "compat", "gold-rated, bottles available"),
    ("Download Ableton Live 12", "ableton_live_12.exe", "https://ableton.com/download",
     "ignore", "silver-rated, not offered"),
    ("Adobe Photoshop", "PhotoshopSetup.exe", "https://adobe.com/products/photoshop",
     "ignore", "not in any compat source"),
    ("Download our tool", "setup.exe", "https://example.com/download",
     "ignore", "unidentifiable installer"),
    ("Epic Games Store", "EpicInstaller.exe", "https://store.epicgames.com/download",
     "compat", "platinum-rated launcher"),
]


def compat_suite():
    print("\n--- windows installers (compat path) ---")
    msgs = [{"type": "download", "pageUrl": u, "downloadUrl": u, "filename": f,
             "pageTitle": t, "mime": "application/octet-stream", "bytes": 99}
            for t, f, u, _, _ in COMPAT_CASES]
    replies = talk(msgs, {"USUAL_FAKE_HOST": FAKE_X86})
    if len(replies) != len(COMPAT_CASES):
        print(f"  FAIL framing: {len(replies)}/{len(COMPAT_CASES)} replies")
        return 1
    fails = 0
    for (t, f, u, want, label), reply in zip(COMPAT_CASES, replies):
        got = reply.get("action")
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {got:8} (want {want:8}) {f:28} {label}")
        print(f"          [{reply.get('reason')}]")

    # Second phase: the .exe finished downloading, so setup can run.
    print("\n--- completed download hands off to setup ---")
    r = talk([{"type": "downloaded", "path": "/tmp/flstudio_win64_21.exe",
               "pageTitle": "FL Studio 21", "pageUrl": "https://image-line.com/download"}],
             {"USUAL_FAKE_HOST": FAKE_X86})
    got = r[0].get("action") if r else None
    ok = got == "handled"
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {got} (want handled)  [{r[0].get('reason') if r else '-'}]")

    # The same machine without any layer and no flatpak must decline.
    print("\n--- no compatibility layer available ---")
    bare = json.dumps({"arch": "x86_64", "vulkan": False, "gpu": "unknown",
                       "flatpak": False, "wine": False, "bottles": False,
                       "lutris": False, "steam": False})
    r = talk([{"type": "download", "pageUrl": "https://image-line.com/download",
               "downloadUrl": "https://image-line.com/x.exe",
               "filename": "flstudio_win64_21.exe", "pageTitle": "FL Studio 21",
               "mime": "application/octet-stream", "bytes": 99}],
             {"USUAL_FAKE_HOST": bare})
    got = r[0].get("action") if r else None
    ok = got == "ignore"
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {got} (want ignore)  [{r[0].get('reason') if r else '-'}]")
    return fails


def main():
    if not (ROOT / "catalogue" / "catalogue.json").is_file():
        print("build the catalogue first: catalogue/build_catalogue.py -o catalogue/catalogue.json")
        return 1

    print("--- linux installers (native path) ---")
    msgs = [{"type": "download", "pageUrl": u, "downloadUrl": u, "filename": f,
             "mime": "application/octet-stream", "bytes": 1234}
            for u, f, _ in CASES]
    replies = talk(msgs)

    if len(replies) != len(CASES):
        print(f"FAIL framing: sent {len(CASES)} messages, got {len(replies)} replies")
        return 1

    fails = 0
    for (url, fname, want), reply in zip(CASES, replies):
        got = reply.get("action") == "handled"
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'handled ' if got else 'ignored '} "
              f"{fname:28} {url}")
        print(f"          [{reply.get('reason')}]")

    fails += compat_suite()

    print(f"\n{'protocol + decisions ok' if not fails else str(fails) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
