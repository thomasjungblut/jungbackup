"""The Background portal: autostart at login, and the status line GNOME shows
under the app in its "Background Apps" menu while no window is open.

Both only work inside a Flatpak: the portal refuses SetStatus from host apps,
and autostart for them is handled by a .desktop file instead.
"""

import math
import os
import time

import gi

from .util import fmt_ago, fmt_until

MAX_STATUS = 96         # the portal rejects longer messages
MIN_INTERVAL = 2.0      # seconds between status updates while the text keeps changing (progress)

_portal = None


def _get_portal():
    """The shared Xdp.Portal, or None outside a Flatpak or without libportal."""
    global _portal
    if _portal is None:
        if not os.path.exists("/.flatpak-info"):
            _portal = False
        else:
            try:
                gi.require_version("Xdp", "1.0")
                from gi.repository import Xdp
                _portal = Xdp.Portal.new()
            except (ImportError, ValueError):
                _portal = False
    return _portal or None


def request_autostart(window, enabled):
    """Ask the Background portal to start us at login."""
    portal = _get_portal()
    if portal is None:
        return
    try:
        gi.require_version("XdpGtk4", "1.0")
        from gi.repository import Xdp, XdpGtk4
    except (ImportError, ValueError):
        return
    parent = XdpGtk4.parent_new_gtk(window) if window is not None else None
    flags = Xdp.BackgroundFlags.AUTOSTART if enabled else Xdp.BackgroundFlags.NONE

    def done(portal, result):
        try:
            portal.request_background_finish(result)
        except Exception:  # denied by the user, or no portal available
            pass

    portal.request_background(parent, "Run scheduled backups", ["jungbackup", "--background"], flags, None, done)


def _time_left(seconds):
    """Minute-granular, so the status doesn't change every second."""
    if seconds < 60:
        return "less than a minute left"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min left"


def status_message(job, settings):
    """One line on what the app is doing: progress while backing up, else when the next backup is."""
    if job is not None:
        if job.cancelled:
            return "Stopping backup…"
        if job.phase == "creating":
            return "Creating backup…"
        if job.phase == "cleaning-up":
            return "Removing old backups…"
        if job.phase == "verifying":
            return "Verifying backup…"
        if job.phase == "backing-up" and job.status:
            pct = min(float(job.status.get("percent_done") or 0), 1.0) * 100
            text = f"Backing up, {pct:.0f}%"
            remaining = job.status.get("seconds_remaining")
            return f"{text} · {_time_left(remaining)}" if remaining else text
        return "Preparing backup…"
    if not settings.configured:
        return None
    last = settings.last_backup_time
    if settings.failed_attempts and settings.last_error:
        if settings.periodic:
            return f"Last backup failed, trying again {fmt_until(settings.next_attempt_time)}"
        return "Last backup failed"
    head = f"Last backup {fmt_ago(last)}" if last else "No backups yet"
    if not settings.periodic:
        return head
    return f"{head} · next {fmt_until(settings.next_backup_time)}" if last else f"{head} · starting soon"


class BackgroundStatus:
    """Sends status lines to the portal: only when they change, and at most every MIN_INTERVAL seconds."""

    def __init__(self, portal=None, clock=time.monotonic):
        self._portal = portal
        self._clock = clock
        self._sent = None
        self._sent_at = -math.inf
        self._disabled = False

    def update(self, text, urgent=False):
        """Show `text` (None clears it). `urgent` skips the rate limit, for phase changes."""
        if self._disabled:
            return
        if text is not None and len(text) > MAX_STATUS:
            text = text[:MAX_STATUS - 1] + "…"
        if text == self._sent:
            return
        if not urgent and self._clock() - self._sent_at < MIN_INTERVAL:
            return      # the next tick catches up
        portal = self._portal or _get_portal()
        if portal is None:
            self._disabled = True
            return
        self._sent, self._sent_at = text, self._clock()
        portal.set_background_status(text, None, self._done)

    def _done(self, portal, result):
        try:
            portal.set_background_status_finish(result)
        except Exception:   # older portal without SetStatus, or not allowed: stop trying
            self._disabled = True
