#!/usr/bin/env bash
# Tier 3 -- the part a container cannot do: render the windows on a real X
# display and run a real flatpak install, with no USUAL_DRY_RUN.
#
# Run inside the Lima VM:  bash /usual-install/test/tier3.sh
# Watch it from the host:  open vnc://localhost:5900
set -uo pipefail
LOGDIR=$(mktemp -d)
trap \'rm -rf "$LOGDIR"\' EXIT
ulimit -c 0 2>/dev/null || true
cd /usual-install
ARCH=$(uname -m)
OUT=/usual-install/test/screenshots
CAT=/tmp/catalogue-$ARCH.json
mkdir -p "$OUT"
PASS=0; FAIL=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }

step "catalogue for $ARCH"
# The mounted catalogue is x86_64. Flathub publishes per-architecture metadata,
# and on arm64 Zoom/Slack/Spotify simply do not exist -- so build the right one
# rather than testing against packages this machine cannot install.
if [ -f "$CAT" ]; then
  ok "reusing $CAT"
elif python3 catalogue/build_catalogue.py --arch "$ARCH" -o "$CAT" 2>&1 | tail -4; then
  ok "built $CAT"
else
  bad "catalogue build failed"; exit 1
fi
export USUAL_CATALOGUE="$CAT"

step "display"
pgrep -x Xvfb >/dev/null || (Xvfb :99 -screen 0 1280x800x24 >$LOGDIR/xvfb.log 2>&1 &)
sleep 2
export DISPLAY=:99
pkill -f "session/installer.py" 2>/dev/null
# Xvfb has no GL, and GTK4's default GSK renderer needs one -- without this the
# window chrome appears but the content area is blank black.
export GSK_RENDERER=cairo
export GDK_DISABLE=gl
export GTK_A11Y=none
pgrep -x openbox >/dev/null || (openbox >$LOGDIR/openbox.log 2>&1 &)
pgrep -x x11vnc  >/dev/null || (x11vnc -display :99 -forever -shared -bg -quiet \
    -passwd "${USUAL_VNC_PASSWORD:-usual}" >$LOGDIR/x11vnc.log 2>&1)
sleep 2
if xdpyinfo -display :99 >/dev/null 2>&1; then
  ok "X display :99 up, VNC on 5900 (host: open vnc://localhost:5900 — password 'usual')"
else
  bad "no X display"; exit 1
fi

# Render a window from a payload, screenshot it, leave it running if asked.
render() {  # name, payload, keep?
  local name="$1" payload="$2" keep="${3:-no}"
  python3 session/installer.py --payload "$payload" >/tmp/$name.log 2>&1 &
  local pid=$!
  sleep 5
  if ! kill -0 $pid 2>/dev/null; then
    bad "$name: installer exited early"; sed 's/^/        /' /tmp/$name.log | tail -5; return 1
  fi
  import -display :99 -window root "$OUT/$name.png" 2>/dev/null
  if [ -s "$OUT/$name.png" ]; then ok "$name rendered -> test/screenshots/$name.png"; else bad "$name: no screenshot"; fi
  if [ "$keep" = keep ]; then echo "$pid" > /tmp/render.pid; else kill $pid 2>/dev/null; wait $pid 2>/dev/null; fi
}

step "native installer window"
# A real resolution, not a hand-written payload: ask the resolver what
# apps.gnome.org/Loupe/ maps to on this architecture.
NATIVE=$(python3 - <<'PY'
import json, sys
sys.path.insert(0, "resolver")
from resolve import resolve
cat = json.load(open(__import__("os").environ["USUAL_CATALOGUE"]))
for url in ["https://apps.gnome.org/Loupe/", "https://www.inkscape.org/release/",
            "https://obsidian.md/download"]:
    c, why = resolve(url, cat)
    if len(c) == 1:
        print(json.dumps({"mode": "native", "candidates": c, "pageUrl": url})); break
PY
)
if [ -z "$NATIVE" ]; then bad "no single-candidate domain resolved"; else
  echo "   payload: $(echo "$NATIVE" | head -c 150)…"
  render native "$NATIVE"
fi

step "picker (two candidates)"
PICKER=$(python3 - <<'PY'
import json, sys, os
sys.path.insert(0, "resolver")
from resolve import resolve
cat = json.load(open(os.environ["USUAL_CATALOGUE"]))
for url in ["https://www.jetbrains.com/idea/download/", "https://vscodium.com/"]:
    c, why = resolve(url, cat)
    if len(c) > 1:
        print(json.dumps({"mode": "native", "candidates": c, "pageUrl": url})); break
PY
)
[ -n "$PICKER" ] && render picker "$PICKER" || printf '   \033[33mskip\033[0m  no multi-candidate domain on %s\n' "$ARCH"

step "compat window (windows app via a layer)"
render compat '{"mode":"compat","pageUrl":"https://image-line.com/download","exePath":"/tmp/flstudio_win64_21.exe","compat":{"entry":{"name":"FL Studio","slug":"flstudio","grade":"gold","category":"software"},"plan":{"runner":"bottles","setup":"install-flatpak","package":"com.usebottles.bottles","grade":"gold","category":"software","notes":["no Vulkan driver found — DXVK will fall back to OpenGL and may be slow"]},"reason":"gold rating, running via bottles"}}'

