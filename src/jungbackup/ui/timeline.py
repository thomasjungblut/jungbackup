"""Pick a snapshot on a time axis."""

import math
from datetime import datetime, timedelta

from gi.repository import Adw, Gdk, GLib, Gtk, Pango, PangoCairo

from ..core.util import fmt_ago, fmt_date, parse_time
from .charts import _set, _Watcher
from .widgets import label

PAD = 14          # keeps the end markers clear of the card's edge
HIT = 16          # how close a click or hover has to be to a snapshot, in px
LABEL_GAP = 12


def axis_marks(start, end):
    """Calendar boundaries between two datetimes to label the axis with: [(datetime, text)].

    The unit follows the span: years, months, days or hours. Labels that don't
    fit are dropped when drawing, so this can return more than will be shown.
    """
    start, end = start.astimezone(), end.astimezone()
    days = (end - start).total_seconds() / 86400
    tz = start.tzinfo
    marks = []
    if days > 3 * 365:
        for year in range(start.year, end.year + 1):
            marks.append((datetime(year, 1, 1, tzinfo=tz), str(year)))
    elif days > 75:
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            marks.append((datetime(year, month, 1, tzinfo=tz),
                          str(year) if month == 1 else datetime(year, month, 1).strftime("%b")))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    elif days > 2:
        day = datetime(start.year, start.month, start.day, tzinfo=tz)
        while day <= end:
            marks.append((day, day.strftime("%d %b")))
            day += timedelta(days=1)
    else:
        hour = datetime(start.year, start.month, start.day, start.hour, tzinfo=tz)
        while hour <= end:
            marks.append((hour, hour.strftime("%H:%M")))
            hour += timedelta(hours=1)
    return [(t, text) for t, text in marks if start <= t <= end]


def _accent():
    try:
        return Adw.StyleManager.get_default().get_accent_color_rgba()
    except AttributeError:   # libadwaita < 1.6
        rgba = Gdk.RGBA()
        rgba.parse("#3584e4")
        return rgba


class Timeline(Gtk.Box):
    """Snapshots as ticks on a time axis, the chosen one highlighted.

    on_changed(snapshot) is called when the user picks another one.
    """

    def __init__(self, on_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "timeline"])
        self._on_changed = on_changed
        self._snapshots = []      # oldest first
        self._times = []
        self._index = None
        self._hover = None
        self._xs = []

        head = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self._title = label("", ("heading",), ellipsize=Pango.EllipsizeMode.END)
        self._sub = label("", ("caption", "dim-label"), ellipsize=Pango.EllipsizeMode.END)
        text.append(self._title)
        text.append(self._sub)
        head.append(text)
        steps = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self._older = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Older Snapshot")
        self._older.connect("clicked", lambda *_: self._step(-1))
        self._newer = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Newer Snapshot")
        self._newer.connect("clicked", lambda *_: self._step(1))
        steps.append(self._older)
        steps.append(self._newer)
        head.append(steps)
        self.append(head)

        self._area = _Area(self)
        self.append(self._area)

    # Data ----------------------------------------------------------------------

    def set_snapshots(self, snapshots, selected=None):
        """`snapshots` in any order; `selected` is the one to highlight (default: newest)."""
        pairs = sorted(((parse_time(s.get("time")), s) for s in snapshots), key=lambda p: p[0] or datetime.min)
        pairs = [(t, s) for t, s in pairs if t is not None]
        self._times = [t for t, _s in pairs]
        self._snapshots = [s for _t, s in pairs]
        ids = [s.get("id") for s in self._snapshots]
        self._index = (ids.index(selected["id"]) if selected and selected.get("id") in ids
                       else len(ids) - 1 if ids else None)
        self._hover = None
        self._render()

    @property
    def selected(self):
        return self._snapshots[self._index] if self._index is not None else None

    def _select(self, index):
        index = max(0, min(index, len(self._snapshots) - 1))
        if index == self._index or not self._snapshots:
            return
        self._index = index
        self._render()
        self._on_changed(self.selected)

    def _step(self, delta):
        if self._index is not None:
            self._select(self._index + delta)

    def _render(self):
        n, i = len(self._snapshots), self._index
        self._older.set_sensitive(i is not None and i > 0)
        self._newer.set_sensitive(i is not None and i < n - 1)
        if i is None:
            self._title.set_label("No snapshots")
            self._sub.set_label("")
        else:
            t = self._times[i]
            self._title.set_label(fmt_date(t))
            bits = ["Latest" if i == n - 1 else fmt_ago(t).capitalize(), f"snapshot {i + 1} of {n}"]
            host = self._snapshots[i].get("hostname")
            if host and host != GLib.get_host_name():
                bits.append(f"from {host}")
            self._sub.set_label(" · ".join(bits))
        self._area.queue_draw()

    # Geometry ------------------------------------------------------------------

    def x_positions(self, width):
        if not self._times:
            return []
        left, right = PAD, width - PAD
        t0, t1 = self._times[0], self._times[-1]
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return [(left + right) / 2] * len(self._times)
        return [left + (right - left) * (t - t0).total_seconds() / span for t in self._times]

    def nearest(self, x):
        if not self._xs:
            return None
        best = min(range(len(self._xs)), key=lambda i: (abs(self._xs[i] - x), -i))
        return best


