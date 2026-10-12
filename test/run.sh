#!/usr/bin/env bash
# Usual Install test loop, in tiers by what the environment can actually prove.
#
#   tier 1  catalogue, resolver, protocol        -- works anywhere with python3
#   tier 2  browser provisioning + interception  -- needs chromium + xvfb
#   tier 3  real flatpak install, visual check   -- needs an Arch VM, not a container
#
# Tier 2 is skipped automatically when chromium is absent, so this script is
# also the right thing to run on a dev machine.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
# Emulated Chromium aborts and dumps core into the working directory, which on
# a mounted source tree means multi-hundred-megabyte files in the repo.
ulimit -c 0 2>/dev/null || true
# Logs go in a per-run directory, not fixed /tmp paths. With
# fs.protected_regular (the default on modern kernels) root cannot write to a
# /tmp file another user already created, so a shared path silently turns every
# redirect into a failure and makes passing tests look broken.
LOGDIR=$(mktemp -d)
trap 'rm -rf "$LOGDIR"' EXIT
PASS=0; FAIL=0; SKIP=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '   \033[33mskip\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }

# ---------------------------------------------------------------- tier 1 ----
step "tier 1a — catalogue"
if [ -f catalogue/catalogue.json ]; then
  ok "catalogue present ($(python3 -c 'import json;d=json.load(open("catalogue/catalogue.json"));print(len(d["hosts"]),"hosts")'))"
elif python3 catalogue/build_catalogue.py -o catalogue/catalogue.json; then
  ok "catalogue built from Flathub"
else
  bad "catalogue build failed (no network?)"
fi

step "tier 1b — resolver self-test"
python3 resolver/resolve.py >$LOGDIR/resolver.log 2>&1 && ok "resolver" || { bad "resolver"; tail -20 $LOGDIR/resolver.log; }

step "tier 1c — real vendor URLs"
python3 test/real_urls.py >$LOGDIR/urls.log 2>&1 && ok "real URL expectations" || { bad "real URL expectations"; tail -30 $LOGDIR/urls.log; }

step "tier 1d — native messaging protocol"
python3 test/protocol_test.py >$LOGDIR/proto.log 2>&1 && ok "protocol + decisions" || { bad "protocol"; tail -20 $LOGDIR/proto.log; }

step "tier 1e — catalogue signing"
# The catalogue decides what gets installed, so it is signed at build time and
# verified before use. A dev key is generated here; a real deployment keeps the
# private half offline.
if [ ! -f keys/catalogue-signing.key ]; then
  python3 catalogue/sign.py keygen --key keys/catalogue-signing.key >/dev/null 2>&1
fi
python3 catalogue/sign.py sign catalogue/catalogue.json --key keys/catalogue-signing.key >/dev/null 2>&1
[ -f compat/compat.json ] && python3 catalogue/sign.py sign compat/compat.json --key keys/catalogue-signing.key >/dev/null 2>&1
python3 test/signing_test.py >$LOGDIR/signing.log 2>&1 && ok "signing enforced (tampered catalogue refused)" || { bad "signing"; tail -20 $LOGDIR/signing.log; }

step "tier 1f — windows compatibility"
if [ -f compat/compat.json ]; then
  ok "compat catalogue present ($(python3 -c 'import json;d=json.load(open("compat/compat.json"));e=d["entries"];print(len(e),"entries,",sum(1 for x in e if (x.get("grade") or "") in ("platinum","gold")),"offerable")'))"
elif python3 compat/build_compat.py -o compat/compat.json; then
  ok "compat catalogue built"
else
  bad "compat catalogue build failed"
fi
python3 compat/runners.py --self-test >$LOGDIR/runners.log 2>&1 && ok "runner selection" || { bad "runner selection"; tail -20 $LOGDIR/runners.log; }
python3 compat/match.py --self-test >$LOGDIR/match.log 2>&1 && ok "name matching" || { bad "name matching"; tail -20 $LOGDIR/match.log; }
python3 daemon/aur.py --self-test >$LOGDIR/aur.log 2>&1 && ok "AUR checks" || { bad "AUR checks"; tail -20 $LOGDIR/aur.log; }
python3 test/compat_setup_test.py >$LOGDIR/csetup.log 2>&1 && ok "setup orchestration (stub runners, real subprocesses)" || { bad "setup orchestration"; tail -20 $LOGDIR/csetup.log; }
printf '   this machine: '; python3 compat/runners.py | tr '\n' ' ' | sed 's/this machine: //'; echo

step "tier 1g — catalogue updates"
python3 test/update_test.py >$LOGDIR/update.log 2>&1 && ok "updates verify, never downgrade, never clobber a good catalogue" || { bad "catalogue updates"; tail -20 $LOGDIR/update.log; }

step "tier 1h — usuald"
python3 test/daemon_test.py >$LOGDIR/daemon.log 2>&1 && ok "daemon: privileged surface refuses what it should" || { bad "usuald"; tail -25 $LOGDIR/daemon.log; }
python3 daemon/backends.py | sed 's/^/   /'

