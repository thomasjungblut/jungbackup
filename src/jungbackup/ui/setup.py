"""The first-run / change-location setup flow."""

import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..core import google
from ..core.paths import describe_path
from ..core.settings import Settings
from ..storage import BACKENDS, EXISTING, NEW, OCCUPIED, UNREACHABLE, LocalStorage, for_repository
from ..storage.local import is_empty_or_missing, is_repository

PROBE_DELAY_MS = 700   # wait for typing to settle before connecting to a server


class SetupDialog(Adw.Dialog):
    """Choose a backup location and password, creating the repository if needed.

    on_done(settings, password) is called once the repository exists and the
    password has been verified.
    """

    def __init__(self, restic, settings, on_done, title="Set Up Backups", notes=()):
        super().__init__(title=title, content_width=480, content_height=600)
        self._restic = restic
        self._settings = Settings(**vars(settings))
        self._on_done = on_done
        self._state = None
        self._probe_source = 0
        self._probe_cancellable = None
        self._sign_in = None
        self.connect("closed", lambda *_: self._closed())

        self._nav = Adw.NavigationView()
        self.set_child(self._nav)
        self._nav.add(self._location_page(notes))
        self._password_page = None
        self._backend_changed()

    # Page 1: where ---------------------------------------------------------

    def _location_page(self, notes):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)

        intro = Adw.PreferencesGroup(
            description="Choose where to keep your backups — ideally a different drive, a network share "
                        "or a server. Pick an empty folder to start fresh, or an existing Déjà Dup/restic backup.")
        page.add(intro)

        for note in notes:
            banner = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=GLib.markup_escape_text(note), title_lines=0)
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            banner.add(row)
            page.add(banner)

        group = Adw.PreferencesGroup(title="Backup Location")
        page.add(group)
        current = for_repository(self._settings.repository) if self._settings.repository else LocalStorage("")
        self._backend = Adw.ComboRow(title="Store Backups On", model=Gtk.StringList.new([b.name for b in BACKENDS]),
                                     selected=BACKENDS.index(type(current)))
        self._backend.connect("notify::selected", lambda *_: self._backend_changed())
        group.add(self._backend)

        self._folder_row = Adw.ActionRow(title="Folder", subtitle_selectable=True)
        self._folder_row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose)
        self._folder_row.add_suffix(choose)
        group.add(self._folder_row)
        self._local_repo = current.repo if isinstance(current, LocalStorage) else ""

        # The cloud account to store backups with, for backends that need one.
        self._account_row = Adw.ActionRow(title_lines=0)
        self._account_row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        self._account_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._account_button.connect("clicked", self._account_clicked)
        self._account_row.add_suffix(self._account_button)
        group.add(self._account_row)

        # Entry rows for every remote backend, filled from the current location when it's that kind.
        self._field_rows = {}
        for backend in BACKENDS:
            values = current.fields() if type(current) is backend else backend.default_fields()
            rows = {}
            for key, title, hint in backend.FIELDS:
                row = Adw.EntryRow(title=title, text=values.get(key, ""), tooltip_text=hint)
                row.connect("changed", lambda *_: self._fields_changed())
                group.add(row)
                rows[key] = row
            self._field_rows[backend] = rows
        self._field_hint = Adw.ActionRow(title_lines=0, css_classes=["dim-label"])
        group.add(self._field_hint)

        self._state_row = Adw.ActionRow(title_lines=0)
        self._state_icon = Gtk.Image()
        self._state_spinner = Adw.Spinner(visible=False)
        self._state_row.add_prefix(self._state_icon)
        self._state_row.add_prefix(self._state_spinner)
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

    @property
    def _selected_backend(self):
        return BACKENDS[self._backend.get_selected()]

    def _backend_changed(self):
        self._field_hint.set_title(GLib.markup_escape_text(self._selected_backend.HINT))
        self._update_account()
        self._fields_changed()

    def _show_rows(self):
        """Show the rows the chosen backend uses; until its sign-in is done, only that."""
        backend = self._selected_backend
        ready = not backend.ACCOUNT or google.account() is not None
        self._folder_row.set_visible(backend is LocalStorage)
        for kind, rows in self._field_rows.items():
            for row in rows.values():
                row.set_visible(kind is backend and ready)
        self._field_hint.set_visible(bool(backend.HINT) and ready)
        # Finding an existing backup needs no comment; the next page asks for its password if it has one.
        self._state_row.set_visible(ready and self._state != EXISTING)
        self._account_row.set_visible(backend.ACCOUNT)

    # Google account --------------------------------------------------------

    def _update_account(self, waiting=False):
        acct = google.account()
        if waiting:
            title, label = "Continue in your browser. Sign in there and allow access.", "Cancel"
        elif acct is not None:
            title = f"Signed in as {acct['email']}" if acct["email"] else "Signed in to Google Drive"
            label = "Sign Out"
        else:
            title, label = "Sign in to Google to store backups in your Drive.", "Sign In…"
        self._account_row.set_title(GLib.markup_escape_text(title))
        self._account_button.set_visible(label is not None)
        self._account_button.set_label(label or "")
        self._account_button.set_css_classes([] if acct or waiting else ["suggested-action"])
        self._show_rows()

    def _account_clicked(self, _btn):
        if self._sign_in is not None:
            self._sign_in.cancel()
            self._sign_in = None
            self._update_account()
            return
        if google.account() is not None:
            google.sign_out()
            self._update_account()
            self._fields_changed()
            return

        def done(_acct, message):
            self._sign_in = None
            self._update_account()
            if message:
                self._account_row.set_title(GLib.markup_escape_text(message))
            self._fields_changed()

        def open_link(url):
            Gtk.UriLauncher.new(url).launch(self.get_root(), None, None)

        try:
            self._sign_in = google.SignIn(open_link, done)
        except OSError as e:
            self._account_row.set_title(GLib.markup_escape_text(str(e)))
            return
        self._update_account(waiting=True)

    def _closed(self):
        self._cancel_probe()
        if self._sign_in is not None:
            self._sign_in.cancel()
            self._sign_in = None

    def _fields_changed(self):
        backend = self._selected_backend
        if backend is LocalStorage:
            self._settings.repository = self._local_repo
        else:
            storage = backend.from_fields({k: r.get_text() for k, r in self._field_rows[backend].items()})
            self._settings.repository = storage.repo if storage else ""
        self._update_location()

    def _choose(self, _btn):
        dialog = Gtk.FileDialog(title="Choose Backup Folder", modal=True, accept_label="Select")
        parent = self._local_repo and os.path.dirname(self._local_repo)
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
            self._local_repo = path
            self._fields_changed()

        dialog.select_folder(self.get_root(), None, done)

    def _cancel_probe(self):
        if self._probe_source:
            GLib.source_remove(self._probe_source)
            self._probe_source = 0
        if self._probe_cancellable is not None:
            self._probe_cancellable.cancel()
            self._probe_cancellable = None

    def _update_location(self):
        self._cancel_probe()
        repo = self._settings.repository
        local = self._selected_backend is LocalStorage
        self._folder_row.set_subtitle(GLib.markup_escape_text(repo if local and repo else "Not chosen yet"))
        if not repo:
            self._show_state(None, "Choose a folder to continue." if local else "Enter the server and folder.")
            return
        storage = for_repository(repo)
        if local:
            storage.probe(self._restic, self._show_state)
            return
        self._show_state("checking", None)

        def start():
            self._probe_source = 0
            self._probe_cancellable = Gio.Cancellable()
            storage.probe(self._restic, self._show_state, self._probe_cancellable)
            return GLib.SOURCE_REMOVE
        self._probe_source = GLib.timeout_add(PROBE_DELAY_MS, start)

    def _show_state(self, state, message):
        self._probe_cancellable = None
        self._state = state
        text, icon = {
            None: (message, "folder-symbolic"),
            "checking": ("Connecting…", None),
            EXISTING: ("", "object-select-symbolic"),   # row hidden, see _show_rows
            NEW: ("A new, empty backup will be created here.", "list-add-symbolic"),
            OCCUPIED: ("This folder contains other files. Choose an empty folder.", "dialog-warning-symbolic"),
            UNREACHABLE: (message, "dialog-warning-symbolic"),
        }[state]
        self._state_row.set_title(GLib.markup_escape_text(text or ""))
        self._state_spinner.set_visible(icon is None)
        self._state_icon.set_visible(icon is not None)
        if icon:
            self._state_icon.set_from_icon_name(icon)
        self._next.set_sensitive(state in (EXISTING, NEW))
        self._show_rows()

    # Page 2: password ------------------------------------------------------

    def _go_password(self, _btn):
        existing = self._state == EXISTING
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
                    self._show_error("Wrong password." if error.wrong_password else for_repository(repo).explain(error))
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
        storage = for_repository(repo)
        problem = storage.prepare()
        if problem:
            self._show_error(problem)
            return
        self._busy(True, label)

        def created(_data, error):
            self._busy(False, label)
            if error is not None:
                self._show_error(f"Could not create the backup: {storage.explain(error)}")
                return
            self._settings.encrypted = encrypt
            self._settings.last_backup = ""
            self._done(password)
        self._restic.init(repo, password, created)

    def _done(self, password):
        self.close()
        self._on_done(self._settings, password)
