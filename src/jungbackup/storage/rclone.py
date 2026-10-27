"""A folder on any rclone remote, through restic's rclone backend.

restic starts `rclone serve restic --stdio <remote>:<path>` and talks to
it over stdio, so every service rclone knows (OneDrive, Dropbox, WebDAV,
S3, pCloud, …) works. The remote is one you set up yourself with
`rclone config`; we use your rclone.conf as it is.
"""

import configparser
import os
import shutil

from gi.repository import GLib

from .base import UNREACHABLE, Storage

RCLONE_FLATPAK = "/app/bin/rclone"
# Inside the Flatpak XDG_CONFIG_HOME points into the sandbox, where rclone
# would look for its config; --filesystem=host lets us use the real one.
HOST_CONFIG = os.path.expanduser("~/.config/rclone/rclone.conf")


def in_flatpak():
    return os.path.exists("/.flatpak-info")


def find_rclone():
    """Path of the rclone restic should start, or None to leave it to restic's PATH."""
    override = os.environ.get("JUNGBACKUP_RCLONE")
    if override:
        return override
    if os.path.exists(RCLONE_FLATPAK):
        return RCLONE_FLATPAK
    return None


def rclone_available():
    return find_rclone() is not None or shutil.which("rclone") is not None


def config_path():
    """The rclone.conf rclone will read."""
    if os.environ.get("RCLONE_CONFIG"):
        return os.environ["RCLONE_CONFIG"]
    if in_flatpak():
        return HOST_CONFIG
    return os.path.join(GLib.get_user_config_dir(), "rclone", "rclone.conf")


def configured_remotes(path=None):
    """Remote names in rclone.conf, or None when that can't be told (missing or encrypted config)."""
    try:
        with open(path or config_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if text.startswith("RCLONE_ENCRYPT_V"):
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    return parser.sections()


def build_repository(remote, path):
    return f"rclone:{remote}:{path}"


def parse_repository(repo):
    remote, _, path = repo[len("rclone:"):].partition(":")
    return {"remote": remote, "path": path}


class RcloneStorage(Storage):
    kind = "rclone"
    name = "Rclone Remote"
    icon = "folder-remote-symbolic"
    FIELDS = (
        ("remote", "Remote", "The name of a remote you set up with “rclone config”"),
        ("path", "Folder on Remote", "Created if it doesn't exist"),
    )
    HINT = ("Uses your rclone configuration, so any service rclone supports works. "
            "Set up the remote with “rclone config” first.")

    @classmethod
    def handles(cls, repo):
        return repo.startswith("rclone:")

    @classmethod
    def default_fields(cls):
        # Déjà Dup's default ($HOSTNAME), so a backup made by either app is where the other looks.
        return {"path": GLib.get_host_name()}

    @classmethod
    def from_fields(cls, values):
        remote = values.get("remote", "").strip().rstrip(":")
        path = values.get("path", "").strip().rstrip("/")
        if not remote or not path or any(c in remote for c in ":/"):
            return None
        return cls(build_repository(remote, path))

    def fields(self):
        return parse_repository(self.repo)

    @property
    def label(self):
        f = self.fields()
        return f"{f['remote']} › {f['path']}"

    def restic_options(self):
        rclone = find_rclone()
        return ["-o", f"rclone.program={rclone}"] if rclone else []

    def restic_env(self):
        if in_flatpak() and not os.environ.get("RCLONE_CONFIG"):
            return {"RCLONE_CONFIG": HOST_CONFIG}
        return {}

    def missing(self):
        """Why restic isn't worth starting, or None."""
        if not rclone_available():
            return "This needs rclone, which isn't installed."
        remote = self.fields()["remote"]
        remotes = configured_remotes()
        defined_by_env = f"RCLONE_CONFIG_{remote.upper()}_TYPE" in os.environ
        if remotes is not None and remote not in remotes and not defined_by_env:
            return f"There's no rclone remote called “{remote}”. Set it up with “rclone config”."
        return None

    def probe(self, restic, callback, cancellable=None):
        problem = self.missing()
        if problem:
            callback(UNREACHABLE, problem)
        else:
            super().probe(restic, callback, cancellable)

    def explain(self, error):
        text = f"{error}\n{getattr(error, 'details', '')}"
        if "executable file not found" in text and "rclone" in text:
            return "This needs rclone, which isn't installed."
        if "didn't find section in config file" in text:
            return f"There's no rclone remote called “{self.fields()['remote']}”. Set it up with “rclone config”."
        if "unable to decrypt configuration" in text or "RCLONE_CONFIG_PASS" in text:
            return "Your rclone configuration is encrypted, which isn't supported. Store it unencrypted."
        if any(s in text for s in ("no such host", "dial tcp", "i/o timeout", "network is unreachable")):
            return "Couldn't reach the remote. Are you online?"
        return str(error)
