"""Dashboard sections built from the repository's snapshots and stats."""

from gi.repository import Adw, GLib, Gtk

from ..core.util import (
    fmt_ago,
    fmt_bytes,
    fmt_bytes_short,
    fmt_count,
    fmt_date,
    fmt_duration,
    fmt_duration_short,
    parse_time,
    snapshot_duration,
)
from .charts import BarChart, BreakdownBar, Swatch
from .widgets import StatTile, card, label

HISTORY_BARS = 30
SNAPSHOT_ROWS = 12
TILES = (
    ("size", "Backup Size", "drive-harddisk-symbolic"),
    ("protected", "Protected Data", "security-high-symbolic"),
    ("files", "Files", "folder-documents-symbolic"),
    ("snapshots", "Snapshots", "document-open-recent-symbolic"),
    ("age", "Last Backup", "object-select-symbolic"),
    ("duration", "Last Duration", "preferences-system-time-symbolic"),
    ("added", "Last Upload", "send-to-symbolic"),
    ("free", "Free Space", "drive-harddisk-system-symbolic"),
)
FILE_KINDS = (("new", "New"), ("changed", "Changed"), ("unmodified", "Unchanged"))


def _summary(snap):
    return (snap or {}).get("summary") or {}


def _data_added(summary):
    return summary.get("data_added_packed", summary.get("data_added"))


class StatTiles(Gtk.FlowBox):
    def __init__(self):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         min_children_per_line=2, max_children_per_line=4,
                         column_spacing=12, row_spacing=12)
        self._tiles = {}
        for key, title, icon in TILES:
            tile = StatTile(title, icon)
            self._tiles[key] = tile
            self.append(tile)
            tile.get_parent().set_focusable(False)

    def update(self, snapshots, raw_stats, fs_info, last_backup, storage, fetching):
        """`snapshots` is None until loaded; `fs_info` is (size, free) or None."""
        t = self._tiles
        snaps = snapshots or []
        latest = snaps[-1] if snaps else None
        summary = _summary(latest)
        loading = "Loading…" if fetching else ""

        raw = raw_stats or {}
        if raw.get("total_size") is not None:
            ratio = raw.get("compression_ratio")
            t["size"].set(fmt_bytes(raw["total_size"]),
                          f"{fmt_bytes(raw.get('total_uncompressed_size'))} before compression" if ratio else "")
        else:
            t["size"].set("—", loading or "Unlock to see")

        if summary:
            t["protected"].set(fmt_bytes(summary.get("total_bytes_processed")), "in the latest snapshot")
            t["files"].set(fmt_count(summary.get("total_files_processed")),
                           f"{fmt_count(summary.get('files_new', 0))} new, "
                           f"{fmt_count(summary.get('files_changed', 0))} changed")
            t["added"].set(fmt_bytes(_data_added(summary)),
                           f"new data stored {fmt_ago(parse_time(summary.get('backup_end')))}")
            durations = [d for d in (snapshot_duration(s) for s in snaps[-10:]) if d]
            avg = sum(durations) / len(durations) if durations else None
            t["duration"].set(fmt_duration(snapshot_duration(latest)),
                              f"average {fmt_duration(avg)}" if avg else "")
        else:
            for key in ("protected", "files", "added", "duration"):
                t[key].set("—", loading)

        if snaps:
            oldest = parse_time(snaps[0].get("time"))
            t["snapshots"].set(fmt_count(len(snaps)), f"oldest from {fmt_date(oldest)}")
        else:
            t["snapshots"].set("—" if snapshots is None else "0", loading)

        t["age"].set(fmt_ago(last_backup).replace(" ago", "") if last_backup else "Never",
                     fmt_date(last_backup) if last_backup else "")

        if fs_info and fs_info[0]:
            size, free = fs_info
            t["free"].set(fmt_bytes(free), f"{100 * free / size:.0f}% of {fmt_bytes(size)}")
        else:
            t["free"].set("—", "unknown for this location" if storage.reachable() else "location not available")


