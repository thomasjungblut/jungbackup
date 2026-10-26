"""Get files back out of a snapshot.

Every restore runs `restic restore <snapshot>:<folder> --target <dir>` with
an --include per chosen item, relative to that folder. Starting at a folder
instead of the snapshot's root means restic never touches anything above it:
no recreated /home, no attempt to change the owner or times of folders the
user didn't pick.
"""

import os
import signal

from gi.repository import GLib

from .job import _signal_restic_descendants
from .restic import EXIT_LOCKED, pattern_literal


def _existing_folder(path, isdir):
    while path != "/" and not isdir(path):
        path = os.path.dirname(path)
    return path


def _relative(path, folder):
    return path if folder == "/" else path[len(folder):]


def _grouped(pairs):
    groups = {}
    for (folder, target), include in pairs:
        groups.setdefault((folder, target), []).append(pattern_literal(include))
    return [(folder, target, includes) for (folder, target), includes in groups.items()]


def plan_in_place(paths, isdir=os.path.isdir):
    """Steps that put `paths` back where they were: [(folder in snapshot, target, includes)].

    Each step starts at the deepest folder that still exists on disk, so
    folders that are gone come back with their original permissions and times.
    """
    pairs = []
    for path in paths:
        folder = _existing_folder(os.path.dirname(path), isdir)
        pairs.append(((folder, folder), _relative(path, folder)))
    return _grouped(pairs)


def plan_copy(paths, target):
    """Steps that copy `paths` into the folder `target`, each under its own name."""
    pairs = []
    for path in paths:
        folder = os.path.dirname(path)
        pairs.append(((folder, target), _relative(path, folder)))
    return _grouped(pairs)


class RestoreJob:
    """Runs the steps of a plan one after the other.

    on_update(job) is called with progress; on_done(job, ok, error) at the end.
    """

    def __init__(self, restic, repo, password, snapshot, steps, on_update, on_done, explain=str):
        self._restic = restic
        self._repo = repo
        self._password = password
        self._snapshot = snapshot
        self._steps = list(steps)
        self._on_update = on_update
        self._on_done = on_done
        self._explain = explain
        self._job = None
        self._step = 0
        self._done_bytes = 0
        self.cancelled = False
        self.status = {}
        self.files_restored = 0
        self.total_bytes = 0
        self.bytes_restored = 0
        self.warnings = []

    @property
    def fraction(self):
        """0…1 over all steps; restic reports progress per step."""
        if not self._steps:
            return 1.0
        return min(1.0, (self._step + self.status.get("percent_done", 0)) / len(self._steps))

    def start(self):
        self._next()

    def cancel(self):
        self.cancelled = True
        if self._job is not None:
            pid = self._job.proc.get_identifier()
            if pid is None or not _signal_restic_descendants(int(pid), signal.SIGINT):
                self._job.cancel()

    def _next(self):
        if self.cancelled:
            self._on_done(self, False, None)
            return
        if self._step >= len(self._steps):
            self._on_done(self, True, None)
            return
        folder, target, includes = self._steps[self._step]
        args = ["restore", f"{self._snapshot}:{folder}", f"--target={target}",
                *(f"--include={pattern}" for pattern in includes)]
        self.status = {}
        try:
            self._job = self._restic.stream(self._repo, self._password, args, self._on_message, self._after_step,
                                            nice=False)
        except GLib.Error as e:
            self._on_done(self, False, f"Could not start restic: {e.message}")

    def _on_message(self, msg):
        kind = msg.get("message_type")
        if kind == "status":
            self.status = msg
            self.total_bytes = self._done_bytes + msg.get("total_bytes", 0)
            self.bytes_restored = self._done_bytes + msg.get("bytes_restored", 0)
            self._on_update(self)
        elif kind == "summary":
            self.files_restored += msg.get("files_restored", 0)
            self._done_bytes += msg.get("total_bytes", 0)
            self.total_bytes = self.bytes_restored = self._done_bytes
        elif kind == "error":
            item = msg.get("item") or ""
            error = msg.get("error", {})
            text = error.get("message") if isinstance(error, dict) else str(error)
            self.warnings.append(f"{item}: {text}" if item else str(text))

    def _after_step(self, status, error):
        self._job = None
        if self.cancelled:
            self._on_done(self, False, None)
            return
        if status == EXIT_LOCKED:
            self._on_done(self, False, "The backup is in use by another program (maybe a cleanup). Try again later.")
            return
        if status != 0 and not self.warnings:
            self._on_done(self, False, self._explain(error) if error else f"restic exited with status {status}")
            return
        self._step += 1
        self.status = {}
        self._on_update(self)
        self._next()
