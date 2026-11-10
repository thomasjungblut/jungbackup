"""Browse a snapshot and get files back: in place, as a copy somewhere else, or just to look at."""

import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..backup.preview import prepare, preview_path, seal
from ..backup.restore import RestoreJob, plan_copy, plan_in_place
from ..backup.search import Query, bad_pattern
from ..core.paths import describe_path
from ..core.util import fmt_bytes, fmt_date, parse_time
from .timeline import Timeline
from .widgets import label

PULSE_MS = 120
MAX_RESULTS = 1000

# Fetching a single file: (progress label, toast when cancelled, heading when it fails).
SAVING = ("Saving “{}”…", "Saving stopped", "Couldn't Save the File")
OPENING = ("Opening “{}”…", "Opening stopped", "Couldn't Open the File")


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _names(paths, limit=3):
    names = [f"“{os.path.basename(p) or p}”" for p in paths[:limit]]
    if len(paths) > limit:
        names.append(f"{len(paths) - limit} more")
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]


def _start_path(snapshot):
    paths = snapshot.get("paths") or []
    try:
        return os.path.commonpath(paths) if paths else "/"
    except ValueError:
        return "/"


class Entry(GObject.Object):
    """One file or folder in a snapshot, from a `restic ls` node."""

    def __init__(self, node):
        super().__init__()
        self.name = node.get("name", "")
        self.path = node.get("path", "")
        self.is_dir = node.get("type") == "dir"
        self.size = node.get("size")
        self.mtime = parse_time(node.get("mtime"))
        self.link = node.get("type") == "symlink"
        self.previewable = node.get("type") == "file"
        self.sort_key = (not self.is_dir, GLib.utf8_collate_key_for_filename(self.name, -1))
        self.folder = None      # for search results: where it is, below the searched folder

    @classmethod
    def found(cls, node, searched):
        """A search result in `searched` or below, sorted by its path there."""
        entry = cls(node)
        below = os.path.dirname(entry.path)[len(searched.rstrip("/")):].lstrip("/")
        entry.folder = below
        entry.sort_key = (False, GLib.utf8_collate_key_for_filename(os.path.join(below, entry.name), -1))
        return entry

    def icon(self):
        if self.is_dir:
            return Gio.ThemedIcon.new("folder-symbolic")
        if self.link:
            return Gio.ThemedIcon.new("emblem-symbolic-link-symbolic")
        kind, _uncertain = Gio.content_type_guess(self.name, None)
        return Gio.content_type_get_symbolic_icon(kind)


def _sort(a, b, _data):
    """Folders first, then by name the way Files sorts them."""
    return (a.sort_key > b.sort_key) - (a.sort_key < b.sort_key)


