#!/usr/bin/env bash
# Does a Chromium fork actually install the sensor from a preinstall manifest?
#
# This is the question the whole "the user never installs an extension" promise
# rests on. Writing the manifest proves nothing -- the browser has to read it,
# accept an off-store CRX, and create the extension in its profile. So this
# launches each browser against a fresh profile and then looks for the
# extension on disk.
set -uo pipefail
LOGDIR=$(mktemp -d)
trap \'rm -rf "$LOGDIR"\' EXIT
cd "$(dirname "$0")/.."
ROOT=$PWD
PASS=0; FAIL=0; SKIP=0
ok()   { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '   \033[33mskip\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "prepare the sensor"
python3 provision/provision.py keygen >/dev/null 2>&1
EXT_ID=$(cat sensor/extension-id)
VERSION=$(python3 -c 'import json;print(json.load(open("sensor/manifest.json"))["version"])')
echo "   extension id $EXT_ID  version $VERSION"

PACKER=$(command -v chromium || command -v chromium-browser)
rm -f "$ROOT/sensor.crx" /tmp/sensor.crx
"$PACKER" --pack-extension="$ROOT/sensor" --pack-extension-key="$ROOT/keys/sensor.pem" \
          --no-sandbox >$LOGDIR/pack.log 2>&1
if [ -f "$ROOT/sensor.crx" ]; then
  mv "$ROOT/sensor.crx" /tmp/sensor.crx; ok "packed sensor.crx ($(stat -c%s /tmp/sensor.crx) bytes)"
else
  bad "could not pack the crx"; cat $LOGDIR/pack.log; exit 1
fi

# browser label : binary
BROWSERS=(
  "Chromium:chromium"
  "Chromium:chromium-browser"
  "Brave:brave-browser"
  "Vivaldi:vivaldi-stable"
  "Google Chrome:google-chrome-stable"
  "Microsoft Edge:microsoft-edge-stable"
  "Opera:opera"
)

declare -A SEEN
for entry in "${BROWSERS[@]}"; do
  LABEL="${entry%%:*}"; BIN="${entry##*:}"
  BINPATH=$(command -v "$BIN" 2>/dev/null)
  [ -z "$BINPATH" ] && continue
  [ -n "${SEEN[$LABEL]:-}" ] && continue
  SEEN[$LABEL]=1

  step "$LABEL ($BIN)"
  PROFILE=$(mktemp -d)
  # Chromium resolves user-scope external extensions relative to the profile
  # in use, so the manifest goes inside --user-data-dir.
  mkdir -p "$PROFILE/External Extensions"
  cat > "$PROFILE/External Extensions/$EXT_ID.json" <<EOF
{
  "external_crx": "/tmp/sensor.crx",
  "external_version": "$VERSION"
}
EOF

  timeout 60 xvfb-run -a "$BINPATH" \
    --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check --no-sandbox \
    --disable-gpu --disable-dev-shm-usage \
    --disable-features=DialMediaRouteProvider \
    about:blank >/tmp/$BIN.log 2>&1 &
  BPID=$!
  # Give it time to start, read the manifest, and write the extension out.
  sleep 30
  kill $BPID 2>/dev/null; wait $BPID 2>/dev/null

  INSTALLED=$(find "$PROFILE" -maxdepth 4 -type d -name "$EXT_ID" 2>/dev/null | head -1)
  PREFS_HIT=$(grep -l "$EXT_ID" "$PROFILE"/*/Preferences "$PROFILE"/*/Secure\ Preferences 2>/dev/null | head -1)

  if [ -n "$INSTALLED" ]; then
    ok "$LABEL installed the sensor itself -> ${INSTALLED#$PROFILE/}"
    ls "$INSTALLED" | sed 's/^/        /'
  elif [ -n "$PREFS_HIT" ]; then
    bad "$LABEL saw the manifest but did not install it (found only in ${PREFS_HIT#$PROFILE/})"
    python3 - "$PREFS_HIT" "$EXT_ID" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"        (could not parse prefs: {e})"); raise SystemExit
ext = d.get("extensions", {}).get("settings", {}).get(sys.argv[2])
if ext:
    for k in ("state", "disable_reasons", "location", "install_time"):
        if k in ext: print(f"        {k}: {ext[k]}")
PY
  else
    bad "$LABEL ignored the preinstall manifest entirely"
    grep -iE "extension|external" /tmp/$BIN.log 2>/dev/null | tail -4 | sed 's/^/        /'
  fi
  rm -rf "$PROFILE"
done

step "not testable here"
for n in "Google Chrome" "Microsoft Edge" "Opera"; do
  [ -z "${SEEN[$n]:-}" ] && skip "$n — no $(dpkg --print-architecture) Linux build published"
done

printf '\n\033[1m%s pass, %s fail, %s skip\033[0m\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
