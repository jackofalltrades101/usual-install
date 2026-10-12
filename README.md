# Usual Install

Make a vendor's own "Download for Linux" button end in a working, installed
application — the way an `.exe` installer does on Windows — instead of a
tarball, an AppImage with no desktop entry, or a copy-pasted `curl | sh`.

The user installs one package, once. After that it is invisible: every
Chromium-family browser on the machine is covered, including ones installed
later, with **no browser extension for the user to find or install**.

```
┌────────────────────────────────────────────────────────────────────┐
│ usuald — system service (root, systemd)                            │
│   · owns the signed catalogue and all resolution                   │
│   · provisions every Chromium browser it finds, and new ones       │
│   · installs native packages behind one narrow polkit action       │
└────────────────────────────────────────────────────────────────────┘
        ▲ unix socket                            │ writes manifests
        │                                        ▼
┌─────────────────────────────┐      ┌──────────────────────────────┐
│ usual-session (per user)    │      │ Chromium / Chrome / Brave /  │
│   · native-messaging host   │◀────▶│ Edge / Vivaldi …             │
│   · draws the installer     │ stdio│   · sensor extension         │
│   · rootless flatpak        │      │     (provisioned, not found) │
└─────────────────────────────┘      └──────────────────────────────┘
```

## Install

There is no published package yet, so you build it. You need `python3`,
`flatpak`, `polkit`, GTK 4 with its Python bindings, and any Chromium-family
browser (used to pack the extension).

```sh
git clone https://github.com/jackofalltrades101/usual-install
cd usual-install

make all        # generates a signing key, builds and SIGNS the catalogue,
                # packs the extension. ~11 MB download, a minute or two.
```

`make all` is not optional. A fresh clone contains no catalogue and no signing
key — both are build output, and the daemon refuses an unsigned catalogue, so
skipping this leaves you with a service that resolves nothing.

Then either install straight from the tree:

```sh
sudo make install
sudo systemctl enable --now usuald.service
```

or build a real package, which is preferable because uninstall is then clean:

```sh
# Debian / Ubuntu
cp -a packaging/debian debian && dpkg-buildpackage -us -uc -b
sudo apt install ../usual-install_*.deb

# Fedora
rpmbuild -ba packaging/fedora/usual-install.spec

# Arch
cp packaging/arch/PKGBUILD . && makepkg -si
```

Then, once:

```sh
sudo systemctl enable --now usuald.service   # provisions every browser it finds
usual-provision install --flatpak            # only if you have Flatpak browsers; no root
```

Restart your browser. Nothing else is needed — there is no extension to install,
and browsers added later are picked up automatically.

### Who holds the signing key

`make all` generates a keypair and signs the catalogue with it, so a self-built
installation trusts its own build. That is the right default for one machine and
the wrong one for distribution: anyone shipping this to other people replaces
`catalogue/signing.pub` with their own public key, keeps the private half
offline, and signs catalogues there. Neither half is in the repository.

### Updates

`catalogue_url` in `/etc/usual-install/config.json` is empty, because nobody is
hosting a catalogue yet. Set it and enable the timer to receive updates:

```sh
sudo systemctl enable --now usual-install-update.timer
```

Until then the catalogue is whatever `make all` produced, and will go stale.

## How resolution works

The catalogue is generated from Flathub's AppStream metadata — every app
declares its own homepage, so the mapping from a vendor's domain to a package
is **derived, never hand-curated**. Most-specific-first:

1. **hostname + longest matching path prefix**, with locale prefixes stripped,
   so `mozilla.org/en-US/firefox/new/` reaches the app that declared `/firefox`.
   61% of entries carry a path, so this is the common case.
2. **hostname root** — `zoom.us/download` → `us.zoom.Zoom`.
3. **registrable domain** — last resort.

A bucket larger than `max_candidates` (8) is shared hosting and never resolves.
That single threshold excludes `github.com` (963 apps), `sourceforge.net`,
`itch.io` and the roots of `apps.gnome.org`/`apps.kde.org` without naming any of
them. One candidate installs directly; two to eight become a picker, which is
the right answer for `jetbrains.com/idea/` (Community vs Ultimate).

Measured on the live catalogue: **3,375 apps, 1,351 domains, 1,224 of them
(90.6%) resolving to exactly one app.**

### Two guards

