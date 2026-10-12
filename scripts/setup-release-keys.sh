#!/usr/bin/env bash
# One-time setup for the release workflow: make sure the two signing keys
# exist, and store them as this repository's Actions secrets.
#
#   bash scripts/setup-release-keys.sh
#
# Run it where your keys live (your own checkout), or anywhere if you have none
# yet, e.g. a GitHub Codespace opened on this repository. Existing keys in keys/
# are reused, never replaced. New keys change the pinned extension id, which is
# fine before the first release and is committed and pushed for you.
#
# The keys never leave this machine except into the encrypted secrets. Keep a
# copy of keys/ somewhere safe: GitHub secrets cannot be read back, and losing
# keys/sensor.pem changes the extension id for every user.
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null || { echo "needs $1"; exit 1; }; }
need gh; need openssl; need python3; need git

# In a Codespace, GITHUB_TOKEN is scoped to the codespace and cannot set
# secrets; use your own login instead.
if [ "${CODESPACES:-}" = true ]; then unset GITHUB_TOKEN; fi
gh auth status >/dev/null 2>&1 || gh auth login --hostname github.com --web

umask 077
mkdir -p keys
if [ -f keys/sensor.pem ]; then
  echo "reusing keys/sensor.pem"
else
  openssl genrsa -out keys/sensor.pem 2048 2>/dev/null
  echo "created keys/sensor.pem"
fi
if [ -f keys/catalogue-signing.key ]; then
  echo "reusing keys/catalogue-signing.key"
else
  python3 catalogue/sign.py keygen --key keys/catalogue-signing.key >/dev/null
  echo "created keys/catalogue-signing.key"
fi

# Pin the extension id to this sensor key (no-op if it already matches).
python3 provision/provision.py keygen >/dev/null
echo "extension id: $(cat sensor/extension-id)"

gh secret set SENSOR_PEM            < keys/sensor.pem
gh secret set CATALOGUE_SIGNING_KEY < keys/catalogue-signing.key
echo "secrets SENSOR_PEM and CATALOGUE_SIGNING_KEY are set"

if ! git diff --quiet -- sensor/extension-id sensor/manifest.json; then
  git add sensor/extension-id sensor/manifest.json
  git commit -m "Pin the extension id to the release sensor key"
  git push
  echo "committed and pushed the new extension id"
fi

echo
echo "Done. Back up the keys/ folder somewhere private; it is not in git."
