"""Calendar labels on the restore timeline's axis."""

from datetime import datetime, timedelta

from jungbackup.ui.timeline import axis_marks


def _texts(start, end):
    """Naive datetimes are local time, as the labels are."""
    return [text for _t, text in axis_marks(start.astimezone(), end.astimezone())]


def test_months_with_the_year_at_january():
    assert _texts(datetime(2025, 10, 20), datetime(2026, 3, 5)) == ["Nov", "Dec", "2026", "Feb", "Mar"]


def test_years_for_long_histories():
    assert _texts(datetime(2019, 5, 1), datetime(2026, 3, 1)) == [str(y) for y in range(2020, 2027)]


def test_days_for_a_few_weeks():
    texts = _texts(datetime(2026, 10, 1, 12), datetime(2026, 10, 20, 9))
    assert texts[0] == "02 Oct" and texts[-1] == "20 Oct" and len(texts) == 19


def test_hours_within_a_day():
    start = datetime(2026, 10, 26, 7, 40)
    assert _texts(start, start + timedelta(hours=3)) == ["08:00", "09:00", "10:00"]
