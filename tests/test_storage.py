"""Storage backends without restic: repository strings, labels, local probing."""

import pytest

from jungbackup.backup.restic import ResticError
from jungbackup.storage import (
    EXISTING,
    NEW,
    OCCUPIED,
    UNREACHABLE,
    LocalStorage,
    SftpStorage,
    for_repository,
)
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
