"""Storage backends without restic: repository strings, labels, local probing."""

import json
import os

import pytest

from jungbackup.backup.restic import ResticError
from jungbackup.storage import (
    EXISTING,
    NEW,
    OCCUPIED,
    UNREACHABLE,
    GoogleDriveStorage,
    LocalStorage,
    RcloneStorage,
    SftpStorage,
    for_repository,
    rclone,
)
from jungbackup.storage.gdrive import rclone_env
from jungbackup.storage.sftp import build_repository, parse_repository


@pytest.mark.parametrize(("host", "path", "user", "port", "repo"), [
    ("nas", "backups/laptop", "", "", "sftp:nas:backups/laptop"),
    ("nas.local", "/srv/restic", "thomas", "", "sftp:thomas@nas.local:/srv/restic"),
    ("10.0.0.2", "/srv/restic", "thomas", "2222", "sftp://thomas@10.0.0.2:2222//srv/restic"),
    ("10.0.0.2", "rel/dir", "", "2222", "sftp://10.0.0.2:2222/rel/dir"),
    ("::1", "/x", "u", "", "sftp:u@[::1]:/x"),
    ("::1", "/x", "u", "22", "sftp://u@[::1]:22//x"),
])
def test_sftp_repository_round_trip(host, path, user, port, repo):
    assert build_repository(host, path, user, port) == repo
    assert parse_repository(repo) == {"host": host, "user": user, "port": port, "path": path}
    assert isinstance(for_repository(repo), SftpStorage)


def test_for_repository_falls_back_to_local():
    assert isinstance(for_repository("/run/media/me/disk/backup"), LocalStorage)


@pytest.mark.parametrize("values", [
    {"host": "", "path": "b"},
    {"host": "nas", "path": " "},
    {"host": "nas", "path": "b", "port": "ssh"},
])
def test_sftp_from_fields_incomplete(values):
    assert SftpStorage.from_fields(values) is None


def test_sftp_from_fields_trims():
    storage = SftpStorage.from_fields({"host": " nas ", "user": "me", "port": "", "path": "backups "})
    assert storage.repo == "sftp:me@nas:backups"
    assert storage.label == "me@nas › backups"


@pytest.mark.parametrize(("stderr", "expected"), [
    ("me@nas: Permission denied (publickey).", "didn't accept your SSH key"),
    ("Host key verification failed.", "identity of nas changed"),
    ("ssh: Could not resolve hostname nas: Name or service not known", "“nas” wasn't found"),
    ("ssh: connect to host nas port 22: Connection refused", "Couldn't connect to nas"),
    ("subsystem request failed on channel 0", "doesn't offer SFTP"),
])
def test_sftp_explains_ssh_errors(stderr, expected):
    error = ResticError("Fatal: unable to open repository", 1, stderr)
    assert expected in SftpStorage("sftp:nas:b").explain(error)


def test_sftp_keeps_unknown_errors():
    assert SftpStorage("sftp:nas:b").explain(ResticError("something odd")) == "something odd"


def _probe(storage):
    result = []
    storage.probe(None, lambda state, message: result.append(state))
    return result[0]


def test_local_probe_states(tmp_path):
    assert _probe(LocalStorage(str(tmp_path / "missing"))) == NEW
    assert _probe(LocalStorage(str(tmp_path))) == NEW
    (tmp_path / "file.txt").write_text("x")
    assert _probe(LocalStorage(str(tmp_path))) == OCCUPIED
    assert _probe(LocalStorage(str(tmp_path / "no" / "such" / "parent"))) == UNREACHABLE
    repo = tmp_path / "repo"
    (repo / "snapshots").mkdir(parents=True)
    (repo / "config").write_text("")
    assert _probe(LocalStorage(str(repo))) == EXISTING


def test_gdrive_repository_round_trip():
    storage = GoogleDriveStorage.from_fields({"path": " /Backups/laptop/ "})
    assert storage.repo == "rclone:jungbackupgdrive:Backups/laptop"
    assert isinstance(for_repository(storage.repo), GoogleDriveStorage)
    assert storage.fields() == {"path": "Backups/laptop"}
    assert GoogleDriveStorage.from_fields({"path": " / "}) is None


