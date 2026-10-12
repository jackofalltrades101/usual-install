#!/usr/bin/env python3
"""The installer window -- the '.exe installer' surface.

Drawn entirely by us. Page content cannot influence what it says: it is handed
a resolved result by usual-session and shows the package id, the remote it
comes from, and nothing the website supplied.

Three modes:

  native       a Linux package exists -- install it from Flathub
  compat       no Linux build, but the Windows version runs well through a
               compatibility layer; says so plainly rather than pretending
  unsupported  we identified the application and it will not run acceptably

    ./installer.py --payload '{"candidates":[{"id":"us.zoom.Zoom","name":"Zoom",
                               "summary":"Video conferencing","remote":"flathub"}],
                               "pageUrl":"https://zoom.us/download"}'
"""
import argparse, json, os, re, subprocess, sys, threading, time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "compat"))
sys.path.insert(0, str(_ROOT / "daemon"))
import setup as compat_setup  # noqa: E402
import client as daemon_client  # noqa: E402

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
# Containers and CI have no usable flatpak. USUAL_DRY_RUN exercises the whole
# window -- picker, progress, log, completion -- without touching the system.
DRY_RUN = os.environ.get("USUAL_DRY_RUN") == "1"
PCT = re.compile(r"(\d{1,3})\s*%")


