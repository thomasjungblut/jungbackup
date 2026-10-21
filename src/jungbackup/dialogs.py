"""Password prompt and the first-run / change-location setup flow."""

import os

from gi.repository import Adw, Gio, GLib, Gtk

from .dejadup import describe_path
from .restic import is_empty_or_missing, is_repository
from .settings import Settings


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
        box.append(Gtk.Label(label=repo, xalign=0, wrap=True, wrap_mode=2, css_classes=["dim-label", "caption"]))

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
        password = self._entry.get_text()
        if not password:
            return
        self._button.set_sensitive(False)
        self._button.set_child(Adw.Spinner())
        self._error.set_visible(False)

        def verified(data, error):
            self._button.set_sensitive(True)
            self._button.set_child(None)
            self._button.set_label("Unlock")
            if error is not None:
                self._error.set_label("Wrong password." if error.wrong_password else str(error))
                self._error.set_visible(True)
                self._entry.grab_focus()
                return
            self.close()
            self._on_unlocked(password, data)

        self._restic.snapshots(self._repo, password, verified)


class SetupDialog(Adw.Dialog):
    """Choose a backup folder and password, creating the repository if needed.

    on_done(settings, password) is called once the repository exists and the
    password has been verified.
    """

    def __init__(self, restic, settings, on_done, title="Set Up Backups", notes=()):
        super().__init__(title=title, content_width=480, content_height=560)
        self._restic = restic
        self._settings = Settings(**vars(settings))
        self._on_done = on_done

        self._nav = Adw.NavigationView()
        self.set_child(self._nav)
        self._nav.add(self._location_page(notes))
        self._password_page = None
        self._update_location()

    # Page 1: where ---------------------------------------------------------

    def _location_page(self, notes):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)

        intro = Adw.PreferencesGroup(
            description="Choose where to keep your backups — ideally a different drive or a network share. "
                        "Pick an empty folder to start fresh, or an existing Déjà Dup/restic backup folder.")
        page.add(intro)

        for note in notes:
            banner = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=GLib.markup_escape_text(note), title_lines=0)
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            banner.add(row)
            page.add(banner)

        group = Adw.PreferencesGroup(title="Backup Location")
        page.add(group)
        self._folder_row = Adw.ActionRow(title="Folder", subtitle_selectable=True)
        self._folder_row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose)
        self._folder_row.add_suffix(choose)
        group.add(self._folder_row)

        self._state_row = Adw.ActionRow(title_lines=0)
        self._state_icon = Gtk.Image()
        self._state_row.add_prefix(self._state_icon)
        group.add(self._state_row)

        what = Adw.PreferencesGroup(title="What Gets Backed Up",
                                    description="Change folders and the schedule any time with the edit button.")
        page.add(what)
        what.add(Adw.ActionRow(title="Folders", subtitle=GLib.markup_escape_text(
            ", ".join(describe_path(p) for p in self._settings.includes) or "None")))
        what.add(Adw.ActionRow(title="Except", subtitle=GLib.markup_escape_text(
            ", ".join(describe_path(p) for p in self._settings.excludes) or "Nothing")))

        bottom = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=18)
        self._next = Gtk.Button(label="Next", css_classes=["pill", "suggested-action"])
        self._next.connect("clicked", self._go_password)
        bottom.append(self._next)
        view.add_bottom_bar(bottom)
        return Adw.NavigationPage(title=self.get_title(), child=view, tag="location")

    def _choose(self, _btn):
        dialog = Gtk.FileDialog(title="Choose Backup Folder", modal=True, accept_label="Select")
        parent = self._settings.repository and os.path.dirname(self._settings.repository)
        if parent and os.path.isdir(parent):
            dialog.set_initial_folder(Gio.File.new_for_path(parent))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            # Like Déjà Dup, use a per-machine subfolder when the chosen folder is in use.
            if not is_repository(path) and not is_empty_or_missing(path):
                path = os.path.join(path, GLib.get_host_name())
            self._settings.repository = path
            self._update_location()

        dialog.select_folder(self.get_root(), None, done)

    def _repo_state(self):
        path = self._settings.repository
        if not path:
            return "none"
        if is_repository(path):
            return "existing"
        if is_empty_or_missing(path):
            parent = os.path.dirname(path.rstrip("/"))
            return "new" if os.path.isdir(path) or os.path.isdir(parent) else "unreachable"
        return "occupied"

    def _update_location(self):
        state = self._repo_state()
        self._folder_row.set_subtitle(GLib.markup_escape_text(self._settings.repository or "Not chosen yet"))
        text, icon = {
            "none": ("Choose a folder to continue.", "folder-symbolic"),
            "existing": ("An existing backup was found here. You'll need its password.", "object-select-symbolic"),
            "new": ("A new, empty backup will be created here.", "list-add-symbolic"),
            "occupied": ("This folder contains other files. Choose an empty folder.", "dialog-warning-symbolic"),
            "unreachable": ("This folder isn't reachable. Is the drive or share mounted?", "dialog-warning-symbolic"),
        }[state]
        self._state_row.set_title(text)
        self._state_icon.set_from_icon_name(icon)
        self._next.set_sensitive(state in ("existing", "new"))

    # Page 2: password ------------------------------------------------------

    def _go_password(self, _btn):
        existing = self._repo_state() == "existing"
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)

        group = Adw.PreferencesGroup(
            description=("Enter the password of the existing backup." if existing else
                         "Your backup will be encrypted with this password. Store it somewhere safe: "
                         "without it, your files can't be restored — for example after losing this computer."))
        page.add(group)

        self._encrypt = Adw.SwitchRow(title="Encrypt Backup", active=True, visible=not existing)
        group.add(self._encrypt)
        self._pw = Adw.PasswordEntryRow(title="Password")
        self._pw2 = Adw.PasswordEntryRow(title="Confirm Password", visible=not existing)
        group.add(self._pw)
        group.add(self._pw2)
        self._encrypt.bind_property("active", self._pw, "sensitive", 2)
        self._encrypt.bind_property("active", self._pw2, "sensitive", 2)

        self._error = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["error"], margin_top=12)
        group.add(self._error)

        bottom = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=18)
        self._finish_btn = Gtk.Button(label="Unlock" if existing else "Create Backup",
                                      css_classes=["pill", "suggested-action"])
        self._finish_btn.connect("clicked", self._finish, existing)
        self._pw.connect("entry-activated", lambda *_: self._finish(None, existing))
        self._pw2.connect("entry-activated", lambda *_: self._finish(None, existing))
        bottom.append(self._finish_btn)
        view.add_bottom_bar(bottom)

        self._nav.push(Adw.NavigationPage(title="Password", child=view, tag="password"))
        self.set_focus(self._pw)

    def _show_error(self, text):
        self._error.set_label(text)
        self._error.set_visible(True)

    def _busy(self, busy, label):
        self._finish_btn.set_sensitive(not busy)
        if busy:
            self._finish_btn.set_child(Adw.Spinner())
        else:
            self._finish_btn.set_child(None)
            self._finish_btn.set_label(label)

    def _finish(self, _btn, existing):
        repo = self._settings.repository
        label = "Unlock" if existing else "Create Backup"
        if existing:
            password = self._pw.get_text()   # empty for unencrypted backups
            self._busy(True, label)

            def verified(_data, error):
                self._busy(False, label)
                if error is not None:
                    self._show_error("Wrong password." if error.wrong_password else str(error))
                    return
                self._settings.encrypted = bool(password)
                self._done(password)
            self._restic.snapshots(repo, password, verified)
            return

        encrypt = self._encrypt.get_active()
        password = self._pw.get_text() if encrypt else ""
        if encrypt:
            if len(password) < 8:
                self._show_error("Use at least 8 characters.")
                return
            if password != self._pw2.get_text():
                self._show_error("The passwords don't match.")
                return
        self._busy(True, label)
        try:
            os.makedirs(repo, exist_ok=True)
        except OSError as e:
            self._busy(False, label)
            self._show_error(f"Could not create the folder: {e.strerror}")
            return

        def created(_data, error):
            self._busy(False, label)
            if error is not None:
                self._show_error(f"Could not create the backup: {error}")
                return
            self._settings.encrypted = encrypt
            self._settings.last_backup = ""
            self._done(password)
        self._restic.init(repo, password, created)

    def _done(self, password):
        self.close()
        self._on_done(self._settings, password)
