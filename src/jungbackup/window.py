"""The dashboard window."""

import json
import os
import time
from datetime import timedelta

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import keyring
from .charts import BarChart, BreakdownBar, Sparkline, Swatch
from .dejadup import DejaDupConfig, describe_path
from .dialogs import PasswordDialog, SetupDialog
from .engine import BackupJob
from .monitor import NUM_CPUS, ProcessMonitor
from .prefs import PreferencesDialog, open_deja_dup
from .restic import Restic, is_repository
from .settings import Settings, import_from_deja_dup
from .util import (
    fmt_ago,
    fmt_bytes,
    fmt_bytes_short,
    fmt_count,
    fmt_date,
    fmt_days,
    fmt_duration,
    fmt_duration_short,
    fmt_rate,
    fmt_until,
    now,
    parse_time,
)

STATS_CACHE = os.path.join(GLib.get_user_cache_dir(), "jungbackup", "stats.json")
HISTORY_BARS = 30
SNAPSHOT_ROWS = 12
SCHEDULER_INTERVAL = 60


def _label(text="", css=(), xalign=0.0, **kw):
    label = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css:
        label.add_css_class(c)
    return label


def _snapshot_duration(snap):
    summary = snap.get("summary") or {}
    start, end = parse_time(summary.get("backup_start")), parse_time(summary.get("backup_end"))
    if start and end:
        return (end - start).total_seconds()
    return None


class StatTile(Gtk.Box):
    def __init__(self, title, icon):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.add_css_class("stat-tile")
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Image(icon_name=icon, css_classes=["dim-label"]))
        head.append(_label(title, ("caption-heading", "dim-label")))
        self.append(head)
        self._value = _label("—", ("stat-value",), ellipsize=Pango.EllipsizeMode.END)
        self._sub = _label("", ("caption", "dim-label"), ellipsize=Pango.EllipsizeMode.END)
        self.append(self._value)
        self.append(self._sub)

    def set(self, value, sub=""):
        self._value.set_label(value)
        self._sub.set_label(sub)
        self.set_tooltip_text(f"{value}\n{sub}" if sub else value)


