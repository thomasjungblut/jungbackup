"""The Settings view: schedule, location, folders, performance and Déjà Dup."""

from gi.repository import Adw, GLib, Gtk

from ..core.paths import collapse_path, describe_path
from ..core.util import fmt_days
from ..dejadup.launch import open_deja_dup
from ..storage import for_repository

PERIODS = [1, 2, 3, 7, 14, 30]
KEEP = [30, 90, 182, 365, 730, 0]
PACK_SIZES = [0, 32, 64, 128]          # restic refuses packs above 128 MiB
COMPRESSION = {"auto": "Automatic", "off": "Off", "fastest": "Fastest", "better": "Better", "max": "Maximum"}
READ_CONCURRENCY = [0, 4, 8]
CHECK_DAYS = [0, 7, 14, 30]
CHECK_PARTS = [20, 10, 4, 1]           # 5%, 10%, 25%, all of the data per check


def _choices(values, current, label):
    if current not in values:
        values = sorted(values + [current], key=lambda v: (v == 0, v))
    return values, Gtk.StringList.new([label(v) for v in values])


class SettingsPage(Adw.Bin):
    """Built from the current settings by load(), which the window calls whenever it's shown or they change."""

    def __init__(self, dd, on_changed, on_change_location, on_forget_password, toast, on_verify):
        super().__init__()
        self._on_verify = on_verify
        self._dd = dd
        self._on_changed = on_changed
        self._on_change_location = on_change_location
        self._on_forget_password = on_forget_password
        self._toast = toast
        self._settings = None

    def load(self, settings):
        self._settings = settings
        dd, on_change_location, on_forget_password = self._dd, self._on_change_location, self._on_forget_password
        page = Adw.PreferencesPage()
        self.set_child(page)

        # Schedule ---------------------------------------------------------
        schedule = Adw.PreferencesGroup(title="Schedule")
        page.add(schedule)

        auto = Adw.SwitchRow(title="Back Up Automatically", active=settings.periodic,
                             subtitle="Keeps running in the background after the window is closed")
        auto.connect("notify::active", lambda row, _p: self._set("periodic", row.get_active()))
        schedule.add(auto)

        periods, model = _choices(PERIODS, settings.period_days,
                                  lambda d: "Daily" if d == 1 else f"Every {fmt_days(d)}")
        freq = Adw.ComboRow(title="Frequency", model=model, selected=periods.index(settings.period_days))
        freq.connect("notify::selected", lambda row, _p: self._set("period_days", periods[row.get_selected()]))
        auto.bind_property("active", freq, "sensitive", 2)  # SYNC_CREATE
        schedule.add(freq)

        keeps, model = _choices(KEEP, settings.keep_days,
                                lambda d: "Forever" if d == 0 else f"At least {fmt_days(d)}")
        keep = Adw.ComboRow(title="Keep Backups", subtitle="Older snapshots are removed after each backup",
                            model=model, selected=keeps.index(settings.keep_days))
        keep.connect("notify::selected", lambda row, _p: self._set("keep_days", keeps[row.get_selected()]))
        schedule.add(keep)

        # Verification -----------------------------------------------------
        verify = Adw.PreferencesGroup(
            title="Verification",
            description="Reads stored data back after a backup to find damage early, before you need a restore")
        verify_now = Gtk.Button(label="Verify Now", valign=Gtk.Align.CENTER, css_classes=["flat"])
        verify_now.connect("clicked", lambda *_: self._on_verify())
        verify.set_header_suffix(verify_now)
        page.add(verify)

        days, model = _choices(CHECK_DAYS, settings.check_days,
                               lambda d: "Off" if d == 0 else f"Every {fmt_days(d)}")
        check = Adw.ComboRow(title="Verify Automatically", model=model, selected=days.index(settings.check_days))
        verify.add(check)

        parts, model = _choices(CHECK_PARTS, settings.check_parts,
                                lambda n: "All of it" if n <= 1 else f"{100 / n:g}%, all of it every {n} checks")
        amount = Adw.ComboRow(title="Data Read per Check", model=model, selected=parts.index(settings.check_parts),
                              subtitle="Downloaded from the backup location, which can cost money with cloud storage")
        amount.set_sensitive(settings.check_days > 0)
        amount.connect("notify::selected", lambda row, _p: self._set("check_parts", parts[row.get_selected()]))
        verify.add(amount)

        def check_changed(row, _p):
            amount.set_sensitive(days[row.get_selected()] > 0)
            self._set("check_days", days[row.get_selected()])
        check.connect("notify::selected", check_changed)

        # Location ---------------------------------------------------------
        location = Adw.PreferencesGroup(title="Storage Location")
        page.add(location)
        storage = for_repository(settings.repository)
        row = Adw.ActionRow(title=storage.name, subtitle=GLib.markup_escape_text(storage.label),
                            subtitle_selectable=True)
        row.add_prefix(Gtk.Image(icon_name=storage.icon))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda *_: on_change_location())
        row.add_suffix(change)
        location.add(row)

        # Folders ----------------------------------------------------------
        self._folder_group(page, "Folders to Back Up", "includes")
        self._folder_group(page, "Folders to Ignore", "excludes")

        # Performance ------------------------------------------------------
        perf = Adw.PreferencesGroup(title="Performance",
                                    description="Advanced restic options. They take effect on the next backup.")
        page.add(perf)

        sizes, model = _choices(PACK_SIZES, settings.pack_size_mb,
                                lambda v: "Default (16 MiB)" if v == 0 else f"{v} MiB")
        pack = Adw.ComboRow(title="Pack Size", model=model, selected=sizes.index(settings.pack_size_mb),
                            subtitle="Larger packs mean fewer files, which helps on slow or network storage")
        pack.connect("notify::selected", lambda row, _p: self._set("pack_size_mb", sizes[row.get_selected()]))
        perf.add(pack)

        modes = list(COMPRESSION)
        if settings.compression not in modes:
            modes.append(settings.compression)
        comp = Adw.ComboRow(title="Compression", selected=modes.index(settings.compression),
                            model=Gtk.StringList.new([COMPRESSION.get(m, m) for m in modes]),
                            subtitle="Needs a restic repository in format version 2")
        comp.connect("notify::selected", lambda row, _p: self._set("compression", modes[row.get_selected()]))
        perf.add(comp)

        reads, model = _choices(READ_CONCURRENCY, settings.read_concurrency,
                                lambda v: "Default (2)" if v == 0 else str(v))
        conc = Adw.ComboRow(title="Files Read in Parallel", model=model,
                            selected=reads.index(settings.read_concurrency),
                            subtitle="More can be faster on SSDs, slower on spinning disks")
        conc.connect("notify::selected", lambda row, _p: self._set("read_concurrency", reads[row.get_selected()]))
        perf.add(conc)

        # Password ---------------------------------------------------------
        if settings.encrypted:
            pw = Adw.PreferencesGroup(title="Password",
                                      description="Your backup password is saved in your keyring.")
            page.add(pw)
            forget = Adw.ButtonRow(title="Forget Saved Password")
            forget.add_css_class("destructive-action")
            forget.connect("activated", lambda *_: (on_forget_password(), self._toast("Password forgotten")))
            pw.add(forget)

        # Déjà Dup coexistence ---------------------------------------------
        if dd.available:
            group = Adw.PreferencesGroup(
                title="Déjà Dup",
                description="Both apps can use the same backup. To avoid backing up twice, "
                            "let only one of them run automatically.")
            page.add(group)
            dd_auto = Adw.SwitchRow(title="Déjà Dup Backs Up Automatically", active=dd.periodic)
            dd_auto.connect("notify::active", lambda r, _p: dd.set("periodic", r.get_active()))
            group.add(dd_auto)
            more = Adw.ButtonRow(title="Open Déjà Dup", end_icon_name="adw-external-link-symbolic")
            more.connect("activated", lambda *_: open_deja_dup())
            group.add(more)

    def _set(self, key, value):
        setattr(self._settings, key, value)
        self._settings.save()
        self._on_changed()

    # Folder lists ----------------------------------------------------------

    def _folder_group(self, page, title, key):
        group = Adw.PreferencesGroup(title=title)
        page.add(group)
        state = {"group": group, "key": key, "rows": []}
        self._fill_folders(state)

    def _fill_folders(self, state):
        group, key = state["group"], state["key"]
        for row in state["rows"]:
            group.remove(row)
        state["rows"] = []
        for entry in getattr(self._settings, key):
            row = Adw.ActionRow(title=GLib.markup_escape_text(describe_path(entry)))
            row.add_prefix(Gtk.Image(icon_name="user-home-symbolic" if entry == "$HOME" else "folder-symbolic"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
            remove.add_css_class("flat")
            remove.connect("clicked", self._remove_folder, state, entry)
            row.add_suffix(remove)
            group.add(row)
            state["rows"].append(row)
        add = Adw.ButtonRow(title="Add Folder…", start_icon_name="list-add-symbolic")
        add.connect("activated", self._add_folder, state)
        group.add(add)
        state["rows"].append(add)

    def _remove_folder(self, _btn, state, entry):
        self._set(state["key"], [e for e in getattr(self._settings, state["key"]) if e != entry])
        self._fill_folders(state)

    def _add_folder(self, _row, state):
        dialog = Gtk.FileDialog(title="Choose Folder", modal=True)

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            entry = collapse_path(folder.get_path())
            entries = list(getattr(self._settings, state["key"]))
            if entry not in entries:
                self._set(state["key"], entries + [entry])
            self._fill_folders(state)

        dialog.select_folder(self.get_root(), None, done)
