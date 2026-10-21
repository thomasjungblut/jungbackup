"""This app's own configuration.

Stored as JSON in the user config dir (inside a Flatpak that's
~/.var/app/<app-id>/config). On first start it can be filled from
Déjà Dup's settings, or set up from scratch.
"""

import json
import os
from dataclasses import asdict, dataclass, field

from gi.repository import GLib

from .util import now, parse_time

PATH = os.path.join(GLib.get_user_config_dir(), "jungbackup", "settings.json")


@dataclass
class Settings:
    repository: str = ""                       # path to the restic repository
    encrypted: bool = True
    includes: list = field(default_factory=lambda: ["$HOME"])
    excludes: list = field(default_factory=lambda: ["$TRASH", "$DOWNLOAD"])
    periodic: bool = True
    period_days: int = 1
    keep_days: int = 0                          # 0 = keep forever
    last_backup: str = ""                       # ISO 8601, last successful backup
    last_attempt: str = ""
    last_error: str = ""
    imported_from: str = ""                     # "deja-dup" when imported

    @property
    def configured(self):
        return bool(self.repository)

    @property
    def last_backup_time(self):
        return parse_time(self.last_backup)

    @property
    def next_backup_time(self):
        if not self.periodic:
            return None
        last = self.last_backup_time
        if last is None:
            return now()
        from datetime import timedelta
        return last + timedelta(days=self.period_days)

    @classmethod
    def load(cls):
        try:
            with open(PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @staticmethod
    def exists():
        return os.path.exists(PATH)

    def save(self):
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, PATH)


@dataclass
class ImportResult:
    settings: Settings
    notes: list


def import_from_deja_dup(dd):
    """Build Settings from a DejaDupConfig. Returns ImportResult or None."""
    if not dd.available:
        return None
    notes = []
    loc = dd.location()
    s = Settings(
        includes=dd.includes,
        excludes=dd.excludes,
        periodic=dd.periodic,
        period_days=dd.period_days,
        keep_days=dd.keep_days,
        imported_from="deja-dup",
    )
    if loc.path:
        s.repository = loc.path
    elif loc.backend == "local":
        s.repository = loc.label
        notes.append("The backup folder isn't reachable right now; mount it before the next backup.")
    else:
        notes.append(f"{loc.label} can't be used yet — choose a folder for your backups.")
    tool = dd.detect_tool(loc)
    if tool == "duplicity":
        s.repository = ""
        notes.append("Your Déjà Dup backups use the older Duplicity format, which this app can't read. "
                     "Choose a new folder to start a fresh restic backup.")
    last = dd.last_backup
    if last is not None:
        s.last_backup = last.isoformat()
    return ImportResult(s, notes)
