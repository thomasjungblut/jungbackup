"""Formatting helpers shared by the UI."""

from datetime import UTC, datetime

from gi.repository import GLib


def now():
    return datetime.now(UTC)


def parse_time(value):
    """Parse the ISO 8601 timestamps written by Déjà Dup and restic."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        parsed = GLib.DateTime.new_from_iso8601(value, None)
        if parsed is None:
            return None
        dt = datetime.fromtimestamp(parsed.to_unix() + parsed.get_microsecond() / 1e6, UTC)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def fmt_bytes(n):
    if n is None:
        return "—"
    return GLib.format_size(int(n))


def fmt_rate(n):
    if n is None:
        return "—"
    return f"{GLib.format_size(int(n))}/s"


def fmt_count(n):
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def fmt_duration_short(seconds):
    """Compact form for chart axes: 45s, 12m, 3.5h."""
    if not seconds:
        return "0"
    if seconds < 10:
        return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}".rstrip("0").rstrip(".") + "h"


def fmt_bytes_short(n):
    return "0" if not n else fmt_bytes(n)


def _span(seconds):
    seconds = abs(seconds)
    if seconds < 60:
        return None
    for unit, size in (("year", 365 * 86400), ("month", 30 * 86400), ("week", 7 * 86400),
                       ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return None


def fmt_ago(dt):
    if dt is None:
        return "never"
    span = _span((now() - dt).total_seconds())
    return "just now" if span is None else f"{span} ago"


def fmt_until(dt):
    if dt is None:
        return "—"
    delta = (dt - now()).total_seconds()
    if delta < 0:
        span = _span(delta)
        return "overdue" if span is None else f"overdue by {span}"
    span = _span(delta)
    return "any moment" if span is None else f"in {span}"


def fmt_date(dt):
    if dt is None:
        return "—"
    local = dt.astimezone()
    if local.date() == datetime.now().date():
        return local.strftime("Today, %H:%M")
    if local.year == datetime.now().year:
        return local.strftime("%a %d %b, %H:%M")
    return local.strftime("%d %b %Y, %H:%M")


def fmt_days(days):
    if days == 0:
        return "forever"
    if days % 365 == 0:
        n = days // 365
        return f"{n} year{'' if n == 1 else 's'}"
    if days in (182, 183):
        return "6 months"
    if days % 30 == 0 and days >= 30:
        n = days // 30
        return f"{n} month{'' if n == 1 else 's'}"
    if days % 7 == 0:
        n = days // 7
        return f"{n} week{'' if n == 1 else 's'}"
    return f"{days} day{'' if days == 1 else 's'}"
