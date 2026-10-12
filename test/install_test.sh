#!/usr/bin/env bash
# Install into a staging root and run the result.
#
# Packaging is the one thing that cannot be checked by reading: the code finds
# its siblings by relative path, so an install that rearranges the tree breaks
# it in ways no unit test sees. This stages a real install and then runs the
# installed daemon, not the one in the source tree.
set -uo pipefail
LOGDIR=$(mktemp -d)
trap \'rm -rf "$LOGDIR"\' EXIT
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
ok()  { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
step(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

step "make install"
if make install DESTDIR="$STAGE" PREFIX=/usr >$LOGDIR/install.log 2>&1; then
  ok "staged install succeeded"
else
  bad "make install failed"; tail -20 $LOGDIR/install.log; exit 1
fi

step "everything a package promises is present"
for f in usr/bin/usuald usr/bin/usual-session usr/bin/usual-settings \
         usr/bin/usualctl usr/bin/usual-provision usr/bin/usual-install-update \
         usr/lib/usual-install/daemon/usuald.py \
         usr/lib/usual-install/resolver/resolve.py \
         usr/lib/usual-install/catalogue/sign.py \
         usr/lib/usual-install/catalogue/signing.pub \
         usr/lib/usual-install/session/usual-session \
         usr/lib/usual-install/settings/usual-settings.py \
         usr/lib/usual-install/sensor/manifest.json \
         usr/lib/usual-install/sensor/extension-id \
         usr/lib/usual-install/sensor/icons/icon128.png \
         usr/lib/systemd/system/usuald.service \
         usr/lib/systemd/system/usual-install-update.timer \
         usr/share/polkit-1/actions/org.usualinstall.policy \
         usr/share/applications/usual-install.desktop \
         usr/share/icons/hicolor/scalable/apps/usual-install.svg \
         etc/usual-install/config.json ; do
  [ -e "$STAGE/$f" ] && ok "$f" || bad "$f missing"
done

step "the signing key must NOT be packaged"
if [ -e "$STAGE/usr/lib/usual-install/keys/catalogue-signing.key" ] \
   || find "$STAGE" -name '*.key' -o -name 'key.pem' -o -name 'sensor.pem' 2>/dev/null | grep -q .; then
  bad "a private key was staged into the package"
  find "$STAGE" -name '*.key' -o -name '*.pem' | sed 's/^/        /'
else
  ok "no private key in the staged package"
fi

step "wrappers point at the installed tree"
grep -q "/usr/lib/usual-install/daemon/usuald.py" "$STAGE/usr/bin/usuald" \
  && ok "usuald wrapper targets the installed path" || bad "usuald wrapper is wrong"

step "the INSTALLED daemon runs from its own layout"
# Relocate the staging root so nothing can accidentally resolve back into the
# source tree, which is exactly the bug this test exists to catch.
RELOC=$(mktemp -d)
cp -a "$STAGE/usr" "$RELOC/"
cp -a "$STAGE/etc" "$RELOC/"
RUN=$(mktemp -d); DATA=$(mktemp -d)
cp catalogue/catalogue.json "$DATA/" 2>/dev/null
cp catalogue/catalogue.json.sig "$DATA/" 2>/dev/null

USUAL_RUN_DIR="$RUN" USUAL_DATA_DIR="$DATA" \
USUAL_CONFIG="$RELOC/etc/usual-install/config.json" \
  python3 "$RELOC/usr/lib/usual-install/daemon/usuald.py" >$LOGDIR/usuald.log 2>&1 &
DPID=$!
for _ in $(seq 1 60); do [ -S "$RUN/usuald.sock" ] && break; sleep 0.2; done

if [ -S "$RUN/usuald.sock" ]; then
  ok "installed daemon started and bound its socket"
  OUT=$(USUAL_SOCKET="$RUN/usuald.sock" python3 "$RELOC/usr/lib/usual-install/daemon/client.py" status 2>&1)
  echo "$OUT" | grep -q '"ok": true' && ok "installed daemon answers status" || { bad "status failed"; echo "$OUT" | head -5; }
  echo "$OUT" | grep -q '"verified": true' && ok "installed daemon verified its catalogue with the packaged public key" \
    || bad "catalogue not verified from the installed layout"
  RES=$(USUAL_SOCKET="$RUN/usuald.sock" python3 "$RELOC/usr/lib/usual-install/daemon/client.py" resolve https://zoom.us/download 2>&1)
  echo "$RES" | grep -q "us.zoom.Zoom" && ok "resolution works from the installed layout" || { bad "resolve failed"; echo "$RES" | head -5; }
else
  bad "installed daemon never started"; tail -20 $LOGDIR/usuald.log
fi
kill $DPID 2>/dev/null; wait $DPID 2>/dev/null
rm -rf "$RELOC" "$RUN" "$DATA"

step "systemd and polkit files are well formed"
python3 - "$STAGE" <<'PY'
import sys, pathlib, xml.etree.ElementTree as ET
stage = pathlib.Path(sys.argv[1])
pol = stage / "usr/share/polkit-1/actions/org.usualinstall.policy"
try:
    root = ET.parse(pol).getroot()
    ids = [a.get("id") for a in root.findall("action")]
    print(f"   \033[32mpass\033[0m  polkit policy parses; actions: {ids}")
except Exception as e:
    print(f"   \033[31mFAIL\033[0m  polkit policy: {e}")
    sys.exit(1)
for unit in (stage / "usr/lib/systemd/system").glob("*"):
    text = unit.read_text()
    if "[Unit]" in text and ("[Service]" in text or "[Timer]" in text):
        print(f"   \033[32mpass\033[0m  {unit.name} has the required sections")
    else:
        print(f"   \033[31mFAIL\033[0m  {unit.name} is malformed")
        sys.exit(1)
PY
[ $? -eq 0 ] && PASS=$((PASS+4)) || FAIL=$((FAIL+1))

printf '\n\033[1m%s pass, %s fail\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
