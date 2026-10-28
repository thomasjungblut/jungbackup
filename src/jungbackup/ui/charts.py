"""Small Cairo-drawn charts that follow the libadwaita look."""

import math

from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

# Categorical slots 1–3 (blue, orange, aqua), stepped separately for light and dark.
SERIES = {
    False: ["#2a78d6", "#eb6834", "#1baf7a"],
    True: ["#3987e5", "#d95926", "#199e70"],
}


def series_color(index):
    rgba = Gdk.RGBA()
    rgba.parse(SERIES[Adw.StyleManager.get_default().get_dark()][index])
    return rgba


def _set(cr, rgba, alpha=1.0):
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha * alpha)


def _rounded_top(cr, x, y, w, h, r):
    """Bar with rounded data-end (top), square at the baseline."""
    r = min(r, w / 2, h)
    cr.new_path()
    cr.move_to(x, y + h)
    cr.line_to(x, y + r)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    cr.line_to(x + w, y + h)
    cr.close_path()


def _nice_ceiling(value):
    if value <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * exp >= value:
            return m * exp
    return 10 * exp


class _Watcher:
    """Redraw when the light/dark style flips."""

    def _watch_style(self):
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())


class BarChart(Gtk.DrawingArea, _Watcher):
    """Single-series column chart with hover highlight and tooltips."""

    def __init__(self, value_format, height=150, series=0):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_tooltip)
        self._format = value_format
        self._series = series
        self._items = []        # (x_label, value, tooltip)
        self._hover = None
        self._geometry = None
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        self._watch_style()

    def set_items(self, items):
        self._items = list(items)
        self._hover = None
        self.queue_draw()

    def _index_at(self, x):
        if not self._geometry or not self._items:
            return None
        left, slot = self._geometry
        i = int((x - left) // slot)
        return i if 0 <= i < len(self._items) else None

    def _on_motion(self, _ctrl, x, _y):
        i = self._index_at(x)
        if i != self._hover:
            self._hover = i
            self.queue_draw()

    def _on_leave(self, _ctrl):
        self._hover = None
        self.queue_draw()

    def _on_tooltip(self, _widget, x, _y, _keyboard, tooltip):
        i = self._index_at(x)
        if i is None:
            return False
        tooltip.set_markup(self._items[i][2])
        return True

    def _text(self, cr, text, x, y, color, align=0.0, alpha=1.0, small=True):
        layout = self.create_pango_layout(text)
        if small:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_scale_new(0.8))
            attrs.insert(Pango.attr_font_features_new("tnum=1"))
            layout.set_attributes(attrs)
        _w, _h = layout.get_pixel_size()
        _set(cr, color, alpha)
        cr.move_to(x - _w * align, y - _h / 2)
        PangoCairo.show_layout(cr, layout)
        return _w

    def _draw(self, _area, cr, width, height):
        fg = self.get_color()
        color = series_color(self._series)
        if not self._items:
            self._text(cr, "No data yet", width / 2, height / 2, fg, align=0.5, alpha=0.55, small=False)
            return

        top_value = _nice_ceiling(max(v for _, v, _ in self._items))
        labels = [self._format(top_value), self._format(top_value / 2), self._format(0)]
        gutter = max(self.create_pango_layout(t).get_pixel_size()[0] for t in labels) * 0.8 + 10
        top, bottom = 8, height - 22
        plot_h = bottom - top
        left, right = gutter, width - 2

        # Recessive grid: baseline, middle, top.
        for frac, label in ((1.0, labels[0]), (0.5, labels[1]), (0.0, labels[2])):
            y = round(bottom - plot_h * frac) + 0.5
            _set(cr, fg, 0.10 if frac else 0.25)
            cr.set_line_width(1)
            cr.move_to(left, y)
            cr.line_to(right, y)
            cr.stroke()
            self._text(cr, label, gutter - 8, y, fg, align=1.0, alpha=0.6)

        n = len(self._items)
        slot = (right - left) / n
        gap = max(2.0, slot * 0.25)
        bar_w = min(max(1.0, slot - gap), 36)
        self._geometry = (left, slot)
        for i, (_label, value, _tip) in enumerate(self._items):
            h = plot_h * (value / top_value) if top_value else 0
            x = left + i * slot + (slot - bar_w) / 2
            if h < 1 and value > 0:
                h = 1
            _rounded_top(cr, x, bottom - h, bar_w, h, 4)
            _set(cr, color, 1.0 if self._hover in (None, i) else 0.45)
            cr.fill()

        # Label first and last bars only, to keep the axis quiet.
        self._text(cr, self._items[0][0], left, height - 9, fg, align=0.0, alpha=0.6)
        if n > 1:
            self._text(cr, self._items[-1][0], right, height - 9, fg, align=1.0, alpha=0.6)