step "tier 1i — packaging"
if command -v make >/dev/null 2>&1; then
  bash test/install_test.sh >$LOGDIR/installtest.log 2>&1 \
    && ok "staged install runs from its own layout ($(grep -cE 'pass' $LOGDIR/installtest.log) checks)" \
    || { bad "packaging"; tail -25 $LOGDIR/installtest.log; }
else
  skip "make not installed; packaging test skipped"
fi

step "tier 1j — distro packages actually build"
if command -v rpmbuild >/dev/null 2>&1 || command -v makepkg >/dev/null 2>&1; then
  bash test/package_build_test.sh >$LOGDIR/pkgbuild.log 2>&1 \
    && ok "distro package builds and ships no private key" \
    || { bad "package build"; tail -20 $LOGDIR/pkgbuild.log; }
else
  skip "no rpmbuild or makepkg here (the .deb path is covered by install_test)"
fi

step "tier 1k — polkit authorisation"
if [ "$(id -u)" -eq 0 ]; then
  skip "polkit is bypassed for root; run the suite as a normal user to cover it"
elif command -v pkcheck >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  bash test/polkit_test.sh >$LOGDIR/polkit.log 2>&1 \
    && ok "a polkit-authorised privileged install succeeds" \
    || { bad "polkit"; tail -20 $LOGDIR/polkit.log; }
else
  skip "needs polkit and passwordless sudo"
fi

step "tier 1l — provisioning"
python3 provision/provision.py keygen >$LOGDIR/keygen.log 2>&1 && ok "keygen ($(cat sensor/extension-id 2>/dev/null))" || { bad "keygen"; cat $LOGDIR/keygen.log; }
python3 provision/provision.py install >$LOGDIR/prov.log 2>&1 && ok "native host manifests written" || skip "no browsers detected"
python3 provision/provision.py status

# ---------------------------------------------------------------- tier 2 ----
CHROME=$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)
CHROME_RUNS=no
if [ -n "$CHROME" ]; then
  # x86_64 Chromium does not survive QEMU user emulation (it aborts in rcu
  # with "qemu: unknown option 'type=utility'"), so probe before asserting.
  if "$CHROME" --headless=new --no-sandbox --disable-gpu \
       --dump-dom about:blank >/dev/null 2>&1; then
    CHROME_RUNS=yes
  fi
fi
if [ -z "$CHROME" ]; then
  step "tier 2 — browser"; skip "no chromium on PATH"
elif [ "$CHROME_RUNS" = no ]; then
  step "tier 2 — browser"
  skip "chromium present but cannot run here (emulated x86 on an arm64 host?)"
  skip "use test/Dockerfile.debian for an arm64-native loop, or run on x86_64 Linux"
else
step "tier 2a — pack the sensor"
rm -f /tmp/sensor.crx
"$CHROME" --pack-extension="$ROOT/sensor" --pack-extension-key="$ROOT/keys/sensor.pem" \
          --no-sandbox >$LOGDIR/pack.log 2>&1
if [ -f "$ROOT/sensor.crx" ]; then
  mv "$ROOT/sensor.crx" /tmp/sensor.crx; ok "packed sensor.crx"
else
  bad "pack failed"; cat $LOGDIR/pack.log
fi

step "tier 2b — preinstall manifest is written"
# NOTE: this only checks that *we* wrote the manifest. Whether the browser then
# honours it and installs the extension is a different question, answered by
# test/preinstall_test.sh (run via test/Dockerfile.browsers), which launches
# each browser and looks for the extension on disk afterwards.
if [ -f /tmp/sensor.crx ]; then
  python3 provision/provision.py install --preinstall --crx /tmp/sensor.crx >$LOGDIR/pre.log 2>&1 \
    && ok "preinstall manifest written (browser acceptance: see preinstall_test.sh)" \
    || { bad "preinstall manifest"; cat $LOGDIR/pre.log; }
else
  skip "no crx to preinstall"
fi

step "tier 2c — end-to-end interception"
# Real vendor domains, served locally. Needs root to edit /etc/hosts; without it
# the test still runs against a host alias if one is already present.
HOSTS_MAPPED=no
if [ -w /etc/hosts ]; then
  for d in zoom.us slack.com www.jetbrains.com image-line.com; do
    grep -q " $d\$" /etc/hosts || echo "127.0.0.1 $d" >> /etc/hosts
  done
  HOSTS_MAPPED=yes
  ok "/etc/hosts mapped"
elif grep -q " zoom.us\$" /etc/hosts 2>/dev/null \
     && grep -q " image-line.com\$" /etc/hosts 2>/dev/null; then
  HOSTS_MAPPED=yes
  ok "/etc/hosts already mapped"
else
  skip "/etc/hosts not writable or incomplete (rerun as root for tiers 2c and 2d)"
fi