**A path that names the app marks a directory, not a vendor.** An app whose
homepage is `flathub.org/apps/<its-own-id>` is a listing in someone else's
catalogue, so a *different* path there is a different app. Without this,
`flathub.org/apps/org.kde.kdenlive` resolved to "Learn 6502 Assembly" — a
wrong-app match, the worst failure mode available here.

**Only installer-shaped downloads count**, judged by filename suffix, so a PDF
from a vendor's own domain is left alone.

## Security

**The catalogue is signed and verified before every use.** It decides what gets
installed, so anyone who could substitute it could choose what you install.
Ed25519 via the openssl CLI (the project has no third-party dependencies). A
catalogue that fails verification is not downgraded to "use it anyway" — it is
replaced with an empty one, making every download a no-op.

**Updates cannot make things worse.** The signature is checked before anything
moves into place, the move is atomic, an older catalogue cannot roll back a
newer one, and any failure leaves the previous catalogue untouched.

**The privileged surface is tiny.** A client may ask `usuald` to install a
*catalogue entry id*; it cannot name a package. The daemon re-resolves that id
against its own verified catalogue, so even a compromised session agent cannot
install something the catalogue does not list.

**Native installs are cross-checked against the distro's own metadata.** A
package name derived from a Flathub id is only a guess, so before installing
one `usuald` asks the local package manager what homepage that package
declares and requires it to match the domain the user was actually on:

```
curl declares https://curl.se/ — matches curl.se          → proceeds
refusing 'curl': it declares 'curl.se', not 'evil.example' → refused
```

**`flatpak --user` never runs as root.** It installs into the calling user's
flatpak, so a root daemon doing it would silently install into root's. That
backend stays with the session agent; the daemon exists only for installs that
genuinely need privilege, behind `org.usualinstall.install`.

## Distro support

Phase 1's installs go through `flatpak --user`, which is distro-independent;
`usuald` adds `pacman`, `apt` and `dnf`, chosen from `/etc/os-release`.

| Distro | Suite | Native backend | |
|---|---|---|---|
| Debian 13 | 21 pass | apt | arm64 native |
| Fedora 44 | 21 pass | dnf | arm64 native |
| Arch | 14 pass, 3 skip | pacman | x86_64 image; Chromium cannot run under QEMU on an arm64 host |

Arch's skipped tiers are an artefact of this hardware, not of Arch — the same
image runs the full suite on an x86_64 host.

## Browsers

**The user never installs an extension.** `usuald` writes a preinstall manifest
into each browser's own extensions directory and the browser installs the
sensor itself on next launch. Verified by launching each browser against a
fresh profile and looking for the extension on disk afterwards:

| Browser | Result |
|---|---|
| Chromium 154 | installs the sensor itself |
| Brave 1.97 | installs the sensor itself |
| Vivaldi 8.2 | installs the sensor itself |
| Chrome, Edge, Opera | untested — no arm64 Linux build exists |

Local `external_crx` still works on these versions, so the feared fallback to a
self-hosted `external_update_url` is not needed. `test/Dockerfile.browsers`
covers all six on an x86_64 host.

**Flatpak browsers work too, and need no root.** `flatpak override --user`
requires no privilege, so `usual-provision install --flatpak` grants the
permission, installs a `flatpak-spawn --host` wrapper the sandbox can execute,
and writes the manifest and CRX where the sandbox can actually read them. A
sandboxed Chromium both reaches the host and installs the sensor itself.

The subtlety that cost a round of debugging: Flatpak does **not** redirect
`~/.config` and `~/.local/share` inside the sandbox. It points
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` at `~/.var/app/<id>/{config,data}` by
their real absolute paths. A manifest referring to `~/.local/share/...` looks
correct and is invisible to the browser.

**Snap browsers cannot be supported.** Strict confinement offers no route to an
arbitrary host binary and no user-grantable permission that changes it. They are
detected and reported, with a recommendation to install a non-snap build. Snap
Chromium is Ubuntu's default, so this matters.

**Firefox and Zen are not covered.** They are Gecko, not Chromium: different
extension format, different provisioning, and Mozilla signing is mandatory.
Covering them is a second provisioning backend plus a signing pipeline.

## When there is no Linux version

A `.exe` or `.msi` means the vendor ships no Linux build, and is the only
trigger for this path. The download is **not** cancelled — the installer file is
what gets run — so the sensor lets it finish and setup happens around it.

Three outcomes, two of them "no": set up and run, an explicit **"not
supported"** window, or silence when the product cannot be identified.

Coverage is poor, and honestly so. Bottles' curated index has **36 entries**;
Lutris publishes no compatibility grade; ProtonDB is games-only via Steam
AppID; and **WineHQ AppDB — the real database — has no API at all**. Scraping a
volunteer-run site per install would be rude and a privacy leak, so it is not
implemented. That is the ceiling on this feature.

Identification therefore rests on weak signals (page title, filename, domain),
so the matcher refuses more than it accepts: "Steam" will not match
`steamy-photo-editor.exe`, and two candidate matches refuse outright.

## Testing

```sh
bash test/run.sh                                     # everything testable here

