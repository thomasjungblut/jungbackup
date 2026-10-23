"""A folder on this computer: internal disk, USB drive or a mounted network share."""

import os

from gi.repository import Gio, GLib

from ..core.paths import describe_path
from .base import EXISTING, NEW, OCCUPIED, UNREACHABLE, Storage


def is_repository(path):
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return "config" in names and "snapshots" in names


def is_empty_or_missing(path):
    try:
        return not os.listdir(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False


class LocalStorage(Storage):
    kind = "local"
    name = "Folder"
    icon = "folder-symbolic"

    @classmethod
    def handles(cls, repo):
        return os.path.isabs(repo)

    @property
    def label(self):
        return describe_path(self.repo)

    @property
    def path(self):
        return self.repo

    def reachable(self):
        return os.path.isdir(self.repo)

    def has_repository(self):
        return is_repository(self.repo)

    def prepare(self):
        try:
            os.makedirs(self.repo, exist_ok=True)
        except OSError as e:
            return f"Could not create the folder: {e.strerror}"
        return None

    def query_space(self, callback):
        if not self.reachable():
            callback(None)
            return

        def done(file, result):
            try:
                info = file.query_filesystem_info_finish(result)
                callback((info.get_attribute_uint64("filesystem::size"),
                          info.get_attribute_uint64("filesystem::free")))
            except GLib.Error:
                callback(None)
        Gio.File.new_for_path(self.repo).query_filesystem_info_async(
            "filesystem::size,filesystem::free", GLib.PRIORITY_DEFAULT, None, done)

    def probe(self, restic, callback, cancellable=None):
        path = self.repo
        if is_repository(path):
            callback(EXISTING, None)
        elif not is_empty_or_missing(path):
            callback(OCCUPIED, None)
        elif os.path.isdir(path) or os.path.isdir(os.path.dirname(path.rstrip("/"))):
            callback(NEW, None)
        else:
            callback(UNREACHABLE, "This folder isn't reachable. Is the drive or share mounted?")
