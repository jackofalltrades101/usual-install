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
sys.path.insert(0, str(_ROOT / "session"))
import aurbuild  # noqa: E402
import style  # noqa: E402

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
# Containers and CI have no usable flatpak. USUAL_DRY_RUN exercises the whole
# window -- picker, progress, log, completion -- without touching the system.
DRY_RUN = os.environ.get("USUAL_DRY_RUN") == "1"
PCT = re.compile(r"(\d{1,3})\s*%")


def source_label(c):
    """Where a candidate comes from, as the picker shows it."""
    if c.get("backend") == "aur":
        return "AUR, built on this computer"
    if c.get("verified"):
        return "Flathub, from the developer"
    if c.get("verified") is False:
        return "Flathub, community package"
    return c.get("id", "")


class InstallerWindow(Gtk.ApplicationWindow):
    def __init__(self, app, candidates, page_url, domain=""):
        self.candidates = candidates
        self.page_url = page_url
        self.domain = domain
        self.chosen = candidates[0]
        super().__init__(application=app, title=f"{self.chosen['name']} Installer")
        header_box, body, footer = style.shell(self)

        self.header = style.Header(header_box)

        # Picker when the site maps to more than one product, or one product
        # available from more than one source (Flathub and the AUR, say).
        self.combo = None
        self.form = style.Form()
        body.append(self.form.grid)

        # Who made what you are about to install: the one thing this window
        # must never leave unsaid.
        self.message = style.Message()
        body.append(self.message.box)

        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        body.append(self.progress)

        self.details, self.log_scroll, self.log_view = style.log_view()
        body.append(self.details)

        footer.append(style.spacer())
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.connect("clicked", self.on_install)
        footer.append(self.cancel_btn); footer.append(self.install_btn)
        self.set_default_widget(self.install_btn)
        self.install_btn.grab_focus()
        # Closing mid-install has to stop the install. Without this the
        # subprocess outlives the window and keeps installing invisibly.
        self.proc = None
        self.connect("close-request", self.on_close)

        if len(candidates) > 1:
            self.combo = Gtk.DropDown.new_from_strings(
                [f"{c['name']} ({source_label(c)})" for c in candidates])
            self.combo.connect("notify::selected", self.on_pick)
        self.refresh_subtitle()

    # ---- state -----------------------------------------------------------

    def refresh_subtitle(self):
        c = self.chosen
        is_aur = c.get("backend") == "aur"
        self.header.set(c["name"], c.get("summary") or "")
        self.set_title(f"{c['name']} Installer")
        self.install_btn.set_label("Build and install" if is_aur else "Install")

        self.form.clear()
        if self.combo:
            same_app = len({x.get("name") for x in self.candidates}) == 1
            self.form.add("Install from" if same_app else "Application", self.combo)
        else:
            self.form.add("Source", source_label(c))
        if is_aur:
            self.form.add("Package", c.get("aur_name", ""), mono=True)
            self.form.add("Maintainer", f"{c.get('maintainer')}, {c.get('votes')} votes")
            self.message.show(
                "warning",
                "This is a community build script from the AUR. Like a Windows "
                "installer, it runs code on your computer, and you will be asked "
                "for your password before anything is installed.",
                "Read the build script",
                "https://aur.archlinux.org/cgit/aur.git/tree/PKGBUILD?h="
                + c.get("pkgbase", ""))
        else:
            self.form.add("Package", c["id"], mono=True)
            self.form.add("Installs for", "You only, no password needed")
            if c.get("verified") is True:
                self.message.show("positive", "Published by the developer and "
                                              "verified on Flathub.")
            elif c.get("verified") is False:
                self.message.show("warning", "Community package: not published or "
                                             "supported by the developer.")
            else:
                self.message.hide()

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
        self.cancel_btn.set_label("Stop")
        self.progress.set_visible(True)
        self.progress.set_text("Preparing…")
        self.details.set_visible(True)
        threading.Thread(target=self.run_install, daemon=True).start()

    def unavailable_for_this_arch(self, app_id):
        """True when Flathub simply has no build of this app for this machine.

        Worth checking before blaming anything else: Flathub's application set
        differs per architecture, and Zoom, Slack and Spotify have no aarch64
        build at all. Without this the user is told the package "is not in the
        apt repositories", which is true and completely beside the point.
        """
        try:
            r = subprocess.run(["flatpak", "--user", "remote-info", "flathub", app_id],
                               capture_output=True, timeout=30)
            return r.returncode != 0
        except (OSError, subprocess.SubprocessError):
            return False

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
        if self.unavailable_for_this_arch(app_id):
            GLib.idle_add(
                self.append_log,
                f"note: Flathub has no {os.uname().machine} build of {app_id}. "
                f"This application is not published for this architecture.\n")
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
        if self.chosen.get("backend") == "aur" and not DRY_RUN:
            self.run_aur()
            return
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

    def run_aur(self):
        holder = []
        self.proc_holder = holder

        def status(text):
            GLib.idle_add(self.progress.set_text, text)
            GLib.idle_add(self.progress.pulse)

        def log(text):
            GLib.idle_add(self.append_log, text)
            GLib.idle_add(self.progress.pulse)
            if holder:
                self.proc = holder[-1]

        try:
            ok, why = aurbuild.build_and_install(self.chosen, log, status, holder)
        except Exception as e:
            ok, why = False, f"unexpected error: {e}"
        GLib.idle_add(self.finish, ok, None if ok else why)

    def set_pct(self, pct):
        self.progress.set_fraction(min(pct, 100) / 100)
        self.progress.set_text(f"Installing… {pct}%")

    def finish(self, ok, error):
        name = self.chosen["name"]
        self.progress.set_visible(False)
        if ok:
            self.message.show("positive", f"{name} is installed. You can open it "
                                          f"now or find it in your applications menu.")
            self.install_btn.set_label(f"Open {name}")
            self.install_btn.set_sensitive(True)
            self.install_btn.disconnect_by_func(self.on_install)
            self.install_btn.connect("clicked", self.on_open)
            self.cancel_btn.set_label("Close")
        else:
            self.message.show("negative", f"{name} was not installed: {error}. "
                                          f"The details below show what happened.")
            self.append_log(f"\n*** {error}\n")
            self.details.set_expanded(True)
            self.cancel_btn.set_label("Close")
            self.install_btn.set_label("Try again")
            self.install_btn.set_sensitive(True)
            if self.combo:
                self.combo.set_sensitive(True)

    def on_open(self, _btn):
        if DRY_RUN:
            self.close()
            return
        if self.chosen.get("backend") == "aur":
            desktop = aurbuild.desktop_entry(self.chosen["aur_name"])
            cmd = ["gtk-launch", desktop] if desktop else None
        else:
            cmd = ["flatpak", "run", self.chosen["id"]]
        if cmd:
            subprocess.Popen(cmd, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.close()


class CompatWindow(Gtk.ApplicationWindow):
    """No Linux build, but the Windows version runs well here."""

    def __init__(self, app, entry, plan, exe_path, reason):
        self.entry, self.plan, self.exe_path = entry, plan, exe_path
        super().__init__(application=app, title=f"{entry['name']} Setup")
        header_box, body, footer = style.shell(self)
        header = style.Header(header_box, icon_name="application-x-executable")
        header.set(entry["name"], "Windows application")

        # Say what this actually is. A user who thinks they installed a native
        # app and then hits Wine quirks will blame Usual Install.
        runner = plan["runner"].title()
        self.message = style.Message()
        self.message.show(
            "info", f"There is no Linux version. This is the Windows application, "
                    f"which will run through {runner}.")
        body.append(self.message.box)

        form = style.Form()
        form.add("Runs with", runner)
        form.add("Compatibility", f"{plan.get('grade', 'unknown').title()} rating")
        form.add("Installer", os.path.basename(exe_path), mono=True)
        if plan.get("setup") == "install-flatpak":
            form.add("Also installs", f"{plan['package']}, for you only, no password needed")
        body.append(form.grid)

        for n in plan.get("notes", []):
            m = style.Message(); m.show("warning", n)
            body.append(m.box)

        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        body.append(self.progress)
        self.details, self.log_scroll, self.log_view = style.log_view(150)
        body.append(self.details)

        footer.append(style.spacer())
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        self.go_btn = Gtk.Button(label="Set up and run")
        self.go_btn.add_css_class("suggested-action")
        self.go_btn.connect("clicked", self.on_go)
        footer.append(self.cancel_btn); footer.append(self.go_btn)
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
        self.cancel_btn.set_label("Stop")
        self.progress.set_visible(True)
        self.progress.set_text("Setting up…")
        self.details.set_visible(True)
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
        self.progress.set_visible(False)
        name = self.entry["name"]
        if rc == 0:
            self.message.show("positive", f"{name} is set up and running.")
            self.go_btn.set_label("Done")
            self.go_btn.set_sensitive(True)
            self.go_btn.disconnect_by_func(self.on_go)
            self.go_btn.connect("clicked", lambda *_: self.close())
        else:
            self.message.show("negative", f"Setting up {name} failed (exit code {rc}). "
                                          f"The details below show what happened.")
            self.details.set_expanded(True)
        self.cancel_btn.set_label("Close")


class UnsupportedWindow(Gtk.ApplicationWindow):
    """We know what this is, and it will not run acceptably. Say so."""

    def __init__(self, app, entry, reason):
        super().__init__(application=app, title=f"{entry['name']}: not supported")
        header_box, body, footer = style.shell(self, width=470)
        header = style.Header(header_box, icon_name="dialog-information")
        header.set(entry["name"], "Windows application")

        m = style.Message()
        m.show("negative", f"{entry['name']} has no Linux version, and the Windows "
                           f"version does not run well enough on this computer to "
                           f"offer it.")
        body.append(m.box)
        why = Gtk.Label(xalign=0, wrap=True, max_width_chars=56, label=reason)
        why.add_css_class("summary")
        body.append(why)
        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Your download has been left as it is.")
        body.append(hint)

        footer.append(style.spacer())
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        footer.append(close)
        self.set_default_widget(close)


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