class InstallerWindow(Gtk.ApplicationWindow):
    def __init__(self, app, candidates, page_url, domain=""):
        self.candidates = candidates
        self.page_url = page_url
        self.domain = domain
        self.chosen = candidates[0]

        title = f"{self.chosen['name']} Installer"
        super().__init__(application=app, title=title)
        self.set_default_size(460, 220)
        self.set_resizable(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(22); outer.set_margin_bottom(18)
        outer.set_margin_start(22); outer.set_margin_end(22)
        self.set_child(outer)

        self.heading = Gtk.Label(xalign=0)
        self.heading.set_markup(f"<span size='x-large' weight='bold'>{GLib.markup_escape_text(self.chosen['name'])}</span>")
        outer.append(self.heading)

        self.subtitle = Gtk.Label(xalign=0, wrap=True)
        self.subtitle.add_css_class("dim-label")
        outer.append(self.subtitle)

        # Picker only when the domain genuinely maps to more than one product.
        self.combo = None
        if len(candidates) > 1:
            pick = Gtk.Label(xalign=0, label="This site offers more than one application:")
            outer.append(pick)
            self.combo = Gtk.DropDown.new_from_strings(
                [f"{c['name']}  ({c['id']})" for c in candidates])
            self.combo.connect("notify::selected", self.on_pick)
            outer.append(self.combo)

        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        outer.append(self.progress)

        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll.set_min_content_height(140)
        self.log_scroll.set_child(self.log_view)
        self.log_scroll.set_visible(False)
        outer.append(self.log_scroll)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.connect("clicked", self.on_install)
        buttons.append(self.cancel_btn); buttons.append(self.install_btn)
        outer.append(buttons)
        self.set_default_widget(self.install_btn)
        self.install_btn.grab_focus()
        # Closing mid-install has to stop the install. Without this the
        # subprocess outlives the window and keeps installing invisibly.
        self.proc = None
        self.connect("close-request", self.on_close)

        self.refresh_subtitle()

    # ---- state -----------------------------------------------------------

    def refresh_subtitle(self):
        c = self.chosen
        size = c.get("summary") or "No description provided."
        # Say who actually published it. An unverified Flathub app is usually a
        # community repackage, and "Flathub" alone reads as more official than
        # that. Catalogues built before the flag existed show neither line.
        origin = ""
        if c.get("verified") is True:
            origin = "\n✓ Published by the developer (verified on Flathub)"
        elif c.get("verified") is False:
            origin = ("\n⚠ Community package: not published or supported by "
                      "the developer")
        self.subtitle.set_markup(
            f"{GLib.markup_escape_text(size)}\n"
            f"<tt>{GLib.markup_escape_text(c['id'])}</tt> · "
            f"{GLib.markup_escape_text(c.get('remote', 'flathub'))} · user install"
            f"{GLib.markup_escape_text(origin)}")
        self.heading.set_markup(
            f"<span size='x-large' weight='bold'>{GLib.markup_escape_text(c['name'])}</span>")
        self.set_title(f"{c['name']} Installer")

    def on_close(self, *_a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        return False

    def on_pick(self, dropdown, _param):
        self.chosen = self.candidates[dropdown.get_selected()]
        self.refresh_subtitle()

    def append_log(self, text):
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        adj = self.log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    # ---- install ---------------------------------------------------------

    def on_install(self, _btn):
        self.install_btn.set_sensitive(False)
        if self.combo:
            self.combo.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_text("Preparing…")
        self.log_scroll.set_visible(True)
        self.set_default_size(460, 420)
        threading.Thread(target=self.run_install, daemon=True).start()

    def native_fallback(self, app_id):
        """Ask usuald to install from the distro repositories instead.

        Reached when Flathub cannot serve this machine -- no flatpak, or the
        app has no build for this architecture. The daemon re-resolves the id
        against its own catalogue and checks the distro package's declared
        homepage against this domain before touching anything.
        """
        GLib.idle_add(self.append_log,
                      "\nFlathub did not work here; asking usuald for a "
                      "native package\n")
        try:
            rc = 1
            for msg in daemon_client.request("install", id=app_id,
                                             backend="native", domain=self.domain):
                if msg.get("type") == "log":
                    GLib.idle_add(self.append_log, msg.get("line", ""))
                elif msg.get("type") == "progress":
                    GLib.idle_add(self.set_pct, msg.get("pct", 0))
                elif msg.get("type") == "done":
                    rc = 0 if msg.get("ok") else 1
                    if not msg.get("ok"):
                        GLib.idle_add(self.append_log,
                                      f"*** {msg.get('reason')}\n")
            return rc
        except daemon_client.DaemonUnavailable:
            GLib.idle_add(self.append_log,
                          "*** usuald is not running, so no native package "
                          "backend is available\n")
            return 1

    def run_install(self):
        app_id = self.chosen["id"]
        if DRY_RUN:
            GLib.idle_add(self.append_log, f"[dry run] would install {app_id}\n")
            for pct in range(0, 101, 10):
                time.sleep(0.12)
                GLib.idle_add(self.set_pct, pct)
                GLib.idle_add(self.append_log, f"[dry run] {pct}%\n")
            GLib.idle_add(self.finish, True, None)
            return
        steps = [
            ["flatpak", "--user", "remote-add", "--if-not-exists", "flathub", FLATHUB_REPO],
            ["flatpak", "--user", "install", "-y", "--noninteractive", "flathub", app_id],
        ]
        for cmd in steps:
            GLib.idle_add(self.append_log, "$ " + " ".join(cmd) + "\n")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                self.proc = proc
            except FileNotFoundError:
                if self.native_fallback(app_id) == 0:
                    GLib.idle_add(self.finish, True, None)
                else:
                    GLib.idle_add(self.finish, False,
                                  "flatpak is not installed and no native "
                                  "package was available")
                return
            for line in proc.stdout:
                GLib.idle_add(self.append_log, line)
                m = PCT.search(line)
                if m:
                    GLib.idle_add(self.set_pct, int(m.group(1)))
                else:
                    GLib.idle_add(self.progress.pulse)
            rc = proc.wait()
            if rc != 0:
                if self.native_fallback(app_id) == 0:
                    GLib.idle_add(self.finish, True, None)
                else:
                    GLib.idle_add(self.finish, False, f"{cmd[2]} exited {rc}")
                return
        GLib.idle_add(self.finish, True, None)

    def set_pct(self, pct):
        self.progress.set_fraction(min(pct, 100) / 100)
        self.progress.set_text(f"Installing… {pct}%")

    def finish(self, ok, error):
        if ok:
            self.progress.set_fraction(1.0)
            self.progress.set_text("Installed")
            self.install_btn.set_label("Open")
            self.install_btn.set_sensitive(True)
            self.install_btn.disconnect_by_func(self.on_install)
            self.install_btn.connect("clicked", self.on_open)
            self.cancel_btn.set_label("Close")
        else:
            self.progress.set_fraction(0.0)
            self.progress.set_text(f"Failed — {error}")
            self.append_log(f"\n*** {error}\n")
            self.cancel_btn.set_label("Close")

    def on_open(self, _btn):
        if DRY_RUN:
            self.close()
            return
        subprocess.Popen(["flatpak", "run", self.chosen["id"]], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.close()


def _frame(window, title):
    """Shared window chrome."""
    window.set_default_size(480, 240)
    window.set_resizable(False)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    outer.set_margin_top(22); outer.set_margin_bottom(18)
    outer.set_margin_start(22); outer.set_margin_end(22)
    window.set_child(outer)
    heading = Gtk.Label(xalign=0)
    heading.set_markup(f"<span size='x-large' weight='bold'>{GLib.markup_escape_text(title)}</span>")
    outer.append(heading)
    return outer


class CompatWindow(Gtk.ApplicationWindow):
    """No Linux build, but the Windows version runs well here."""

    def __init__(self, app, entry, plan, exe_path, reason):
        self.entry, self.plan, self.exe_path = entry, plan, exe_path
        super().__init__(application=app, title=f"{entry['name']} Setup")
        outer = _frame(self, entry["name"])

        # Say what this actually is. A user who thinks they installed a native
        # app and then hits Wine quirks will blame Usual Install.
        sub = Gtk.Label(xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.set_markup(
            f"No Linux version is available. This is the <b>Windows</b> "
            f"application, which will run through "
            f"<b>{GLib.markup_escape_text(plan['runner'].title())}</b>.\n"
            f"Compatibility rating: <b>{GLib.markup_escape_text(plan.get('grade','?'))}</b> · "
            f"{GLib.markup_escape_text(os.path.basename(exe_path))}")
        outer.append(sub)

        if plan.get("setup") == "install-flatpak":
            note = Gtk.Label(xalign=0, wrap=True,
                             label=f"{plan['package']} will be installed first "
                                   f"(user scope, no password needed).")
            note.add_css_class("dim-label")
            outer.append(note)

        for n in plan.get("notes", []):
            w = Gtk.Label(xalign=0, wrap=True, label=f"⚠  {n}")
            outer.append(w)

        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        outer.append(self.progress)

        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll.set_min_content_height(150)
        self.log_scroll.set_child(self.log_view)
        self.log_scroll.set_visible(False)
        outer.append(self.log_scroll)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        self.go_btn = Gtk.Button(label="Set up and run")
        self.go_btn.add_css_class("suggested-action")
        self.go_btn.connect("clicked", self.on_go)
        row.append(self.cancel_btn); row.append(self.go_btn)
        outer.append(row)
        self.set_default_widget(self.go_btn)
        self.go_btn.grab_focus()
        self.holder = {}
        self.connect("close-request", self.on_close)

    def on_close(self, *_a):
        proc = self.holder.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
        return False

    def append_log(self, text):
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        adj = self.log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    def on_go(self, _btn):
        self.go_btn.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_text("Setting up…")
        self.log_scroll.set_visible(True)
        self.set_default_size(480, 440)
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        gen = compat_setup.full_setup(self.entry, self.plan, self.exe_path,
                                      proc_holder=self.holder)
        rc = 0
        try:
            while True:
                line = next(gen)
                GLib.idle_add(self.append_log, line)
                GLib.idle_add(self.progress.pulse)
        except StopIteration as stop:
            rc = stop.value or 0
        except Exception as e:
            GLib.idle_add(self.append_log, f"*** {e!r}\n")
            rc = 1
        GLib.idle_add(self.done, rc)

    def done(self, rc):
        if rc == 0:
            self.progress.set_fraction(1.0)
            self.progress.set_text("Ready")
            self.go_btn.set_label("Done")
            self.go_btn.set_sensitive(True)
            self.go_btn.disconnect_by_func(self.on_go)
            self.go_btn.connect("clicked", lambda *_: self.close())
        else:
            self.progress.set_fraction(0.0)
            self.progress.set_text(f"Setup failed (exit {rc})")
        self.cancel_btn.set_label("Close")


class UnsupportedWindow(Gtk.ApplicationWindow):
    """We know what this is, and it will not run acceptably. Say so."""

    def __init__(self, app, entry, reason):
        super().__init__(application=app, title=f"{entry['name']} — not supported")
        outer = _frame(self, f"{entry['name']} is not supported")
        self.set_default_size(460, 190)

        body = Gtk.Label(xalign=0, wrap=True)
        body.set_markup(
            f"There is no Linux version, and the Windows version does not run "
            f"well enough through any compatibility layer on this system to be "
            f"worth offering.\n\n<i>{GLib.markup_escape_text(reason)}</i>")
        outer.append(body)

        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        hint.set_label("The original download has not been cancelled.")
        outer.append(hint)

        row = Gtk.Box(halign=Gtk.Align.END)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        row.append(close)
        outer.append(row)


class InstallerApp(Gtk.Application):
    def __init__(self, data):
        super().__init__(application_id="org.usualinstall.Installer",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.data = data

    def do_activate(self):
        d = self.data
        mode = d.get("mode", "native")
        if mode == "compat":
            c = d["compat"]
            CompatWindow(self, c["entry"], c["plan"], d.get("exePath", ""),
                         c.get("reason", "")).present()
        elif mode == "unsupported":
            c = d["compat"]
            UnsupportedWindow(self, c["entry"], c.get("reason", "")).present()
        else:
            InstallerWindow(self, d["candidates"], d.get("pageUrl", ""),
                            d.get("domain", "")).present()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True, help="JSON from usual-session")
    args = ap.parse_args()

    data = json.loads(args.payload)
    mode = data.get("mode", "native")
    if mode == "native" and not data.get("candidates"):
        sys.stderr.write("no candidates\n")
        return 2
    if mode in ("compat", "unsupported") and not data.get("compat"):
        sys.stderr.write("no compat payload\n")
        return 2
    return InstallerApp(data).run([])


if __name__ == "__main__":
    sys.exit(main())
