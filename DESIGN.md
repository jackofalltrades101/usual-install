# Usual Install

> Daemon `usuald` · session agent `usual-session` · CLI `usualctl` · package `usual-install`
> DBus `org.usualinstall.Daemon` · native host `com.usualinstall.host`

## Goal

Make "Download for Linux" on a vendor's own website end in a working, installed
application — the way an `.exe` installer does on Windows — instead of a tarball,
an AppImage with no desktop entry, or a copy-pasted `curl | sh`.

The user installs one thing, once. After that it is invisible: every Chromium-based
browser on the machine is covered, including ones installed later, with no extension
to fetch, no per-site setup, and no configuration required to get the default behaviour.

## Shape

A root-level background service, plus a thin browser-side sensor that the service
provisions into each browser by itself, plus two small GUI surfaces: the installer
window shown per install, and a settings app for people who want to change defaults.

```
┌──────────────────────────────────────────────────────────────────────┐
│ usuald — system service (root, systemd)                            │
│   · owns the signed domain→package catalogue                         │
│   · provisions every Chromium browser it finds (and new ones)        │
│   · executes privileged installs behind one narrow polkit action     │
└──────────────────────────────────────────────────────────────────────┘
          ▲ DBus (system bus)                      │ writes config files
          │                                        ▼
┌─────────────────────────────┐        ┌────────────────────────────────┐
│ usual-session (user)      │        │ Chromium / Chrome / Brave /    │
│   · native-messaging host   │◀──────▶│ Edge / Vivaldi / Opera …       │
│   · draws installer window  │ stdio  │   · sensor extension           │
│   · runs flatpak --user     │        │     (provisioned, not fetched) │
└─────────────────────────────┘        └────────────────────────────────┘
```

Why the browser-side piece still exists: nothing outside the browser can observe or
cancel a download. There is no supported hook for it, and the unsupported routes
(`LD_PRELOAD`, binary patching, attaching to `--remote-debugging-port`) break on every
Chromium update. The sensor is unavoidable — but the user never installs it, and never
sees it. `usuald` puts it there.

## Browser support

Two guarantees, stated plainly because they are the whole point of the architecture:

**The user never installs an extension.** Not from the Chrome Web Store, not a sideloaded
`.crx`, not a developer-mode unpacked folder. `usuald` writes a preinstall manifest into the
browser's own extensions directory and the browser installs the sensor itself on next launch.
There is no point in the flow where the user is sent to a store page or asked to enable
anything. Uninstalling `usual-install` removes the manifests, and the browsers drop the sensor.

**Installing once covers browsers added later.** The inotify watch on `/opt`, `/usr/share`,
and the flatpak roots means a browser installed six months from now gets provisioned when it
appears, not at `usual-install` setup time.

### Coverage

| Browser | Engine | Status |
|---|---|---|
| Chromium | Chromium | supported |
| Google Chrome | Chromium | supported |
| Brave | Chromium | supported |
| Vivaldi | Chromium | supported |
| Microsoft Edge | Chromium | supported — verify, Edge restricts some Chromium extension paths |
| Opera | Chromium | uncertain — own add-on store, historically weak Chromium policy support |
| **Zen** | **Gecko** | **not covered — see below** |
| Firefox, LibreWolf, Waterfox | Gecko | not covered |

### Zen is not Chromium

Zen is a Firefox fork, not a Chromium one. Nothing in this design reaches it: different
extension format (XPI, not CRX), different provisioning mechanism (`policies.json` with
`ExtensionSettings`, not preinstall JSON), different native-messaging directory
(`/usr/lib/mozilla/native-messaging-hosts/`), and a hard additional dependency — Firefox
release builds refuse unsigned extensions, so the XPI must be signed by Mozilla. AMO offers
self-distribution signing without public listing, so this is possible, but it means an
external signing step in the build and a review dependency outside the project's control.

