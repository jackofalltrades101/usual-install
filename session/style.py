"""Shared look for every Usual Install window: Plasma's structure, Cinnamon's warmth.

From KDE Plasma (Breeze): the neutral window greys, Breeze blue for focus,
progress and links, forms whose labels sit right-aligned against their values,
and the inline message bar -- a tinted strip with a coloured edge -- which is
where these windows say who made what you are about to install.

From Cinnamon (Mint-Y): the friendly green primary button, slightly rounder
controls, and a dialog footer that holds the actions apart from the content.

Follows the desktop's light or dark preference. Nothing here changes behaviour.
"""
import os

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

LIGHT = {
    "window": "#eff0f1", "view": "#fcfcfc", "text": "#232629", "muted": "#5f6368",
    "border": "#cdd1d5", "footer": "#e3e5e7",
    "focus": "#3daee9", "go": "#2f8a46", "go_hover": "#287a3d",
    "info": "#3daee9", "positive": "#27ae60", "warning": "#f67400", "negative": "#da4453",
    "tint": "0.10",
}
DARK = {
    "window": "#202326", "view": "#17191b", "text": "#fcfcfc", "muted": "#a8adb3",
    "border": "#3a3f44", "footer": "#1a1c1e",
    "focus": "#3daee9", "go": "#2f8a46", "go_hover": "#36a052",
    "info": "#3daee9", "positive": "#27ae60", "warning": "#f67400", "negative": "#da4453",
    "tint": "0.16",
}

CSS = """
window.usual {{ background-color: {window}; color: {text}; }}
window.usual .header {{
  background-color: {view};
  border-bottom: 1px solid {border};
  padding: 18px 22px 16px 22px;
}}
window.usual .title {{ font-size: 1.45em; font-weight: 700; }}
window.usual .tile {{ background-color: {focus}; color: #ffffff; border-radius: 10px;
  font-size: 1.6em; font-weight: 700; min-width: 48px; min-height: 48px; }}
window.usual .summary {{ color: {muted}; }}
window.usual .body {{ padding: 16px 22px 18px 22px; }}
window.usual .form-key {{ color: {muted}; }}
window.usual .form-value {{ color: {text}; }}
window.usual .mono {{ font-family: monospace; font-size: 0.92em; }}

/* Plasma's inline message: tinted strip, coloured leading edge. */
window.usual .message {{
  border-radius: 6px;
  border-left: 4px solid {info};
  background-color: alpha({info}, {tint});
  padding: 9px 12px;
}}
window.usual .message.positive {{ border-left-color: {positive};
  background-color: alpha({positive}, {tint}); }}
window.usual .message.warning {{ border-left-color: {warning};
  background-color: alpha({warning}, {tint}); }}
window.usual .message.negative {{ border-left-color: {negative};
  background-color: alpha({negative}, {tint}); }}
window.usual .message .glyph {{ font-weight: 700; }}
window.usual .message.positive .glyph {{ color: {positive}; }}
window.usual .message.warning .glyph {{ color: {warning}; }}
window.usual .message.negative .glyph {{ color: {negative}; }}
window.usual .message .linkish {{ color: {focus}; padding: 0; min-height: 0;
  background: none; box-shadow: none; border: none; }}
window.usual .message .linkish:hover {{ text-decoration: underline; }}

/* Cinnamon's footer: actions held apart from the content. */
window.usual .footer {{
  background-color: {footer};
  border-top: 1px solid {border};
  padding: 12px 22px;
}}
window.usual button {{ border-radius: 5px; min-height: 30px; padding: 2px 16px; }}
window.usual button.suggested-action {{
  background-image: none; background-color: {go}; color: #ffffff;
  border: 1px solid shade({go}, 0.85); font-weight: 600;
}}
window.usual button.suggested-action:hover {{ background-color: {go_hover}; }}
window.usual button.suggested-action:disabled {{ background-color: alpha({go}, 0.45);
  color: alpha(#ffffff, 0.8); }}
window.usual button:focus-visible, window.usual dropdown:focus-within {{
  outline: 2px solid {focus}; outline-offset: 1px; }}

/* Breeze progress: a slim blue bar. */
window.usual progressbar trough {{ min-height: 6px; border-radius: 3px;
  background-color: alpha({text}, 0.10); }}
window.usual progressbar progress {{ min-height: 6px; border-radius: 3px;
  background-color: {focus}; }}
window.usual progressbar text {{ color: {muted}; font-size: 0.92em; }}

window.usual .log {{ background-color: {view}; border: 1px solid {border};
  border-radius: 6px; padding: 8px; }}
window.usual .log text {{ background-color: {view}; color: {text}; }}

/* Settings: Plasma-style groups. */
window.usual .group-title {{ font-weight: 700; margin-top: 14px; }}
window.usual .group {{ background-color: {view}; border: 1px solid {border};
  border-radius: 6px; padding: 12px 14px; }}
"""