class Sparkline(Gtk.DrawingArea, _Watcher):
    """Recent values as a filled line.

    With `trend` set, the raw values fade into the background and the trend
    (e.g. a moving average of them) is drawn on top as the main line.
    """

    def __init__(self, series=0, height=36):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self._series = series
        self._values = []
        self._trend = None
        self._watch_style()

    def set_values(self, values, trend=None):
        self._values = list(values)
        self._trend = list(trend) if trend is not None else None
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        values = self._values
        if len(values) < 2:
            return
        color = series_color(self._series)
        peak = max(values) or 1
        step = width / (max(len(values), 30) - 1)
        x0 = width - step * (len(values) - 1)

        def points(vals):
            return [(x0 + i * step, height - 2 - (height - 4) * v / peak) for i, v in enumerate(vals)]

        def line(pts):
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)

        pts = points(values)
        cr.move_to(pts[0][0], height)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], height)
        cr.close_path()
        _set(cr, color, 0.15)
        cr.fill()
        cr.set_line_join(1)  # round
        if self._trend:
            line(pts)
            cr.set_line_width(1)
            _set(cr, color, 0.4)
            cr.stroke()
            line(points(self._trend))
        else:
            line(pts)
        cr.set_line_width(2)
        _set(cr, color)
        cr.stroke()


class BreakdownBar(Gtk.DrawingArea, _Watcher):
    """Horizontal stacked bar; segments separated by a 2px gap."""

    def __init__(self, height=14):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_tooltip)
        self._segments = []  # (value, series_index, tooltip)
        self._spans = []
        self._watch_style()

    def set_segments(self, segments):
        self._segments = [s for s in segments if s[0] > 0]
        self.queue_draw()

    def _on_tooltip(self, _w, x, _y, _kb, tooltip):
        for x0, x1, tip in self._spans:
            if x0 <= x <= x1:
                tooltip.set_markup(tip)
                return True
        return False

    def _draw(self, _area, cr, width, height):
        total = sum(v for v, _, _ in self._segments)
        self._spans = []
        if not total:
            _set(cr, self.get_color(), 0.1)
            cr.rectangle(0, 0, width, height)
            cr.fill()
            return
        r = min(4, height / 2)
        # Clip to a rounded rectangle so only the outer ends are rounded.
        cr.new_path()
        cr.arc(r, r, r, math.pi, 1.5 * math.pi)
        cr.arc(width - r, r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(width - r, height - r, r, 0, 0.5 * math.pi)
        cr.arc(r, height - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.clip()
        gap = 2
        usable = width - gap * (len(self._segments) - 1)
        x = 0
        for value, series, tip in self._segments:
            w = max(2, usable * value / total)
            cr.rectangle(x, 0, w, height)
            _set(cr, series_color(series))
            cr.fill()
            self._spans.append((x, x + w, tip))
            x += w + gap


class Swatch(Gtk.DrawingArea, _Watcher):
    def __init__(self, series):
        super().__init__()
        self.set_content_width(10)
        self.set_content_height(10)
        self.set_valign(Gtk.Align.CENTER)
        self._series = series
        self.set_draw_func(self._draw)
        self._watch_style()

    def _draw(self, _area, cr, width, height):
        cr.arc(width / 2, height / 2, min(width, height) / 2, 0, 2 * math.pi)
        _set(cr, series_color(self._series))
        cr.fill()