# Ask the kernel for a free port rather than assuming one: a leftover server
# from an earlier run would otherwise fail the tier for no real reason.
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
python3 test/fakesite.py --port "$PORT" >$LOGDIR/fakesite.log 2>&1 &
SITE=$!
sleep 1
if kill -0 "$SITE" 2>/dev/null; then ok "fakesite up on :$PORT"; else bad "fakesite died"; cat $LOGDIR/fakesite.log; fi

if [ "$HOSTS_MAPPED" = no ]; then
  skip "tier 2c needs /etc/hosts; the vendor domain would not resolve"
else
rm -f ~/.cache/usual-install/session.log
PROFILE=$(mktemp -d)
DOWNLOADS=$(mktemp -d)
export XDG_DOWNLOAD_DIR="$DOWNLOADS"
mkdir -p "$HOME/Downloads"

# Chromium resolves user-level native-messaging manifests relative to the
# profile in use, so a throwaway --user-data-dir will not see the one written
# to ~/.config/chromium. Mirror it into the test profile.
mkdir -p "$PROFILE/NativeMessagingHosts"
for d in "$HOME/.config/chromium/NativeMessagingHosts" \
         "$HOME/.config/google-chrome/NativeMessagingHosts"; do
  [ -f "$d/com.usualinstall.host.json" ] && \
    cp "$d/com.usualinstall.host.json" "$PROFILE/NativeMessagingHosts/" && break
done
if [ -f "$PROFILE/NativeMessagingHosts/com.usualinstall.host.json" ]; then
  ok "native host manifest mirrored into the test profile"
else
  bad "no native host manifest to mirror"
fi
timeout 45 xvfb-run -a "$CHROME" \
  --user-data-dir="$PROFILE" \
  --load-extension="$ROOT/sensor" \
  --no-first-run --no-default-browser-check --no-sandbox \
  --disable-gpu --disable-dev-shm-usage \
  "http://zoom.us:$PORT/download" >$LOGDIR/chrome.log 2>&1
kill "$SITE" 2>/dev/null

LOG=~/.cache/usual-install/session.log
if [ -f "$LOG" ]; then
  echo "   --- session.log ---"; sed 's/^/   /' "$LOG"
  if grep -q "us.zoom.Zoom\|1 candidate" "$LOG"; then
    ok "sensor reached the agent and zoom.us resolved"
  else
    bad "agent was reached but zoom.us did not resolve"
  fi
else
  bad "agent never ran — sensor did not connect (check $LOGDIR/chrome.log)"
  tail -20 $LOGDIR/chrome.log
fi

step "tier 2d — windows download takes the compatibility path"
if [ "$HOSTS_MAPPED" = no ]; then
  skip "needs /etc/hosts; the vendor domain would not resolve"
else
# Same browser, a .exe from a vendor with no Linux build. The decision depends
# on host hardware, so a viable x86 machine is injected -- the container is
# arm64 and would correctly refuse everything.
rm -f ~/.cache/usual-install/session.log
HOSTJSON='{"arch":"x86_64","vulkan":true,"gpu":"amd","flatpak":true,"wine":false,"bottles":true,"lutris":false,"steam":true}'
PROFILE2=$(mktemp -d)
mkdir -p "$PROFILE2/NativeMessagingHosts"
cp "$PROFILE/NativeMessagingHosts/com.usualinstall.host.json" \
   "$PROFILE2/NativeMessagingHosts/" 2>/dev/null

python3 test/fakesite.py --port "$PORT" >$LOGDIR/fakesite2.log 2>&1 &
SITE2=$!
sleep 1
USUAL_FAKE_HOST="$HOSTJSON" timeout 45 xvfb-run -a "$CHROME" \
  --user-data-dir="$PROFILE2" \
  --load-extension="$ROOT/sensor" \
  --no-first-run --no-default-browser-check --no-sandbox \
  --disable-gpu --disable-dev-shm-usage \
  "http://image-line.com:$PORT/flstudio" >$LOGDIR/chrome2.log 2>&1
kill "$SITE2" 2>/dev/null

if [ -f "$LOG" ]; then
  echo "   --- session.log ---"; sed 's/^/   /' "$LOG"
  if grep -q "FL Studio via bottles" "$LOG"; then
    ok "windows installer routed to a compatibility layer"
  elif grep -qi "compat" "$LOG"; then
    bad "compat path ran but did not choose bottles"
  else
    bad "windows download did not reach the compat path"
  fi
else
  bad "agent never ran for the .exe"
  tail -10 $LOGDIR/chrome2.log
fi
rm -rf "$PROFILE2"
fi
rm -rf "$PROFILE" "$DOWNLOADS"
fi
fi

# ---------------------------------------------------------------- tier 3 ----
step "tier 3 — real install and visual check"
skip "needs an Arch VM with a display and working flatpak; see README"

printf '\n\033[1m%s pass, %s fail, %s skip\033[0m\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
