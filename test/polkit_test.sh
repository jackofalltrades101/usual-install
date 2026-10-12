#!/usr/bin/env bash
# Does the polkit ALLOW path actually work?
#
# Every other test has seen polkit refuse: as root it is bypassed, and over SSH
# there is no agent to answer a password prompt. This installs a rule that
# authorises a specific user for our action, which is what a real deployment
# does for a desktop session, and then performs a genuinely privileged install
# through it.
#
# Must run with sudo available. Removes its rule afterwards.
set -uo pipefail
cd "$(dirname "$0")/.."
LOGDIR=$(mktemp -d)
RULE=/etc/polkit-1/rules.d/49-usual-install-test.rules
POLICY=/usr/share/polkit-1/actions/org.usualinstall.policy
STAGED_POLICY=no
PASS=0; FAIL=0
ok()  { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
step(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }
cleanup() {
  sudo rm -f "$RULE" 2>/dev/null
  [ "$STAGED_POLICY" = yes ] && sudo rm -f "$POLICY" 2>/dev/null
  sudo pkill -f "daemon/usuald.py" 2>/dev/null
  rm -rf "$LOGDIR" "$TD" 2>/dev/null
}
trap cleanup EXIT

command -v pkcheck >/dev/null || { echo "   polkit not installed; nothing to prove"; exit 0; }
if [ "$(id -u)" -eq 0 ]; then
  # polkit is bypassed for root by design, so running this as root would
  # exercise nothing. It has to be driven by an unprivileged caller.
  echo "   running as root: polkit is bypassed by design, so this proves nothing."
  echo "   run it as an unprivileged user with sudo available."
  exit 0
fi
PKG=${1:-htop}
WHO=$(id -un)

step "a package that is not installed, with a homepage to match"
if dpkg-query -W -f='${Status}' "$PKG" 2>/dev/null | grep -q "ok installed"; then
  echo "   $PKG is already installed; the install will be a no-op but the"
  echo "   authorisation path is still exercised"
fi
HOME_URL=$(apt-cache show "$PKG" 2>/dev/null | awk -F': ' '/^Homepage:/{print $2; exit}')
[ -n "$HOME_URL" ] && ok "$PKG declares $HOME_URL" || { bad "$PKG has no homepage; pick another"; exit 1; }
DOMAIN=$(python3 -c "
import sys;sys.path.insert(0,'resolver')
from resolve import registrable
from urllib.parse import urlparse
print(registrable(urlparse('$HOME_URL').hostname or ''))")
ok "its registrable domain is $DOMAIN"

step "a catalogue the daemon will accept, listing exactly that package"
TD=$(mktemp -d); chmod 755 "$TD"
python3 - "$TD" "$PKG" "$DOMAIN" <<'PY'
import json, sys, pathlib
td, pkg, domain = sys.argv[1], sys.argv[2], sys.argv[3]
entry = {"id": f"test.{pkg}.App", "name": pkg, "summary": "", "backend": "flatpak",
         "remote": "flathub", "native_hint": pkg, "path": ""}
doc = {"schema": 1, "generated": "2026-10-12T00:00:00Z", "max_candidates": 8,
       "hosts": {domain: [entry]}, "domains": {domain: [entry]}}
pathlib.Path(td, "catalogue.json").write_text(json.dumps(doc))
PY
python3 catalogue/sign.py keygen --key "$TD/k.key" --pubkey "$TD/k.pub" >/dev/null 2>&1
python3 catalogue/sign.py sign "$TD/catalogue.json" --key "$TD/k.key" >/dev/null 2>&1
ok "signed test catalogue naming $PKG"

step "the action must be registered with polkit"
# Normally the package installs this. Running from a source tree (or after the
# package has been removed) it is absent, so stage it and take it away again.
if ! pkaction --action-id org.usualinstall.install >/dev/null 2>&1; then
  sudo install -D -m 0644 packaging/polkit/org.usualinstall.policy "$POLICY"
  STAGED_POLICY=yes
  sleep 2
fi
pkaction --action-id org.usualinstall.install >/dev/null 2>&1 \
  && ok "org.usualinstall.install is registered$([ "$STAGED_POLICY" = yes ] && echo " (staged by this test)")" \
  || { bad "could not register the action"; exit 1; }

step "install the polkit rule that authorises $WHO"
sudo tee "$RULE" >/dev/null <<EOF
// Test rule: authorise $WHO for the Usual Install action without a prompt.
// A real deployment relies on auth_admin_keep for an active desktop session;
// this stands in for that so the allow path can be exercised unattended.
polkit.addRule(function(action, subject) {
    if (action.id == "org.usualinstall.install" && subject.user == "$WHO") {
        return polkit.Result.YES;
    }
});
EOF
sleep 2
if pkcheck --action-id org.usualinstall.install --process $$ >/dev/null 2>&1; then
  ok "polkit now authorises $WHO for org.usualinstall.install"
else
  bad "polkit still refuses; is the policy file installed? (pkaction --action-id org.usualinstall.install)"
  exit 1
fi

step "a privileged install, authorised by polkit, as an unprivileged user"
sudo USUAL_RUN_DIR="$TD/run" USUAL_DATA_DIR="$TD" USUAL_CONFIG="$TD/config.json" \
     USUAL_PUBKEY="$TD/k.pub" python3 daemon/usuald.py >"$LOGDIR/usuald.log" 2>&1 &
for _ in $(seq 1 80); do [ -S "$TD/run/usuald.sock" ] && break; sleep 0.2; done
[ -S "$TD/run/usuald.sock" ] || { bad "daemon did not start"; tail -5 "$LOGDIR/usuald.log"; exit 1; }

OUT=$(USUAL_SOCKET="$TD/run/usuald.sock" python3 daemon/client.py \
        install "test.$PKG.App" native "$DOMAIN" 2>&1 </dev/null)
echo "$OUT" | sed 's/^/        /' | tail -12

if echo "$OUT" | grep -q "authorized by polkit"; then
  ok "polkit AUTHORISED the install (the path never previously exercised)"
else
  bad "the authorisation line never appeared"
fi
if echo "$OUT" | grep -qE '"ok": true'; then
  ok "the privileged install completed"
else
  bad "the install did not complete"
fi

printf '\n\033[1m%s pass, %s fail\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
