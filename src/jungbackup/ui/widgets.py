"""Small building blocks shared by the dashboard sections."""

from gi.repository import Gtk, Pango

from .charts import Sparkline

COLUMN_SPACING = 24


def label(text="", css=(), xalign=0.0, **kw):
    widget = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css:
        widget.add_css_class(c)
    return widget


def card(title, child):
    """Titled card around a chart or similar."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "chart-card"])
    box.append(label(title, ("heading",)))
    box.append(child)
    return box


class StatTile(Gtk.Box):
    def __init__(self, title, icon):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.add_css_class("stat-tile")
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Image(icon_name=icon, css_classes=["dim-label"]))
        head.append(label(title, ("caption-heading", "dim-label")))
        self.append(head)
        self._value = label("—", ("stat-value",), ellipsize=Pango.EllipsizeMode.END)
        self._sub = label("", ("caption", "dim-label"), ellipsize=Pango.EllipsizeMode.END)
        self.append(self._value)
        self.append(self._sub)

    def set(self, value, sub=""):
        self._value.set_label(value)
        self._sub.set_label(sub)
        self.set_tooltip_text(f"{value}\n{sub}" if sub else value)


class Metric(Gtk.Box):
    """Small labelled value used in the live activity card."""

    def __init__(self, title, sparkline_series=None, tooltip=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.append(label(title, ("caption-heading", "dim-label")))
        self._value = label("—", ("metric-value",))
        self.append(self._value)
        self.spark = None
        if sparkline_series is not None:
            self.spark = Sparkline(series=sparkline_series)
            self.append(self.spark)
        self._detail = label("", ("caption", "dim-label", "numeric"), visible=False)
        self.append(self._detail)
        if tooltip:
            self.set_tooltip_text(tooltip)

    def set(self, text, detail=""):
        self._value.set_label(text)
        self._detail.set_label(detail)
        self._detail.set_visible(bool(detail))


class MetricRow(Gtk.Grid):
    """A row of metrics on a shared grid of equal columns.

    Every row has the same number of columns and spacing, so stacked rows line
    up even though each is its own widget and can be hidden on its own.
    """

    COLUMNS = 4

    def __init__(self, *metrics):
        super().__init__(column_spacing=COLUMN_SPACING, column_homogeneous=True)
        col = 0
        for metric, span in metrics:
            self.attach(metric, col, 0, span, 1)
            col += span
        assert col == self.COLUMNS, f"metric row spans {col} columns, not {self.COLUMNS}"
