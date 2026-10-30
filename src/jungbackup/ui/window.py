"""The dashboard window: owns the state and flows, the sections render it."""

import json
import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..backup.job import BackupJob
from ..backup.monitor import ProcessMonitor
from ..backup.restic import Restic
from ..core import keyring
from ..core.paths import describe_path
from ..core.settings import Settings
from ..core.util import fmt_days, now, parse_time, snapshot_duration
from ..dejadup.config import DejaDupConfig
from ..dejadup.importer import import_from_deja_dup
from ..dejadup.launch import open_deja_dup
from ..storage import for_repository
from .hero import Hero
from .history import FilesBreakdown, HistoryCharts, SnapshotList, StatTiles
from .live import LiveCard
from .password import PasswordDialog
from .prefs import SettingsPage
from .restore import RestorePage
from .setup import SetupDialog
from .summary import SettingsSummary
from .welcome import WelcomePage

STATS_CACHE = os.path.join(GLib.get_user_cache_dir(), "jungbackup", "stats.json")
SCHEDULER_INTERVAL = 60


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
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.stack)
        self._build_actions()

        welcome = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self._menu_button())
        welcome.add_top_bar(header)
        self.welcome = WelcomePage(on_import=self.start_import, on_new=self.start_setup)
        welcome.set_content(self.welcome)
        self.stack.add_named(welcome, "welcome")
        self.stack.add_named(self._build_main(), "main")

        self._load_stats_cache()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.timeout_add_seconds(30, self._slow_tick)
        GLib.timeout_add_seconds(SCHEDULER_INTERVAL, self._scheduler)
        self.connect("close-request", self._on_close)
        self.show_page()

    # Construction ------------------------------------------------------------


    def _build_actions(self):
        for name, cb in (("reimport", lambda *_: self.start_import()),
                         ("open-deja-dup", lambda *_: open_deja_dup())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        show = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        show.connect("activate", lambda _a, name: self.pages.set_visible_child_name(name.get_string()))
        self.add_action(show)

    def _menu_button(self):
        menu = Gio.Menu()
        for items in ((("Home", "win.show-page::backup"),
                       ("Restore Files", "win.show-page::restore"),
                       ("Backup Settings", "win.show-page::settings")),
                      (("Re-Import from Déjà Dup…", "win.reimport"),
                       ("Open Déjà Dup", "win.open-deja-dup")),
                      (("About Jung Backup", "app.about"),
                       ("Quit", "app.quit"))):
            section = Gio.Menu()
            for title, action in items:
                section.append(title, action)
            menu.append_section(None, section)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu",
                              primary=True)

    def _build_main(self):
        """A hidden-by-default sidebar with the Backup and Restore views, which share the header bar and banner."""
        self.pages = Adw.ViewStack()
        self.restore = RestorePage(self.restic, self.toasts, on_unlock=self.ask_password,
                                   on_retry=self.refresh)
        self.pages.add_titled_with_icon(self._build_dashboard(), "backup", "Backup",
                                        "io.jungblut.JungBackup-symbolic")
        self.pages.add_titled_with_icon(self.restore, "restore", "Restore", "document-revert-symbolic")
        self.settings_page = SettingsPage(self.dd, on_changed=self._on_settings_changed,
                                          on_change_location=lambda: self.start_setup("Change Location"),
                                          on_forget_password=self._forget_password, toast=self.toast)
        self.pages.add_titled_with_icon(self.settings_page, "settings", "Settings", "emblem-system-symbolic")

        sidebar = Adw.ToolbarView()
        sidebar.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Jung Backup")))
        switcher = Adw.ViewSwitcherSidebar(stack=self.pages)
        # On narrow windows the sidebar covers the content; picking a view (even the current one) closes it.
        switcher.connect("activated", lambda *_: self.split.get_collapsed() and self.split.set_show_sidebar(False))
        sidebar.set_content(switcher)

        content = Adw.ToolbarView()
        content.add_top_bar(self._build_header())
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self._banner_action())
        content.add_top_bar(self.banner)
        content.set_content(self.pages)

        self.split = Adw.OverlaySplitView(sidebar=sidebar, content=content, show_sidebar=False)
        self.sidebar_btn.bind_property("active", self.split, "show-sidebar",
                                       GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        toggle = Gio.SimpleAction.new("toggle-sidebar", None)
        toggle.connect("activate", lambda *_: self.split.set_show_sidebar(not self.split.get_show_sidebar()))
        self.add_action(toggle)
        self.get_application().set_accels_for_action("win.toggle-sidebar", ["F9"])
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.restore.modified_column, "visible", False)
        self.add_breakpoint(narrow)
        self.pages.connect("notify::visible-child", lambda *_: self._page_changed())
        self._page_changed()
        return self.split

    def _build_header(self):
        self.title = Adw.WindowTitle()
        header = Adw.HeaderBar(title_widget=self.title)
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show Sidebar (F9)")
        header.pack_start(self.sidebar_btn)
        header.pack_end(self._menu_button())
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh Statistics")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        self.edit_btn = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit Backup Settings")
        self.edit_btn.connect("clicked", lambda *_: self.edit_settings())
        self._page_buttons = {"backup": [self.refresh_btn, self.edit_btn],
                              "restore": self.restore.header_start + self.restore.header_end}
        header.pack_start(self.refresh_btn)
        for button in self.restore.header_start:
            header.pack_start(button)
        for button in reversed(self.restore.header_end):
            header.pack_end(button)
        header.pack_end(self.edit_btn)
        return header

    def _page_changed(self):
        name = self.pages.get_visible_child_name()
        self.title.set_title(self.pages.get_page(self.pages.get_visible_child()).get_title())
        for page, buttons in self._page_buttons.items():
            for button in buttons:
                button.set_visible(page == name)
        if name == "restore":
            self.restore.shown()
        elif name == "settings" and self.settings.configured:
            self.settings_page.load(self.settings)

    def _build_dashboard(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=800)
        scroller.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=18, margin_bottom=32, margin_start=16, margin_end=16)
        clamp.set_child(box)

        self.hero = Hero(on_backup=self.backup_now)
        self.live = LiveCard(on_cancel=self._stop_job)
        self.tiles = StatTiles()
        self.breakdown = FilesBreakdown()
        self.charts = HistoryCharts()
        self.snapshot_list = SnapshotList()
        self.summary = SettingsSummary(on_edit=self.edit_settings)
        for section in (self.hero, self.live, self.tiles, self.breakdown, self.charts,
                        self.snapshot_list, self.summary):
            box.append(section)
        return scroller

    # Pages & flows -------------------------------------------------------------

    def show_page(self):
        if not self.settings.configured:
            result = import_from_deja_dup(self.dd)
            where = None
            if result is not None:
                s = result.settings
                where = for_repository(s.repository).label if s.repository else "a location this app can't use yet"
            self.welcome.offer_import(where)
            self.stack.set_visible_child_name("welcome")
            self.lookup_action("show-page").set_enabled(False)
            self.banner.set_revealed(False)
            return
        self.stack.set_visible_child_name("main")
        self.lookup_action("show-page").set_enabled(True)
        self.settings_page.load(self.settings)   # a new location or import replaces the settings
        self._sync_autostart()
        self._render_all()
        self.unlock_and_refresh()

    def start_import(self):
        result = import_from_deja_dup(self.dd)
        if result is None:
            self.toast("No Déjà Dup settings found")
            return
        s = result.settings
        body = (f"Location: {for_repository(s.repository).label if s.repository else '—'}\n"
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

            if for_repository(s.repository).has_repository():
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
        if self.settings.configured:
            self.pages.set_visible_child_name("settings")

    def _on_settings_changed(self):
        self._sync_autostart()
        self._render_all()

    def _sync_autostart(self):
        wanted = self.settings.configured and self.settings.periodic
        if wanted != getattr(self, "_autostart", None):
            self._autostart = wanted
            from ..app import request_autostart
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
        if self.storage.has_repository() is False:
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
            self.stats_error = self.storage.explain(error)
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

    @property
    def storage(self):
        return for_repository(self.settings.repository)

    def _query_filesystem(self):
        if not self.settings.repository:
            self.fs_info = None
            return

        def done(info):
            self.fs_info = info
            self._render_tiles()
        self.storage.query_space(done)

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
        self.job = BackupJob(self.restic, self.settings, self.password, self._on_job_update, self._on_job_done,
                             create=not automatic)
        self.get_application().hold()
        self.job.start()
        self._render_all()

    def _stop_job(self):
        if self.job is not None:
            self.job.cancel()
            self._render_live()

    def _on_job_update(self, job):
        self._render_live()

    def _on_job_done(self, job, ok, error):
        self.job = None
        self.get_application().release()
        if ok:
            self.settings.last_backup = now().isoformat()
            self.settings.last_error = ""
            self.settings.failed_attempts = 0
            message = "Backup finished"
            if job.warnings:
                message += f" with {len(job.warnings)} warning{'s' if len(job.warnings) != 1 else ''}"
            self.toast(message)
            self._notify("Backup finished", message if job.warnings else "Your files are safe.")
        elif error:
            self.settings.last_error = error
            self.settings.failed_attempts += 1
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
            due = s.next_attempt_time
            if due is not None and due <= now() and self.storage.has_repository() is not False:
                self.backup_now(automatic=True)
        return GLib.SOURCE_CONTINUE

    def _on_close(self, _win):
        if self.restore.busy:
            self.restore.confirm_stop(then=self.close)
            return True
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
        elif self.stats_error == "unreachable" or not self.storage.reachable():
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
        latest = self._latest()
        self.breakdown.update(latest)
        self.charts.update(self.snapshots)
        self.snapshot_list.update(self.snapshots)
        self.restore.set_backup(self.settings.repository, self.password, self.storage.explain)
        self.restore.set_snapshots(self.snapshots, self.stats_error if self.snapshots is None else None)
        self._render_config()

    def _latest(self):
        return self.snapshots[-1] if self.snapshots else None

    def _render_hero(self):
        running = self.job is not None or self.activity is not None
        self.hero.update(self.settings, running, self.restic.available)

    def _render_live(self):
        latest = self._latest()
        self.live.update(self.job, self.activity, self.storage.label,
                         snapshot_duration(latest) if latest else None)

    def _render_tiles(self):
        self.tiles.update(self.snapshots, self.raw_stats, self.fs_info, self.settings.last_backup_time,
                          self.storage, self._fetching)

    def _render_config(self):
        if self.settings.configured:
            self.summary.update(self.settings, self.storage, self._latest(), self.dd)
        return False
