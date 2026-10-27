"""Where backups are kept. One module per backend; see base.Storage."""

from .base import EXISTING, NEW, OCCUPIED, UNREACHABLE, Storage
from .gdrive import GoogleDriveStorage
from .local import LocalStorage
from .rclone import RcloneStorage
from .sftp import SftpStorage

# In the order the setup dialog offers them. for_repository asks them in this
# order too, so Google Drive comes before the rclone remotes it's one of.
BACKENDS = (LocalStorage, SftpStorage, GoogleDriveStorage, RcloneStorage)


def for_repository(repo):
    for backend in BACKENDS:
        if backend.handles(repo):
            return backend(repo)
    return LocalStorage(repo)


__all__ = ["BACKENDS", "EXISTING", "GoogleDriveStorage", "NEW", "OCCUPIED", "UNREACHABLE",
           "LocalStorage", "RcloneStorage", "SftpStorage", "Storage", "for_repository"]
