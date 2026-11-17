"""Verifying the backup: when it's due, which part of the data it reads, and how errors are taken."""

import json
from datetime import timedelta

from jungbackup.backup.job import check_damage, check_findings, check_result
from jungbackup.core.settings import CheckResult, Settings
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
    s = Settings(check_days=7, check_parts=4, check_part=9)
    assert s.check_subset() == ["--read-data-subset=1/4"]


def test_errors_count_once_a_second_check_confirms_them():
    s = Settings(check_days=7, check_parts=20, check_part=3)
    found = CheckResult("Verification found 2 errors.", 2, ("abc",), ("pack abc: does not exist",))
    s.record_check(found)
    assert s.check_suspect and not s.check_damaged
    assert s.check_findings == ["pack abc: does not exist"] and s.check_read == "3/20"
    s.check_days = 0
    assert s.check_due                          # re-checks even with automatic checks off
    s.check_days = 7
    assert s.check_subset() == ["--read-data-subset=3/20"]  # the same part again
    assert verified_tile(s)[0] == "Checking Again"

    s.record_check(found)
    assert s.check_damaged and not s.check_suspect
    assert s.check_broken == ["abc"]
    assert s.check_subset() == ["--read-data-subset=3/20"]
    assert verified_tile(s) == ("Damaged", "2 errors confirmed · see details")
    assert [e["result"] for e in s.check_history] == ["damaged", "suspect"]


def test_a_passing_read_error_clears():
    s = Settings(check_days=7, check_parts=20, check_part=3)
    s.record_check(CheckResult("Verification found 1 error.", 1, ("abc",), ("x",)))
    s.record_check(None)
    assert not s.check_suspect and not s.check_damaged and not s.check_error and not s.check_broken
    assert not s.check_findings
    assert s.check_subset() == ["--read-data-subset=4/20"]


def test_check_damage_from_summary():
    assert check_damage(None) is None
    assert check_damage({"num_errors": 0, "broken_packs": None}) is None
    assert check_damage({"num_errors": 2, "broken_packs": ["a"]}) == (
        "Verification found 2 errors in 1 damaged file of the backup. Files stored in them can't be restored.")
    assert check_damage({"num_errors": 1}) == "Verification found 1 error."


def _stderr(*messages, exit_message="Fatal: repository contains errors"):
    lines = [json.dumps({"message_type": "error", "message": m}) for m in messages]
    return "\n".join(lines + [json.dumps({"message_type": "exit_error", "code": 1, "message": exit_message})])


# As restic 0.19 prints them for a pack with a corrupted byte.
DAMAGED = _stderr(
    "error for tree e4c363cc:\n",
    "  decrypting blob <tree/e4c363cc> from pack ad81 failed: ciphertext verification failed\n",
    "pack ad81 contains 2 errors: [blob e4c3: ciphertext verification failed]\n",
    "\nThe repository contains damaged pack files. These damaged files must be removed to repair the "
    "repository.\n\n",
    "restic repair packs ad81\nrestic repair snapshots --forget\n\n",
    "Damaged pack files can be caused by backend problems, hardware problems or bugs in restic.\n",
)


def test_findings_drop_restics_advice_and_join_details():
    assert check_findings(DAMAGED) == [
        "error for tree e4c363cc: decrypting blob <tree/e4c363cc> from pack ad81 failed: "
        "ciphertext verification failed",
        "pack ad81 contains 2 errors: [blob e4c3: ciphertext verification failed]",
    ]


def test_damage_needs_errors_not_just_a_summary():
    summary = {"message_type": "summary", "num_errors": 2, "broken_packs": ["ad81"], "suggest_prune": True}
    result = check_result(summary, DAMAGED)
    assert result.errors == 2 and result.broken == ("ad81",) and result.hints == ("prune",)
    assert len(result.findings) == 2

    # restic couldn't start (an unreadable lock, a missing repository), yet its summary counts one error.
    fatal = {"message_type": "summary", "num_errors": 1, "broken_packs": None}
    assert check_result(fatal, _stderr(exit_message="Fatal: unable to create lock: input/output error")) is None
    # A lock file it can't read or write: restic reports each try as an error before giving up.
    lock = _stderr("Load(<lock/94f1d039a5>, 0, 0) returned error, retrying after 2.4s: input/output error",
                   "Save(<lock/2ebc6d12f1>) failed: open locks/2ebc…-tmp: permission denied",
                   exit_message="unable to create lock in backend: permission denied")
    assert check_findings(lock) == []
    assert check_result(fatal, lock) is None
    # An error that isn't about a lock, but restic still gave up rather than finishing the check.
    gave_up = _stderr("ReadFull(<data/ad81>): connection reset by peer",
                      exit_message="Fatal: unable to load index: connection reset by peer")
    assert check_result(fatal, gave_up) is None


def test_a_failed_attempt_keeps_the_state():
    s = Settings(check_days=7, check_parts=4)
    s.record_check_failure("The backup is in use by another program.")
    assert not s.check_damaged and s.last_check == ""
    assert s.check_failure and s.check_history[0]["result"] == "failed"
    s.record_check(None)
    assert s.check_failure == ""


def test_damage_recorded_without_messages_is_dropped_on_load(tmp_path, monkeypatch):
    from jungbackup.core import settings as module
    path = tmp_path / "settings.json"
    monkeypatch.setattr(module, "PATH", str(path))
    path.write_text(json.dumps({"repository": "/r", "check_damaged": True, "check_error": "Verification found 1 error.",
                                "last_check": "2026-11-17T11:54:49+00:00", "check_broken": []}))
    s = Settings.load()
    assert not s.check_damaged and not s.check_error and s.last_check == ""
    s.save()
    s.record_check(CheckResult("Verification found 1 error.", 1, (), ("tree x: missing",)))
    s.record_check(CheckResult("Verification found 1 error.", 1, (), ("tree x: missing",)))
    s.save()
    assert Settings.load().check_damaged       # recorded with its messages: kept


def test_reads_everything_with_automatic_checks_off():
    s = Settings(check_days=0, check_parts=4, check_part=2)
    assert s.check_subset() == ["--read-data"] and s.next_check_read() == "all"
    s.record_check(None)
    assert s.check_part == 2                    # a full read doesn't move the rotation along
    assert verified_tile(s) == ("just now", "all data read back")
    s.check_days = 7
    assert s.check_subset() == ["--read-data-subset=2/4"]