Covering Zen and Firefox is a **second provisioning backend plus a Mozilla signing pipeline**.
The sensor logic ports over nearly unchanged via WebExtensions, and `usuald` is engine-agnostic
below the provisioner, so the cost is contained — but it is real work, not a path addition. Not
in Phases 1–3 as written. Worth scheduling as Phase 4 if Gecko coverage matters.

### The honest gap: sandboxed browsers

Snap and Flatpak browsers cannot spawn a host binary without an override the user runs
themselves. This is the one place "no user action" does not hold, and it is not a niche case:
Snap Chromium is Ubuntu's default browser package, and Flatpak Brave is common on Fedora and
immutable distros.

**Resolved, and better than this section assumed.** `flatpak override --user` needs no
root and no interaction, so the provisioner grants the permission itself —
"install once" does not have to break for Flatpak browsers. `provision.py install
--flatpak` grants the override, installs a `flatpak-spawn --host` wrapper the sandbox can
execute, and writes the manifest and CRX into the sandbox's view of `~/.config`. A
sandboxed Chromium demonstrably reaches the host through it. One piece is still
unconfirmed: that browser did not install the sensor from its preinstall manifest, and
why is an open question.

**Snap remains unsupportable.** Strict confinement offers no route to an arbitrary host
binary and no user-grantable permission that changes it. Since Snap Chromium is Ubuntu's
default browser package, the honest answer there is to detect it and recommend a non-snap
build — which `provision.py status` now does.

Either way these browsers report as *unprovisioned* rather than appearing covered and
silently doing nothing.

## Components

### `usuald` — system service

Runs as root under systemd. Three jobs:

**Catalogue.** Holds a signed, locally cached map of domain → package. Verifies the
signature before use. Resolution happens here and nowhere else.

**Provisioning.** Enumerates installed Chromium-family browsers and writes two files
per browser:

| File | Purpose |
|---|---|
| `<browser>/extensions/<ext-id>.json` | preinstall manifest — browser installs the sensor itself |
| `<browser>/native-messaging-hosts/com.usualinstall.host.json` | lets the sensor reach `usual-session` |

Known locations:

```
/usr/share/chromium/extensions/              /etc/chromium/native-messaging-hosts/
/opt/google/chrome/extensions/               /etc/opt/chrome/native-messaging-hosts/
/opt/brave.com/brave/extensions/             /etc/brave/native-messaging-hosts/
/opt/microsoft/msedge/extensions/            /etc/opt/edge/native-messaging-hosts/
/opt/vivaldi/extensions/                     /etc/vivaldi/native-messaging-hosts/
```

An inotify watch on `/opt`, `/usr/share`, and the flatpak install roots re-provisions
when a new browser appears, so "install once" holds even for browsers added months later.

