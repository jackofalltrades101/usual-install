Name:           usual-install
Version:        0.2.0
Release:        1%{?dist}
Summary:        Install applications from vendor download pages using your package manager

License:        MIT
URL:            https://github.com/jackofalltrades101/usual-install
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3, make
Requires:       python3, python3-gobject, gtk4, flatpak, polkit
Recommends:     chromium

%description
Usual Install makes a vendor's own "Download for Linux" button end in a
working, installed application, the way an .exe installer does on Windows,
instead of a tarball or an AppImage with no desktop entry.

A system service provisions every Chromium-family browser on the machine,
including ones installed later, so there is no browser extension for the user
to find or install. When a download page is recognised, the matching package is
installed from Flathub or from the distribution's own repositories.

%prep
%autosetup

%build
[ -f catalogue/catalogue.json ] || python3 catalogue/build_catalogue.py -o catalogue/catalogue.json
[ -f compat/compat.json ] || python3 compat/build_compat.py -o compat/compat.json
# Both are produced by `make all` and carried in the source tarball; a package
# without them installs a service that resolves nothing.
test -f catalogue/catalogue.json.sig || { echo "catalogue is unsigned; run 'make all'"; exit 1; }
test -f sensor.crx || { echo "sensor.crx missing; run 'make all'"; exit 1; }

%install
make install DESTDIR=%{buildroot} PREFIX=%{_prefix}

%post
%systemd_post usuald.service usual-install-update.timer

%preun
%systemd_preun usuald.service usual-install-update.timer

%postun
%systemd_postun_with_restart usuald.service

%files
%license LICENSE
%doc README.md
%{_bindir}/usuald
%{_bindir}/usual-session
%{_bindir}/usual-settings
%{_bindir}/usualctl
%{_bindir}/usual-provision
%{_bindir}/usual-install-update
%{_prefix}/lib/usual-install/
%{_datadir}/usual-install/
%{_unitdir}/usuald.service
%{_unitdir}/usual-install-update.service
%{_unitdir}/usual-install-update.timer
%{_datadir}/polkit-1/actions/org.usualinstall.policy
%{_datadir}/applications/usual-install.desktop
%{_datadir}/icons/hicolor/scalable/apps/usual-install.svg
%config(noreplace) %{_sysconfdir}/usual-install/config.json

%changelog
* Sun Oct 12 2026 Usual Install <usual-install@example.invalid> - 0.2.0-1
- Phase 2: system service, native package backends, settings application.
