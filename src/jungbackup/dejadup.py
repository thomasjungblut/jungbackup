"""Read (and minimally write) Déjà Dup's configuration, for importing.

Déjà Dup keeps its settings in GSettings. When it is installed as a Flatpak
(the common case) those settings live in a GKeyFile inside its sandbox data
directory instead of dconf, so we support both stores.
"""

import os
from dataclasses import dataclass

from gi.repository import Gio, GLib

SCHEMA = "org.gnome.DejaDup"
KEYFILE = os.path.expanduser("~/.var/app/org.gnome.DejaDup/config/glib-2.0/settings/keyfile")

SECTIONS = {
    "local": "Local",
    "remote": "Remote",
    "drive": "Drive",
    "google": "Google",
    "microsoft": "Microsoft",
    "rclone": "Rclone",
}

KEY_TYPES = {
    "periodic": "b",
    "periodic-period": "i",
    "delete-after": "i",
    "full-backup-period": "i",
    "include-list": "as",
    "exclude-list": "as",
    "allow-metered": "b",
    "allow-power-saver": "b",
}

DEFAULTS = {
    "periodic": False,
    "periodic-period": 7,
    "delete-after": 0,
    "include-list": ["$HOME"],
    "exclude-list": ["$TRASH", "$DOWNLOAD"],
    "backend": "auto",
    "tool": "unset",
}

BACKEND_NAMES = {
    "local": "Local folder",
    "drive": "External drive",
    "remote": "Network server",
    "google": "Google Drive",
    "microsoft": "Microsoft OneDrive",
    "rclone": "Rclone",
    "auto": "Not configured",
}

SPECIAL_DIRS = {
    "$DESKTOP": GLib.UserDirectory.DIRECTORY_DESKTOP,
    "$DOCUMENTS": GLib.UserDirectory.DIRECTORY_DOCUMENTS,
    "$DOWNLOAD": GLib.UserDirectory.DIRECTORY_DOWNLOAD,
    "$MUSIC": GLib.UserDirectory.DIRECTORY_MUSIC,
    "$PICTURES": GLib.UserDirectory.DIRECTORY_PICTURES,
    "$PUBLIC_SHARE": GLib.UserDirectory.DIRECTORY_PUBLIC_SHARE,
    "$TEMPLATES": GLib.UserDirectory.DIRECTORY_TEMPLATES,
    "$VIDEOS": GLib.UserDirectory.DIRECTORY_VIDEOS,
}

SPECIAL_LABELS = {
    "$HOME": "Home",
    "$TRASH": "Trash",
    "$DESKTOP": "Desktop",
    "$DOCUMENTS": "Documents",
    "$DOWNLOAD": "Downloads",
    "$MUSIC": "Music",
    "$PICTURES": "Pictures",
    "$PUBLIC_SHARE": "Public",
    "$TEMPLATES": "Templates",
    "$VIDEOS": "Videos",
}


def expand_path(token):
    home = GLib.get_home_dir()
    if token == "$HOME":
        return home
    if token == "$TRASH":
        return os.path.join(GLib.get_user_data_dir(), "Trash")
    if token in SPECIAL_DIRS:
        return GLib.get_user_special_dir(SPECIAL_DIRS[token])   # None if not set up
    if token.startswith("$HOME/"):
        return os.path.join(home, token[6:])
    return token


def describe_path(token):
    """Human label for an include/exclude entry."""
    if token in SPECIAL_LABELS:
        return SPECIAL_LABELS[token]
    path = expand_path(token) or token
    home = GLib.get_home_dir()
    if path == home:
        return "Home"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1:]
    return path


def collapse_path(path):
    """Store paths the same way Déjà Dup does."""
    home = GLib.get_home_dir()
    if path == home:
        return "$HOME"
    for token, kind in SPECIAL_DIRS.items():
        if GLib.get_user_special_dir(kind) == path:
            return token
    return path


class _GSettingsStore:
    def __init__(self, on_changed):
        self._root = Gio.Settings.new(SCHEMA)
        self._children = {}
        self._root.connect("changed", lambda *_: on_changed())
        for section, suffix in SECTIONS.items():
            settings = Gio.Settings.new(f"{SCHEMA}.{suffix}")
            settings.connect("changed", lambda *_: on_changed())
            self._children[section] = settings

    def _settings(self, section):
        return self._children[section] if section else self._root

    def get(self, key, section=None):
        settings = self._settings(section)
        if key not in settings.props.settings_schema.list_keys():
            return DEFAULTS.get(key, "")
        return settings.get_value(key).unpack()

    def set(self, key, value, section=None):
        settings = self._settings(section)
        settings.set_value(key, GLib.Variant(settings.get_value(key).get_type_string(), value))

    description = "GSettings"