Optionally — **off by default**, see [Decisions](#decisions) — also writes a policy file
to `/etc/<browser>/policies/managed/usual-install.json` to force-install the sensor so it
cannot be disabled. Must *merge* with existing policy files rather than overwrite them;
a managed machine may already have real admin policy there.

**Privileged install.** Exposes one DBus method that accepts a *catalogue entry ID*, not
a package name and not a URL. Validates the ID against the local catalogue, then runs the
backend behind a single narrow polkit action.

### `usual-session` — per-user session agent

Runs under `systemd --user`. The browser spawns the native-messaging host as the logged-in
user, so this is the only piece the browser talks to directly. It owns the installer
window, performs rootless `flatpak --user` installs itself, and forwards anything needing
privilege to `usuald` over DBus.

### Sensor extension

MV3, minimal. Permissions: `downloads`, and host access only for domains in the catalogue.

- On navigation, asks whether the current domain is handled.
- On download, cancels it (`downloads.onCreated` → `downloads.cancel`, or
  `declarativeNetRequest` to block the request outright, which is cleaner) and reports
  `{domain, url, filename}` to the session agent.
- Holds no catalogue, makes no install decision, names no package.

### Installer window

The `.exe` feel from the original idea, drawn by `usual-session`:

```
┌─────────────────────────────────────────┐
│  Zoom Installer                         │
│                                         │
│  Zoom Workplace                         │
│  us.zoom.Zoom · Flathub · 312 MB        │
│                                         │
│  [████████████░░░░░░░░]  58%            │
│                                         │
│                    [ Cancel ] [Install] │
└─────────────────────────────────────────┘
```

Shows the *resolved* package, backend, and source repo — the page cannot influence what
this window says. For AUR or any source-built package the progress bar is replaced by a
streamed build log, since those are minutes, not seconds.

### Settings app

GTK4/Adwaita. Deliberately small:

- Master on/off, and on/off per browser
- Backend preference order (default: `flatpak --user` → native repo → nothing)
- Catalogue sources, update cadence, last-verified timestamp
- Per-domain overrides and an ignore list
- Install history
- Which browsers are currently provisioned (and which failed, e.g. sandboxed ones)

## Catalogue

Hand-maintaining a domain→package map from zero is what kills projects like this. Bootstrap
instead:

- **Flathub AppStream** — every app ships `<url type="homepage">`, which is a domain→flatpak
  mapping for thousands of apps, free.
- **Repology** — covers the distro repositories for native-package fallback.
- **Curated overrides** — a small hand-maintained layer on top for misses and ambiguities
  (`zoom.us` → `us.zoom.Zoom`), which is also where disambiguation lives when a vendor ships
  several products from one domain.

Shipped signed, verified on update. This file decides what root installs, so it is the
highest-value target in the system and should be treated as such.

### Resolution

Most-specific-first, every rule derived from the data rather than curated:

1. **hostname + longest matching path prefix**, tried with and without a locale prefix.
   61% of entries carry a path, so this is the common case, and locale prefixes are
   everywhere — `mozilla.org/en-US/firefox/new/` has to reach the app that declared
   `/firefox`.
2. **hostname root** — `zoom.us/download` → `us.zoom.Zoom`.
3. **registrable domain** — last resort.

A bucket larger than `max_candidates` (8) is shared hosting and never resolves. That one
threshold excludes `github.com` (963 apps), `github.io`, `gitlab.com`, `sourceforge.net`,
`itch.io` and the roots of `apps.gnome.org` and `apps.kde.org` without naming any of them.
One candidate installs directly; two to eight become the picker.

Measured against the live catalogue: **3,375 apps, 1,351 domains, 1,224 of them (90.6%)
resolving to exactly one app.** Path matching is what makes the umbrella domains work —
`gnome.org`'s 181 apps separate into 180 distinct host+path pairs, `kde.org`'s 161 into 160.

### Two guards

**A path that names the app marks a directory, not a vendor.** An app whose declared
homepage is `flathub.org/apps/<its-own-id>` is a listing in someone else's catalogue, so a
*different* path on that host is a different app. Without this rule,
`flathub.org/apps/org.kde.kdenlive` resolved to "Learn 6502 Assembly" — a wrong-app match,
which is the worst failure mode available here and the one most corrosive to trust. A
vendor *section* path like `libreoffice.org/discover/libreoffice` is deliberately not
treated this way, so `libreoffice.org/download/...` still resolves.

**Only installer-shaped downloads count.** Judged by filename suffix, so a PDF or an image
from a vendor's own domain is left alone.

## When there is no Linux version

A `.exe` or `.msi` on a vendor's download page means they ship no Linux build. That is
the only trigger for this path — a Linux-shaped download never reaches it.

The download is **not** cancelled here, unlike the native path. The installer file is the
thing that gets run, so the sensor lets it finish and the session agent sets up a
compatibility layer around the completed file. Three outcomes:

| | |
|---|---|
| **Set up and run** | Identified, rated platinum or gold, and a layer is available or installable |
| **"Not supported"** | Identified, but rated too low or this machine cannot run it. Says so plainly, and leaves the download alone |
| **Silence** | Could not identify the product. Behaves exactly as if Usual Install were absent |

The window never pretends this is a native application. It names the compatibility layer
and the rating, because a user who thinks they installed a native app and then hits Wine
quirks will blame Usual Install.

### The data problem, stated plainly

The native catalogue works because every Flathub app declares its own homepage, so it can
be keyed by domain. **No compatibility source does that**, and the best one is not
machine-readable at all:

| Source | Machine-readable | Coverage |
|---|---|---|
| Bottles `index.yml` | yes | **36 entries**, 23 platinum/gold, mostly game launchers |
| Lutris API | yes | games and some apps, but publishes no compatibility grade |
| ProtonDB | yes | games only, keyed by Steam AppID — not where vendor downloads come from |
| **WineHQ AppDB** | **no — HTML only** | thousands of apps, with exactly the grades needed |

WineHQ is the real Windows-application compatibility database and it has no API. Scraping
a volunteer-run site on every install would be both rude and a privacy leak, so it is not
implemented. **That is the ceiling on this feature.** "Not supported" is therefore the
common answer, which is the correct behaviour but worth knowing is mostly a data problem
rather than a design one. A rate-limited, cached, build-time WineHQ index is the single
change that would most improve coverage.

### Identification and the refusal to guess

With no domain key, identification rests on the page title, the download filename, and the
domain label. Weak signals plus a low bar produce wrong-app matches, which are worse than
having no feature — so the bar is high: exact squashed name match, or containment of a
name at least six characters long, or every identity-bearing word present. Single short
names cannot match a superstring, so "Steam" never matches `steamy-photo-editor.exe`. Two
candidate matches means refuse outright rather than pick.

### Choosing a layer

Architecture, Vulkan, GPU vendor, and which of Bottles/Lutris/Steam/Wine are present are
all detected locally — no network, no guessing. Only platinum and gold are offered: silver
and bronze mean "runs with problems", which in practice means a user who concludes Usual
Install is broken. Non-x86 is refused outright; box64 and FEX exist but not at a quality
worth offering.

Bottles is preferred, and installed from Flathub at user scope when absent — the same
rootless mechanism the native path already uses, so this adds no privilege requirement.
Lutris and Proton are preferred where already present, but their per-title install scripts
are not driven from here; that is a per-game format, not something to fake.

## Privilege model

| Path | Cost to the user |
|---|---|
| `flatpak --user` | nothing — no root, no prompt |
| Native repo (`pacman`/`apt`/`dnf`) | one narrow polkit action, authorized once at setup |

Preferring user-scope flatpak when the app is on Flathub is what keeps the common case
promptless. Falling back to the native repo through a pre-authorized polkit action — rather
than a `pkexec` password prompt per install — is what keeps the uncommon case from feeling
like `sudo apt install` with extra steps.

## Security

A root service acting on signals derived from web pages needs these to be true. They are
load-bearing, not boilerplate:

- **The page never names a package.** It contributes a domain. `usuald` resolves
  domain → package against its own signed catalogue.
- **Unknown domain is a no-op**, never a passthrough. The download simply proceeds normally.
- **The privileged interface accepts catalogue entry IDs only**, validated locally.
  Arbitrary package names are unrepresentable in it.
- **A native package name is cross-checked against the distro's own metadata.** The package
  name derived from a Flathub id is a guess, so before installing one `usuald` asks the
  local package manager what homepage that package declares and requires it to match the
  domain the user was on. This is the same principle the Flathub catalogue rests on,
  applied locally and at install time.
- **The confirmation window is drawn by the session agent**, never by page content, and shows
  what will actually happen.
- **The catalogue signature is verified before use**, every time.
- **One pending install at a time**, rate-limited, so a hostile page cannot queue work.

Explicitly rejected: a local MITM proxy with its own root CA. It would remove the need for a
browser-side component entirely, and intercepting all of the user's TLS is far worse than
anything else in this document.

## Phases

**Phase 1 — proof of feel.** Arch only. Flathub only. The full generated catalogue —
no hardcoded domains, nothing hand-held. Sensor + session agent + installer window; no
`usuald`, no polkit, no settings app. Rootless throughout, since `flatpak --user` needs
nothing. Goal is a single question: does the UX actually land?

Built, in `usual-install/`, including the compatibility-layer fallback and catalogue
signing. **Everything passes**: 18 checks in a container, 4 across three real browsers,
and 9 in a Lima VM where the windows render and a real `flatpak --user install`
completes — `org.gnome.Loupe` installed through the window, no dry run.

Tier 3 earned its keep: it caught four bugs every earlier tier passed over, including
every application name rendering in a localised language rather than English, and Cancel
failing to stop an install already running.

**The preinstall mechanism is confirmed.** Chromium 154, Brave 1.97 and Vivaldi 8.2 each
install the sensor themselves from the manifest, with no user action — local
`external_crx` still works, so the feared fallback to a self-hosted `external_update_url`
is not needed. Chrome, Edge and Opera remain untested: none publishes an arm64 Linux
build, and the test harness covers all six on an x86_64 host.

**Phase 2 — install once, every browser. Built.** `usuald` runs under systemd, owns the
signed catalogue and all resolution, provisions every Chromium fork it finds (and any
installed later, by polling rather than inotify — the same promise kept more simply), and
performs native installs behind `org.usualinstall.install`. Backends for `pacman`, `apt`
and `dnf`, chosen from `/etc/os-release`. GTK4 settings application. Catalogue updates on
a systemd timer, with rollback protection.

One deliberate deviation: the daemon speaks over a Unix socket rather than DBus. Both ends
are ours, `SO_PEERCRED` gives polkit exactly the caller identity it needs, and it keeps the
project free of third-party dependencies.

**Phase 3 — breadth. Mostly built.** Packaging for all three families — PKGBUILD,
`debian/`, and an RPM spec, all driving one `make install`. systemd units, a polkit policy,
a desktop entry and icons. Flatpak browser provisioning, rootlessly.

Still open: catalogue governance (who holds the key, who may add an entry, how revocation
works), and AUR as a backend — Arch native installs currently come from the official
repositories only.

Catalogue signing landed early and is enforced everywhere: an Ed25519 signature is
verified before every use, and anything that fails is replaced with an empty catalogue
rather than trusted. Updates verify before moving anything into place, replace atomically,
and refuse to roll back to an older catalogue.

## Decisions

**Force-install defaults off.** The mechanics are identical either way, so defaulting to
preinstall costs nothing technically. Force-installed extensions cannot be removed from
`chrome://extensions` and show "Installed by your administrator" — precisely the adware
signature, and a reliable way to get flagged by scanners and refused by distro packagers.
It stays available as an opt-in for people who want it locked down.

## Open questions

- **Flatpak browsers: why was the sensor not installed?** The permission grant works
  rootless and the sandbox can reach the host (`HOST-REACHED`), but the sandboxed
  Chromium did not install the extension from its preinstall manifest. This is the last
  thing between Flatpak browsers being fully supported and being a documented gap.
- **Chrome, Edge and Opera preinstall.** Settled for Chromium, Brave and Vivaldi; these
  three publish no arm64 Linux build so they could not be tested here. The harness covers
  all six on an x86_64 host — one run finishes the matrix.
- **Multi-variant download pages.** A page offering `.deb`, `.rpm`, and AppImage — does the
  sensor intercept all of them identically?
- **Enterprise collision.** Merge semantics for existing managed-policy files.
- **Catalogue governance.** Who can add an entry, and what stops a malicious one.
- **Version drift.** The repo package may be older than what the site offers. Say so in the
  installer window, or stay quiet?
- **A WineHQ source.** Scraped at build time, rate-limited and cached, or not at all? This
  is the difference between the compatibility feature being useful and being a curiosity.
- **Grade bar.** Platinum and gold only is the current rule. Is silver worth offering behind
  an explicit "this is known to have problems" confirmation, or does that just generate
  support load?

## Out of scope

Gecko browsers, Zen included — see [Browser support](#zen-is-not-chromium) for what covering
them would actually take. Windows and macOS as *host* platforms. Replacing the package
manager. Hosting packages. Anything that installs without the user clicking Install. Making
a poorly-rated Windows application work anyway — if no layer runs it well, the answer is
"not supported".
