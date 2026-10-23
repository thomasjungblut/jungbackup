"""Where backups are kept. One module per backend; see base.Storage."""

from .base import EXISTING, NEW, OCCUPIED, UNREACHABLE, Storage
from .local import LocalStorage
from .sftp import SftpStorage

# In the order the setup dialog offers them.
BACKENDS = (LocalStorage, SftpStorage)


def for_repository(repo):
    for backend in BACKENDS:
        if backend.handles(repo):
            return backend(repo)
    return LocalStorage(repo)


__all__ = ["BACKENDS", "EXISTING", "NEW", "OCCUPIED", "UNREACHABLE",
           "LocalStorage", "SftpStorage", "Storage", "for_repository"]