class FilesBreakdown(Gtk.Box):
    """New / changed / unchanged files in the latest snapshot."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "chart-card"])
        head = Gtk.Box()
        head.append(label("Files in Last Backup", ("heading",), hexpand=True))
        self._total = label("", ("dim-label", "numeric"))
        head.append(self._total)
        self.append(head)
        self._bar = BreakdownBar()
        self.append(self._bar)
        legend = Gtk.Box(spacing=18)
        self._legend = {}
        for i, (key, name) in enumerate(FILE_KINDS):
            item = Gtk.Box(spacing=6)
            item.append(Swatch(i))
            self._legend[key] = label(name, ("caption", "numeric"))
            item.append(self._legend[key])
            legend.append(item)
        self.append(legend)
        self._dirs = label("", ("caption", "dim-label"))
        self.append(self._dirs)

    def update(self, latest):
        summary = _summary(latest)
        self.set_visible(bool(summary))
        if not summary:
            return
        values = {k: summary.get(f"files_{k}", 0) for k, _ in FILE_KINDS}
        self._bar.set_segments([(values[k], i, f"{fmt_count(values[k])} {name.lower()} files")
                                for i, (k, name) in enumerate(FILE_KINDS)])
        for key, name in FILE_KINDS:
            self._legend[key].set_label(f"{name} {fmt_count(values[key])}")
        self._total.set_label(f"{fmt_count(sum(values.values()))} files")
        self._dirs.set_label(
            f"Folders: {fmt_count(summary.get('dirs_new', 0))} new · {fmt_count(summary.get('dirs_changed', 0))} "
            f"changed · {fmt_count(summary.get('dirs_unmodified', 0))} unchanged")


class HistoryCharts(Gtk.Box):
    """Data added and duration of the recent backups, side by side."""

    def __init__(self):
        super().__init__(spacing=12, homogeneous=True)
        self._added = BarChart(fmt_bytes_short)
        self._durations = BarChart(fmt_duration_short)
        self.append(card("Data Added per Backup", self._added))
        self.append(card("Duration per Backup", self._durations))

    def update(self, snapshots):
        snaps = [s for s in (snapshots or []) if s.get("summary")][-HISTORY_BARS:]
        self.set_visible(bool(snaps))
        added, durations = [], []
        for s in snaps:
            t = parse_time(s.get("time"))
            bar = t.astimezone().strftime("%d %b") if t else ""
            when = GLib.markup_escape_text(fmt_date(t))
            data = _data_added(s["summary"]) or 0
            added.append((bar, data, f"<b>{when}</b>\n{fmt_bytes(data)} added"))
            d = snapshot_duration(s) or 0
            durations.append((bar, d, f"<b>{when}</b>\nTook {fmt_duration(d)}"))
        self._added.set_items(added)
        self._durations.set_items(durations)


class SnapshotList(Adw.PreferencesGroup):
    def __init__(self):
        super().__init__(title="Recent Snapshots")
        self._rows = []

    def update(self, snapshots):
        for row in self._rows:
            self.remove(row)
        self._rows = []
        snaps = snapshots or []
        self.set_visible(bool(snaps))
        if not snaps:
            return
        host = GLib.get_host_name()
        self.set_description(f"{len(snaps)} snapshots in total")
        for s in reversed(snaps[-SNAPSHOT_ROWS:]):
            summary = _summary(s)
            bits = []
            if summary:
                bits.append(f"{fmt_bytes(_data_added(summary))} added")
                bits.append(f"{fmt_count(summary.get('total_files_processed'))} files")
                bits.append(f"took {fmt_duration(snapshot_duration(s))}")
            if s.get("hostname") and s["hostname"] != host:
                bits.append(f"from {s['hostname']}")
            row = Adw.ActionRow(title=GLib.markup_escape_text(fmt_date(parse_time(s.get("time")))),
                                subtitle=GLib.markup_escape_text(" · ".join(bits)))
            row.add_suffix(label(s.get("short_id", s.get("id", "")[:8]), ("caption", "monospace", "dim-label")))
            self.add(row)
            self._rows.append(row)
