"""Turning Déjà Dup locations into repositories, with its settings faked."""

from gi.repository import GLib

from jungbackup.dejadup.config import DejaDupConfig


class FakeStore:
    def __init__(self, values):
        self.values = values

    def get(self, key, section=None):
        return self.values.get((section, key), "")


def _config(values):
    dd = DejaDupConfig.__new__(DejaDupConfig)
    dd._store = FakeStore(values)
    return dd


def test_rclone_location():
    loc = _config({(None, "backend"): "rclone", ("rclone", "remote"): "onedrive:",
                   ("rclone", "folder"): "/backups/$HOSTNAME"}).location()
    assert loc.problem is None
    assert loc.repository == f"rclone:onedrive:backups/{GLib.get_host_name()}"


def test_rclone_location_without_remote():
    loc = _config({(None, "backend"): "rclone", ("rclone", "folder"): "$HOSTNAME"}).location()
    assert loc.repository is None and loc.problem
