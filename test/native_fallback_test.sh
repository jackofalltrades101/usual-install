#!/usr/bin/env bash
# The installer window's native fallback, end to end.
#
# When Flathub cannot serve a machine, the window asks usuald for a native
# package instead. That path has never run: every successful install so far
# went through flatpak. This drives it by pointing the window at a Flathub id
# that does not exist, so the flatpak step genuinely fails, while the catalogue
# entry names a real distro package.
set -uo pipefail
cd "$(dirname "$0")/.."
LOGDIR=$(mktemp -d)
RULE=/etc/polkit-1/rules.d/49-usual-install-test.rules
PASS=0; FAIL=0
ok()  { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
step(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }
cleanup(){ sudo rm -f "$RULE"; sudo pkill -f "daemon/usuald.py" 2>/dev/null; pkill -f installer.py 2>/dev/null; rm -rf "$LOGDIR" "$TD"; }
trap cleanup EXIT

PKG=${1:-htop}
WHO=$(id -un)
export DISPLAY=:99
ulimit -c 0
pgrep -x Xvfb >/dev/null || (Xvfb :99 -screen 0 1280x800x24 >"$LOGDIR/x.log" 2>&1 &)
sleep 2
export GSK_RENDERER=cairo GTK_A11Y=none

step "start from $PKG not installed"
sudo apt-get -qq remove -y "$PKG" >/dev/null 2>&1 || true
dpkg-query -W -f='${Status}' "$PKG" 2>/dev/null | grep -q "ok installed" \
  && { bad "$PKG is still installed; cannot prove a real install"; exit 1; } \
  || ok "$PKG is not installed"

HOME_URL=$(apt-cache show "$PKG" 2>/dev/null | awk -F': ' '/^Homepage:/{print $2; exit}')
DOMAIN=$(python3 -c "
import sys;sys.path.insert(0,'resolver')
from resolve import registrable
from urllib.parse import urlparse
print(registrable(urlparse('$HOME_URL').hostname or ''))")
ok "$PKG declares $HOME_URL (domain $DOMAIN)"

step "a catalogue whose flatpak id does not exist on Flathub"
TD=$(mktemp -d); chmod 755 "$TD"
BOGUS="dev.usualinstall.NoSuchApp"
python3 - "$TD" "$PKG" "$DOMAIN" "$BOGUS" <<'PY'
import json, sys, pathlib
td, pkg, domain, bogus = sys.argv[1:5]
entry = {"id": bogus, "name": pkg, "summary": "fallback test", "backend": "flatpak",
         "remote": "flathub", "native_hint": pkg, "path": ""}
doc = {"schema": 1, "generated": "2026-10-12T00:00:00Z", "max_candidates": 8,
       "hosts": {domain: [entry]}, "domains": {domain: [entry]}}
pathlib.Path(td, "catalogue.json").write_text(json.dumps(doc))
pathlib.Path(td, "payload.json").write_text(json.dumps(
    {"mode": "native", "candidates": [entry], "pageUrl": f"https://{domain}/download",
     "domain": domain}))
PY
python3 catalogue/sign.py keygen --key "$TD/k.key" --pubkey "$TD/k.pub" >/dev/null 2>&1
python3 catalogue/sign.py sign "$TD/catalogue.json" --key "$TD/k.key" >/dev/null 2>&1
ok "catalogue names $BOGUS on Flathub and $PKG natively"

step "authorise and start the daemon"
sudo tee "$RULE" >/dev/null <<EOF
polkit.addRule(function(action, subject) {
    if (action.id == "org.usualinstall.install" && subject.user == "$WHO") {
        return polkit.Result.YES;
    }
});
EOF
sleep 2
sudo USUAL_RUN_DIR="$TD/run" USUAL_DATA_DIR="$TD" USUAL_CONFIG="$TD/config.json" \
     USUAL_PUBKEY="$TD/k.pub" python3 daemon/usuald.py >"$LOGDIR/usuald.log" 2>&1 &
for _ in $(seq 1 80); do [ -S "$TD/run/usuald.sock" ] && break; sleep 0.2; done
[ -S "$TD/run/usuald.sock" ] && ok "usuald up" || { bad "usuald did not start"; exit 1; }
export USUAL_SOCKET="$TD/run/usuald.sock"

step "drive the installer window"
unset USUAL_DRY_RUN
python3 session/installer.py --payload "$(cat "$TD/payload.json")" >"$LOGDIR/inst.log" 2>&1 &
IPID=$!
sleep 5
WID=$(xdotool search --name "Installer" 2>/dev/null | tail -1)
if [ -n "$WID" ]; then
  xdotool windowactivate --sync "$WID" 2>/dev/null
  xdotool key Return 2>/dev/null
  ok "clicked Install"
else
  bad "no installer window"; tail -5 "$LOGDIR/inst.log"; exit 1
fi

for _ in $(seq 1 40); do
  dpkg-query -W -f='${Status}' "$PKG" 2>/dev/null | grep -q "ok installed" && break
  sleep 3
done
mkdir -p test/screenshots
import -display :99 -window root test/screenshots/native-fallback.png 2>/dev/null
kill $IPID 2>/dev/null

step "what happened"
if dpkg-query -W -f='${Status}' "$PKG" 2>/dev/null | grep -q "ok installed"; then
  ok "$PKG was installed through the NATIVE fallback"
else
  bad "$PKG was not installed"
fi
grep -q "asking usuald for a native package" "$LOGDIR/inst.log" 2>/dev/null \
  && ok "the window reported falling back" \
  || echo "        (window log) $(tail -3 "$LOGDIR/inst.log" | tr '\n' ' ')"

printf '\n\033[1m%s pass, %s fail\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
