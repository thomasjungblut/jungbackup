"""Read-only summary of the backup settings, with an edit button."""

from gi.repository import Adw, GLib, Gtk

from ..core.paths import describe_path
from ..core.util import fmt_days

ROWS = (
    ("location", "Location", "folder-remote-symbolic"),
    ("schedule", "Schedule", "alarm-symbolic"),
    ("keep", "Keep", "document-open-recent-symbolic"),
    ("includes", "Backing Up", "folder-symbolic"),
    ("excludes", "Ignoring", "action-unavailable-symbolic"),
    ("engine", "Engine", "applications-system-symbolic"),
)


class SettingsSummary(Adw.PreferencesGroup):
    def __init__(self, on_edit):
        super().__init__(title="Configuration")
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit", css_classes=["flat"])
        edit.connect("clicked", lambda *_: on_edit())
        self.set_header_suffix(edit)
        self._rows = {}
        for key, title, icon in ROWS:
            row = Adw.ActionRow(title=title, subtitle_selectable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            self._rows[key] = row
            self.add(row)

    def update(self, settings, storage, latest, dd):
        s, r = settings, self._rows
        esc = GLib.markup_escape_text
        r["location"].set_subtitle(esc(storage.label))
        r["schedule"].set_subtitle(("Daily" if s.period_days == 1 else f"Every {fmt_days(s.period_days)}")
                                   if s.periodic else "Manual only")
        r["keep"].set_subtitle("Forever" if s.keep_days == 0 else f"At least {fmt_days(s.keep_days)}")
        r["includes"].set_subtitle(esc(", ".join(describe_path(p) for p in s.includes) or "Nothing"))
        r["excludes"].set_subtitle(esc(", ".join(describe_path(p) for p in s.excludes) or "Nothing"))
        engine = (latest or {}).get("program_version") or "restic"
        extra = []
        if not s.encrypted:
            extra.append("not encrypted")
        if dd.available:
            extra.append("Déjà Dup schedule " + ("on" if dd.periodic else "off"))
        r["engine"].set_subtitle(esc(" · ".join([engine] + extra)))