class Metric(Gtk.Box):
    """Small labelled value used in the live activity card."""

    def __init__(self, title, sparkline_series=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.append(_label(title, ("caption-heading", "dim-label")))
        self._value = _label("—", ("metric-value",))
        self.append(self._value)
        self.spark = None
        if sparkline_series is not None:
            self.spark = Sparkline(series=sparkline_series)
            self.append(self.spark)
        self._detail = _label("", ("caption", "dim-label", "numeric"), visible=False)
        self.append(self._detail)

    def set(self, text, detail=""):
        self._value.set_label(text)
        self._detail.set_label(detail)
        self._detail.set_visible(bool(detail))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Jung Backup", default_width=1080, default_height=860)
        self.set_size_request(360, 400)
        self.restic = Restic()
        self.dd = DejaDupConfig(on_changed=lambda: GLib.idle_add(self._render_config))
        self.settings = Settings.load()
        self.password = None
        self.job = None
        self.activity = None
        self.snapshots = None
        self.raw_stats = None
        self.fs_info = None
        self.stats_error = None
        self._fetching = False
        self._cancellable = None
        self._monitor = ProcessMonitor()
        self._was_active = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        view.add_top_bar(self._build_header())
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self._banner_action())
        view.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self._build_dashboard(), "dashboard")
        view.set_content(self.stack)

        self._load_stats_cache()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.timeout_add_seconds(30, self._slow_tick)
        GLib.timeout_add_seconds(SCHEDULER_INTERVAL, self._scheduler)
        self.connect("close-request", self._on_close)
        self.show_page()

    # Construction ------------------------------------------------------------

    def _build_header(self):
        header = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh Statistics")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_start(self.refresh_btn)

        menu = Gio.Menu()
        menu.append("Re-Import from Déjà Dup…", "win.reimport")
        menu.append("Open Déjà Dup", "win.open-deja-dup")
        menu.append("About Jung Backup", "app.about")
        menu.append("Quit", "app.quit")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu")
        header.pack_end(menu_btn)

        self.edit_btn = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit Backup Settings")
        self.edit_btn.connect("clicked", lambda *_: self.edit_settings())
        header.pack_end(self.edit_btn)

        for name, cb in (("reimport", lambda *_: self.start_import()),
                         ("open-deja-dup", lambda *_: open_deja_dup())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        return header

    def _build_welcome(self):
        page = Adw.StatusPage(icon_name="io.jungblut.JungBackup", title="Back Up Your Files",
                              description="Keep copies of your files safe on another drive or a network share, "
                                          "and see at a glance how your backups are doing.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.import_btn = Gtk.Button(css_classes=["pill", "suggested-action"])
        self.import_btn.set_child(Adw.ButtonContent(icon_name="document-revert-symbolic",
                                                    label="Import from Déjà Dup"))
        self.import_btn.connect("clicked", lambda *_: self.start_import())
        self.import_hint = _label("", ("caption", "dim-label"), xalign=0.5, wrap=True, max_width_chars=50,
                                  justify=Gtk.Justification.CENTER)
        new_btn = Gtk.Button(label="Set Up New Backup", css_classes=["pill"])
        new_btn.connect("clicked", lambda *_: self.start_setup())
        box.append(self.import_btn)
        box.append(self.import_hint)
        box.append(new_btn)
        page.set_child(box)
        return page

    def _build_dashboard(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=800)
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=18, margin_bottom=32, margin_start=16, margin_end=16)
        clamp.set_child(box)

        box.append(self._build_hero())
        self.live_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.live_revealer.set_child(self._build_live())
        box.append(self.live_revealer)

        self.tiles = {}
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=2, max_children_per_line=4,
                           column_spacing=12, row_spacing=12)
        for key, title, icon in (
            ("size", "Backup Size", "drive-harddisk-symbolic"),
            ("protected", "Protected Data", "security-high-symbolic"),
            ("files", "Files", "folder-documents-symbolic"),
            ("snapshots", "Snapshots", "document-open-recent-symbolic"),
            ("age", "Last Backup", "object-select-symbolic"),
            ("duration", "Last Duration", "preferences-system-time-symbolic"),
            ("added", "Last Upload", "send-to-symbolic"),
            ("free", "Free Space", "drive-harddisk-system-symbolic"),
        ):
            tile = StatTile(title, icon)
            self.tiles[key] = tile
            flow.append(tile)
            tile.get_parent().set_focusable(False)
        box.append(flow)

        box.append(self._build_breakdown())

        charts = Gtk.Box(spacing=12, homogeneous=True)
        self.added_chart = BarChart(fmt_bytes_short)
        self.duration_chart = BarChart(fmt_duration_short)
        charts.append(self._chart_card("Data Added per Backup", self.added_chart))
        charts.append(self._chart_card("Duration per Backup", self.duration_chart))
        self.charts_box = charts
        box.append(charts)

        self.snap_group = Adw.PreferencesGroup(title="Recent Snapshots")
        self._snap_rows = []
        box.append(self.snap_group)

        self.config_group = Adw.PreferencesGroup(title="Configuration")
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit", css_classes=["flat"])
        edit.connect("clicked", lambda *_: self.edit_settings())
        self.config_group.set_header_suffix(edit)
        self.cfg_rows = {}
        for key, title, icon in (("location", "Location", "folder-remote-symbolic"),
                                 ("schedule", "Schedule", "alarm-symbolic"),
                                 ("keep", "Keep", "document-open-recent-symbolic"),
                                 ("includes", "Backing Up", "folder-symbolic"),
                                 ("excludes", "Ignoring", "action-unavailable-symbolic"),
                                 ("engine", "Engine", "applications-system-symbolic")):
            row = Adw.ActionRow(title=title, subtitle_selectable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            self.cfg_rows[key] = row
            self.config_group.add(row)
        box.append(self.config_group)
        return scroller

    def _build_hero(self):
        hero = Gtk.Box(spacing=18, css_classes=["card", "hero"])
        self.hero_icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER, css_classes=["hero-icon"])
        hero.append(self.hero_icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.hero_title = _label("", ("title-2",), wrap=True)
        self.hero_sub = _label("", ("dim-label",), wrap=True)
        self.hero_error = _label("", ("caption", "error"), wrap=True, visible=False, selectable=True)
        text.append(self.hero_title)
        text.append(self.hero_sub)
        text.append(self.hero_error)
        hero.append(text)
        self.backup_btn = Gtk.Button(label="Back Up Now", valign=Gtk.Align.CENTER,
                                     css_classes=["pill", "suggested-action"])
        self.backup_btn.connect("clicked", lambda *_: self.backup_now())
        hero.append(self.backup_btn)
        return hero

    def _build_live(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["card", "live"])
        top = Gtk.Box(spacing=12)
        top.append(Adw.Spinner(width_request=20, height_request=20))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.live_title = _label("", ("heading",))
        self.live_sub = _label("", ("caption", "dim-label"), wrap=True)
        titles.append(self.live_title)
        titles.append(self.live_sub)
        top.append(titles)
        self.cancel_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER, css_classes=["pill"])
        self.cancel_btn.connect("clicked", lambda *_: self.job and self.job.cancel())
        top.append(self.cancel_btn)
        card.append(top)

        self.live_progress = Gtk.ProgressBar(show_text=False)
        card.append(self.live_progress)
        self.live_progress_text = _label("", ("caption", "numeric"))
        card.append(self.live_progress_text)

        grid = Gtk.Grid(column_spacing=24, row_spacing=12, column_homogeneous=True)
        self.m_read = Metric("Reading", sparkline_series=0)
        self.m_write = Metric("Writing to Backup", sparkline_series=1)
        self.m_cpu = Metric("CPU")
        self.m_mem = Metric("Memory")
        self.m_elapsed = Metric("Elapsed")
        self.m_eta = Metric("Remaining")
        grid.attach(self.m_read, 0, 0, 2, 1)
        grid.attach(self.m_write, 2, 0, 2, 1)
        for i, m in enumerate((self.m_cpu, self.m_mem, self.m_elapsed, self.m_eta)):
            grid.attach(m, i, 1, 1, 1)
        card.append(grid)

        self.live_files = _label("", ("caption", "monospace", "dim-label"),
                                 ellipsize=Pango.EllipsizeMode.START, selectable=False)
        card.append(self.live_files)
        return card

    def _build_breakdown(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "chart-card"])
        head = Gtk.Box()
        head.append(_label("Files in Last Backup", ("heading",), hexpand=True))
        self.breakdown_total = _label("", ("dim-label", "numeric"))
        head.append(self.breakdown_total)
        card.append(head)
        self.breakdown = BreakdownBar()
        card.append(self.breakdown)
        legend = Gtk.Box(spacing=18)
        self.legend = {}
        for i, (key, name) in enumerate((("new", "New"), ("changed", "Changed"), ("unmodified", "Unchanged"))):
            item = Gtk.Box(spacing=6)
            item.append(Swatch(i))
            lbl = _label(name, ("caption", "numeric"))
            item.append(lbl)
            self.legend[key] = (lbl, name)
            legend.append(item)
        card.append(legend)
        self.breakdown_dirs = _label("", ("caption", "dim-label"))
        card.append(self.breakdown_dirs)
        self.breakdown_card = card
        return card

    def _chart_card(self, title, chart):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "chart-card"])
        card.append(_label(title, ("heading",)))
        card.append(chart)
        return card

    # Pages & flows -------------------------------------------------------------

    def show_page(self):
        if not self.settings.configured:
            result = import_from_deja_dup(self.dd)
            self.import_btn.set_visible(result is not None)
            if result is not None:
                s = result.settings
                where = s.repository or "a location this app can't use yet"
                self.import_hint.set_label(f"Found Déjà Dup backups in {describe_path(where)}")
            self.stack.set_visible_child_name("welcome")
            self.refresh_btn.set_visible(False)
            self.edit_btn.set_visible(False)
            self.banner.set_revealed(False)
            return
        self.stack.set_visible_child_name("dashboard")
        self._sync_autostart()
        self.refresh_btn.set_visible(True)
        self.edit_btn.set_visible(True)
        self._render_all()
        self.unlock_and_refresh()

    def start_import(self):
        result = import_from_deja_dup(self.dd)
        if result is None:
            self.toast("No Déjà Dup settings found")
            return
        s = result.settings
        body = (f"Location: {s.repository or '—'}\n"
                f"Backing up: {', '.join(describe_path(p) for p in s.includes)}\n"
                f"Ignoring: {', '.join(describe_path(p) for p in s.excludes) or 'nothing'}\n"
                f"Schedule: {'every ' + fmt_days(s.period_days) if s.periodic else 'manual'}"
                f" · keep {fmt_days(s.keep_days)}")
        dialog = Adw.AlertDialog(heading="Import Déjà Dup Settings?", body=body)
        extra = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        turn_off = Adw.SwitchRow(title="Turn Off Déjà Dup's Automatic Backups",
                                 subtitle="This app takes over the schedule; both can still restore",
                                 active=self.dd.periodic)
        turn_off.set_visible(self.dd.periodic)
        extra.append(turn_off)
        dialog.set_extra_child(extra)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("import", "Import")
        dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)

        def answered(_d, response):
            if response != "import":
                return

            def finished(settings, password):
                if turn_off.get_active() and turn_off.get_visible():
                    self.dd.set("periodic", False)
                self._adopt(settings, password)
                self.toast("Imported settings from Déjà Dup")

            if is_repository(s.repository):
                # Same repository as Déjà Dup: we only need its password.
                def unlocked(password, data):
                    s.encrypted = bool(password)
                    finished(s, password)
                    self._on_snapshots(data, None)

                def tried_without_password(data, error):
                    if error is None:
                        # Déjà Dup lets you skip encryption; such repos open without a password.
                        unlocked("", data)
                        self.toast("This backup is not encrypted")
                        return
                    PasswordDialog(self.restic, s.repository, unlocked,
                                   body="Enter the password you chose in Déjà Dup. Déjà Dup keeps it in its own "
                                        "private keyring, so it can't be imported automatically.").present(self)

                self.restic.snapshots(s.repository, "", tried_without_password)
            else:
                SetupDialog(self.restic, s, finished, title="Import from Déjà Dup",
                            notes=result.notes).present(self)

        dialog.connect("response", answered)
        dialog.present(self)

    def start_setup(self, title="Set Up Backups"):
        base = self.settings if self.settings.configured else Settings()
        SetupDialog(self.restic, base, self._adopt, title=title).present(self)

    def _adopt(self, settings, password):
        old_repo = self.settings.repository
        self.settings = settings
        self.settings.save()
        self.password = password
        if settings.repository != old_repo:
            self.snapshots = self.raw_stats = None
        if password:
            keyring.store(settings.repository, password)
        self.show_page()

    def edit_settings(self):
        if not self.settings.configured:
            return
        PreferencesDialog(self.settings, self.dd, on_changed=self._on_settings_changed,
                          on_change_location=lambda: self.start_setup("Change Location"),
                          on_forget_password=self._forget_password).present(self)

    def _on_settings_changed(self):
        self._sync_autostart()
        self._render_all()

    def _sync_autostart(self):
        wanted = self.settings.configured and self.settings.periodic
        if wanted != getattr(self, "_autostart", None):
            self._autostart = wanted
            from .app import request_autostart
            request_autostart(self if self.get_visible() else None, wanted)

    def _forget_password(self):
        keyring.forget(self.settings.repository)
        self.password = None
        self._render_all()

    # Password & refresh --------------------------------------------------------

    def unlock_and_refresh(self):
        if not self.settings.encrypted:
            self.password = ""
            self.refresh()
            return
        if self.password is not None:
            self.refresh()
            return

        def found(password):
            if password is None:
                self._render_all()
                return
            self.password = password
            self.refresh()
        keyring.lookup(self.settings.repository, found)

    def ask_password(self):
        def unlocked(password, data):
            self.password = password
            keyring.store(self.settings.repository, password)
            self._on_snapshots(data, None)
            self.refresh()
        PasswordDialog(self.restic, self.settings.repository, unlocked).present(self)

    def refresh(self):
        self._query_filesystem()
        if self.password is None or self._fetching or not self.restic.available:
            self._render_all()
            return
        if not is_repository(self.settings.repository):
            self.stats_error = "unreachable"
            self._render_all()
            return
        self._fetching = True
        self._render_all()
        self._cancellable = Gio.Cancellable()
        self.restic.snapshots(self.settings.repository, self.password, self._on_snapshots, self._cancellable)

    def _on_snapshots(self, data, error):
        if error is not None:
            self._fetching = False
            if error.wrong_password:
                self.password = None
                keyring.forget(self.settings.repository)
            self.stats_error = str(error)
            self._render_all()
            return
        self.stats_error = None
        self.snapshots = sorted(data or [], key=lambda s: s.get("time", ""))
        self._sync_last_backup()
        self._save_stats_cache()
        self._render_all()
        if self._fetching:
            self.restic.raw_stats(self.settings.repository, self.password, self._on_raw_stats, self._cancellable)

    def _on_raw_stats(self, data, error):
        self._fetching = False
        if error is None:
            self.raw_stats = data
            self._save_stats_cache()
        self._render_all()

    def _sync_last_backup(self):
        """Pick up backups made by Déjà Dup (or another machine's scheduler)."""
        ours = [s for s in self.snapshots if s.get("hostname") == GLib.get_host_name()]
        if not ours:
            return
        latest = parse_time((ours[-1].get("summary") or {}).get("backup_end") or ours[-1].get("time"))
        current = self.settings.last_backup_time
        if latest and (current is None or latest > current):
            self.settings.last_backup = latest.isoformat()
            self.settings.save()

    def _query_filesystem(self):
        path = self.settings.repository
        if not path or not os.path.isdir(path):
            self.fs_info = None
            return

        def done(file, result):
            try:
                info = file.query_filesystem_info_finish(result)
                self.fs_info = (info.get_attribute_uint64("filesystem::size"),
                                info.get_attribute_uint64("filesystem::free"))
            except GLib.Error:
                self.fs_info = None
            self._render_tiles()
        Gio.File.new_for_path(path).query_filesystem_info_async(
            "filesystem::size,filesystem::free", GLib.PRIORITY_DEFAULT, None, done)

    def _load_stats_cache(self):
        try:
            with open(STATS_CACHE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("repository") == self.settings.repository:
            self.snapshots = data.get("snapshots")
            self.raw_stats = data.get("raw_stats")

    def _save_stats_cache(self):
        os.makedirs(os.path.dirname(STATS_CACHE), exist_ok=True)
        with open(STATS_CACHE, "w") as f:
            json.dump({"repository": self.settings.repository, "snapshots": self.snapshots,
                       "raw_stats": self.raw_stats, "fetched": now().isoformat()}, f)

    # Backups -------------------------------------------------------------------

    def backup_now(self, automatic=False):
        if self.job is not None or (self.activity and not self.activity.ours):
            return
        if self.password is None:
            if not automatic:
                self.ask_password()
            return
        if not self.restic.available:
            self.toast("restic is not installed")
            return
        self.settings.last_attempt = now().isoformat()
        self.settings.save()
        self.job = BackupJob(self.restic, self.settings, self.password, self._on_job_update, self._on_job_done)
        self.get_application().hold()
        self.job.start()
        self._render_all()

    def _on_job_update(self, job):
        self._render_live()

    def _on_job_done(self, job, ok, error):
        self.job = None
        self.get_application().release()
        if ok:
            self.settings.last_backup = now().isoformat()
            self.settings.last_error = ""
            message = "Backup finished"
            if job.warnings:
                message += f" with {len(job.warnings)} warning{'s' if len(job.warnings) != 1 else ''}"
            self.toast(message)
            self._notify("Backup finished", message if job.warnings else "Your files are safe.")
        elif error:
            self.settings.last_error = error
            self.toast("Backup failed")
            self._notify("Backup failed", error)
        else:
            self.toast("Backup stopped")
        self.settings.save()
        self.refresh()

    def _notify(self, title, body):
        if self.is_visible():
            return
        note = Gio.Notification.new(title)
        note.set_body(body)
        self.get_application().send_notification("backup", note)

    def _scheduler(self):
        s = self.settings
        if s.configured and s.periodic and self.job is None and self.activity is None:
            due = s.next_backup_time
            last_attempt = parse_time(s.last_attempt)
            retry_ok = last_attempt is None or now() - last_attempt > timedelta(hours=1)
            if due is not None and due <= now() and retry_ok and is_repository(s.repository):
                self.backup_now(automatic=True)
        return GLib.SOURCE_CONTINUE

    def _on_close(self, _win):
        if self.job is not None or (self.settings.configured and self.settings.periodic):
            # Keep running in the background for the schedule / the running job.
            self.set_visible(False)
            app = self.get_application()
            if not getattr(app, "background_held", False):
                app.hold()
                app.background_held = True
            return True
        return False

    # Ticks ---------------------------------------------------------------------

    def _tick(self):
        try:
            self.activity = self._monitor.sample()
        except OSError:
            self.activity = None
        active = self.activity is not None or self.job is not None
        if active or self._was_active:
            self._render_live()
            self._render_hero()
        if self._was_active and not active:
            self.refresh()   # a backup (ours or Déjà Dup's) just finished
        self._was_active = active
        return GLib.SOURCE_CONTINUE

    def _slow_tick(self):
        if self.is_visible() and self.settings.configured:
            self._render_hero()
            self._render_tiles()
        return GLib.SOURCE_CONTINUE

    # Rendering -----------------------------------------------------------------

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    def _banner_action(self):
        action = getattr(self, "_banner_cb", None)
        if action:
            action()

    def _set_banner(self, title=None, button=None, action=None):
        self._banner_cb = action
        if title is None:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(title)
        self.banner.set_button_label(button)
        self.banner.set_revealed(True)

    def _render_all(self):
        if not self.settings.configured:
            return
        if not self.restic.available:
            self._set_banner("restic was not found. Install restic or Déjà Dup to use this app.")
        elif self.password is None and self.settings.encrypted:
            self._set_banner("Enter your backup password to see statistics and back up", "Unlock",
                             self.ask_password)
        elif self.stats_error == "unreachable" or not os.path.isdir(self.settings.repository):
            self._set_banner("The backup location isn't available. Is the drive or network share mounted?",
                             "Retry", self.refresh)
        elif self.stats_error:
            self._set_banner(f"Couldn't read the backup: {self.stats_error}", "Retry", self.refresh)
        else:
            self._set_banner(None)
        self.refresh_btn.set_sensitive(not self._fetching)
        self._render_hero()
        self._render_live()
        self._render_tiles()
        self._render_breakdown()
        self._render_charts()
        self._render_snapshots()
        self._render_config()

    def _latest(self):
        return self.snapshots[-1] if self.snapshots else None

    def _render_hero(self):
        s = self.settings
        last = s.last_backup_time
        icon_classes = ("good", "warning", "error", "accent")
        for c in icon_classes:
            self.hero_icon.remove_css_class(c)

        running = self.job is not None or self.activity is not None
        failed = s.last_error and (last is None or (parse_time(s.last_attempt) or last) > last)
        overdue = s.periodic and s.next_backup_time and now() > s.next_backup_time + timedelta(days=1)
        if running:
            state, icon, title = "accent", "view-refresh-symbolic", "Backing Up…"
        elif last is None:
            state, icon, title = "error", "dialog-error-symbolic", "No Backups Yet"
        elif failed:
            state, icon, title = "warning", "dialog-warning-symbolic", "Last Backup Failed"
        elif overdue:
            state, icon, title = "warning", "dialog-warning-symbolic", "Backup Overdue"
        else:
            state, icon, title = "good", "object-select-symbolic", "Backups Are Up to Date"
        self.hero_icon.set_from_icon_name(icon)
        self.hero_icon.add_css_class(state)
        self.hero_title.set_label(title)

        parts = [f"Last backup {fmt_ago(last)}" if last else "Your files are not backed up yet"]
        if s.periodic:
            parts.append(f"next {fmt_until(s.next_backup_time)}" if last else "starting soon")
        else:
            parts.append("automatic backups are off")
        self.hero_sub.set_label(" · ".join(parts))
        self.hero_error.set_visible(bool(failed) and not running)
        self.hero_error.set_label(s.last_error or "")
        self.backup_btn.set_sensitive(not running and self.restic.available)

    def _render_live(self):
        job, act = self.job, self.activity
        if job is None and act is None:
            self.live_revealer.set_reveal_child(False)
            return
        self.live_revealer.set_reveal_child(True)
        self.cancel_btn.set_visible(job is not None)

        if job is not None:
            title = {"creating": "Creating Backup…", "backing-up": "Backing Up…",
                     "cleaning-up": "Removing Old Backups…"}.get(job.phase, "Preparing…")
            started = job.started
            sub = f"Saving to {describe_path(self.settings.repository)}"
        else:
            who = "Déjà Dup" if act.engine in ("restic", "duplicity") else act.engine
            title = f"{who} is running {act.operation}"
            started = act.started
            sub = "Started outside this app"
        self.live_title.set_label(title)
        self.live_sub.set_label(sub)

        status = job.status if job is not None and job.phase == "backing-up" else {}
        elapsed = time.time() - started
        self.m_elapsed.set(fmt_duration(elapsed))
        if status:
            frac = float(status.get("percent_done") or 0)
            self.live_progress.set_fraction(min(frac, 1.0))
            files_done, total_files = status.get("files_done", 0), status.get("total_files", 0)
            self.live_progress_text.set_label(
                f"{frac * 100:.0f}% · {fmt_bytes(status.get('bytes_done', 0))} of "
                f"{fmt_bytes(status.get('total_bytes', 0))} · {fmt_count(files_done)} of {fmt_count(total_files)} files"
                + (f" · {len(job.warnings)} skipped" if job.warnings else ""))
            remaining = status.get("seconds_remaining")
            self.m_eta.set(fmt_duration(remaining) if remaining else "estimating…")
            current = status.get("current_files") or []
            self.live_files.set_label("\n".join(describe_path(f) for f in current[:2]))
        else:
            # No progress feed: estimate from how long the previous backup took.
            latest = self._latest()
            typical = _snapshot_duration(latest) if latest else None
            if typical and job is None and act and act.operation == "backup":
                self.live_progress.set_fraction(min(elapsed / typical, 0.99))
                self.live_progress_text.set_label(f"Estimated from the last backup, which took {fmt_duration(typical)}")
                self.m_eta.set(f"~{fmt_duration(max(typical - elapsed, 0))}")
            else:
                self.live_progress.pulse()
                self.live_progress_text.set_label("")
                self.m_eta.set("—")
            self.live_files.set_label(describe_path(act.current_file) if act and act.current_file else "")

        if act is not None:
            self.m_read.set(fmt_rate(act.read_rate))
            self.m_write.set(fmt_rate(act.write_rate))
            self.m_read.spark.set_values(act.read_history)
            self.m_write.spark.set_values(act.write_history)
            self.m_cpu.set(f"{act.cpu_percent:.0f}%", "Load " + " · ".join(f"{l:.2f}" for l in act.load))
            self.m_cpu.set_tooltip_text(f"Share of all {NUM_CPUS} cores used by the backup.\n"
                                        "System load average over 1, 5 and 15 minutes.")
            self.m_mem.set(fmt_bytes(act.rss))
            self.m_read.set_tooltip_text(f"{fmt_bytes(act.read_total)} read in total")
            self.m_write.set_tooltip_text(f"{fmt_bytes(act.write_total)} written in total")
        else:
            for m in (self.m_read, self.m_write, self.m_cpu, self.m_mem):
                m.set("—")

    def _render_tiles(self):
        t = self.tiles
        snaps = self.snapshots or []
        latest = self._latest()
        summary = (latest or {}).get("summary") or {}
        loading = "Loading…" if self._fetching else ""

        raw = self.raw_stats or {}
        if raw.get("total_size") is not None:
            ratio = raw.get("compression_ratio")
            t["size"].set(fmt_bytes(raw["total_size"]),
                          f"{fmt_bytes(raw.get('total_uncompressed_size'))} before compression" if ratio else "")
        else:
            t["size"].set("—", loading or "Unlock to see")

        if summary:
            t["protected"].set(fmt_bytes(summary.get("total_bytes_processed")), "in the latest snapshot")
            t["files"].set(fmt_count(summary.get("total_files_processed")),
                           f"{fmt_count(summary.get('files_new', 0))} new, "
                           f"{fmt_count(summary.get('files_changed', 0))} changed")
            t["added"].set(fmt_bytes(summary.get("data_added_packed", summary.get("data_added"))),
                           f"new data stored {fmt_ago(parse_time(summary.get('backup_end')))}")
            durations = [d for d in (_snapshot_duration(s) for s in snaps[-10:]) if d]
            avg = sum(durations) / len(durations) if durations else None
            t["duration"].set(fmt_duration(_snapshot_duration(latest)),
                              f"average {fmt_duration(avg)}" if avg else "")
        else:
            for key in ("protected", "files", "added", "duration"):
                t[key].set("—", loading)

        if snaps:
            oldest = parse_time(snaps[0].get("time"))
            t["snapshots"].set(fmt_count(len(snaps)), f"oldest from {fmt_date(oldest)}")
        else:
            t["snapshots"].set("—" if self.snapshots is None else "0", loading)

        last = self.settings.last_backup_time
        t["age"].set(fmt_ago(last).replace(" ago", "") if last else "Never",
                     fmt_date(last) if last else "")

        if self.fs_info and self.fs_info[0]:
            size, free = self.fs_info
            t["free"].set(fmt_bytes(free), f"{100 * free / size:.0f}% of {fmt_bytes(size)}")
        else:
            t["free"].set("—", "location not available")

    def _render_breakdown(self):
        latest = self._latest()
        summary = (latest or {}).get("summary") or {}
        self.breakdown_card.set_visible(bool(summary))
        if not summary:
            return
        values = {k: summary.get(f"files_{k}", 0) for k in ("new", "changed", "unmodified")}
        self.breakdown.set_segments([
            (values[k], i, f"{fmt_count(values[k])} {self.legend[k][1].lower()} files")
            for i, k in enumerate(("new", "changed", "unmodified"))])
        for key, (lbl, name) in self.legend.items():
            lbl.set_label(f"{name} {fmt_count(values[key])}")
        self.breakdown_total.set_label(f"{fmt_count(sum(values.values()))} files")
        self.breakdown_dirs.set_label(
            f"Folders: {fmt_count(summary.get('dirs_new', 0))} new · {fmt_count(summary.get('dirs_changed', 0))} "
            f"changed · {fmt_count(summary.get('dirs_unmodified', 0))} unchanged")

    def _render_charts(self):
        snaps = [s for s in (self.snapshots or []) if s.get("summary")][-HISTORY_BARS:]
        self.charts_box.set_visible(bool(snaps))
        added, durations = [], []
        for s in snaps:
            summary = s["summary"]
            t = parse_time(s.get("time"))
            label = t.astimezone().strftime("%d %b") if t else ""
            when = GLib.markup_escape_text(fmt_date(t))
            data = summary.get("data_added_packed", summary.get("data_added", 0))
            added.append((label, data, f"<b>{when}</b>\n{fmt_bytes(data)} added"))
            d = _snapshot_duration(s) or 0
            durations.append((label, d, f"<b>{when}</b>\nTook {fmt_duration(d)}"))
        self.added_chart.set_items(added)
        self.duration_chart.set_items(durations)

    def _render_snapshots(self):
        for row in self._snap_rows:
            self.snap_group.remove(row)
        self._snap_rows = []
        snaps = self.snapshots or []
        self.snap_group.set_visible(bool(snaps))
        if not snaps:
            return
        host = GLib.get_host_name()
        self.snap_group.set_description(f"{len(snaps)} snapshots in total")
        for s in reversed(snaps[-SNAPSHOT_ROWS:]):
            t = parse_time(s.get("time"))
            summary = s.get("summary") or {}
            bits = []
            if summary:
                bits.append(f"{fmt_bytes(summary.get('data_added_packed', summary.get('data_added')))} added")
                bits.append(f"{fmt_count(summary.get('total_files_processed'))} files")
                bits.append(f"took {fmt_duration(_snapshot_duration(s))}")
            if s.get("hostname") and s["hostname"] != host:
                bits.append(f"from {s['hostname']}")
            row = Adw.ActionRow(title=GLib.markup_escape_text(fmt_date(t)),
                                subtitle=GLib.markup_escape_text(" · ".join(bits)))
            row.add_suffix(_label(s.get("short_id", s.get("id", "")[:8]), ("caption", "monospace", "dim-label")))
            self.snap_group.add(row)
            self._snap_rows.append(row)

    def _render_config(self):
        if not self.settings.configured:
            return False
        s, r = self.settings, self.cfg_rows
        esc = GLib.markup_escape_text
        r["location"].set_subtitle(esc(s.repository))
        r["schedule"].set_subtitle(("Daily" if s.period_days == 1 else f"Every {fmt_days(s.period_days)}")
                                   if s.periodic else "Manual only")
        r["keep"].set_subtitle("Forever" if s.keep_days == 0 else f"At least {fmt_days(s.keep_days)}")
        r["includes"].set_subtitle(esc(", ".join(describe_path(p) for p in s.includes) or "Nothing"))
        r["excludes"].set_subtitle(esc(", ".join(describe_path(p) for p in s.excludes) or "Nothing"))
        latest = self._latest()
        engine = (latest or {}).get("program_version") or "restic"
        extra = []
        if not s.encrypted:
            extra.append("not encrypted")
        if self.dd.available:
            extra.append("Déjà Dup schedule " + ("on" if self.dd.periodic else "off"))
        r["engine"].set_subtitle(esc(" · ".join([engine] + extra)))
        return False
