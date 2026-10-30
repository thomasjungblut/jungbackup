"""When the scheduler retries after failed backups."""

from datetime import timedelta

from jungbackup.core.settings import Settings
from jungbackup.core.util import now


def _failed(attempts, ago=timedelta(minutes=1)):
    t = now()
    return Settings(repository="/r", last_backup=(t - timedelta(days=3)).isoformat(),
                    last_attempt=(t - ago).isoformat(), failed_attempts=attempts), t - ago


def test_retry_delay_doubles_up_to_a_day():
    hours = [Settings(failed_attempts=n).retry_delay / timedelta(hours=1) for n in range(8)]
    assert hours == [1, 1, 2, 4, 8, 16, 24, 24]


def test_next_attempt_waits_for_the_retry_delay():
    s, attempt = _failed(3)
    assert s.next_attempt_time == attempt + timedelta(hours=4)


def test_next_attempt_is_due_once_the_delay_has_passed():
    s, attempt = _failed(1, ago=timedelta(hours=2))
    assert s.next_attempt_time == attempt + timedelta(hours=1) <= now()


def test_next_attempt_without_any_attempt_is_the_schedule():
    s = Settings(repository="/r")
    assert s.next_attempt_time is not None and s.next_attempt_time <= now()
