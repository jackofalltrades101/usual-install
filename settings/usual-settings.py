#!/usr/bin/env python3
"""Usual Install settings.

Deliberately small. Everything here is something a person might reasonably
want to change or check; nothing here is required to make the product work.

Talks to usuald over the same socket the session agent uses, and degrades to a
read-only view explaining what is missing when the daemon is not running.
"""
import json, pathlib, sys, threading
import subprocess

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
sys.path.insert(0, str(ROOT / "provision"))
import client as daemon_client  # noqa: E402
sys.path.insert(0, str(ROOT / "session"))
import style  # noqa: E402


def readable_time(stamp):
    """'2026-10-12T07:23:00Z' -> '12 Oct 2026, 07:23 UTC'."""
    import time
    try:
        return time.strftime("%-d %b %Y, %H:%M UTC", time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return stamp


def group(box, title):
    """A titled Plasma-style group. Returns its Form."""
    lbl = Gtk.Label(xalign=0, label=title)
    lbl.add_css_class("group-title")
    box.append(lbl)
    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    frame.add_css_class("group")
    form = style.Form()
    frame.append(form.grid)
    box.append(frame)
    form.frame = frame
    return form


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Usual Install")
        style.apply()
        self.add_css_class("usual")
        self.set_icon_name("usual-install")
        self.set_default_size(620, 680)
        self.status = None
        self.config = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        head.add_css_class("header")
        style.Header(head, icon_name="usual-install").set(
            "Usual Install", "Installs apps from the download pages you already use")
        root.append(head)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.add_css_class("body")
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.box)
        root.append(scroll)
        self.set_child(root)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            done, _ = daemon_client.call("status")
            status = done.get("status") if done.get("ok") else None
            try:
                upd, _ = daemon_client.call("updates")
                self.updates = upd.get("items") or []
            except Exception:
                self.updates = []
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
        self.updatable = []
        child = self.box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.box.remove(child)
            child = nxt

        if error or not status:
            self.render_no_daemon(error)
            return

        f = group(self.box, "Service")
        sw = Gtk.Switch(active=bool(config.get("enabled", True)), halign=Gtk.Align.START)
        sw.connect("state-set", self.on_enabled)
        f.add("Turned on", sw)
        f.add("Version", status.get("version"))
        f.add("Distribution", status.get("distro"))
        f.add("System packages", status.get("native_backend") or "none on this system")
        f.add("Install methods", ", ".join(status.get("backends", [])) or "none")

        # Which source the Install button lands on. Both stay available in the
        # installer; this only decides which one is offered first.
        prefer = config.get("prefer_source", "vendor")
        self.prefer_labels = ["The developer's own package",
                              "Flathub and your distribution first"]
        drop = Gtk.DropDown.new_from_strings(self.prefer_labels)
        drop.set_selected(0 if prefer == "vendor" else 1)
        drop.connect("notify::selected", self.on_prefer)
        f.add("Install by default", drop)

        cat = status.get("catalogue", {})
        f = group(self.box, "App catalogue")
        f.add("Last updated", readable_time(cat.get("generated")) or "unknown")
        f.add("Covers", f"{cat.get('hosts', 0)} websites")
        f.add("Location", cat.get("path") or "none", mono=True)
        m = style.Message()
        if cat.get("verified"):
            m.show("positive", "The catalogue's signature is valid.")
        else:
            m.show("negative", "The catalogue's signature did not verify, so it is "
                               "not being used and nothing will be installed.")
        f.frame.append(m.box)

        f = group(self.box, "Browsers")
        for name in status.get("browsers", []):
            f.add(name, "Set up")
        ready = set(status.get("flatpak_ready", []))
        for name in status.get("flatpak_browsers", []):
            f.add(f"{name} (Flatpak)", "Set up" if name in ready
                  else "Not set up yet: press Refresh browsers")
        for name in status.get("snap_browsers", []):
            f.add(f"{name} (Snap)", "Cannot be supported; install a non-Snap version")
        if not any((status.get("browsers"), status.get("flatpak_browsers"),
                    status.get("snap_browsers"))):
            f.add("", "No Chromium-based browser found")
        btn = Gtk.Button(label="Refresh browsers", halign=Gtk.Align.START,
                         tooltip_text="Set up every browser again, including ones "
                                      "installed since Usual Install was. "
                                      "Same as: usualctl refresh")
        btn.connect("clicked", self.on_provision)
        f.frame.append(btn)

        self.render_updates()

        f = group(self.box, "Recent activity")
        hist = status.get("history", [])
        if not hist:
            f.add("", "Nothing installed yet")
        for h in reversed(hist[-6:]):
            f.add(readable_time(h.get("at")) or "",
                  f"{h.get('id')} from {h.get('backend')}: "
                  f"{'installed' if h.get('rc') == 0 else 'failed'}")

        f = group(self.box, "Sites to leave alone")
        ign = config.get("ignored_domains", [])
        self.entry = Gtk.Entry(placeholder_text="example.com, another.org",
                               text=", ".join(ign), hexpand=True)
        self.entry.connect("activate", self.on_ignored)
        f.add("Websites", self.entry)
        f.add("", "Downloads from these sites are never intercepted. Press Enter to save.")

    def render_updates(self):
        """Everything Usual Install put on this machine, and what can move on.

        Worth being plain about: a package taken from a vendor's own download
        page usually has nowhere to update from. That is the real cost of
        preferring it, and it belongs in front of the user rather than in a
        README.
        """
        f = group(self.box, "Installed by Usual Install")
        rows = list(self.updates or [])

        # flatpak --user installs belong to this user, not to root, so ask
        # flatpak directly rather than through the daemon.
        try:
            inst = subprocess.run(
                ["flatpak", "--user", "list", "--app", "--columns=application,version"],
                capture_output=True, text=True, timeout=30).stdout.strip()
            upd = subprocess.run(
                ["flatpak", "--user", "remote-ls", "--updates", "--columns=application"],
                capture_output=True, text=True, timeout=60).stdout.split()
            for line in [l for l in inst.splitlines() if l.strip()]:
                parts = line.split("\t") if "\t" in line else line.split()
                app = parts[0]
                rows.append({"name": app, "backend": "flatpak-user",
                             "installed": parts[1] if len(parts) > 1 else "",
                             "updatable": app in upd, "tracked": True})
        except (OSError, subprocess.SubprocessError):
            pass

        if not rows:
            f.add("", "Nothing yet")
            return

        self.updatable = [r["name"] for r in rows
                          if r.get("updatable") and r["backend"] != "flatpak-user"]
        for r in rows:
            where = {"flatpak-user": "Flathub"}.get(r["backend"], r["backend"])
            if r.get("updatable"):
                state = f"update available ({r.get('available') or 'newer'})"
            elif not r.get("tracked"):
                state = "no automatic updates — the developer's own package"
            else:
                state = f"up to date ({r.get('installed') or ''})".strip()
            f.add(r["name"], f"{where} · {state}")

        flat_updates = [r["name"] for r in rows
                        if r.get("updatable") and r["backend"] == "flatpak-user"]
        if self.updatable or flat_updates:
            btn = Gtk.Button(label="Update everything", halign=Gtk.Align.START)
            btn.connect("clicked", self.on_update)
            f.frame.append(btn)
        else:
            m = style.Message()
            m.show("positive", "Everything that can update is up to date.")
            f.frame.append(m.box)

    def on_prefer(self, drop, _p):
        self.set_config(prefer_source="vendor" if drop.get_selected() == 0 else "flathub")

    def on_update(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Updating…")

        def work():
            msgs = []
            try:
                subprocess.run(["flatpak", "--user", "update", "-y", "--noninteractive"],
                               capture_output=True, timeout=1800)
                msgs.append("Flathub applications updated.")
            except (OSError, subprocess.SubprocessError) as e:
                msgs.append(f"Flathub update failed: {e}")
            if self.updatable:
                try:
                    done, _ = daemon_client.call("update", names=self.updatable)
                    msgs.append(done.get("reason", "done"))
                except Exception as e:
                    msgs.append(str(e))
            GLib.idle_add(self.toast, "\n".join(msgs))
            GLib.idle_add(self.refresh)
        threading.Thread(target=work, daemon=True).start()

    def render_no_daemon(self, error):
        m = style.Message()
        m.show("warning", "The Usual Install service is not running. Downloads still "
                          "install from Flathub without it, but installs from your "
                          "distribution's repositories and these settings need it. "
                          "Start it with: sudo systemctl enable --now usuald")
        self.box.append(m.box)
        if error:
            d = Gtk.Label(xalign=0, wrap=True, label=error, selectable=True)
            d.add_css_class("summary")
            self.box.append(d)
        btn = Gtk.Button(label="Check again", halign=Gtk.Align.START)
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

    def on_provision(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Refreshing…")

        def work():
            try:
                results = daemon_client.refresh_browsers()
                text = "\n".join(("✓ " if ok else "✕ ") + m for ok, m in results)
                text += "\n\nRestart your browsers to pick up any changes."
            except Exception as e:
                text = f"Refreshing failed: {e}"
            GLib.idle_add(self.toast, text)
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
