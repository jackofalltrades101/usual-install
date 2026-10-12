#!/usr/bin/env python3
"""Usual Install settings.

Deliberately small. Everything here is something a person might reasonably
want to change or check; nothing here is required to make the product work.

Talks to usuald over the same socket the session agent uses, and degrades to a
read-only view explaining what is missing when the daemon is not running.
"""
import json, pathlib, sys, threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "provision"))
import client as daemon_client  # noqa: E402


def row(label, value, dim=False):
    b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    k = Gtk.Label(xalign=0, label=label)
    k.set_size_request(190, -1)
    k.add_css_class("dim-label")
    v = Gtk.Label(xalign=0, label=str(value), wrap=True, selectable=True)
    if dim:
        v.add_css_class("dim-label")
    b.append(k); b.append(v)
    return b


def section(title):
    lbl = Gtk.Label(xalign=0)
    lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
    lbl.set_margin_top(10)
    return lbl


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Usual Install")
        self.set_default_size(620, 680)
        self.status = None
        self.config = {}

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.set_margin_top(18); self.box.set_margin_bottom(18)
        self.box.set_margin_start(20); self.box.set_margin_end(20)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.box)
        self.set_child(scroll)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            done, _ = daemon_client.call("status")
            status = done.get("status") if done.get("ok") else None
            done2, _ = daemon_client.call("config-get")
            config = done2.get("config", {}) if done2.get("ok") else {}
            GLib.idle_add(self.render, status, config, None)
        except daemon_client.DaemonUnavailable as e:
            GLib.idle_add(self.render, None, {}, str(e))
        except Exception as e:
            GLib.idle_add(self.render, None, {}, repr(e))

    # -- view ---------------------------------------------------------------

    def render(self, status, config, error):
        self.status, self.config = status, config
        child = self.box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.box.remove(child)
            child = nxt

        if error or not status:
            self.render_no_daemon(error)
            return

        self.box.append(section("Service"))
        self.box.append(row("Version", status.get("version")))
        self.box.append(row("Distribution", status.get("distro")))
        self.box.append(row("Native backend",
                            status.get("native_backend") or "none on this system"))
        self.box.append(row("Backends available", ", ".join(status.get("backends", [])) or "none"))

        sw = Gtk.Switch(active=bool(config.get("enabled", True)), halign=Gtk.Align.START)
        sw.connect("state-set", self.on_enabled)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        k = Gtk.Label(xalign=0, label="Enabled"); k.set_size_request(190, -1)
        k.add_css_class("dim-label")
        r.append(k); r.append(sw)
        self.box.append(r)

        cat = status.get("catalogue", {})
        self.box.append(section("Catalogue"))
        self.box.append(row("Generated", cat.get("generated") or "unknown"))
        self.box.append(row("Entries", f"{cat.get('hosts', 0)} hostnames"))
        verified = cat.get("verified")
        self.box.append(row("Signature",
                            "verified" if verified else "NOT VERIFIED — not in use"))
        self.box.append(row("Path", cat.get("path") or "none", dim=True))

        self.box.append(section("Browsers"))
        for name in status.get("browsers", []):
            self.box.append(row(name, "provisioned"))
        for name in status.get("flatpak_browsers", []):
            self.box.append(row(f"{name} (flatpak)", "sandboxed — run: provision --flatpak"))
        for name in status.get("snap_browsers", []):
            self.box.append(row(f"{name} (snap)",
                                "not supportable; install a non-snap build"))
        if not any((status.get("browsers"), status.get("flatpak_browsers"),
                    status.get("snap_browsers"))):
            self.box.append(row("", "no Chromium-family browser found", dim=True))

        btn = Gtk.Button(label="Re-provision browsers now", halign=Gtk.Align.START)
        btn.connect("clicked", self.on_provision)
        self.box.append(btn)

        self.box.append(section("Recent installs"))
        hist = status.get("history", [])
        if not hist:
            self.box.append(row("", "nothing yet", dim=True))
        for h in reversed(hist[-10:]):
            self.box.append(row(h.get("at", ""),
                                f"{h.get('id')} via {h.get('backend')} "
                                f"{'ok' if h.get('rc') == 0 else 'failed'}"))

        self.box.append(section("Ignored domains"))
        ign = config.get("ignored_domains", [])
        self.entry = Gtk.Entry(placeholder_text="example.com, one per line or comma separated",
                               text=", ".join(ign))
        self.entry.connect("activate", self.on_ignored)
        self.box.append(self.entry)
        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Downloads from these domains are left alone. Press Enter to save.")
        hint.add_css_class("dim-label")
        self.box.append(hint)

    def render_no_daemon(self, error):
        self.box.append(section("usuald is not running"))
        msg = Gtk.Label(xalign=0, wrap=True, label=(
            "Usual Install still works without the service — the browser "
            "extension and user-scope Flathub installs do not need it — but "
            "native package installs, system-wide provisioning and these "
            "settings do.\n\nStart it with:  sudo systemctl start usuald"))
        self.box.append(msg)
        if error:
            self.box.append(row("Detail", error, dim=True))
        btn = Gtk.Button(label="Try again", halign=Gtk.Align.START)
        btn.connect("clicked", lambda *_: self.refresh())
        self.box.append(btn)

    # -- actions ------------------------------------------------------------

    def set_config(self, **kw):
        def work():
            try:
                done, _ = daemon_client.call("config-set", config=kw)
                if not done.get("ok"):
                    GLib.idle_add(self.toast, done.get("reason", "could not save"))
            except Exception as e:
                GLib.idle_add(self.toast, str(e))
            GLib.idle_add(self.refresh)
        threading.Thread(target=work, daemon=True).start()

    def on_enabled(self, _sw, state):
        self.set_config(enabled=bool(state))
        return False

    def on_ignored(self, entry):
        items = [x.strip() for x in entry.get_text().replace("\n", ",").split(",")]
        self.set_config(ignored_domains=[x for x in items if x])

    def on_provision(self, _b):
        def work():
            try:
                done, _ = daemon_client.call("provision")
                GLib.idle_add(self.toast, done.get("reason", "done"))
            except Exception as e:
                GLib.idle_add(self.toast, str(e))
            GLib.idle_add(self.refresh)
        threading.Thread(target=work, daemon=True).start()

    def toast(self, text):
        d = Gtk.AlertDialog()
        d.set_message(str(text))
        d.show(self)


class SettingsApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.usualinstall.Settings",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        SettingsWindow(self).present()


if __name__ == "__main__":
    sys.exit(SettingsApp().run(sys.argv))
