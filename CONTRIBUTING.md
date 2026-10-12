# Working on Usual Install

## Layout

```
catalogue/   builds and signs the domain -> package catalogue
resolver/    the only place a package is chosen
daemon/      usuald: catalogue, provisioning, privileged installs
session/     per-user agent and the installer windows
sensor/      the browser component
compat/      Windows-application support
provision/   writes browser manifests
settings/    the settings window
packaging/   systemd units, polkit policy, distro packaging
test/        test suite and environments
```

Everything is Python with no third-party dependencies, deliberately: the daemon
runs as root and installs packages, so the less of it there is the better.

## Running the tests

```sh
bash test/run.sh
```

Skips whatever the machine cannot do and says so. On macOS that is most of it;
on Linux with a browser installed it covers nearly everything.

### Environments

```sh
# the fast loop, architecture-native
docker build -t ui-debian -f test/Dockerfile.debian . && docker run --rm --user root ui-debian

# the other two distro families
docker build -t ui-fedora -f test/Dockerfile.fedora . && docker run --rm --user root ui-fedora
docker build --platform linux/amd64 -t ui-arch -f test/Dockerfile . && docker run --rm --platform linux/amd64 ui-arch

# does each browser really install the sensor on its own?
docker build -t ui-browsers -f test/Dockerfile.browsers . && docker run --rm ui-browsers

# a real display and a real install
limactl start --name=usual test/lima-usual.yaml
limactl shell usual -- bash /usual-install/test/tier3.sh
open vnc://localhost:5900          # password: usual
```

The Arch image is x86_64 only. On an ARM host it runs under emulation, where
Chromium cannot start, so its browser tiers skip.

### What the individual suites cover

| Script | Covers |
|---|---|
| `real_urls.py` | resolution against the live catalogue, including what it declines |
| `signing_test.py` | a tampered catalogue is refused and nothing installs |
| `update_test.py` | updates verify, never downgrade, never clobber a good catalogue |
| `daemon_test.py` | the privileged surface refuses what it should |
| `install_test.sh` | a staged install runs from its own layout, ships no private key |
| `package_build_test.sh` | `rpmbuild` / `makepkg` actually produce a package |
| `polkit_test.sh` | an authorised privileged install succeeds (needs polkit, non-root) |
| `preinstall_test.sh` | each browser installs the sensor from a manifest |
| `native_fallback_test.sh` | the installer window falls back to a distro package |
| `tier3.sh` | windows render; a real install completes |

## Things that are easy to get wrong

Collected because each one cost real time and none is obvious from reading the
code.

**Do not write the system-wide external-extension manifest.**
`/usr/share/chromium/extensions/` is the documented location, and on Debian it
breaks the feature: that Chromium treats entries there as unpacked extension
directories, errors on the JSON, and aborts its external-extension provider
*before* reading the per-profile manifest. The per-profile location works
everywhere tested.

**Flatpak does not redirect `~/.config` and `~/.local/share` inside the
sandbox.** It points `XDG_CONFIG_HOME` and `XDG_DATA_HOME` at
`~/.var/app/<id>/{config,data}` by their real absolute paths. A manifest naming
`~/.local/share/...` looks right and is invisible to the browser.

**Never create a profile directory as root.** It leaves the profile root-owned
and the browser, which runs as the user, cannot use it at all. Only write into
profiles that already exist.

**AppStream ships one `<name>` per translation.** `find("name")` returns document
order, which is alphabetical by language code, so the English name is not the
first one. Pick the element with no `xml:lang`.

**GTK 4 draws nothing on a display without GL.** You get window chrome and a
blank body. `GSK_RENDERER=cairo`.

**Do not write test logs to fixed `/tmp` paths.** With `fs.protected_regular`,
root cannot write a file another user owns in a sticky directory, so the
redirect fails and passing tests report as failures.

**`make clean` must not run during a package build.** The catalogue, its
signature and the packed extension are inputs, not intermediates.

## The signing key

`make all` generates a keypair and signs the catalogue with it, so a machine
trusts its own build. Neither half is in the repository. Anyone distributing
builds to other people replaces `catalogue/signing.pub` with their own public
key and keeps the private half offline.
