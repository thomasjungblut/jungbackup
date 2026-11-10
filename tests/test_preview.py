"""Temporary copies for looking at a file without restoring it."""

import os
import stat

from jungbackup.backup import preview
from jungbackup.backup.preview import PREVIEW_DIR, preview_path


def test_a_copy_keeps_its_name_in_a_folder_of_its_own():
    old = preview_path("abc", "/home/me/Documents/Resume.pdf")
    assert os.path.basename(old) == "Resume.pdf"
    assert os.path.dirname(os.path.dirname(old)) == PREVIEW_DIR
    assert old == preview_path("abc", "/home/me/Documents/Resume.pdf")      # fetched once
    assert old != preview_path("def", "/home/me/Documents/Resume.pdf")      # another snapshot's version
    assert old != preview_path("abc", "/home/me/Desktop/Resume.pdf")


def test_copies_are_private_read_only_and_cleared():
    target = preview_path("abc", "/home/me/Resume.pdf")
    preview.prepare(target)
    assert stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode) == 0o700
    with open(target, "w") as f:
        f.write("pdf\n")
    preview.seal(target)
    assert not os.access(target, os.W_OK) or os.geteuid() == 0
    preview.clear()
    assert not os.path.exists(PREVIEW_DIR)
    preview.clear()     # nothing there: fine
