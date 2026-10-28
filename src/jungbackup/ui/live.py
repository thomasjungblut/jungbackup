"""The live card shown while a backup runs, ours or someone else's.

It's a stack of rows, each fed the same `monitor.Activity` sample: throughput,
process resources, the NFS server (only for a repository on an NFS mount) and
a note saying what the backup is waiting on.
"""

import time

from gi.repository import Adw, Gtk, Pango

from ..backup.monitor import NUM_CPUS, RATE_WINDOW, WAIT_WINDOW, moving_average
from ..core.paths import describe_path
from ..core.util import fmt_bytes, fmt_count, fmt_duration, fmt_rate
from .widgets import Metric, MetricRow, label

PHASES = {"creating": "Creating Backup…", "backing-up": "Backing Up…", "cleaning-up": "Removing Old Backups…"}


def fmt_ms(ms):
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def wait_summary(act):
    """(text, tooltip, warn) saying what a backup with little CPU and I/O is waiting on."""
    waits, nfs = act.waits, act.nfs
    blocked = waits["disk"] + waits["network"]
    slow_nfs = nfs is not None and nfs["rtt_ms"] >= 100
    warn = False
    if blocked >= 0.3:
        warn = True
        if slow_nfs or nfs is not None and waits["network"] >= waits["disk"]:
            where = "the NFS server"
        elif waits["network"] > waits["disk"]:
            where = "a network share"
        else:
            where = "the disk"
        parts = [f"Waiting on {where} {blocked:.0%} of the time"]
    elif waits["cpu"] >= 0.5:
        parts = ["Limited by CPU"]
    elif waits["idle"] >= 0.7:
        parts = ["Mostly idle, waiting on the network, server or another process"]
    else:
        parts = ["No single bottleneck"]

    # NFS details have their own row; only mention what the row doesn't show.
    warn |= slow_nfs or nfs is not None and nfs["retrans"] + nfs["timeouts"] > 0
    if nfs is None and act.tcp_retrans is not None and act.tcp_retrans >= 1:
        parts.append(f"{act.tcp_retrans:.1f}% of TCP packets resent")
        warn = True
    if blocked >= 0.3 and act.io_pressure is not None and act.io_pressure >= 20:
        parts.append(f"system I/O stalled {act.io_pressure:.0f}%")

    tooltip = [
        f"Over the last {WAIT_WINDOW} seconds the backup was:",
        f"  blocked on disk I/O  {waits['disk']:.0%}",
        f"  blocked on network I/O  {waits['network']:.0%}",
        f"  running on CPU  {waits['cpu']:.0%}",
        f"  idle (waiting on a pipe, lock or socket)  {waits['idle']:.0%}",
    ]
    if act.tcp_retrans is not None:
        tooltip.append(f"TCP retransmits (system-wide): {act.tcp_retrans:.2f}% of segments")
    if act.io_pressure is not None:
        tooltip.append(f"I/O pressure: all running tasks stalled {act.io_pressure:.0f}% of the last 10 s")
    if nfs is not None and blocked >= 0.3:
        tooltip.append("Writes to NFS fill memory quickly, then wait while each file is sent to the server,\n"
                       "so this is usually the network, not the server's disks.")
    return " · ".join(parts), "\n".join(tooltip), warn


class ThroughputRow(MetricRow):
    """Read and write rates, averaged so bursty writes to a network share stay readable.

    The big number and the solid line are the RATE_WINDOW-second average; the
    per-second rate is the faint line behind it and the "now" detail.
    """

    def __init__(self):
        self._read = Metric("Reading", sparkline_series=0)
        self._write = Metric("Writing to Backup", sparkline_series=1)
        super().__init__((self._read, 2), (self._write, 2))

    def update(self, act):
        if act is None:
            self._read.set("—")
            self._write.set("—")
            return
        for metric, rate, avg, history, total, verb in (
                (self._read, act.read_rate, act.read_avg, act.read_history, act.read_total, "read"),
                (self._write, act.write_rate, act.write_avg, act.write_history, act.write_total, "written")):
            metric.set(fmt_rate(avg), f"now {fmt_rate(rate)}")
            metric.spark.set_values(history, moving_average(history))
            metric.set_tooltip_text(f"Average over the last {RATE_WINDOW} s; the faint line is per second.\n"
                                    f"{fmt_bytes(total)} {verb} in total")


class ProcessRow(MetricRow):
    def __init__(self):
        self._cpu = Metric("CPU", tooltip=f"Share of all {NUM_CPUS} cores used by the backup.\n"
                                          "System load average over 1, 5 and 15 minutes.")
        self._mem = Metric("Memory")
        self._elapsed = Metric("Elapsed")
        self._eta = Metric("Remaining")
        super().__init__((self._cpu, 1), (self._mem, 1), (self._elapsed, 1), (self._eta, 1))

    def update(self, act, elapsed, remaining):
        self._elapsed.set(elapsed)
        self._eta.set(remaining)
        if act is None:
            self._cpu.set("—")
            self._mem.set("—")
            return
        self._cpu.set(f"{act.cpu_percent:.0f}%", "Load " + " · ".join(f"{avg:.2f}" for avg in act.load))
        self._mem.set(fmt_bytes(act.rss))


