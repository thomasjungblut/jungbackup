"""This app's own configuration.

Stored as JSON in the user config dir (inside a Flatpak that's
~/.var/app/<app-id>/config). On first start it can be filled from
Déjà Dup's settings, or set up from scratch.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import NamedTuple

from gi.repository import GLib

from .util import now, parse_time

PATH = os.path.join(GLib.get_user_config_dir(), "jungbackup", "settings.json")

# After a failed automatic backup, wait an hour before trying again, doubling
# with each further failure (Google Drive throttles uploads for hours, and its
# daily upload limit resets after a day).
RETRY_BASE = timedelta(hours=1)
RETRY_MAX = timedelta(days=1)
CHECK_HISTORY = 20      # checks remembered for the Verify page
CHECK_FINDINGS = 200    # restic error messages kept from a check


class CheckResult(NamedTuple):
    """What a `restic check` that ran to the end found. A clean check is None, not one of these."""
    damage: str                 # one-line summary, e.g. "Verification found 2 errors in 1 damaged file …"
    errors: int = 1             # restic's num_errors
    broken: tuple = ()          # damaged pack IDs, for `restic repair packs`
    findings: tuple = ()        # restic's own error messages, without its repair advice
    hints: tuple = ()           # "repair-index", "prune": non-critical fixes restic suggests


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
    drive_client: str = ""                      # Google OAuth client a Drive backup was made with; "" = rclone's
    check_days: int = 0                         # verify the backup's data every N days; 0 = off (opt-in)
    check_parts: int = 20                       # each check reads 1/N of the data (restic --read-data-subset)
    check_part: int = 1                         # which 1/N the next check reads, so N checks cover all of it
    last_check: str = ""                        # ISO 8601, last check that ran to the end (damage found or not)
    check_suspect: bool = False                 # the last check found errors, not yet confirmed by a second one
    check_damaged: bool = False                 # two checks in a row found errors
    check_broken: list = field(default_factory=list)   # damaged pack IDs, for `restic repair packs`
    check_error: str = ""                       # one-line summary of what the last check found
    check_errors: int = 0                       # how many errors restic counted in the last check
    check_findings: list = field(default_factory=list)  # restic's error messages from the last check
    check_hints: list = field(default_factory=list)     # non-critical repairs restic suggested
    check_read: str = ""                        # what the last check read: "3/20" or "all"
    check_failure: str = ""                     # why the last attempt couldn't run to the end; "" if it did
    check_history: list = field(default_factory=list)   # newest first: {time, read, result, text}

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

    @property
    def last_check_time(self):
        return parse_time(self.last_check)

    @property
    def check_due(self):
        """Verify after this backup? Always while the last check found errors, to confirm them or see them gone."""
        if self.check_suspect or self.check_damaged:
            return True
        if self.check_days <= 0:
            return False
        last = self.last_check_time
        return last is None or now() >= last + timedelta(days=self.check_days)

    @property
    def check_reads_all(self):
        """Does a check read all of the data? Rotating through parts only makes sense on a schedule; a
        check started by hand (or re-checking errors) with automatic checks off reads everything."""
        return self.check_parts <= 1 or self.check_days <= 0

    def check_subset(self):
        """restic check flags for the next check: one rotating part of the data, or all of it."""
        if self.check_reads_all:
            return ["--read-data"]
        part = (self.check_part - 1) % self.check_parts + 1
        return [f"--read-data-subset={part}/{self.check_parts}"]

    def next_check_read(self):
        """What the next check reads: "3/20", or "all"."""
        if self.check_reads_all:
            return "all"
        return f"{(self.check_part - 1) % self.check_parts + 1}/{self.check_parts}"

    def _remember_check(self, read, result, text):
        entry = {"time": now().isoformat(), "read": read, "result": result, "text": text}
        self.check_history = [entry, *self.check_history][:CHECK_HISTORY]

    def record_check(self, result=None):
        """A check ran to the end; `result` is a CheckResult with the errors it found, or None.

        A read error can be a fluke (the network, flaky RAM), so errors only count as damage once a second
        check finds them too. Until then the next check reads the same part again, and it moves on only
        once that part is clean.
        """
        read = self.next_check_read()
        self.last_check = now().isoformat()
        self.check_read = read
        self.check_failure = ""
        if result is None:
            self.check_suspect = self.check_damaged = False
            self.check_error = ""
            self.check_errors = 0
            self.check_broken = []
            self.check_findings = []
            self.check_hints = []
            if not self.check_reads_all:
                self.check_part = self.check_part % self.check_parts + 1
            self._remember_check(read, "clean", "No errors found")
            return
        if self.check_suspect or self.check_damaged:
            self.check_suspect, self.check_damaged = False, True
        else:
            self.check_suspect = True
        self.check_error = result.damage
        self.check_errors = result.errors
        self.check_broken = list(result.broken)
        self.check_findings = list(result.findings)[:CHECK_FINDINGS]
        self.check_hints = list(result.hints)
        self._remember_check(read, "damaged" if self.check_damaged else "suspect", result.damage)

    def record_check_failure(self, reason):
        """A check couldn't run to the end. That says nothing about the data, so the state stays as it was."""
        self.check_failure = reason
        self._remember_check(self.next_check_read(), "failed", reason)

    @classmethod
    def load(cls):
        try:
            with open(PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        settings = cls(**known)
        if ("check_findings" not in data and not settings.check_broken
                and (settings.check_damaged or settings.check_suspect)):
            # Before restic's messages were kept, a check that couldn't even start (say, an unreadable
            # lock file) was taken for damage. Nothing shows it was real, so let the next check decide.
            settings.check_damaged = settings.check_suspect = False
            settings.check_error = ""
            settings.last_check = ""
        return settings

    @staticmethod
    def exists():
        return os.path.exists(PATH)

    def save(self):
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, PATH)
