"""First-start page: import from Déjà Dup or set up a new backup."""

from gi.repository import Adw, Gtk

from .widgets import label


class WelcomePage(Adw.Bin):
    # Adw.StatusPage is a final type, so wrap one instead of subclassing it.
    def __init__(self, on_import, on_new):
        super().__init__()
        page = Adw.StatusPage(icon_name="io.jungblut.JungBackup", title="Back Up Your Files",
                              description="Keep copies of your files safe on another drive or a network share, "
                                          "and see at a glance how your backups are doing.")
        self.set_child(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self._import_btn = Gtk.Button(css_classes=["pill", "suggested-action"])
        self._import_btn.set_child(Adw.ButtonContent(icon_name="document-revert-symbolic",
                                                     label="Import from Déjà Dup"))
        self._import_btn.connect("clicked", lambda *_: on_import())
        self._import_hint = label("", ("caption", "dim-label"), xalign=0.5, wrap=True, max_width_chars=50,
                                  justify=Gtk.Justification.CENTER)
        new_btn = Gtk.Button(label="Set Up New Backup", css_classes=["pill"])
        new_btn.connect("clicked", lambda *_: on_new())
        box.append(self._import_btn)
        box.append(self._import_hint)
        box.append(new_btn)
        page.set_child(box)

    def offer_import(self, where):
        """Show the import button for Déjà Dup backups found in `where`, or hide it for None."""
        self._import_btn.set_visible(where is not None)
        self._import_hint.set_label(f"Found Déjà Dup backups in {where}" if where is not None else "")
