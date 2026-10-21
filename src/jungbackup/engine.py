"""Back up with restic, the same way Déjà Dup does.

Snapshots are tagged and grouped exactly like Déjà Dup's, so either app can
restore and prune backups made by the other.
"""

import os
import signal
import time

from gi.repository import GLib

from .dejadup import expand_path
from .restic import EXIT_INCOMPLETE, EXIT_LOCKED, is_empty_or_missing, is_repository


def _escape_pattern(path):
    return path.replace("$", "$$")


def _escape_path(path):
    escaped = path.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?").replace("[", "\\[")
    return _escape_pattern(escaped)


def _prefix_key(pattern):
    """Sort parents before children, ignoring '!' and treating '/*' as '/'."""
    if pattern.startswith("!"):
        pattern = pattern[1:]
    if pattern.endswith("/*"):
        pattern = pattern[:-1]
    return pattern


def include_exclude_args(includes, excludes):
    """Mirror Déjà Dup's nested include/exclude handling (restic issue #3408)."""
    def resolve(paths):
        expanded = (expand_path(p) for p in paths)
        return list(dict.fromkeys(os.path.realpath(p) for p in expanded if p and os.path.isabs(p)))
    inc = [p for p in resolve(includes) if os.path.exists(p)]
    exc = resolve(excludes)
    patterns = []
    for path in exc:
        if path not in inc:
            patterns += [_escape_path(path), _escape_path(path) + "/*"]
    for path in inc:
        patterns.append("!" + _escape_path(path))
    patterns.sort(key=_prefix_key)
    return inc, [f"--exclude={p}" for p in patterns]


def _signal_restic_descendants(root_pid, sig):
    """SIGINT the real restic process, which may sit behind `flatpak run`/`nice`."""
    parents = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        rpar = raw.rfind(b")")
        comm = raw[raw.find(b"(") + 1:rpar]
        ppid = int(raw[rpar + 2:].split()[1])
        parents[int(name)] = (ppid, comm)
    hit = False
    for pid, (_ppid, comm) in parents.items():
        if comm != b"restic":
            continue
        if pid == root_pid:
            os.kill(pid, sig)
            return True
        p, depth = pid, 0
        while p in parents and depth < 32:
            if p == root_pid:
                try:
                    os.kill(pid, sig)
                    hit = True
                except OSError:
                    pass
                break
            p, depth = parents[p][0], depth + 1
    return hit


class BackupJob:
    """init (if needed) → backup → forget old snapshots."""

    def __init__(self, restic, settings, password, on_update, on_done):
        self._restic = restic
        self._settings = settings
        self._password = password
        self._on_update = on_update
        self._on_done = on_done
        self._job = None
        self.cancelled = False
        self.phase = "preparing"
        self.status = {}
        self.summary = None
        self.started = time.time()
        self.warnings = []

    def start(self):
        repo = self._settings.repository
        if is_repository(repo):
            self._backup()
        elif is_empty_or_missing(repo):
            self._set_phase("creating")
            self._restic.init(repo, self._password, self._after_init)
        else:
            self._finish(False, "The backup folder isn't empty and doesn't contain a restic backup.")

    def cancel(self):
        self.cancelled = True
        if self._job is not None:
            pid = self._job.proc.get_identifier()
            if pid is None or not _signal_restic_descendants(int(pid), signal.SIGINT):
                self._job.cancel()

    def _set_phase(self, phase):
        self.phase = phase
        self._on_update(self)

    def _after_init(self, _data, error):
        if error:
            self._finish(False, f"Could not create the backup: {error}")
        elif self.cancelled:
            self._finish(False, None)
        else:
            self._backup()

    def _backup(self):
        includes, exclude_args = include_exclude_args(self._settings.includes, self._settings.excludes)
        if not includes:
            self._finish(False, "None of the folders to back up exist.")
            return
        host = GLib.get_host_name()
        args = ["backup", f"--host={host}", "--tag=deja-dup", "--group-by=host,tags",
                "--exclude-caches", "--exclude-if-present=.deja-dup-ignore", *exclude_args, *includes]
        self._set_phase("backing-up")
        try:
            self._job = self._restic.stream(self._settings.repository, self._password, args,
                                            self._on_backup_message, self._after_backup)
        except GLib.Error as e:
            self._finish(False, f"Could not start restic: {e.message}")

    def _on_backup_message(self, msg):
        kind = msg.get("message_type")
        if kind == "status":
            self.status = msg
            self._on_update(self)
        elif kind == "summary":
            self.summary = msg
        elif kind == "error":
            item = msg.get("item") or ""
            error = msg.get("error", {})
            text = error.get("message") if isinstance(error, dict) else str(error)
            self.warnings.append(f"{item}: {text}" if item else str(text))

    def _after_backup(self, status, error):
        self._job = None
        if self.cancelled:
            self._finish(False, None)
            return
        if status == EXIT_LOCKED:
            self._finish(False, "The backup is in use by another program (maybe Déjà Dup). Try again later.")
            return
        if status not in (0, EXIT_INCOMPLETE):
            self._finish(False, error or f"restic exited with status {status}")
            return
        if status == EXIT_INCOMPLETE and not self.warnings:
            self.warnings.append("Some files could not be read and were skipped.")
        if self._settings.keep_days > 0:
            self._forget()
        else:
            self._finish(True, None)

    def _forget(self):
        host = GLib.get_host_name()
        args = ["forget", f"--host={host}", "--tag=deja-dup", "--group-by=host,tags", "--prune",
                f"--keep-within={self._settings.keep_days}d"]
        self._set_phase("cleaning-up")
        try:
            self._job = self._restic.stream(self._settings.repository, self._password, args,
                                            lambda _m: None, self._after_forget)
        except GLib.Error:
            self._finish(True, None)

    def _after_forget(self, status, error):
        self._job = None
        # Like Déjà Dup, cleaning up is best effort; the backup itself succeeded.
        if status not in (0,) and not self.cancelled:
            self.warnings.append(f"Old backups were not cleaned up: {error}")
        self._finish(True, None)

    def _finish(self, ok, error):
        self.phase = "done"
        self._on_done(self, ok, error)