class _KeyfileStore:
    def __init__(self, on_changed):
        self._monitor = Gio.File.new_for_path(KEYFILE).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._monitor.set_rate_limit(500)
        self._monitor.connect("changed", self._on_file_changed, on_changed)

    @staticmethod
    def _on_file_changed(_monitor, _file, _other, event, on_changed):
        if event in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            on_changed()

    @staticmethod
    def _group(section):
        return "org/gnome/deja-dup" + (f"/{section}" if section else "")

    def _load(self):
        keyfile = GLib.KeyFile()
        try:
            keyfile.load_from_file(KEYFILE, GLib.KeyFileFlags.KEEP_COMMENTS)
        except GLib.Error:
            pass
        return keyfile

    def get(self, key, section=None):
        keyfile = self._load()
        try:
            raw = keyfile.get_value(self._group(section), key)
            return GLib.Variant.parse(None, raw, None, None).unpack()
        except GLib.Error:
            return DEFAULTS.get(key, "")

    def set(self, key, value, section=None):
        keyfile = self._load()
        variant = GLib.Variant(KEY_TYPES.get(key, "s"), value)
        keyfile.set_value(self._group(section), key, variant.print_(False))
        keyfile.save_to_file(KEYFILE)

    description = "Flatpak settings"


@dataclass
class Location:
    backend: str
    label: str            # what the user sees
    path: str | None      # local filesystem path to the repository, if reachable
    problem: str | None   # why the path is unavailable


class DejaDupConfig:
    """Typed access to the settings the dashboard cares about."""

    def __init__(self, on_changed=lambda: None):
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup(SCHEMA, True) is not None:
            self._store = _GSettingsStore(on_changed)
        elif os.path.exists(KEYFILE):
            self._store = _KeyfileStore(on_changed)
        else:
            self._store = None

    @property
    def available(self):
        return self._store is not None

    @property
    def store_description(self):
        return self._store.description if self._store else "none"

    def get(self, key, section=None):
        if self._store is None:
            return DEFAULTS.get(key, "")
        return self._store.get(key, section)

    def set(self, key, value, section=None):
        if self._store is not None:
            self._store.set(key, value, section)

    # Convenience accessors -------------------------------------------------

    @property
    def backend(self):
        return self.get("backend")

    @property
    def last_backup(self):
        from .util import parse_time
        return parse_time(self.get("last-backup"))

    @property
    def last_run(self):
        from .util import parse_time
        return parse_time(self.get("last-run"))

    @property
    def periodic(self):
        return bool(self.get("periodic"))

    @property
    def period_days(self):
        return int(self.get("periodic-period") or 1)

    @property
    def keep_days(self):
        return int(self.get("delete-after") or 0)

    @property
    def includes(self):
        return list(self.get("include-list"))

    @property
    def excludes(self):
        return list(self.get("exclude-list"))

    def folder(self, section):
        folder = self.get("folder", section) or ""
        return folder.replace("$HOSTNAME", GLib.get_host_name())

    def location(self):
        backend = self.backend
        if backend == "local":
            folder = self.folder("local")
            path = folder if os.path.isabs(folder) else os.path.join(GLib.get_home_dir(), folder)
            if not os.path.isdir(path):
                return Location(backend, path, None,
                                "The backup folder is not reachable. Is the drive or network share mounted?")
            return Location(backend, path, path, None)

        if backend == "drive":
            name = self.get("name", "drive") or "External drive"
            folder = self.folder("drive")
            label = f"{name} › {folder}"
            volume = Gio.VolumeMonitor.get().get_volume_for_uuid(self.get("uuid", "drive"))
            mount = volume.get_mount() if volume else None
            if mount is None:
                return Location(backend, label, None, f"“{name}” is not connected.")
            path = os.path.join(mount.get_root().get_path(), folder)
            return Location(backend, label, path, None)

        if backend == "remote":
            uri = self.get("uri", "remote")
            folder = self.folder("remote")
            label = f"{uri} › {folder}" if uri else "Network server"
            path = Gio.File.new_for_uri(uri).get_path() if uri else None
            if path is None or not os.path.isdir(path):
                return Location(backend, label, None, "The network server is not mounted.")
            return Location(backend, label, os.path.join(path, folder), None)

        if backend in ("google", "microsoft", "rclone"):
            folder = self.folder(backend)
            label = f"{BACKEND_NAMES[backend]} › {folder}"
            return Location(backend, label, None, f"{BACKEND_NAMES[backend]} is not supported yet.")

        return Location(backend, BACKEND_NAMES.get(backend, backend), None, "No backup location is configured.")

    def detect_tool(self, location):
        """Return 'restic', 'duplicity' or None for the repository at location."""
        tool = self.get("tool")
        if location.path and os.path.isdir(location.path):
            try:
                names = os.listdir(location.path)
            except OSError:
                names = []
            if "config" in names and "snapshots" in names:
                return "restic"
            if any(n.startswith("duplicity-") for n in names):
                return "duplicity"
        return tool if tool in ("restic", "duplicity") else None
