"""Verifying the backup: when it's due, which part of the data it reads, and how errors are taken."""

from datetime import timedelta

from jungbackup.backup.job import check_damage
from jungbackup.core.settings import Settings
from jungbackup.core.util import now
from jungbackup.ui.history import verified_tile


def ago(**kw):
    return (now() - timedelta(**kw)).isoformat()


def test_off_by_default():
    s = Settings(repository="/backup")
    assert s.check_days == 0
    assert not s.check_due
    assert verified_tile(s) == ("Never", "turn it on in Settings")


def test_due_after_the_interval():
    s = Settings(repository="/backup", check_days=7)
    assert s.check_due                          # never verified
    s.last_check = ago(days=6)
    assert not s.check_due
    s.last_check = ago(days=7, minutes=1)
    assert s.check_due


def test_rotates_through_all_parts():
    s = Settings(check_days=7, check_parts=4)
    seen = []
    for _ in range(5):
        seen.append(s.check_subset())
        s.record_check(None)
    assert seen == [["--read-data-subset=1/4"], ["--read-data-subset=2/4"], ["--read-data-subset=3/4"],
                    ["--read-data-subset=4/4"], ["--read-data-subset=1/4"]]
    assert verified_tile(s) == ("just now", "part 1 of 4 read back")


def test_all_data():
    s = Settings(check_days=7, check_parts=1)
    assert s.check_subset() == ["--read-data"]
    s.record_check(None)
    assert verified_tile(s)[1] == "all data read back"


def test_part_stays_put_after_a_changed_setting():
    s = Settings(check_parts=4, check_part=9)
    assert s.check_subset() == ["--read-data-subset=1/4"]


def test_errors_count_once_a_second_check_confirms_them():
    s = Settings(check_days=0, check_parts=20, check_part=3)
    s.record_check("Verification found 2 errors.", ["abc"])
    assert s.check_suspect and not s.check_damaged
    assert s.check_due                          # re-checks even with automatic checks off
    assert s.check_subset() == ["--read-data-subset=3/20"]  # the same part again
    assert verified_tile(s)[0] == "Checking Again"

    s.record_check("Verification found 2 errors.", ["abc"])
    assert s.check_damaged and not s.check_suspect
    assert s.check_broken == ["abc"]
    assert s.check_subset() == ["--read-data-subset=3/20"]
    assert verified_tile(s) == ("Damaged", "Verification found 2 errors.")


def test_a_passing_read_error_clears():
    s = Settings(check_parts=20, check_part=3)
    s.record_check("Verification found 1 error.", ["abc"])
    s.record_check(None)
    assert not s.check_suspect and not s.check_damaged and not s.check_error and not s.check_broken
    assert s.check_subset() == ["--read-data-subset=4/20"]


def test_check_damage_from_summary():
    assert check_damage(None) is None
    assert check_damage({"num_errors": 0, "broken_packs": None}) is None
    assert check_damage({"num_errors": 2, "broken_packs": ["a"]}) == (
        "Verification found 2 errors in 1 damaged file of the backup. Files stored in them can't be restored.")
    assert check_damage({"num_errors": 1}) == "Verification found 1 error."