docker build -t ui-debian  -f test/Dockerfile.debian  . && docker run --rm --user root ui-debian
docker build -t ui-fedora  -f test/Dockerfile.fedora  . && docker run --rm --user root ui-fedora
docker build -t ui-browsers -f test/Dockerfile.browsers . && docker run --rm ui-browsers

limactl start --name=usual test/lima-usual.yaml       # tier 3: a real display
limactl shell usual -- bash /usual-install/test/tier3.sh
open vnc://localhost:5900                             # password: usual
```

| Tier | Proves |
|---|---|
| 1 | catalogues, resolver, matcher, runner choice, signing, updates, **usuald**, **packaging**, provisioning |
| 2 | sensor loads, both download paths intercepted end-to-end in a real browser |
| 3 | the windows as a user sees them, and a real `flatpak --user install` |

Tier 3 renders every window to `test/screenshots/` and performs a real install
with no `USUAL_DRY_RUN`.

### What testing has caught

Worth recording, because each was invisible to every earlier layer:

- **Every app name was localised.** AppStream ships one `<name>` per
  translation (Loupe has 59) and `find()` returns document order, which is
  alphabetical by language code. The window announced "عارض الصور" instead of
  "Image Viewer".
- **Closing the window did not stop the install.** The subprocess outlived its
  parent and kept installing invisibly.
- **Enter activated Cancel**, because no widget had focus.
- **GTK4 renders nothing on a GL-less display** — chrome without a body.
  `GSK_RENDERER=cairo` fixes it.
- **The signing key was nearly packaged.** The staged-install test now fails if
  any private key appears in the package.
- **`debian/compat` conflicted with `debhelper-compat`** — the package would
  not build at all, which only building it could reveal.
- **Uninstall left `/usr/lib/usual-install` behind**, because Python wrote
  `__pycache__` after installation and dpkg will not remove a directory holding
  files it does not know about. Bytecode is now shipped with the package and
  the runtime writes none (`python3 -B`).
- **Fixed `/tmp` log paths broke every test under a second user.** With
  `fs.protected_regular`, root cannot write a file another user owns in a
  sticky directory, so 16 passing tests reported as failures while their stale
  logs said "all passed".

## Verified end to end

The production chain, not a staging approximation:

```
.deb built → installed → systemctl enable --now usuald → service active
  → catalogue verified → Chromium provisioned to /etc/chromium/...
  → real browser → sensor → session agent → resolved via usuald
```

Also closed, each by actually doing it rather than reading:

- **polkit authorises a real privileged install.** With a rule permitting the
  user, an unprivileged caller installed `htop` through `apt` via the daemon,
  homepage cross-check passing (`htop declares https://htop.dev/ — matches
  htop.dev`). Every earlier test had only ever seen polkit *refuse*.
- **The native fallback runs.** Pointed at a Flathub id that does not exist, the
  installer window failed over to usuald and installed the distro package.
- **All three packages build.** `dpkg-buildpackage`, `rpmbuild` and `makepkg`,
  each checked for stray private keys.
- **Install → upgrade → uninstall → purge is clean**, leaving nothing behind.

## Known gaps

- **Chrome, Edge and Opera preinstall** is unverified; none publishes an arm64
  Linux build. One run of the browsers image on x86_64 settles it.
- **A real compatibility install** — real Bottles, real Wine, a real `.exe` —
  needs x86_64 hardware. The orchestration is tested against stub runners.
- **Catalogue governance**: signing is implemented, but who holds the key, who
  may add an entry, and how revocation works are unanswered. `catalogue_url`
  ships empty, so nothing updates until someone hosts a catalogue.
- **AUR** is not a backend. Arch native installs come from the official
  repositories only: an AUR PKGBUILD is arbitrary code, and running it would
  put a decision outside the signed catalogue, which is the one thing this
  design does not allow.
