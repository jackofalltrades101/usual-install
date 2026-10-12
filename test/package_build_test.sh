#!/usr/bin/env bash
# Actually build the distro packages.
#
# `make install` staging proves the layout; it does not prove that rpmbuild or
# makepkg accept the spec and PKGBUILD. Only building them does, and the Debian
# build already caught a real defect that reading never would have.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
NAME=usual-install
VER=0.2.0
PASS=0; FAIL=0
ok()  { printf '   \033[32mpass\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '   \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
step(){ printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "source tarball"
TD=$(mktemp -d)
mkdir -p "$TD/$NAME-$VER"
tar -cf - --exclude=.git --exclude=test/screenshots --exclude='*.core' \
    --exclude=keys --exclude=__pycache__ . 2>/dev/null | tar -xf - -C "$TD/$NAME-$VER"
( cd "$TD" && tar -czf "$NAME-$VER.tar.gz" "$NAME-$VER" )
[ -s "$TD/$NAME-$VER.tar.gz" ] && ok "tarball $(du -h "$TD/$NAME-$VER.tar.gz" | cut -f1)" || { bad "tarball"; exit 1; }

if command -v rpmbuild >/dev/null 2>&1; then
  step "rpmbuild"
  mkdir -p "$TD/rpm"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
  cp "$TD/$NAME-$VER.tar.gz" "$TD/rpm/SOURCES/"
  cp packaging/fedora/$NAME.spec "$TD/rpm/SPECS/"
  if rpmbuild --define "_topdir $TD/rpm" -bb "$TD/rpm/SPECS/$NAME.spec" > "$TD/rpm.log" 2>&1; then
    RPM=$(find "$TD/rpm/RPMS" -name '*.rpm' | head -1)
    ok "built $(basename "$RPM")"
    step "the rpm's contents"
    LIST=$(rpm -qlp "$RPM" 2>/dev/null)
    printf '%s\n' "$LIST" | grep -E "bin/|systemd|polkit" | head -8 | sed 's/^/        /'
    case "$LIST" in *"/usr/bin/usuald"*) ok "ships /usr/bin/usuald";; *) bad "missing usuald";; esac
    case "$LIST" in *polkit*) ok "ships the polkit policy";; *) bad "missing polkit policy";; esac
    case "$LIST" in *.key|*key.pem*|*sensor.pem*) bad "a private key is inside the rpm";;
                    *) ok "no private key in the rpm";; esac
  else
    bad "rpmbuild failed"; tail -25 "$TD/rpm.log"
  fi
elif command -v makepkg >/dev/null 2>&1; then
  step "makepkg"
  BD="$TD/arch"; mkdir -p "$BD"
  cp packaging/arch/PKGBUILD packaging/arch/$NAME.install "$BD/"
  # The PKGBUILD builds from $startdir; give it the unpacked tree.
  cp -a "$TD/$NAME-$VER/." "$BD/"
  if ( cd "$BD" && makepkg --nodeps --noconfirm --skipinteg > "$TD/mk.log" 2>&1 ); then
    PKG=$(find "$BD" -name '*.pkg.tar.*' | head -1)
    ok "built $(basename "$PKG")"
    LIST=$(tar -tf "$PKG" 2>/dev/null)
    printf '%s\n' "$LIST" | grep -E "usr/bin/|systemd|polkit" | head -8 | sed 's/^/        /'
    case "$LIST" in *"usr/bin/usuald"*) ok "ships usr/bin/usuald";; *) bad "missing usuald";; esac
    case "$LIST" in *polkit*) ok "ships the polkit policy";; *) bad "missing polkit policy";; esac
    case "$LIST" in *.key|*key.pem*|*sensor.pem*) bad "a private key is inside the package";;
                    *) ok "no private key in the package";; esac
  else
    bad "makepkg failed"; tail -25 "$TD/mk.log"
  fi
else
  echo "   neither rpmbuild nor makepkg here; nothing to do"
fi

rm -rf "$TD"
printf '\n\033[1m%s pass, %s fail\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