def test_gdrive_leaves_other_rclone_remotes_alone():
    assert not GoogleDriveStorage.handles("rclone:mydrive:backups")


def test_gdrive_env_defines_remote_without_config_file():
    env = rclone_env({"email": "me@example.com", "token": {"refresh_token": "r"}})
    assert env["RCLONE_CONFIG"] == os.devnull
    assert env["RCLONE_CONFIG_JUNGBACKUPGDRIVE_TYPE"] == "drive"
    assert env["RCLONE_CONFIG_JUNGBACKUPGDRIVE_SCOPE"] == "drive.file"
    assert json.loads(env["RCLONE_CONFIG_JUNGBACKUPGDRIVE_TOKEN"]) == {"refresh_token": "r"}
    assert "RCLONE_CONFIG_JUNGBACKUPGDRIVE_TOKEN" not in rclone_env(None)


@pytest.mark.parametrize(("stderr", "expected"), [
    ('oauth2: "invalid_grant" "Token has been expired or revoked."', "Sign in again"),
    ('exec: "rclone": executable file not found in $PATH', "needs rclone"),
    ("googleapi: Error 403: The user's Drive storage quota has been exceeded., storageQuotaExceeded", "is full"),
])
def test_gdrive_explains_errors(stderr, expected):
    error = ResticError("Fatal: unable to open repository", 1, stderr)
    assert expected in GoogleDriveStorage("rclone:jungbackupgdrive:b").explain(error)


def test_rclone_repository_round_trip():
    storage = RcloneStorage.from_fields({"remote": " onedrive: ", "path": "Backups/laptop/"})
    assert storage.repo == "rclone:onedrive:Backups/laptop"
    assert storage.fields() == {"remote": "onedrive", "path": "Backups/laptop"}
    assert storage.label == "onedrive › Backups/laptop"


def test_rclone_and_gdrive_dispatch():
    assert type(for_repository("rclone:onedrive:b")) is RcloneStorage
    assert type(for_repository("rclone:jungbackupgdrive:b")) is GoogleDriveStorage


@pytest.mark.parametrize("values", [
    {"remote": "", "path": "b"},
    {"remote": "onedrive", "path": " "},
    {"remote": "one:drive", "path": "b"},
    {"remote": "a/b", "path": "b"},
])
def test_rclone_from_fields_incomplete(values):
    assert RcloneStorage.from_fields(values) is None


def test_rclone_configured_remotes(tmp_path):
    conf = tmp_path / "rclone.conf"
    assert rclone.configured_remotes(str(conf)) is None
    conf.write_text("[onedrive]\ntype = onedrive\n\n[nas]\ntype = webdav\nurl = http://x/%2F\n")
    assert rclone.configured_remotes(str(conf)) == ["onedrive", "nas"]
    conf.write_text("RCLONE_ENCRYPT_V0:\nabc")
    assert rclone.configured_remotes(str(conf)) is None


def test_rclone_probe_catches_unknown_remote(tmp_path, monkeypatch):
    conf = tmp_path / "rclone.conf"
    conf.write_text("[onedrive]\ntype = onedrive\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    monkeypatch.setattr(rclone, "rclone_available", lambda: True)
    assert RcloneStorage("rclone:onedrive:b").missing() is None
    assert "no rclone remote called “dropbox”" in RcloneStorage("rclone:dropbox:b").missing()
    monkeypatch.setenv("RCLONE_CONFIG_DROPBOX_TYPE", "dropbox")
    assert RcloneStorage("rclone:dropbox:b").missing() is None


@pytest.mark.parametrize(("stderr", "expected"), [
    ('Failed to create file system for "x:b": didn\'t find section in config file ("x")', "no rclone remote called"),
    ('exec: "rclone": executable file not found in $PATH', "needs rclone"),
])
def test_rclone_explains_errors(stderr, expected):
    error = ResticError("Fatal: unable to open repository", 1, stderr)
    assert expected in RcloneStorage("rclone:x:b").explain(error)


def test_cloud_folders_default_to_host_name_like_deja_dup():
    from gi.repository import GLib
    assert RcloneStorage.default_fields() == {"path": GLib.get_host_name()}
    assert GoogleDriveStorage.default_fields() == {"path": GLib.get_host_name()}
