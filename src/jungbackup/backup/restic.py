"""Run restic: quick read-only queries and long-running streamed jobs.

The password is always fed through stdin (restic reads it from there when
stdin is not a terminal), so it never shows up in argv or the environment.
"""

import json
import os
import shutil
import signal
import subprocess

from gi.repository import Gio, GLib

from ..storage import for_repository

DEJA_DUP_APP = "org.gnome.DejaDup"
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "jungbackup", "restic")

EXIT_REPO_MISSING = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12
EXIT_INCOMPLETE = 3      # backup finished, but some files could not be read


def pattern_literal(path):
    """Quote a path for restic's --include/--exclude, which use Go's filepath.Match syntax."""
    for special in ("\\", "*", "?", "["):
        path = path.replace(special, "\\" + special)
    return path


class ResticError(Exception):
    def __init__(self, message, code=None, details=""):
        super().__init__(message)
        self.code = code
        self.details = details   # all of stderr, e.g. what ssh printed before restic gave up

    @property
    def wrong_password(self):
        return self.code == EXIT_WRONG_PASSWORD or "wrong password" in str(self).lower()

    @property
    def repo_missing(self):
        return self.code == EXIT_REPO_MISSING or "repository does not exist" in str(self).lower()


def in_flatpak():
    return os.path.exists("/.flatpak-info")


