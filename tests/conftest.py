"""Shared fixtures.

Integration tests run the real restic (found the same way the app finds it,
so $JUNGBACKUP_RESTIC works), rclone with a remote on the local disk, and,
for SFTP, a throwaway OpenSSH container started with podman. Each is
skipped when the tool isn't available.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time

# Keep the app's settings, cache and keyring-free state out of the real home.
# GLib reads these once, so they have to be set before anything imports gi.
_XDG = tempfile.mkdtemp(prefix="jungbackup-test-")
for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    os.environ[var] = os.path.join(_XDG, var.lower())

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest  # noqa: E402
from gi.repository import GLib  # noqa: E402

from jungbackup.backup.restic import Restic  # noqa: E402
from jungbackup.storage import rclone, sftp  # noqa: E402

SFTP_IMAGE = "lscr.io/linuxserver/openssh-server:latest"
SFTP_USER = "backup"
PASSWORD = "correct horse battery staple"


def wait_for(start, timeout=60):
    """Run start(done) and spin the GLib main loop until done(*args) is called; return args."""
    loop = GLib.MainLoop()
    result = []

    def done(*args):
        result.append(args)
        loop.quit()

    start(done)
    if not result:
        timer = GLib.timeout_add_seconds(timeout, loop.quit)
        loop.run()
        if result:
            GLib.source_remove(timer)
    assert result, f"no callback within {timeout}s"
    return result[0]


@pytest.fixture(scope="session")
def restic():
    r = Restic()
    if not r.available:
        pytest.skip("restic not found (set JUNGBACKUP_RESTIC)")
    return r


@pytest.fixture
def source(tmp_path):
    """A small folder to back up."""
    src = tmp_path / "source"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("hello\n")
    (src / "b.bin").write_bytes(os.urandom(64 * 1024))
    return src


@pytest.fixture
def rclone_remote(monkeypatch):
    """An rclone remote of the "local" type, defined in the environment like a user's own would be in rclone.conf."""
    if not rclone.rclone_available():
        pytest.skip("rclone not found (set JUNGBACKUP_RCLONE)")
    monkeypatch.setenv("RCLONE_CONFIG_JUNGBACKUPTEST_TYPE", "local")
    return "jungbackuptest"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def sftp_server(tmp_path_factory):
    """An SSH server on localhost that accepts a key made for this test run.

    The app's ssh flags are extended to use that key and a private
    known_hosts file, so the user's ~/.ssh isn't touched.
    """
    if not shutil.which("podman"):
        pytest.skip("podman not found")
    keys = tmp_path_factory.mktemp("ssh")
    key = keys / "id_test"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    port = _free_port()
    name = f"jungbackup-test-sftp-{os.getpid()}"
    try:
        subprocess.run(["podman", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:2222",
                        "-e", "PUID=1000", "-e", "PGID=1000", "-e", f"USER_NAME={SFTP_USER}",
                        "-e", f"PUBLIC_KEY={(keys / 'id_test.pub').read_text().strip()}", SFTP_IMAGE],
                       check=True, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"could not start the SFTP container: {getattr(e, 'stderr', e)}")

    extra = f"-i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={keys / 'known_hosts'}"
    original = sftp.SSH_ARGS
    sftp.SSH_ARGS = f"{original} {extra}"
    ssh = ["ssh", "-p", str(port), *sftp.SSH_ARGS.split(), f"{SFTP_USER}@127.0.0.1"]
    try:
        deadline = time.time() + 60
        while subprocess.run([*ssh, "true"], capture_output=True).returncode != 0:
            if time.time() > deadline:
                pytest.fail("SFTP container didn't accept the test key")
            time.sleep(1)
        yield {"host": "127.0.0.1", "port": str(port), "user": SFTP_USER, "key": key, "ssh": ssh}
    finally:
        sftp.SSH_ARGS = original
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)
