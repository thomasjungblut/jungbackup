import locale
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .ui.window import Window  # noqa: E402

APP_ID = "io.jungblut.JungBackup"
VERSION = "0.1.0"


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background_held = False
        self.add_main_option("background", 0, 0, 0, "Start without a window (for scheduled backups)", None)
        self._start_hidden = False
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_handle_local_options(self, options):
        self._start_hidden = options.contains("background")
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(os.path.dirname(__file__), "ui", "style.css"))
        # Running from a source checkout: pick up the app icon from data/.
        icons = os.path.join(os.path.dirname(__file__), "..", "..", "data", "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(os.path.abspath(icons))
        Gtk.Window.set_default_icon_name(APP_ID)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.get_active_window() or next(iter(self.get_windows()), None)
        if win is None:
            win = Window(self)
            if self._start_hidden:
                self._start_hidden = False
                self.background_held = True
                self.hold()
                return
        win.present()

    def _about(self, *_):
        Adw.AboutDialog(application_name="Jung Backup", application_icon=APP_ID,
                        version=VERSION, developer_name="Thomas Jungblut",
                        comments="A backup dashboard compatible with Déjà Dup",
                        license_type=Gtk.License.APACHE_2_0, website="https://github.com/thomasjungblut/jungbackup").present(self.get_active_window())


def request_autostart(window, enabled):
    """Ask the Background portal to start us at login (Flatpak only)."""
    if not os.path.exists("/.flatpak-info"):
        return
    try:
        gi.require_version("Xdp", "1.0")
        gi.require_version("XdpGtk4", "1.0")
        from gi.repository import Xdp, XdpGtk4
    except (ImportError, ValueError):
        return
    portal = Xdp.Portal.new()
    parent = XdpGtk4.parent_new_gtk(window) if window is not None else None
    flags = Xdp.BackgroundFlags.AUTOSTART if enabled else Xdp.BackgroundFlags.NONE

    def done(portal, result):
        try:
            portal.request_background_finish(result)
        except Exception:  # denied by the user, or no portal available
            pass

    portal.request_background(parent, "Run scheduled backups", ["jungbackup", "--background"], flags, None, done)


def main():
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    return Application().run(sys.argv)
