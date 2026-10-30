"""This app's own configuration.

Stored as JSON in the user config dir (inside a Flatpak that's
~/.var/app/<app-id>/config). On first start it can be filled from
Déjà Dup's settings, or set up from scratch.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from gi.repository import GLib

from .util import now, parse_time

PATH = os.path.join(GLib.get_user_config_dir(), "jungbackup", "settings.json")

# After a failed automatic backup, wait an hour before trying again, doubling
# with each further failure (Google Drive throttles uploads for hours, and its
# daily upload limit resets after a day).
RETRY_BASE = timedelta(hours=1)
RETRY_MAX = timedelta(days=1)


@dataclass
class Settings:
    repository: str = ""                       # path to the restic repository
    encrypted: bool = True
    includes: list = field(default_factory=lambda: ["$HOME"])
    excludes: list = field(default_factory=lambda: ["$TRASH", "$DOWNLOAD"])
    periodic: bool = True
    period_days: int = 1
    keep_days: int = 0                          # 0 = keep forever
    pack_size_mb: int = 0                       # restic --pack-size; 0 = restic's default (16 MiB)
    compression: str = "auto"                   # restic --compression: auto, off, fastest, better, max
    read_concurrency: int = 0                   # restic backup --read-concurrency; 0 = default (2)
    last_backup: str = ""                       # ISO 8601, last successful backup
    last_attempt: str = ""
    last_error: str = ""
    failed_attempts: int = 0                    # failures since the last successful backup
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
        return last + timedelta(days=self.period_days)

    @property
    def retry_delay(self):
        return min(RETRY_BASE * 2 ** max(self.failed_attempts - 1, 0), RETRY_MAX)

    @property
    def next_attempt_time(self):
        """When the scheduler will next start a backup: when it's due, but not before the retry delay."""
        due = self.next_backup_time
        attempt = parse_time(self.last_attempt)
        if due is None or attempt is None:
            return due
        return max(due, attempt + self.retry_delay)

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