class _Area(Gtk.DrawingArea, _Watcher):
    def __init__(self, timeline):
        super().__init__(content_height=46, hexpand=True, focusable=True, has_tooltip=True)
        self._tl = timeline
        self.set_draw_func(self._draw)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.connect("query-tooltip", self._on_tooltip)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: self._hover(None))
        self.add_controller(motion)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.queue_draw())
        focus.connect("leave", lambda *_: self.queue_draw())
        self.add_controller(focus)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._watch_style()

    def _hover(self, index):
        if index != self._tl._hover:
            self._tl._hover = index
            self.queue_draw()

    def _near(self, x):
        i = self._tl.nearest(x)
        return i if i is not None and abs(self._tl._xs[i] - x) <= HIT else None

    def _on_click(self, _gesture, _n, x, _y):
        self.grab_focus()
        i = self._tl.nearest(x)
        if i is not None:
            self._tl._select(i)

    def _on_motion(self, _ctrl, x, _y):
        self._hover(self._near(x))

    def _on_key(self, _ctrl, keyval, _code, _state):
        tl, n = self._tl, len(self._tl._snapshots)
        moves = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Page_Up: -10, Gdk.KEY_Page_Down: 10}
        if keyval in moves:
            tl._step(moves[keyval])
        elif keyval == Gdk.KEY_Home:
            tl._select(0)
        elif keyval == Gdk.KEY_End:
            tl._select(n - 1)
        else:
            return False
        return True

    def _on_tooltip(self, _w, x, _y, keyboard, tooltip):
        i = self._tl._index if keyboard else self._near(x)
        if i is None:
            return False
        text = fmt_date(self._tl._times[i])
        host = self._tl._snapshots[i].get("hostname")
        if host and host != GLib.get_host_name():
            text += f"\nfrom {host}"
        tooltip.set_text(text)
        return True

    def _text(self, cr, text, x, y, color, alpha, align):
        layout = self.create_pango_layout(text)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_scale_new(0.8))
        attrs.insert(Pango.attr_font_features_new("tnum=1"))
        layout.set_attributes(attrs)
        w, h = layout.get_pixel_size()
        x = min(max(x - w * align, 0), self.get_width() - w)
        _set(cr, color, alpha)
        cr.move_to(x, y - h / 2)
        PangoCairo.show_layout(cr, layout)
        return x, x + w

    def _draw(self, _area, cr, width, height):
        tl = self._tl
        fg, accent = self.get_color(), _accent()
        tl._xs = xs = tl.x_positions(width)
        axis = 14
        cr.set_line_cap(1)  # round

        _set(cr, fg, 0.15)
        cr.set_line_width(2)
        cr.move_to(PAD, axis)
        cr.line_to(width - PAD, axis)
        cr.stroke()
        if not xs:
            return

        cr.set_line_width(1.5)
        for i, x in enumerate(xs):
            hovered = i == tl._hover
            _set(cr, fg, 0.85 if hovered else 0.4)
            reach = 8 if hovered else 5
            cr.move_to(round(x) + 0.5, axis - reach)
            cr.line_to(round(x) + 0.5, axis + reach)
            cr.stroke()

        if tl._index is not None:
            x = xs[tl._index]
            cr.arc(x, axis, 7, 0, 2 * math.pi)
            _set(cr, accent)
            cr.fill()
            if self.has_focus():
                cr.arc(x, axis, 10, 0, 2 * math.pi)
                cr.set_line_width(2)
                _set(cr, accent, 0.5)
                cr.stroke()

        # Calendar labels under the axis, dropping any that would overlap.
        y = height - 9
        t0, t1 = tl._times[0], tl._times[-1]
        spans = []
        if t1 > t0:
            for t, text in axis_marks(t0, t1):
                mx = PAD + (width - 2 * PAD) * (t - t0).total_seconds() / (t1 - t0).total_seconds()
                w = self.create_pango_layout(text).get_pixel_size()[0] * 0.8
                if spans and mx - w / 2 < spans[-1] + LABEL_GAP:
                    continue
                _set(cr, fg, 0.12)
                cr.set_line_width(1)
                cr.move_to(round(mx) + 0.5, axis + 9)
                cr.line_to(round(mx) + 0.5, axis + 13)
                cr.stroke()
                spans.append(self._text(cr, text, mx, y, fg, 0.6, 0.5)[1])
        else:
            self._text(cr, fmt_date(t0), xs[0], y, fg, 0.6, 0.5)