class NfsRow(MetricRow):
    """How the NFS server holding the repository is doing; hidden for anything else."""

    def __init__(self):
        self._rtt = Metric("NFS Round Trip", sparkline_series=2,
                           tooltip="How long the NFS server takes to answer a request.\n"
                                   "A healthy server on the local network answers in a few ms.")
        self._ops = Metric("NFS Requests")
        self._retries = Metric("NFS Retries",
                               tooltip="Requests sent again after the server didn't answer in time,\n"
                                       "and the share of TCP packets resent system-wide.")
        super().__init__((self._rtt, 2), (self._ops, 1), (self._retries, 1))

    def update(self, act):
        nfs = act.nfs if act is not None else None
        self.set_visible(nfs is not None)
        if nfs is None:
            return
        self._rtt.spark.set_values(act.rtt_history)
        if nfs["ops"]:
            queued = f"queued {fmt_ms(nfs['queue_ms'])}, " if nfs["queue_ms"] >= 1 else ""
            self._rtt.set(fmt_ms(nfs["rtt_ms"]), f"{queued}{fmt_ms(nfs['exec_ms'])} per request in total")
            self._ops.set(f"{nfs['ops_rate']:.0f}/s", f"{fmt_bytes(nfs['bytes'] / nfs['ops'])} each")
        else:
            self._rtt.set("—")
            self._ops.set("0/s")
        tcp = f"TCP {act.tcp_retrans:.2f}% resent" if act.tcp_retrans is not None else ""
        self._retries.set(str(nfs["retrans"] + nfs["timeouts"]), tcp)


class WaitNote(Gtk.Box):
    """One line naming the bottleneck, in warning colours when the backup is stuck on it."""

    def __init__(self):
        super().__init__(spacing=6, css_classes=["wait-note"])
        self._icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        self.append(self._icon)
        self._label = label("", ("caption",), wrap=True, hexpand=True)
        self.append(self._label)

    def update(self, act):
        self.set_visible(act is not None)
        if act is None:
            return
        text, tooltip, warn = wait_summary(act)
        self._label.set_label(text)
        self.set_tooltip_text(tooltip)
        self._icon.set_visible(warn)
        if warn:
            self.add_css_class("warning")
        else:
            self.remove_css_class("warning")


class LiveCard(Gtk.Revealer):
    """Slides in while a backup runs."""

    def __init__(self, on_cancel):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["card", "live"])
        top = Gtk.Box(spacing=12)
        top.append(Adw.Spinner(width_request=20, height_request=20))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._title = label("", ("heading",))
        self._sub = label("", ("caption", "dim-label"), wrap=True)
        titles.append(self._title)
        titles.append(self._sub)
        top.append(titles)
        self._cancel_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER, css_classes=["pill"])
        self._cancel_btn.connect("clicked", lambda *_: on_cancel())
        top.append(self._cancel_btn)
        card.append(top)

        self._progress = Gtk.ProgressBar(show_text=False)
        card.append(self._progress)
        self._progress_text = label("", ("caption", "numeric"))
        card.append(self._progress_text)

        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._throughput = ThroughputRow()
        self._process = ProcessRow()
        self._nfs = NfsRow()
        for row in (self._throughput, self._process, self._nfs):
            rows.append(row)
        card.append(rows)

        self._wait = WaitNote()
        card.append(self._wait)
        self._files = label("", ("caption", "monospace", "dim-label"), ellipsize=Pango.EllipsizeMode.START)
        card.append(self._files)
        self.set_child(card)

    def update(self, job, act, storage_label, typical_duration):
        """Render our running `job` and/or the monitored `act`ivity; hide when neither.

        `typical_duration` (seconds the last backup took) gives a progress
        estimate for backups that don't report their own, like Déjà Dup's.
        """
        if job is None and act is None:
            self.set_reveal_child(False)
            return
        self.set_reveal_child(True)
        self._cancel_btn.set_visible(job is not None)
        self._cancel_btn.set_sensitive(job is None or not job.cancelled)

        if job is not None and job.cancelled:
            self._title.set_label("Stopping Backup…")
            self._sub.set_label("Waiting for restic to shut down, this can take a moment")
            started = job.started
        elif job is not None:
            self._title.set_label(PHASES.get(job.phase, "Preparing…"))
            self._sub.set_label(f"Saving to {storage_label}")
            started = job.started
        else:
            who = "Déjà Dup" if act.engine in ("restic", "duplicity") else act.engine
            self._title.set_label(f"{who} is running {act.operation}")
            self._sub.set_label("Started outside this app")
            started = act.started

        elapsed = time.time() - started
        status = job.status if job is not None and job.phase == "backing-up" else {}
        if status:
            remaining = self._show_status(job, status)
        else:
            remaining = self._estimate(job, act, elapsed, typical_duration)

        self._throughput.update(act)
        self._process.update(act, fmt_duration(elapsed), remaining)
        self._nfs.update(act)
        self._wait.update(act)

    def _show_status(self, job, status):
        frac = float(status.get("percent_done") or 0)
        self._progress.set_fraction(min(frac, 1.0))
        files_done, total_files = status.get("files_done", 0), status.get("total_files", 0)
        self._progress_text.set_label(
            f"{frac * 100:.0f}% · {fmt_bytes(status.get('bytes_done', 0))} of "
            f"{fmt_bytes(status.get('total_bytes', 0))} · {fmt_count(files_done)} of {fmt_count(total_files)} files"
            + (f" · {len(job.warnings)} skipped" if job.warnings else ""))
        current = status.get("current_files") or []
        self._files.set_label("\n".join(describe_path(f) for f in current[:2]))
        remaining = status.get("seconds_remaining")
        return fmt_duration(remaining) if remaining else "estimating…"

    def _estimate(self, job, act, elapsed, typical):
        """No progress feed: estimate from how long the previous backup took."""
        self._files.set_label(describe_path(act.current_file) if act and act.current_file else "")
        if typical and job is None and act and act.operation == "backup":
            self._progress.set_fraction(min(elapsed / typical, 0.99))
            self._progress_text.set_label(f"Estimated from the last backup, which took {fmt_duration(typical)}")
            return f"~{fmt_duration(max(typical - elapsed, 0))}"
        self._progress.pulse()
        self._progress_text.set_label("")
        return "—"
