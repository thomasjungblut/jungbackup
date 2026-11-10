"""Temporary copies of single files, to look at an old version without restoring it.

They live in the cache folder rather than /tmp: a Flatpak's /tmp is private
to the sandbox, so the app that opens the file couldn't see it there, and
/tmp is often RAM-backed, which a large video would fill. Each copy gets a
folder of its own, so it keeps its name and the same file from two snapshots
can be open side by side.
"""

import hashlib
import os
import shutil

from gi.repository import GLib

PREVIEW_DIR = os.path.join(GLib.get_user_cache_dir(), "jungbackup", "preview")


def preview_path(snapshot, path):
    """Where the copy of `path` as of `snapshot` goes; the same place every time, so it's fetched once."""
    where = hashlib.sha256(f"{snapshot}\0{path}".encode(errors="replace")).hexdigest()[:16]
    return os.path.join(PREVIEW_DIR, where, os.path.basename(path))


def prepare(target):
    """Create the private folder for a copy."""
    os.makedirs(PREVIEW_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)


def seal(target):
    """Make a fetched copy read-only: edits to it would be lost, so apps should say so."""
    os.chmod(target, 0o400)


def clear():
    """Delete all copies."""
    shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