def _deja_dup_flatpak_installed():
    if not shutil.which("flatpak"):
        return False
    try:
        result = subprocess.run(["flatpak", "info", DEJA_DUP_APP], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_restic():
    """Return the argv prefix used to run restic, or None.

    Packaged as a Flatpak we ship our own restic in /app/bin. During
    development on the host we fall back to a system restic, then to the
    copy bundled inside Déjà Dup's Flatpak.
    """
    override = os.environ.get("JUNGBACKUP_RESTIC")
    if override:
        return override.split()
    if shutil.which("restic"):
        return ["restic"]
    if not in_flatpak() and _deja_dup_flatpak_installed():
        return ["flatpak", "run", "--command=restic", DEJA_DUP_APP]
    return None


class Restic:
    def __init__(self):
        self.argv = find_restic()

    @property
    def available(self):
        return self.argv is not None

    def _argv(self, repo, password, args, nice=False):
        os.makedirs(CACHE_DIR, exist_ok=True)
        argv = (["nice", "-n", "10"] if nice and shutil.which("nice") else []) + list(self.argv)
        argv += ["--repo", repo, "--json", "--cache-dir", CACHE_DIR, *for_repository(repo).restic_options()]
        if not password:
            argv.append("--insecure-no-password")
        return argv + list(args)

    @staticmethod
    def _launcher(flags, repo):
        launcher = Gio.SubprocessLauncher.new(flags)
        for var in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY"):
            launcher.unsetenv(var)
        # Backend credentials (an rclone remote for Google Drive) go in the environment, not argv.
        for var, value in for_repository(repo).restic_env().items():
            launcher.setenv(var, value, True)
        return launcher

    # Quick queries -----------------------------------------------------------

    def run_json(self, repo, password, args, callback, cancellable=None, lines=False):
        """Run `restic <args> --json` and call callback(data, error) on the main loop.

        With lines=True the output is one JSON object per line (as from `ls`)
        and data is the list of them.
        """
        argv = self._argv(repo, password, ["--no-lock", "--quiet"] + list(args))
        launcher = self._launcher(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                                  | Gio.SubprocessFlags.STDERR_PIPE, repo)
        try:
            proc = launcher.spawnv(argv)
        except GLib.Error as e:
            GLib.idle_add(callback, None, ResticError(f"Could not start restic: {e.message}"))
            return

        def done(proc, result):
            try:
                _ok, out, err = proc.communicate_utf8_finish(result)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    callback(None, ResticError(e.message))
                return
            status = proc.get_exit_status()
            if status != 0:
                callback(None, ResticError(_last_error(err, status), status, err or ""))
                return
            try:
                if lines:
                    callback([json.loads(line) for line in out.splitlines() if line.strip()], None)
                else:
                    callback(json.loads(out) if out.strip() else None, None)
            except ValueError as e:
                callback(None, ResticError(f"Unexpected output from restic: {e}"))

        if cancellable is not None:
            cancellable.connect(lambda *_: proc.force_exit())
        proc.communicate_utf8_async((password or "") + "\n", cancellable, done)

    def snapshots(self, repo, password, callback, cancellable=None):
        self.run_json(repo, password, ["snapshots"], callback, cancellable)

    def raw_stats(self, repo, password, callback, cancellable=None):
        self.run_json(repo, password, ["stats", "--mode", "raw-data"], callback, cancellable)

    def ls(self, repo, password, snapshot, path, callback, cancellable=None):
        """Call callback(nodes, error) with what's directly inside `path`, or nodes=None if it isn't there."""
        def done(data, error):
            if error is not None:
                callback(None, error)
                return
            nodes = [n for n in data if n.get("struct_type", n.get("message_type")) == "node"]
            # restic lists the folder itself too; without it, the path isn't in the snapshot.
            if path != "/" and not any(n.get("path") == path for n in nodes):
                callback(None, None)
                return
            callback([n for n in nodes if n.get("path") != path], None)
        self.run_json(repo, password, ["ls", snapshot, path], done, cancellable, lines=True)

    def dump(self, repo, password, snapshot, path, target, callback, cancellable=None):
        """Write one file from a snapshot to `target`; callback(None, error)."""
        self.run_json(repo, password, ["dump", f"--target={target}", snapshot, path], callback, cancellable)

    def init(self, repo, password, callback):
        self.run_json(repo, password, ["init", "--repository-version=2"], callback)

    # Streamed jobs -------------------------------------------------------------

    def stream(self, repo, password, args, on_message, on_exit, nice=True):
        """Run a long job, calling on_message(dict) per JSON line and on_exit(status, ResticError or None)."""
        return _StreamedJob(self._argv(repo, password, args, nice=nice), repo, password, on_message, on_exit)


def _last_error(err, status):
    lines = [line for line in (err or "").strip().splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            msg = json.loads(line)
        except ValueError:
            return line
        if isinstance(msg, dict) and msg.get("message"):
            return msg["message"]
        if isinstance(msg, dict) and msg.get("error"):
            error = msg["error"]
            return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    return f"restic exited with status {status}"


class _StreamedJob:
    def __init__(self, argv, repo, password, on_message, on_exit):
        self._on_message = on_message
        self._on_exit = on_exit
        self._stderr = []
        self._open_streams = 2
        launcher = Restic._launcher(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                                    | Gio.SubprocessFlags.STDERR_PIPE, repo)
        self.proc = launcher.spawnv(argv)
        stdin = self.proc.get_stdin_pipe()
        stdin.write_all(((password or "") + "\n").encode(), None)
        stdin.close(None)
        self._read(Gio.DataInputStream.new(self.proc.get_stdout_pipe()), self._handle_stdout)
        self._read(Gio.DataInputStream.new(self.proc.get_stderr_pipe()), self._stderr.append)

    def _read(self, stream, handler):
        def got_line(stream, result):
            try:
                line, _len = stream.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if line is None:
                self._open_streams -= 1
                if self._open_streams == 0:
                    self.proc.wait_async(None, self._exited)
                return
            handler(line)
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got_line)

    def _handle_stdout(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if isinstance(msg, dict):
            self._on_message(msg)
        elif isinstance(msg, list):
            self._on_message({"message_type": "list", "items": msg})

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        status = proc.get_exit_status() if proc.get_if_exited() else -1
        stderr = "\n".join(self._stderr)
        err = None if status == 0 else ResticError(_last_error(stderr, status), status, stderr)
        self._on_exit(status, err)

    def cancel(self):
        # SIGINT lets restic remove its lock and exit cleanly.
        self.proc.send_signal(signal.SIGINT)
