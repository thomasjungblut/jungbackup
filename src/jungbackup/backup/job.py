"""Back up with restic.

For compatibility, snapshots use the same tag, host and grouping flags as
Déjà Dup, so either app can restore and prune backups made by the other.
"""

import os
import signal
import time

from gi.repository import GLib

from ..core.paths import expand_path
from ..storage import EXISTING, NEW, OCCUPIED, for_repository
from .restic import EXIT_INCOMPLETE, EXIT_LOCKED, pattern_literal

EXCLUDE, INCLUDE = 0, 1


def _literal(path):
    """Quote a path so restic's backup matches it literally.

    On top of the glob characters, backup expands environment variables in
    exclude patterns, so a literal $ is written as $$.
    """
    return pattern_literal(path).replace("$", "$$")


def _absolute_paths(entries):
    """Expand $HOME-style entries to unique, symlink-free absolute paths."""
    paths = []
    for entry in entries:
        path = expand_path(entry)
        if path and os.path.isabs(path):
            path = os.path.realpath(path)
            if path not in paths:
                paths.append(path)
    return paths


def include_exclude_args(includes, excludes):
    """Return (backup targets, --exclude arguments) for restic.

    restic checks exclude patterns in order and the last matching one wins;
    a leading "!" turns a pattern into an exception that keeps the file. To
    support folders picked inside ignored folders (and the other way around),
    each rule is ranked by how deep its path is, so the most specific rule for
    any file comes last. An ignored folder gets two rules: one for the folder
    itself and one for its contents, which is what lets a nested exception win.
    """
    targets = [path for path in _absolute_paths(includes) if os.path.exists(path)]
    rules = []
    for path in _absolute_paths(excludes):
        if path in targets:
            continue  # explicitly chosen for backup; that takes priority
        depth = path.count("/")
        rules.append((depth, EXCLUDE, _literal(path)))
        rules.append((depth + 1, EXCLUDE, _literal(path) + "/*"))
    for path in targets:
        rules.append((path.count("/"), INCLUDE, "!" + _literal(path)))
    rules.sort(key=lambda rule: rule[:2])
    return targets, [f"--exclude={pattern}" for _depth, _kind, pattern in rules]


def tuning_args(settings):
    """Global flags for commands that write packs (backup, prune)."""
    args = []
    if settings.pack_size_mb > 0:
        args.append(f"--pack-size={settings.pack_size_mb}")
    if settings.compression and settings.compression != "auto":
        args.append(f"--compression={settings.compression}")
    return args


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


def check_damage(summary):
    """What `restic check` found, from its JSON summary; None when it found nothing."""
    errors = int((summary or {}).get("num_errors") or 0)
    if not errors:
        return None
    broken = len((summary or {}).get("broken_packs") or [])
    text = f"Verification found {errors} error{'s' if errors != 1 else ''}"
    if not broken:
        return text + "."
    return text + f" in {broken} damaged file{'s' if broken != 1 else ''} of the backup. " \
        "Files stored in them can't be restored."


class BackupJob:
    """init (if needed) → backup → forget old snapshots → verify, when a check is due.

    With check_only it just verifies the backup that's there.
    """

    def __init__(self, restic, settings, password, on_update, on_done, create=True, check_only=False):
        self._restic = restic
        self._settings = settings
        self._storage = for_repository(settings.repository)
        self._create = create
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
        self.check_only = check_only
        self.check = None           # (damage or None, broken pack IDs) once a check ran to the end
        self._check_summary = None

    def start(self):
        self._storage.probe(self._restic, self._after_probe)

    def _after_probe(self, state, message):
        if self.cancelled:
            self._finish(False, None)
        elif state == EXISTING:
            self._check() if self.check_only else self._backup()
        elif state == NEW and self._create:
            self._set_phase("creating")
            self._restic.init(self._settings.repository, self._password, self._after_init)
        elif state == NEW:
            self._finish(False, "The backup wasn't found at its location anymore.")
        elif state == OCCUPIED:
            self._finish(False, "The backup folder isn't empty and doesn't contain a restic backup.")
        else:
            self._finish(False, message)

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
            self._finish(False, f"Could not create the backup: {self._storage.explain(error)}")
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
        args = ["backup", *tuning_args(self._settings), f"--host={host}", "--tag=deja-dup",
                "--group-by=host,tags", "--exclude-caches", "--exclude-if-present=.deja-dup-ignore"]
        if self._settings.read_concurrency > 0:
            args.append(f"--read-concurrency={self._settings.read_concurrency}")
        args += [*exclude_args, *includes]
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
            self._finish(False, self._storage.explain(error) if error else f"restic exited with status {status}")
            return
        if status == EXIT_INCOMPLETE and not self.warnings:
            self.warnings.append("Some files could not be read and were skipped.")
        if self._settings.keep_days > 0:
            self._forget()
        else:
            self._maybe_check()

    def _forget(self):
        host = GLib.get_host_name()
        args = ["forget", *tuning_args(self._settings), f"--host={host}", "--tag=deja-dup", "--group-by=host,tags",
                "--prune", f"--keep-within={self._settings.keep_days}d"]
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
            self.warnings.append(f"Old backups were not cleaned up: {self._storage.explain(error)}")
        if self.cancelled:
            self._finish(True, None)
        else:
            self._maybe_check()

    def _maybe_check(self):
        if self._settings.check_due:
            self._check()
        else:
            self._finish(True, None)

    def _check(self):
        args = ["check", *self._settings.check_subset()]
        self._set_phase("verifying")
        try:
            self._job = self._restic.stream(self._settings.repository, self._password, args,
                                            self._on_check_message, self._after_check)
        except GLib.Error as e:
            self._check_failed(f"Could not start restic: {e.message}")

    def _on_check_message(self, msg):
        if msg.get("message_type") == "summary":
            self._check_summary = msg

    def _after_check(self, status, error):
        self._job = None
        if self.cancelled:
            # Stopping a check doesn't undo the backup before it.
            self._finish(not self.check_only, None)
            return
        damage = check_damage(self._check_summary)
        if status == 0 or damage:
            self.check = (damage, (self._check_summary or {}).get("broken_packs") or [])
            self._finish(True, None)
        elif status == EXIT_LOCKED:
            self._check_failed("The backup is in use by another program (maybe Déjà Dup).")
        else:
            self._check_failed(self._storage.explain(error) if error else f"restic exited with status {status}")

    def _check_failed(self, reason):
        """The check couldn't run to the end; that says nothing about the backup, so try again next time."""
        if self.check_only:
            self._finish(False, f"The backup could not be verified: {reason}")
        else:
            self.warnings.append(f"The backup could not be verified: {reason}")
            self._finish(True, None)

    def _finish(self, ok, error):
        self.phase = "done"
        self._on_done(self, ok, error)