class RestorePage(Gtk.Box):
    """The Restore view: pick a snapshot on the timeline, browse it, get files back.

    The window owns the snapshot list and the password and hands them over
    with set_backup() and set_snapshots(). Nothing is listed until the page is
    first shown. `header_start`/`header_end` are buttons for the window's
    header bar while this page is visible; `modified_column` is the one to
    hide on narrow windows.
    """

    def __init__(self, restic, toasts, on_unlock, on_retry):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._restic, self._toasts, self._on_unlock, self._on_retry = restic, toasts, on_unlock, on_retry
        self._repo = None
        self._password = None
        self._explain = str
        self._all = None            # the window's snapshot list, as last handed over
        self._error = None
        self._shown = False
        self._snapshot = None
        self._path = None
        self._listing = {}          # (snapshot id, path) → [Entry]; snapshots never change
        self._ls_cancellable = None
        self._selected = {}         # path → Entry, kept while moving between folders
        self._checks = []
        self._job = None
        self._dump = None           # (cancellable, target path, texts) while fetching a single file
        self._pulse = 0
        self._after_stop = None
        self._state_shown = True    # showing locked/loading/… instead of a folder
        self._query = None          # the search shown instead of the folder, if any
        self._searched = {}         # (snapshot id, pattern) → [Entry]
        self._find_cancellable = None

        self._up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent Folder (Alt+↑)", sensitive=False)
        self._up.connect("clicked", lambda *_: self._go_up())
        self._search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                            tooltip_text="Search This Folder and Below (Ctrl+F)")
        self.header_start, self.header_end = [self._up], [self._search_btn]

        self._timeline = Timeline(on_changed=self._snapshot_chosen)
        self._timeline_clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=800, child=self._timeline,
                                         margin_top=12, margin_start=12, margin_end=12)
        self.append(self._timeline_clamp)
        self.append(self._build_path_bar())
        self.append(self._build_search())
        self.append(self._build_content())
        self.append(self._build_action_bar())

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Alt>Up"),
                                            action=Gtk.CallbackAction.new(lambda *_: self._go_up() or True)))
        shortcuts.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                            action=Gtk.CallbackAction.new(lambda *_: self._focus_search() or True)))
        self.add_controller(shortcuts)
        self._update_actions()
        self._render_state()

    # Construction ------------------------------------------------------------

    def _build_path_bar(self):
        self._crumbs = Gtk.Box(css_classes=["linked", "path-bar"], margin_start=12, margin_end=12, margin_top=6,
                               margin_bottom=6)
        scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                      hscrollbar_policy=Gtk.PolicyType.EXTERNAL, child=self._crumbs)
        # Keep the current folder in view when the path is wider than the window. These change during
        # allocation, when it's too late to move the content, so scroll right after.
        adj = scroller.get_hadjustment()
        for prop in ("upper", "page-size"):
            adj.connect(f"notify::{prop}", lambda a, _p: GLib.idle_add(_scroll_to_end, a))
        return scroller

    def _build_search(self):
        self._search = Gtk.SearchEntry(hexpand=True, search_delay=300)
        self._search.connect("search-changed", lambda *_: self._search_changed())
        self._searching = Adw.Spinner(visible=False, tooltip_text="Searching…")
        self._found_label = label("", ("dim-label", "caption", "numeric"))
        box = Gtk.Box(spacing=8)
        box.append(self._search)
        box.append(self._searching)
        box.append(self._found_label)
        self._search_bar = Gtk.SearchBar(child=Adw.Clamp(child=box, maximum_size=560), key_capture_widget=self)
        self._search_bar.connect_entry(self._search)
        self._search_btn.bind_property("active", self._search_bar, "search-mode-enabled",
                                       GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self._search_bar.connect("notify::search-mode-enabled", lambda *_: self._search_changed())
        return self._search_bar

    def _build_content(self):
        self._store = Gio.ListStore(item_type=Entry)
        ordered = Gtk.SortListModel(model=self._store, sorter=Gtk.CustomSorter.new(_sort, None))
        self._model = Gtk.SingleSelection(model=ordered, autoselect=False, can_unselect=True)

        table = Gtk.ColumnView(model=self._model, css_classes=["data-table"])
        table.connect("activate", self._on_activate)
        table.append_column(self._column("", self._setup_check, self._bind_check, teardown=self._checks.remove))
        table.append_column(self._column("Name", self._setup_name, self._bind_name, expand=True))
        table.append_column(self._column("Size", self._setup_value, self._bind_size))
        self.modified_column = self._column("Modified", self._setup_value, self._bind_mtime)
        table.append_column(self.modified_column)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=table)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._stack.add_named(scroller, "list")
        self._stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                          width_request=32, height_request=32), "loading")
        self._status = Adw.StatusPage()
        self._status_btn = Gtk.Button(halign=Gtk.Align.CENTER, css_classes=["pill"])
        self._status_btn.connect("clicked", lambda *_: self._status_action and self._status_action())
        self._status_action = None
        self._status.set_child(self._status_btn)
        self._stack.add_named(self._status, "status")
        self._stack.set_visible_child_name("loading")
        return self._stack

    def _column(self, title, setup, bind, expand=False, teardown=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: setup(item))
        factory.connect("bind", lambda _f, item: bind(item.get_child(), item.get_item()))
        if teardown is not None:
            factory.connect("teardown", lambda _f, item: teardown(item.get_child()))
        return Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand)

    def _build_action_bar(self):
        bar = Gtk.ActionBar(vexpand=False)
        self._bar_stack = Gtk.Stack(hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        bar.set_center_widget(self._bar_stack)

        actions = Gtk.Box(spacing=12, margin_start=6, margin_end=6)
        self._selection_label = label("", ("dim-label",), hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        actions.append(self._selection_label)
        self._clear_btn = Gtk.Button(label="Clear Selection", css_classes=["flat"])
        self._clear_btn.connect("clicked", lambda *_: self._set_selection({}))
        actions.append(self._clear_btn)
        self._open_btn = Gtk.Button(label="Open", tooltip_text="Look at this version without restoring it: "
                                    "opens a temporary copy (or double-click the file)")
        self._open_btn.connect("clicked", lambda *_: self._preview(next(iter(self._selected.values()))))
        actions.append(self._open_btn)
        self._download_btn = Gtk.Button(label="Save Copy…", tooltip_text="Save a copy somewhere else; "
                                        "nothing in the original location is changed")
        self._download_btn.connect("clicked", lambda *_: self._download())
        actions.append(self._download_btn)
        self._restore_btn = Gtk.Button(label="Restore", css_classes=["suggested-action"],
                                       tooltip_text="Put back in the original location, replacing what's there")
        self._restore_btn.connect("clicked", lambda *_: self._confirm_restore())
        actions.append(self._restore_btn)
        self._bar_stack.add_named(actions, "actions")

        progress = Gtk.Box(spacing=12, margin_start=6, margin_end=6)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self._progress_label = label("", ("caption", "numeric"), ellipsize=Pango.EllipsizeMode.END)
        self._progress = Gtk.ProgressBar()
        column.append(self._progress_label)
        column.append(self._progress)
        progress.append(column)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._cancel())
        progress.append(cancel)
        self._bar_stack.add_named(progress, "progress")
        return bar

    # Cells -------------------------------------------------------------------

    def _setup_check(self, item):
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.entry = None
        check.connect("toggled", self._on_check)
        item.set_child(check)
        self._checks.append(check)

    def _bind_check(self, check, entry):
        check.entry = None          # don't treat the state change below as a click
        check.set_active(entry.path in self._selected)
        check.set_tooltip_text(f"Select “{entry.name}”")
        check.entry = entry

    def _setup_name(self, item):
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Image())
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label("", ellipsize=Pango.EllipsizeMode.MIDDLE))
        text.append(label("", ("caption", "dim-label"), ellipsize=Pango.EllipsizeMode.MIDDLE))
        box.append(text)
        item.set_child(box)

    def _bind_name(self, box, entry):
        icon, text = box.get_first_child(), box.get_last_child()
        name, folder = text.get_first_child(), text.get_last_child()
        icon.set_from_gicon(entry.icon())
        name.set_label(entry.name)
        folder.set_visible(entry.folder is not None)
        folder.set_label(f"in {entry.folder}" if entry.folder else "in this folder")
        box.set_tooltip_text(entry.path if entry.folder is not None else entry.name)

    def _setup_value(self, item):
        item.set_child(label("", ("dim-label", "numeric"), xalign=1.0, margin_start=12))

    def _bind_size(self, cell, entry):
        cell.set_label("" if entry.is_dir or entry.size is None else fmt_bytes(entry.size))

    def _bind_mtime(self, cell, entry):
        cell.set_label(fmt_date(entry.mtime) if entry.mtime else "")

    # Snapshots ---------------------------------------------------------------

    def set_backup(self, repo, password, explain):
        if repo != self._repo:
            self._repo = repo
            self._all = None
            self._snapshot = self._path = None
            self._listing = {}
            self._searched = {}
            self._selected = {}
            self._update_actions()
        self._password, self._explain = password, explain
        self._render_state()

    def set_snapshots(self, snapshots, error=None):
        """The window's (oldest-first) list; None while it isn't loaded. Unchanged lists are ignored."""
        if snapshots is self._all and error == self._error:
            return
        self._all, self._error = snapshots, error
        if snapshots is not None:
            self._timeline.set_snapshots(snapshots, self._snapshot)
        self._render_state()

    def shown(self):
        """The page became visible: load what's needed."""
        self._shown = True
        self._render_state()

    def _render_state(self):
        locked = self._password is None
        snaps = self._all or []
        self._timeline_clamp.set_visible(not locked and bool(snaps))
        browsing = not locked and bool(snaps) and self._shown
        if browsing:
            selected = self._timeline.selected
            if self._snapshot is None or selected.get("id") != self._snapshot.get("id"):
                self._snapshot_chosen(selected)
            elif self._state_shown:
                self._open(self._path)
        self._state_shown = not browsing
        if locked:
            self._show_status("system-lock-screen-symbolic", "Backup Locked",
                              "Enter your backup password to browse the files in your backups.",
                              "Unlock", self._on_unlock)
        elif self._all is None and self._error:
            self._show_status("dialog-error-symbolic", "Couldn't Read the Backup", self._error,
                              "Try Again", self._on_retry)
        elif self._all is None:
            self._show_loading()
        elif not snaps:
            self._show_status("document-open-recent-symbolic", "No Snapshots Yet",
                              "Once a backup has run, its files can be restored from here.")
        elif not self._shown:
            self._show_loading()
        self._update_actions()

    def _snapshot_chosen(self, snapshot):
        self._snapshot = snapshot
        self._set_selection({})
        if self._query is not None:
            # Keep searching the same folder, to step back until the file turns up.
            self._run_search(Query(self._query.text, self._query.folder))
            return
        # Stay in the same folder, to compare versions of it across snapshots.
        self._open(self._path or _start_path(snapshot))

    # Browsing ----------------------------------------------------------------

    def _open(self, path, keep_text=False):
        self._path = path
        self._end_search()
        if not keep_text:
            self._search.set_text("")
        self._search.set_placeholder_text(f"Search {'everything' if path == '/' else os.path.basename(path)} "
                                          "and the folders in it")
        self._render_crumbs()
        self._up.set_sensitive(path != "/")
        if self._ls_cancellable is not None:
            self._ls_cancellable.cancel()
            self._ls_cancellable = None
        key = (self._snapshot["id"], path)
        if key in self._listing:
            self._show_listing(self._listing[key])
            return
        self._show_loading()
        self._ls_cancellable = Gio.Cancellable()
        self._restic.ls(self._repo, self._password, self._snapshot["id"], path,
                        lambda nodes, error: self._on_listed(key, nodes, error), self._ls_cancellable)

    def _on_listed(self, key, nodes, error):
        if key != (self._snapshot["id"], self._path):
            return   # the user moved on meanwhile
        self._ls_cancellable = None
        if self._query is not None:
            if nodes is not None:
                self._listing[key] = [Entry(n) for n in nodes]
            return   # searching instead; the folder is shown once that ends
        if error is not None:
            self._show_status("dialog-error-symbolic", "Couldn't Open This Folder", self._explain(error),
                              "Try Again", lambda: self._open(self._path))
            return
        if nodes is None:
            start = _start_path(self._snapshot)
            self._show_status("folder-symbolic", "Not in This Snapshot",
                              f"{describe_path(self._path)} wasn't backed up in this snapshot.",
                              "Show Backed-Up Folders" if start != self._path else None,
                              lambda: self._open(start))
            return
        self._listing[key] = [Entry(n) for n in nodes]
        self._show_listing(self._listing[key])

    def _show_listing(self, entries):
        self._store.splice(0, self._store.get_n_items(), entries)
        if entries:
            self._stack.set_visible_child_name("list")
        else:
            self._show_status("folder-symbolic", "Empty Folder", "This folder was empty when it was backed up.")

    def _go_up(self):
        if self._snapshot is not None and self._path and self._path != "/":
            self._open(os.path.dirname(self._path))

    def _on_activate(self, _table, position):
        entry = self._model.get_item(position)
        if entry is None:
            return
        if entry.is_dir:
            self._open(entry.path)
        elif entry.previewable:
            self._preview(entry)
        else:
            self._toggle(entry, entry.path not in self._selected)
            self._sync_checks()

    def _render_crumbs(self):
        while (child := self._crumbs.get_first_child()) is not None:
            self._crumbs.remove(child)
        home = GLib.get_home_dir()
        crumbs, path = [], self._path
        while True:
            crumbs.append(path)
            if path == "/":
                break
            path = os.path.dirname(path)
        for path in reversed(crumbs):
            if path == "/":
                button = Gtk.Button(icon_name="drive-harddisk-symbolic", tooltip_text="Computer")
            else:
                name = "Home" if path == home else os.path.basename(path)
                button = Gtk.Button(label=name, tooltip_text=path)
            if path == self._path:
                button.add_css_class("current")
            button.connect("clicked", lambda _b, p=path: self._open(p))
            self._crumbs.append(button)

    def _show_loading(self):
        self._stack.set_visible_child_name("loading")

    def _show_status(self, icon, title, description, button=None, action=None):
        self._status.set_icon_name(icon)
        self._status.set_title(title)
        self._status.set_description(GLib.markup_escape_text(description or ""))
        self._status_btn.set_visible(bool(button))
        self._status_btn.set_label(button or "")
        self._status_action = action
        self._stack.set_visible_child_name("status")

    # Searching ---------------------------------------------------------------

    def _focus_search(self):
        self._search_bar.set_search_mode(True)
        self._search.grab_focus()

    def _search_changed(self):
        text = self._search.get_text().strip()
        if not self._search_bar.get_search_mode() or not text:
            if self._query is not None:
                self._open(self._path)      # cleared or closed: back to the folder
            return
        if self._snapshot is None or self._password is None or self._path is None:
            return
        query = Query(text, self._path)
        if not query.valid:
            if self._query is not None:
                self._open(self._path, keep_text=True)
            self._found_label.set_label("Keep typing…")
            return
        self._run_search(query)

    def _run_search(self, query):
        self._stop_find()
        self._query = query
        key = (self._snapshot["id"], query.pattern)
        if key in self._searched:
            self._show_results(self._searched[key])
            return
        self._searching.set_visible(True)
        if self._stack.get_visible_child_name() != "list":
            self._show_loading()
        self._find_cancellable = cancellable = Gio.Cancellable()

        def done(data, error):
            if cancellable is not self._find_cancellable:
                return      # superseded
            self._find_cancellable = None
            self._searching.set_visible(False)
            if error is not None:
                self._found_label.set_label("")
                if bad_pattern(error):
                    self._show_status("system-search-symbolic", "Can't Search for That",
                                      f"“{query.text}” isn't a valid pattern. Use * for any text and ? for one "
                                      "character; put a \\ before [ to find it in names.")
                else:
                    self._show_status("dialog-error-symbolic", "Couldn't Search", self._explain(error),
                                      "Try Again", lambda: self._run_search(query))
                return
            self._searched[key] = [Entry.found(n, query.folder) for n in query.results(data)]
            self._show_results(self._searched[key])
        self._restic.find(self._repo, self._password, self._snapshot["id"], query.pattern, done, cancellable)

    def _show_results(self, entries):
        query = self._query
        where = "everything" if query.folder == "/" else describe_path(query.folder)
        if not entries:
            self._found_label.set_label("")
            self._show_status("system-search-symbolic", "No Results",
                              f"Nothing called “{query.text}” in {where} or the folders in it, in this snapshot. "
                              "Older snapshots are on the timeline.")
            return
        shown = sorted(entries, key=lambda e: e.sort_key)[:MAX_RESULTS]
        self._found_label.set_label(_plural(len(entries), "result") if len(shown) == len(entries)
                                    else f"First {len(shown):,} of {len(entries):,}")
        self._store.splice(0, self._store.get_n_items(), shown)
        self._stack.set_visible_child_name("list")

    def _stop_find(self):
        if self._find_cancellable is not None:
            self._find_cancellable.cancel()
            self._find_cancellable = None
        self._searching.set_visible(False)

    def _end_search(self):
        self._stop_find()
        self._query = None
        self._found_label.set_label("")

    # Selection ---------------------------------------------------------------

    def _on_check(self, check):
        if check.entry is not None:
            self._toggle(check.entry, check.get_active())

    def _toggle(self, entry, selected):
        if selected:
            self._selected[entry.path] = entry
        else:
            self._selected.pop(entry.path, None)
        self._update_actions()

    def _set_selection(self, selected):
        self._selected = dict(selected)
        self._sync_checks()
        self._update_actions()

    def _sync_checks(self):
        for check in self._checks:
            entry = check.entry
            if entry is not None:
                check.entry = None
                check.set_active(entry.path in self._selected)
                check.entry = entry

    def _selected_paths(self):
        # Picking a folder and something inside it means the folder; restoring both would do it twice.
        paths = sorted(self._selected)
        return [p for p in paths if not any(p.startswith(other + "/") for other in paths if other != p)]

    def _update_actions(self):
        n = len(self._selected)
        busy = self._job is not None or self._dump is not None
        self._selection_label.set_label(f"{_plural(n, 'item')} selected" if n
                                        else "Select files and folders to restore")
        self._clear_btn.set_visible(n > 0)
        self._open_btn.set_sensitive(n == 1 and not busy and next(iter(self._selected.values())).previewable)
        self._download_btn.set_sensitive(n > 0 and not busy)
        self._restore_btn.set_sensitive(n > 0 and not busy)
        self._timeline.set_sensitive(not busy)
        self._bar_stack.set_visible_child_name("progress" if busy else "actions")

    # Restoring ---------------------------------------------------------------

    def _when(self):
        return fmt_date(parse_time(self._snapshot.get("time")))

    def _confirm_restore(self):
        paths = self._selected_paths()
        dialog = Adw.AlertDialog(
            heading=f"Restore {_plural(len(paths), 'Item')}?",
            body=f"{_names(paths)} will be put back in {'its' if len(paths) == 1 else 'their'} original "
                 f"location as of {self._when()}. Files that exist there now are replaced with the backed-up "
                 "version; files that weren't in the backup are left alone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def answered(_d, response):
            if response == "restore":
                self._run(plan_in_place(paths), f"Restored {_plural(len(paths), 'item')}", None)
        dialog.connect("response", answered)
        dialog.present(self)

    def _download(self):
        paths = self._selected_paths()
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or GLib.get_home_dir()
        dialog = Gtk.FileDialog(modal=True, initial_folder=Gio.File.new_for_path(downloads))
        entry = self._selected[paths[0]]
        if len(paths) == 1 and not entry.is_dir:
            dialog.set_title("Save Copy")
            dialog.set_initial_name(entry.name)

            def saved(dlg, result):
                try:
                    file = dlg.save_finish(result)
                except GLib.Error:
                    return
                self._save_file(entry, file)
            dialog.save(self.get_root(), None, saved)
            return

        dialog.set_title("Choose Where to Save")

        def chosen(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            self._copy_to(paths, folder)
        dialog.select_folder(self.get_root(), None, chosen)

    def _copy_to(self, paths, folder):
        target = folder.get_path()
        if target is None:
            self._alert("Can't Save There", "Choose a folder on this computer or a mounted drive.")
            return
        message = f"Saved {_plural(len(paths), 'item')} to {describe_path(target)}"
        existing = [p for p in paths if os.path.lexists(os.path.join(target, os.path.basename(p)))]
        if not existing:
            self._run(plan_copy(paths, target), message, folder)
            return
        dialog = Adw.AlertDialog(
            heading="Replace Existing Files?",
            body=f"{_names(existing)} already {'exists' if len(existing) == 1 else 'exist'} in "
                 f"{describe_path(target)}. Saving replaces files with the same name.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("replace", "Replace")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "replace" and self._run(plan_copy(paths, target), message,
                                                                              folder))
        dialog.present(self)

    def _run(self, steps, message, show):
        self._job = RestoreJob(self._restic, self._repo, self._password, self._snapshot["id"], steps,
                               self._on_job_update, lambda job, ok, error: self._on_job_done(job, ok, error,
                                                                                             message, show),
                               self._explain)
        self.get_root().get_application().hold()
        self._progress.set_fraction(0)
        self._progress_label.set_label("Restoring…")
        self._update_actions()
        self._job.start()

    def _on_job_update(self, job):
        self._progress.set_fraction(job.fraction)
        if job.total_bytes:
            self._progress_label.set_label(f"Restoring… {fmt_bytes(job.bytes_restored)} of "
                                           f"{fmt_bytes(job.total_bytes)}")

    def _on_job_done(self, job, ok, error, message, show):
        self._job = None
        self.get_root().get_application().release()
        self._update_actions()
        if self._stopped():
            return
        if ok:
            self._set_selection({})
            if job.warnings:
                self._alert(f"{message}, With Problems",
                            f"{_plural(len(job.warnings), 'item')} couldn't be restored:\n\n"
                            + "\n".join(job.warnings[:8]) + ("\n…" if len(job.warnings) > 8 else ""))
            else:
                self._toast(message, show)
        elif error:
            self._alert("Restore Failed", error)
        else:
            self._toast("Restore stopped")

    def _save_file(self, entry, file):
        target = file.get_path()
        if target is None:
            self._alert("Can't Save There", "Choose a folder on this computer or a mounted drive.")
            return
        self._fetch(entry, target, SAVING, lambda: self._toast(f"Saved “{file.get_basename()}”", file))

    def _preview(self, entry):
        """Open the file as it was in this snapshot, from a temporary copy; nothing is restored."""
        if self.busy or self._snapshot is None:
            return
        target = preview_path(self._snapshot["id"], entry.path)
        if os.path.exists(target):
            self._launch(target)    # fetched before; snapshots never change
            return
        try:
            prepare(target)
        except OSError as e:
            self._alert(OPENING[2], str(e))
            return

        def fetched():
            seal(target)
            self._launch(target)
        self._fetch(entry, target, OPENING, fetched)

    def _launch(self, path):
        def launched(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error as e:
                if not e.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self._alert(OPENING[2], f"{e.message}\n\nSave Copy… gets you the file instead.")
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.get_root(), None, launched)

    def _fetch(self, entry, target, texts, then):
        """Write one file from the snapshot to `target`; call then() once it's there."""
        doing, _stopped, failed = texts
        cancellable = Gio.Cancellable()
        self._dump = (cancellable, target, texts)
        self._progress_label.set_label(doing.format(entry.name))
        self._pulse = GLib.timeout_add(PULSE_MS, lambda: self._progress.pulse() or True)
        self.get_root().get_application().hold()
        self._update_actions()

        def done(_data, error):
            if self._end_dump() is None:
                return   # cancelled; _cancel cleaned up
            if error is not None:
                _remove(target)   # restic leaves an empty or partial file behind
                self._alert(failed, self._explain(error))
            elif not self._stopped():
                then()
        self._restic.dump(self._repo, self._password, self._snapshot["id"], entry.path, target, done, cancellable)

    def _end_dump(self):
        if self._dump is None:
            return None
        dump, self._dump = self._dump, None
        GLib.source_remove(self._pulse)
        self.get_root().get_application().release()
        self._update_actions()
        return dump

    def _cancel(self):
        if self._job is not None:
            self._job.cancel()
        elif self._dump is not None:
            # A cancelled query doesn't call back, so clean up here.
            cancellable, target, (_doing, stopped, _failed) = self._end_dump()
            cancellable.cancel()
            _remove(target)
            if not self._stopped():
                self._toast(stopped)

    @property
    def busy(self):
        return self._job is not None or self._dump is not None

    def confirm_stop(self, then):
        """Ask whether to stop the running restore; call then() once it has stopped."""
        dialog = Adw.AlertDialog(heading="Stop Restoring?",
                                 body="Files that were already restored stay; the rest won't be.")
        dialog.add_response("continue", "Keep Restoring")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("continue")

        def answered(_d, response):
            if response == "stop" and self.busy:
                self._after_stop = then
                self._cancel()
        dialog.connect("response", answered)
        dialog.present(self)

    def _stopped(self):
        """After a stop asked for by confirm_stop: run its callback and report True."""
        then, self._after_stop = self._after_stop, None
        if then is not None:
            then()
        return then is not None

    # Feedback ----------------------------------------------------------------

    def _toast(self, text, file=None):
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=8 if file else 4)
        if file is not None:
            toast.set_button_label("Open")
            toast.connect("button-clicked",
                          lambda *_: Gtk.FileLauncher.new(file).launch(self.get_root(), None, _launched))
        self._toasts.add_toast(toast)

    def _alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)


def _scroll_to_end(adj):
    adj.set_value(adj.get_upper() - adj.get_page_size())
    return False


def _launched(launcher, result):
    try:
        launcher.launch_finish(result)
    except GLib.Error:
        pass


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass
