# Usual Install
#
#   make catalogue          build and sign the catalogues (needs a signing key)
#   make crx                pack the sensor extension
#   make check              run everything testable on this machine
#   make install DESTDIR=   install into a staging root (used by all packaging)
#
# The installed layout keeps the source tree's shape under /usr/lib/usual-install,
# because the code finds its siblings by relative path. The /usr/bin entries are
# three-line wrappers, not copies.

PREFIX      ?= /usr
LIBDIR      ?= $(PREFIX)/lib/usual-install
BINDIR      ?= $(PREFIX)/bin
DATADIR     ?= $(PREFIX)/share/usual-install
SYSTEMDDIR  ?= $(PREFIX)/lib/systemd/system
POLKITDIR   ?= $(PREFIX)/share/polkit-1/actions
APPSDIR     ?= $(PREFIX)/share/applications
ICONDIR     ?= $(PREFIX)/share/icons/hicolor/scalable/apps
DOCDIR      ?= $(PREFIX)/share/doc/usual-install
SYSCONFDIR  ?= /etc

MODULES = daemon resolver catalogue compat provision session settings sensor
SIGNING_KEY ?= keys/catalogue-signing.key

.PHONY: all catalogue crx check install uninstall clean

all: catalogue crx

# A catalogue the daemon will refuse is worse than no catalogue, and an
# unsigned one is refused. So signing is not optional here: if no key exists,
# generate one. A self-built installation trusts its own key; a distributor
# replaces catalogue/signing.pub with theirs and keeps the private half offline.
$(SIGNING_KEY):
	@echo "No signing key found. Generating one for this build."
	@echo "The catalogue decides what gets installed, so it must be signed;"
	@echo "keep $(SIGNING_KEY) private and do not commit it."
	python3 catalogue/sign.py keygen --key $(SIGNING_KEY)

catalogue: $(SIGNING_KEY)
	python3 catalogue/build_catalogue.py -o catalogue/catalogue.json --sign-key $(SIGNING_KEY)
	python3 compat/build_compat.py -o compat/compat.json
	python3 catalogue/sign.py sign compat/compat.json --key $(SIGNING_KEY)
	python3 catalogue/sign.py verify catalogue/catalogue.json

# Any Chromium-family browser can pack the extension, not just chromium.
PACKER = $(firstword $(foreach b,chromium chromium-browser brave-browser \
            vivaldi-stable google-chrome-stable microsoft-edge-stable, \
            $(shell command -v $(b) 2>/dev/null)))

crx: $(SIGNING_KEY)
	@test -n "$(PACKER)" || { \
	  echo "No Chromium-family browser found to pack the extension."; \
	  echo "Install one of: chromium, brave-browser, vivaldi-stable,"; \
	  echo "google-chrome-stable, microsoft-edge-stable."; exit 1; }
	@echo "packing with $(PACKER)"
	python3 provision/provision.py keygen >/dev/null 2>&1 || true
	-$(PACKER) --pack-extension=$(CURDIR)/sensor \
	  --pack-extension-key=$(CURDIR)/keys/sensor.pem --no-sandbox >/dev/null 2>&1
	@test -f sensor.crx && echo "packed sensor.crx" \
	  || { echo "sensor.crx was not produced"; exit 1; }

check:
	bash test/run.sh

install:
	install -d $(DESTDIR)$(LIBDIR)
	for m in $(MODULES); do \
	  install -d $(DESTDIR)$(LIBDIR)/$$m; \
	  find $$m -maxdepth 1 -type f ! -name '*.pyc' ! -name 'key.pem' \
	       ! -name 'catalogue.json*' ! -name 'compat.json*' \
	       -exec install -m 0644 {} $(DESTDIR)$(LIBDIR)/$$m/ \; ; \
	done
	@# Subdirectories the loop above does not reach.
	install -d $(DESTDIR)$(LIBDIR)/sensor/icons
	install -m 0644 sensor/icons/*.png $(DESTDIR)$(LIBDIR)/sensor/icons/
	chmod 0755 $(DESTDIR)$(LIBDIR)/daemon/usuald.py \
	           $(DESTDIR)$(LIBDIR)/session/usual-session \
	           $(DESTDIR)$(LIBDIR)/session/installer.py \
	           $(DESTDIR)$(LIBDIR)/settings/usual-settings.py \
	           $(DESTDIR)$(LIBDIR)/provision/provision.py \
	           $(DESTDIR)$(LIBDIR)/catalogue/update.py

	@# Bytecode is shipped, not generated at runtime. Otherwise Python writes
	@# __pycache__ into $(LIBDIR) after installation, dpkg/rpm do not know about
	@# those files, and the directory survives uninstall.
	python3 -m compileall -q $(DESTDIR)$(LIBDIR) >/dev/null 2>&1 || true

	install -d $(DESTDIR)$(DATADIR)
	-install -m 0644 catalogue/catalogue.json     $(DESTDIR)$(DATADIR)/
	-install -m 0644 catalogue/catalogue.json.sig $(DESTDIR)$(DATADIR)/
	-install -m 0644 compat/compat.json           $(DESTDIR)$(DATADIR)/
	-install -m 0644 compat/compat.json.sig       $(DESTDIR)$(DATADIR)/
	-install -m 0644 sensor.crx                   $(DESTDIR)$(DATADIR)/

	install -d $(DESTDIR)$(BINDIR)
	@for b in usuald:daemon/usuald.py usual-session:session/usual-session \
	          usual-settings:settings/usual-settings.py \
	          usualctl:daemon/client.py usual-install-update:catalogue/update.py \
	          usual-provision:provision/provision.py; do \
	  name=$${b%%:*}; target=$${b##*:}; \
	  printf '#!/bin/sh\nexec python3 -B %s/%s "$$@"\n' "$(LIBDIR)" "$$target" \
	    > $(DESTDIR)$(BINDIR)/$$name; \
	  chmod 0755 $(DESTDIR)$(BINDIR)/$$name; \
	done
	@# The update wrapper reads the URL out of the configuration file.
	printf '#!/bin/sh\nurl=$$(python3 -c "import json,sys;print(json.load(open(\\"$(SYSCONFDIR)/usual-install/config.json\\")).get(\\"catalogue_url\\",\\"\\"))" 2>/dev/null)\n[ -n "$$url" ] || { echo "no catalogue_url configured; nothing to do"; exit 0; }\nexec python3 -B %s/catalogue/update.py --url "$$url"\n' "$(LIBDIR)" \
	  > $(DESTDIR)$(BINDIR)/usual-install-update
	chmod 0755 $(DESTDIR)$(BINDIR)/usual-install-update

	install -d $(DESTDIR)$(SYSTEMDDIR)
	install -m 0644 packaging/systemd/*.service packaging/systemd/*.timer $(DESTDIR)$(SYSTEMDDIR)/
	install -d $(DESTDIR)$(POLKITDIR)
	install -m 0644 packaging/polkit/org.usualinstall.policy $(DESTDIR)$(POLKITDIR)/
	install -d $(DESTDIR)$(APPSDIR)
	install -m 0644 packaging/usual-install.desktop $(DESTDIR)$(APPSDIR)/
	install -d $(DESTDIR)$(ICONDIR)
	install -m 0644 packaging/usual-install.svg $(DESTDIR)$(ICONDIR)/
	install -d $(DESTDIR)$(SYSCONFDIR)/usual-install
	install -m 0644 packaging/config.json $(DESTDIR)$(SYSCONFDIR)/usual-install/config.json
	install -d $(DESTDIR)$(DOCDIR)
	install -m 0644 README.md $(DESTDIR)$(DOCDIR)/

uninstall:
	rm -rf $(DESTDIR)$(LIBDIR) $(DESTDIR)$(DATADIR) $(DESTDIR)$(DOCDIR)
	rm -f $(DESTDIR)$(BINDIR)/usuald $(DESTDIR)$(BINDIR)/usual-session \
	      $(DESTDIR)$(BINDIR)/usual-settings $(DESTDIR)$(BINDIR)/usualctl \
	      $(DESTDIR)$(BINDIR)/usual-install-update $(DESTDIR)$(BINDIR)/usual-provision
	rm -f $(DESTDIR)$(SYSTEMDDIR)/usuald.service \
	      $(DESTDIR)$(SYSTEMDDIR)/usual-install-update.service \
	      $(DESTDIR)$(SYSTEMDDIR)/usual-install-update.timer
	rm -f $(DESTDIR)$(POLKITDIR)/org.usualinstall.policy
	rm -f $(DESTDIR)$(APPSDIR)/usual-install.desktop
	rm -f $(DESTDIR)$(ICONDIR)/usual-install.svg

clean:
	rm -f sensor.crx
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