_applied = False


def prefers_dark():
    env = os.environ.get("GTK_THEME", "").lower()
    if env.endswith(":dark") or "dark" in env:
        return True
    s = Gtk.Settings.get_default()
    if s is None:
        return False
    if s.get_property("gtk-application-prefer-dark-theme"):
        return True
    name = (s.get_property("gtk-theme-name") or "").lower()
    return "dark" in name


def apply():
    """Install the stylesheet once per process."""
    global _applied
    if _applied:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS.format(**(DARK if prefers_dark() else LIGHT))) \
        if hasattr(provider, "load_from_string") \
        else provider.load_from_data(CSS.format(**(DARK if prefers_dark() else LIGHT)).encode())
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _applied = True


# ---- building blocks --------------------------------------------------------

def shell(window, width=500):
    """Header, body and footer boxes inside a styled window. Returns them."""
    apply()
    window.add_css_class("usual")
    window.set_icon_name("usual-install")
    window.set_default_size(width, -1)
    window.set_resizable(False)
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    header.add_css_class("header")
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, vexpand=True)
    body.add_css_class("body")
    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    footer.add_css_class("footer")
    root.append(header); root.append(body); root.append(footer)
    window.set_child(root)
    return header, body, footer


class Header:
    """Icon, product name, one line of what it is."""

    def __init__(self, box, icon_name="system-software-install"):
        # The desktop's own icon when its theme has one; otherwise a tile with
        # the product's initial, rather than GTK's broken-image placeholder.
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        self.tile = None
        if theme.has_icon(icon_name):
            self.icon = Gtk.Image.new_from_icon_name(icon_name)
            self.icon.set_pixel_size(48)
        else:
            self.tile = Gtk.Label()
            self.tile.add_css_class("tile")
            self.icon = self.tile
        self.icon.set_valign(Gtk.Align.START)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        text.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("title")
        self.summary = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        self.summary.add_css_class("summary")
        text.append(self.title); text.append(self.summary)
        box.append(self.icon); box.append(text)

    def set(self, title, summary):
        if self.tile is not None:
            self.tile.set_label((title or "?")[:1].upper())
        self.title.set_label(title)
        self.summary.set_label(summary or "")
        self.summary.set_visible(bool(summary))


class Form:
    """Plasma form layout: muted keys right-aligned against their values."""

    def __init__(self):
        self.grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.rows = 0

    def clear(self):
        child = self.grid.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        self.rows = 0

    def add(self, key, value, mono=False):
        k = Gtk.Label(label=key, xalign=1, valign=Gtk.Align.START)
        k.add_css_class("form-key")
        if isinstance(value, Gtk.Widget):
            v = value
        else:
            v = Gtk.Label(label=str(value), xalign=0, wrap=True, selectable=True,
                          max_width_chars=40)
            v.add_css_class("form-value")
            if mono:
                v.add_css_class("mono")
        v.set_hexpand(True)
        self.grid.attach(k, 0, self.rows, 1, 1)
        self.grid.attach(v, 1, self.rows, 1, 1)
        self.rows += 1
        return v


class Message:
    """Plasma's inline message bar. kind: info | positive | warning | negative."""

    GLYPHS = {"info": "i", "positive": "✓", "warning": "!", "negative": "✕"}

    def __init__(self):
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.add_css_class("message")
        self.glyph = Gtk.Label(valign=Gtk.Align.START)
        self.glyph.add_css_class("glyph")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.text = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        self.link = Gtk.LinkButton(halign=Gtk.Align.START)
        self.link.add_css_class("linkish")
        self.link.set_visible(False)
        col.append(self.text); col.append(self.link)
        self.box.append(self.glyph); self.box.append(col)
        self.kind = None

    def show(self, kind, text, link_label=None, uri=None):
        if self.kind:
            self.box.remove_css_class(self.kind)
        self.kind = kind
        self.box.add_css_class(kind)
        self.glyph.set_label(self.GLYPHS[kind])
        self.text.set_label(text)
        if link_label and uri:
            self.link.set_label(link_label)
            self.link.set_uri(uri)
            self.link.set_visible(True)
        else:
            self.link.set_visible(False)
        self.box.set_visible(True)

    def hide(self):
        self.box.set_visible(False)


def log_view(min_height=140):
    """A collapsed 'Show details' log, the way Windows installers hide theirs."""
    view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(min_height)
    scroll.add_css_class("log")
    scroll.set_child(view)
    expander = Gtk.Expander(label="Show details")
    expander.set_child(scroll)
    expander.set_visible(False)
    return expander, scroll, view


def spacer():
    s = Gtk.Box(); s.set_hexpand(True)
    return s


def escape(text):
    return GLib.markup_escape_text(text or "")
