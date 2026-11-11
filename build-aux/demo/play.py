"""Walk the app through its features and record the window, for the README demo.

Runs the real app (inside record.sh's sandbox) and plays the part of the
user: the steps in scenario() press the same buttons a person would. Frames
come from GTK itself, drawn onto a canvas with a caption below, and are piped
to ffmpeg; no screen recorder or visible window is involved.
"""

import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from jungbackup.app import Application  # noqa: E402

FPS = 12
SIZE = (1000, 820)          # same as the screenshots in data/screenshots
PAD, BAND = 28, 60          # canvas around the window, and the caption's strip below it
BACKGROUND, SHADOW, TEXT = "#e9e9ee", "rgba(0,0,0,0.22)", "#2e2e36"
LOST = "resume"             # what the demo searches for; gen.py deleted it from the newest backups


def _rgba(spec):
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


class Recorder:
    """Pipes the window, as GTK draws it, to ffmpeg at a steady frame rate."""

    def __init__(self, win, target):
        self.caption = ""
        self._win = win
        self._paintable = Gtk.WidgetPaintable.new(win)
        self._w, self._h = win.get_width(), win.get_height()
        self._width = (self._w + 2 * PAD + 1) // 2 * 2      # even, for video encoders
        self._height = (self._h + PAD + BAND + 1) // 2 * 2
        self._font = win.get_pango_context().get_font_description().copy()
        self._font.set_weight(Pango.Weight.BOLD)
        self._font.set_size(15 * Pango.SCALE)
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
             "-s", f"{self._width}x{self._height}", "-r", str(FPS), "-i", "-",
             "-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast", target], stdin=subprocess.PIPE)
        self._started = time.monotonic()
        self._written = 0
        self._timer = GLib.timeout_add(1000 // FPS // 2, self._tick)

    def _frame(self):
        s = Gtk.Snapshot()
        s.append_color(_rgba(BACKGROUND), _rect(0, 0, self._width, self._height))
        outline = Gsk.RoundedRect()
        outline.init_from_rect(_rect(PAD, PAD, self._w, self._h), 14)
        s.append_outset_shadow(outline, _rgba(SHADOW), 0, 6, 0, 24)
        s.save()
        s.translate(Graphene.Point().init(PAD, PAD))
        self._paintable.snapshot(s, self._w, self._h)
        s.restore()
        layout = self._win.create_pango_layout(self.caption)
        layout.set_font_description(self._font)
        width, height = layout.get_pixel_size()
        s.save()
        s.translate(Graphene.Point().init((self._width - width) // 2, PAD + self._h + (BAND - height) // 2))
        s.append_layout(layout, _rgba(TEXT))
        s.restore()
        texture = self._win.get_renderer().render_texture(s.to_node(), _rect(0, 0, self._width, self._height))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        assert stride == self._width * 4, stride
        return data.get_data()

    def _tick(self):
        # Repeat the frame when drawing fell behind, so the video keeps real time.
        due = int((time.monotonic() - self._started) * FPS) - self._written
        if due > 0:
            frame = self._frame()
            for _ in range(due):
                self._ffmpeg.stdin.write(frame)
            self._written += due
        return GLib.SOURCE_CONTINUE

    def stop(self):
        GLib.source_remove(self._timer)
        self._ffmpeg.stdin.close()
        if self._ffmpeg.wait() != 0:
            raise SystemExit("ffmpeg failed")


def play(steps, on_error):
    """Run a generator of steps: a number waits that many seconds, a callable waits until it returns true."""
    def advance():
        try:
            step = next(steps)
        except StopIteration:
            return GLib.SOURCE_REMOVE
        except Exception:
            on_error()
            raise
        if callable(step):
            deadline = time.monotonic() + 120

            def poll():
                if step():
                    advance()
                    return GLib.SOURCE_REMOVE
                if time.monotonic() > deadline:
                    on_error()
                    raise SystemExit("the demo got stuck waiting")
                return GLib.SOURCE_CONTINUE
            GLib.timeout_add(50, poll)
        else:
            GLib.timeout_add(int(step * 1000), advance)
        return GLib.SOURCE_REMOVE
    advance()


def scroll(adjustment, target, seconds):
    start, frames = adjustment.get_value(), max(int(seconds * 30), 1)
    for i in range(1, frames + 1):
        t = i / frames
        adjustment.set_value(start + (target - start) * t * t * (3 - 2 * t))
        yield 1 / 30


def bottom(adjustment):
    return adjustment.get_upper() - adjustment.get_page_size()


def find(widget, kind):
    """The first widget of a type inside another, itself included."""
    if isinstance(widget, kind):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = find(child, kind)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def scenario(app, win, target):
    restore, home = win.restore, GLib.get_home_dir()

    def listed():
        return restore._stack.get_visible_child_name() == "list"

    def searched():
        return restore._find_cancellable is None and restore._stack.get_visible_child_name() != "loading"

    yield lambda: win.raw_stats is not None and not win._fetching
    yield 1
    rec = Recorder(win, target)
    try:
        rec.caption = "A dashboard for your backups: how big, how old, how long they take"
        yield 2.2
        dashboard = win.pages.get_child_by_name("backup").get_vadjustment()
        yield from scroll(dashboard, bottom(dashboard), 2.5)
        yield 1.2
        yield from scroll(dashboard, 0, 1.2)

        rec.caption = "Back up now, and watch progress, throughput, CPU and memory live"
        yield 1
        win.hero._backup_btn.emit("clicked")
        yield lambda: win.job is not None
        yield lambda: win.job is None and not win._fetching
        yield 2

        rec.caption = "Restore: every snapshot is on a timeline"
        win.split.set_show_sidebar(True)
        yield 1.2
        win.pages.set_visible_child_name("restore")
        yield 0.8
        win.split.set_show_sidebar(False)
        yield listed
        yield 1.2
        restore._open(os.path.join(home, "Pictures"))
        yield listed
        yield 0.6
        restore._open(os.path.join(home, "Pictures", "2026"))
        yield listed
        yield 1
        for _ in range(4):                      # the same folder, further and further back
            restore._timeline._step(-1)
            yield listed
            yield 0.6
        restore._timeline._select(len(win.snapshots))      # and the newest again
        yield listed
        yield 0.8
        restore._open(os.path.join(home, "Documents"))
        yield listed
        yield 1

        rec.caption = "Lost a file? Search, then step back until it turns up"
        restore._focus_search()
        yield 0.6
        for i in range(1, len(LOST) + 1):
            restore._search.set_text(LOST[:i])
            yield 0.16
        yield lambda: restore._query is not None
        yield searched
        yield 1.4
        for _ in range(6):
            if listed():
                break
            restore._timeline._step(-1)
            yield searched
            yield 0.9
        yield 0.8

        rec.caption = "Restore it in place, save a copy, or just open that version"
        restore._toggle(restore._model.get_item(0), True)
        restore._sync_checks()
        yield 1.5
        restore._restore_btn.emit("clicked")
        yield 1.8
        dialog = win.get_visible_dialog()
        dialog.emit("response", "restore")
        dialog.force_close()
        yield lambda: restore._job is None
        yield 2.2

        rec.caption = "Schedule, retention, folders and restic tuning in Settings"
        win.pages.set_visible_child_name("settings")
        yield 1.3
        settings = find(win.settings_page, Gtk.ScrolledWindow).get_vadjustment()
        yield from scroll(settings, bottom(settings), 2)
        yield 1

        rec.caption = "Jung Backup: restic backups, compatible with Déjà Dup"
        win.pages.set_visible_child_name("backup")
        yield 2.5
    finally:
        rec.stop()
    app.quit()


def main():
    target = sys.argv[1]
    app = Application()

    def activated(app):
        win = app.get_active_window()
        win.set_default_size(*SIZE)
        play(scenario(app, win, target), on_error=app.quit)
    app.connect_after("activate", activated)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
