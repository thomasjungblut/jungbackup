"""Ask for an existing backup's password."""

from gi.repository import Adw, Gtk

from ..storage import for_repository


def _boxed(*rows):
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    return box


class PasswordDialog(Adw.Dialog):
    """Ask for an existing repository's password and verify it with restic."""

    def __init__(self, restic, repo, on_unlocked, body=None):
        super().__init__(title="Unlock Backup", content_width=420)
        self._restic, self._repo, self._on_unlocked = restic, repo, on_unlocked

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24, margin_start=24, margin_end=24)
        view.set_content(box)
        intro = Gtk.Label(wrap=True, xalign=0, label=body or
                          "Enter the password you chose when setting up this backup. "
                          "It is saved in your keyring so you only need to do this once.")
        box.append(intro)
        box.append(Gtk.Label(label=for_repository(repo).label, xalign=0, wrap=True, wrap_mode=2,
                             css_classes=["dim-label", "caption"]))

        self._entry = Adw.PasswordEntryRow(title="Backup Password")
        self._entry.connect("entry-activated", self._unlock)
        box.append(_boxed(self._entry))

        self._error = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["error"])
        box.append(self._error)

        self._button = Gtk.Button(label="Unlock", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        self._button.connect("clicked", self._unlock)
        box.append(self._button)
        self.set_focus(self._entry)

    def _unlock(self, *_):
        password = self._entry.get_text()   # empty for unencrypted backups
        self._button.set_sensitive(False)
        self._button.set_child(Adw.Spinner())
        self._error.set_visible(False)

        def verified(data, error):
            self._button.set_sensitive(True)
            self._button.set_child(None)
            self._button.set_label("Unlock")
            if error is not None:
                self._error.set_label("Wrong password." if error.wrong_password
                                      else for_repository(self._repo).explain(error))
                self._error.set_visible(True)
                self._entry.grab_focus()
                return
            self.close()
            self._on_unlocked(password, data)

        self._restic.snapshots(self._repo, password, verified)
