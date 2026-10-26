"""Turning chosen snapshot paths into `restic restore` steps."""

from jungbackup.backup.restic import pattern_literal
from jungbackup.backup.restore import plan_copy, plan_in_place


def test_in_place_starts_at_the_parent_folder():
    existing = {"/home/me", "/home/me/Documents"}
    steps = plan_in_place(["/home/me/Documents/tax.pdf", "/home/me/Documents/old"], existing.__contains__)
    assert steps == [("/home/me/Documents", "/home/me/Documents", ["/tax.pdf", "/old"])]


def test_in_place_recreates_missing_folders_from_the_snapshot():
    existing = {"/home/me"}
    steps = plan_in_place(["/home/me/Gone/Deeper/file.txt"], existing.__contains__)
    assert steps == [("/home/me", "/home/me", ["/Gone/Deeper/file.txt"])]


def test_in_place_groups_by_folder_and_falls_back_to_root():
    existing = {"/a", "/b"}
    steps = plan_in_place(["/a/1", "/b/2", "/a/3", "/top"], existing.__contains__)
    assert steps == [("/a", "/a", ["/1", "/3"]), ("/b", "/b", ["/2"]), ("/", "/", ["/top"])]


def test_copy_puts_each_item_into_the_target():
    steps = plan_copy(["/home/me/x/a.pdf", "/home/me/y", "/home/me/x/b"], "/tmp/out")
    assert steps == [("/home/me/x", "/tmp/out", ["/a.pdf", "/b"]), ("/home/me", "/tmp/out", ["/y"])]


def test_includes_are_literal():
    steps = plan_copy(["/d/we$ird [1]", "/d/a*?.txt", "/d/back\\slash"], "/t")
    assert steps == [("/d", "/t", ["/we$ird \\[1]", "/a\\*\\?.txt", "/back\\\\slash"])]
    assert pattern_literal("$HOME") == "$HOME"   # restore doesn't expand variables, unlike backup excludes
