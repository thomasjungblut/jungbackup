"""The status card at the top: are the backups up to date?"""

from datetime import timedelta

from gi.repository import Gtk

from ..core.util import fmt_ago, fmt_until, now, parse_time
from .widgets import label

STATES = ("good", "warning", "error", "accent")


class Hero(Gtk.Box):
    def __init__(self, on_backup):
        super().__init__(spacing=18, css_classes=["card", "hero"])
        self._icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER, css_classes=["hero-icon"])
        self.append(self._icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self._title = label("", ("title-2",), wrap=True)
        self._sub = label("", ("dim-label",), wrap=True)
        self._error = label("", ("caption", "error"), wrap=True, visible=False, selectable=True)
        text.append(self._title)
        text.append(self._sub)
        text.append(self._error)
        self.append(text)
        self._backup_btn = Gtk.Button(label="Back Up Now", valign=Gtk.Align.CENTER,
                                      css_classes=["pill", "suggested-action"])
        self._backup_btn.connect("clicked", lambda *_: on_backup())
        self.append(self._backup_btn)

    def update(self, settings, running, can_back_up):
        s = settings
        last = s.last_backup_time
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
        for c in STATES:
            self._icon.remove_css_class(c)
        self._icon.set_from_icon_name(icon)
        self._icon.add_css_class(state)
        self._title.set_label(title)

        parts = [f"Last backup {fmt_ago(last)}" if last else "Your files are not backed up yet"]
        if s.periodic and failed:
            parts.append(f"trying again {fmt_until(s.next_attempt_time)}")
        elif s.periodic:
            parts.append(f"next {fmt_until(s.next_backup_time)}" if last else "starting soon")
        else:
            parts.append("automatic backups are off")
        self._sub.set_label(" · ".join(parts))
        self._error.set_visible(bool(failed) and not running)
        self._error.set_label(s.last_error or "")
        self._backup_btn.set_sensitive(not running and can_back_up)
