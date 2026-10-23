"""End to end with the real restic, for every storage backend.

Each test gets a fresh, empty repository location; the flow mirrors the app:
probe → init → back up → list snapshots → back up again.
"""

import socket
import subprocess
import uuid

import pytest

from conftest import PASSWORD, wait_for
from jungbackup.backup.job import BackupJob
from jungbackup.core.settings import Settings
from jungbackup.storage import EXISTING, NEW, UNREACHABLE, SftpStorage, for_repository, sftp

pytestmark = pytest.mark.integration


@pytest.fixture(params=["local", "sftp-relative", "sftp-absolute"])
def storage(request, tmp_path):
    if request.param == "local":
        return for_repository(str(tmp_path / "repo"))
    server = request.getfixturevalue("sftp_server")
    # The container listens on a random port, so these use restic's URL form.
    # A relative path lands in the user's home folder (/config in the container).
    path = "repos/" if request.param == "sftp-relative" else "/config/absolute/"
    return SftpStorage.from_fields({**server, "path": path + uuid.uuid4().hex})


def _probe(storage, restic):
    return wait_for(lambda done: storage.probe(restic, done))


def _settings(storage, source):
    return Settings(repository=storage.repo, includes=[str(source)], excludes=[str(source / "docs")])


def _backup(restic, settings, password=PASSWORD, create=True):
    def start(done):
        BackupJob(restic, settings, password, lambda job: None, done, create=create).start()
    job, ok, error = wait_for(start, timeout=180)
    return job, ok, error


def _snapshots(restic, storage, password=PASSWORD):
    return wait_for(lambda done: restic.snapshots(storage.repo, password, done))


def test_backup_creates_repository_and_snapshots(restic, storage, source):
    assert _probe(storage, restic) == (NEW, None)
    settings = _settings(storage, source)

    job, ok, error = _backup(restic, settings)
    assert (ok, error) == (True, None), error
    assert job.summary["files_new"] == 1    # docs/ is excluded
    assert _probe(storage, restic) == (EXISTING, None)

    job, ok, error = _backup(restic, settings)
    assert ok, error
    assert job.summary["files_unmodified"] == 1

    snapshots, error = _snapshots(restic, storage)
    assert error is None
    assert len(snapshots) == 2
    assert all(s["tags"] == ["deja-dup"] for s in snapshots)   # Déjà Dup can see them


def test_wrong_password_is_reported(restic, storage, source):
    assert _backup(restic, _settings(storage, source))[1]
    data, error = _snapshots(restic, storage, password="not the password")
    assert data is None and error.wrong_password


def test_unencrypted_repository_opens_without_password(restic, storage, source):
    _job, ok, error = _backup(restic, _settings(storage, source), password="")
    assert ok, error
    assert _probe(storage, restic) == (EXISTING, None)
    snapshots, error = _snapshots(restic, storage, password="")
    assert error is None and len(snapshots) == 1


def test_scheduled_backup_does_not_create_a_repository(restic, storage, source):
    _job, ok, error = _backup(restic, _settings(storage, source), create=False)
    assert not ok
    assert "wasn't found" in error
    assert _probe(storage, restic) == (NEW, None)


def test_sftp_rejected_key_is_explained(restic, sftp_server, tmp_path):
    other = tmp_path / "other_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    original = sftp.SSH_ARGS
    sftp.SSH_ARGS = original.replace(f"-i {sftp_server['key']}", f"-i {other}")
    try:
        storage = SftpStorage.from_fields({**sftp_server, "path": "repos/x"})
        state, message = _probe(storage, restic)
    finally:
        sftp.SSH_ARGS = original
    assert state == UNREACHABLE
    assert "didn't accept your SSH key" in message


def test_sftp_closed_port_is_explained(restic, sftp_server):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]   # nothing listens once the socket is closed
    storage = SftpStorage.from_fields({**sftp_server, "port": str(port), "path": "repos/x"})
    state, message = _probe(storage, restic)
    assert state == UNREACHABLE
    assert "Couldn't connect to 127.0.0.1" in message