step "not-supported window"
render unsupported '{"mode":"unsupported","pageUrl":"https://ableton.com/download","compat":{"entry":{"name":"Ableton Live","slug":"ableton","grade":"silver","category":"software"},"reason":"rated silver — runs with known problems, not offered"}}'

step "settings window"
# Against a real (unprivileged) daemon, so the window shows live state rather
# than a hand-written payload.
export USUAL_RUN_DIR=/tmp/usual-run
export USUAL_DATA_DIR=/tmp/usual-data
export USUAL_CONFIG=/tmp/usual-config.json
mkdir -p "$USUAL_RUN_DIR" "$USUAL_DATA_DIR"
cp "$CAT" "$USUAL_DATA_DIR/catalogue.json" 2>/dev/null
if [ -f keys/catalogue-signing.key ]; then
  python3 catalogue/sign.py sign "$USUAL_DATA_DIR/catalogue.json" \
          --key keys/catalogue-signing.key >/dev/null 2>&1
fi
echo '{"enabled": true, "provision_interval_seconds": 3600}' > "$USUAL_CONFIG"
python3 daemon/usuald.py >$LOGDIR/usuald.log 2>&1 &
DPID=$!
for _ in $(seq 1 50); do [ -S "$USUAL_RUN_DIR/usuald.sock" ] && break; sleep 0.2; done
if [ -S "$USUAL_RUN_DIR/usuald.sock" ]; then
  ok "usuald running for the settings window to talk to"
  export USUAL_SOCKET="$USUAL_RUN_DIR/usuald.sock"
  python3 settings/usual-settings.py >$LOGDIR/settings.log 2>&1 &
  SPID=$!
  sleep 6
  if kill -0 $SPID 2>/dev/null; then
    import -display :99 -window root "$OUT/settings.png" 2>/dev/null
    [ -s "$OUT/settings.png" ] && ok "settings window rendered -> test/screenshots/settings.png" \
      || bad "settings: no screenshot"
  else
    bad "settings window exited early"; tail -5 $LOGDIR/settings.log | sed "s/^/        /"
  fi
  kill $SPID 2>/dev/null
else
  bad "usuald did not start"; tail -5 $LOGDIR/usuald.log | sed "s/^/        /"
fi

step "REAL flatpak install (no dry run)"
if [ -z "${NATIVE:-}" ]; then
  bad "no payload to install"
else
  APPID=$(echo "$NATIVE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["candidates"][0]["id"])')
  echo "   installing $APPID for real; this downloads a runtime and takes a while"
  flatpak --user uninstall -y "$APPID" >/dev/null 2>&1
  unset USUAL_DRY_RUN
  render installing "$NATIVE" keep
  PID=$(cat /tmp/render.pid 2>/dev/null)
  sleep 1
  # Enter activates the primary action, so the window drives itself.
  # Focus the window before sending the key, or it goes to the root window.
  WID=$(xdotool search --name "Installer" | tail -1)
  if [ -n "$WID" ]; then
    xdotool windowactivate --sync "$WID" 2>/dev/null
    # Exactly one Return. A second one lands on Cancel once Install goes
    # insensitive, which closed the window and hid the install.
    xdotool key Return 2>/dev/null
    ok "activated Install on window $WID"
  else
    bad "could not find the installer window to drive"
  fi
  # The first install pulls a ~1 GB runtime, so allow real time for it.
  for i in $(seq 1 80); do
    sleep 15
    import -display :99 -window root "$OUT/installing-progress.png" 2>/dev/null
    if flatpak --user info "$APPID" >/dev/null 2>&1; then
      echo "   installed after ~$((i*15))s"; break
    fi
  done
  sleep 3
  import -display :99 -window root "$OUT/installed.png" 2>/dev/null
  if flatpak --user info "$APPID" >/dev/null 2>&1; then
    ok "$APPID really installed"
    flatpak --user list --app --columns=application,version | sed 's/^/        /'
  else
    bad "$APPID did not install"
    tail -20 $LOGDIR/installing.log | sed 's/^/        /'
  fi
  kill "$PID" 2>/dev/null
fi

step "native fallback (flathub fails, usuald installs from the distro repo)"
if command -v pkcheck >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  if bash test/native_fallback_test.sh >/tmp/nf.log 2>&1; then
    ok "the installer window fell back to a native package"
  else
    bad "native fallback"; tail -12 /tmp/nf.log | sed 's/^/        /'
  fi
else
  printf '   \033[33mskip\033[0m  %s\n' "needs polkit and passwordless sudo"
fi

kill ${DPID:-0} 2>/dev/null
printf '\n\033[1m%s pass, %s fail\033[0m\n' "$PASS" "$FAIL"
printf 'screenshots in test/screenshots/\n'
[ "$FAIL" -eq 0 ]
