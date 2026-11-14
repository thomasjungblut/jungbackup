"""The status line for GNOME's Background Apps menu, and how often it's sent."""

from datetime import timedelta
from types import SimpleNamespace

from jungbackup.core.portal import MAX_STATUS, BackgroundStatus, status_message
from jungbackup.core.settings import Settings
from jungbackup.core.util import now


def job(phase="backing-up", status=None, cancelled=False):
    return SimpleNamespace(phase=phase, status=status or {}, cancelled=cancelled)


def configured(**kw):
    return Settings(repository="/backup", **kw)


def test_progress_while_backing_up():
    s = configured()
    assert status_message(job(status={"percent_done": 0.432}), s) == "Backing up, 43%"
    assert (status_message(job(status={"percent_done": 0.5, "seconds_remaining": 125}), s)
            == "Backing up, 50% · 3 min left")
    assert (status_message(job(status={"percent_done": 0.9, "seconds_remaining": 30}), s)
            == "Backing up, 90% · less than a minute left")
    assert (status_message(job(status={"percent_done": 0.1, "seconds_remaining": 3 * 3600 + 5 * 60}), s)
            == "Backing up, 10% · 3 h 05 min left")


def test_other_phases():
    s = configured()
    assert status_message(job("preparing"), s) == "Preparing backup…"
    assert status_message(job("backing-up"), s) == "Preparing backup…"   # no status from restic yet
    assert status_message(job("creating"), s) == "Creating backup…"
    assert status_message(job("cleaning-up"), s) == "Removing old backups…"
    assert status_message(job(status={"percent_done": 0.4}, cancelled=True), s) == "Stopping backup…"


def test_idle():
    assert status_message(None, Settings()) is None
    assert status_message(None, configured()) == "No backups yet · starting soon"
    last = (now() - timedelta(hours=2, minutes=1)).isoformat()
    assert status_message(None, configured(last_backup=last)) == "Last backup 2 hours ago · next in 21 hours"
    assert status_message(None, configured(last_backup=last, periodic=False)) == "Last backup 2 hours ago"
    failed = configured(last_backup=last, last_error="boom", failed_attempts=1,
                        last_attempt=(now() - timedelta(minutes=20, seconds=1)).isoformat(), period_days=0)
    assert status_message(None, failed) == "Last backup failed, trying again in 39 minutes"


class FakePortal:
    def __init__(self):
        self.sent = []

    def set_background_status(self, text, cancellable, callback):
        self.sent.append(text)
        callback(self, None)

    def set_background_status_finish(self, result):
        return True


def test_sends_only_changes_and_rate_limits_progress():
    portal, t = FakePortal(), [0.0]
    status = BackgroundStatus(portal, clock=lambda: t[0])
    status.update("Backing up, 1%")
    status.update("Backing up, 1%")
    t[0] = 1.0
    status.update("Backing up, 2%")             # too soon
    status.update("Removing old backups…", urgent=True)
    t[0] = 4.0
    status.update("Removing old backups…")
    status.update(None, urgent=True)
    assert portal.sent == ["Backing up, 1%", "Removing old backups…", None]


def test_truncates_long_messages():
    portal = FakePortal()
    BackgroundStatus(portal).update("x" * 200)
    assert len(portal.sent[0]) == MAX_STATUS


def test_stops_after_the_portal_refuses():
    class Refusing(FakePortal):
        def set_background_status_finish(self, result):
            raise RuntimeError("Only sandboxed applications can set background status")

    portal = Refusing()
    status = BackgroundStatus(portal)
    status.update("a", urgent=True)
    status.update("b", urgent=True)
    assert portal.sent == ["a"]
