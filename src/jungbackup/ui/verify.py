"""The Verify page: what the last checks of the backup's data found, and how to repair damage."""

import shlex

from gi.repository import Adw, Gtk, Pango

from ..backup.restic import in_flatpak
from ..core.util import fmt_ago, fmt_date, fmt_days, parse_time
from .widgets import label

TROUBLESHOOTING = "https://restic.readthedocs.io/en/stable/077_troubleshooting.html"
STATES = ("good", "warning", "error", "accent")
RESULTS = {
    "clean": ("object-select-symbolic", "success"),
    "suspect": ("dialog-warning-symbolic", "warning"),
    "damaged": ("dialog-error-symbolic", "error"),
    "failed": ("action-unavailable-symbolic", "dim-label"),
}


def fmt_read(read):
    if not read:
        return ""
    if read == "all":
        return "all data"
    part, _, parts = read.partition("/")
    return f"part {part} of {parts}"


def fmt_read_of_data(read):
    """"part 3 of 20 of the stored data", or "all of the stored data"."""
    return "all of the stored data" if read == "all" else f"{fmt_read(read)} of the stored data"


def restic_command(repo, *args):
    """A restic command line the user can paste into a terminal; it asks for the password itself."""
    prefix = "flatpak run --command=restic io.jungblut.JungBackup" if in_flatpak() else "restic"
    return " ".join([prefix, "-r", shlex.quote(repo), *args])


def repair_steps(settings):
    """[(title, command)] that repair what the last check found, in order."""
    s = settings
    steps = []
    if s.check_broken:
        steps.append(("Salvage what's readable from the damaged files and remove them",
                      restic_command(s.repository, "repair", "packs", *s.check_broken)))
    if "repair-index" in s.check_hints or (s.check_findings and not s.check_broken):
        steps.append(("Rebuild the index of what the backup contains",
                      restic_command(s.repository, "repair", "index")))
    if s.check_broken or s.check_findings:
        steps.append(("Remove the lost files from older snapshots",
                      restic_command(s.repository, "repair", "snapshots", "--forget")))
    if "prune" in s.check_hints:
        steps.append(("Clean up unused data", restic_command(s.repository, "prune")))
    return steps


def status(settings, verifying):
    """(state, icon, title, description) for the card at the top."""
    s = settings
    last = s.last_check_time
    if verifying:
        return ("accent", "view-refresh-symbolic", "Verifying Backup…",
                f"Reading back {fmt_read_of_data(s.next_check_read())} from the backup location.")
    if s.check_damaged:
        return ("error", "dialog-error-symbolic", "Backup Is Damaged",
                "Two checks in a row found errors in the stored data. Files kept in the damaged parts can't "
                "be restored, and until it's repaired, new backups can reuse the damaged data. "
                "The files on this computer are fine.")
    if s.check_suspect:
        return ("warning", "dialog-warning-symbolic", "Possible Damage Found",
                "The last check found errors. A passing read error (the network, a flaky drive) can cause "
                "that, so the same part is checked again after the next backup, or when you verify now.")
    if last is None:
        return ("accent", "security-medium-symbolic", "Not Verified Yet",
                "Verifying reads stored data back from the backup location to find damage before you "
                "need a restore.")
    return ("good", "object-select-symbolic", "No Problems Found",
            f"Last verified {fmt_ago(last)}, {fmt_read(s.check_read) or 'part of the data'} read back "
            "without errors.")


def _copy(widget, text):
    widget.get_clipboard().set(text)


def _copy_button(text, toast, what="Copied"):
    button = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy",
                        css_classes=["flat"])

    def clicked(b):
        _copy(b, text)
        toast(what)
    button.connect("clicked", clicked)
    return button


def _mono(text):
    return label(text, ("monospace",), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)


class VerifyPage(Gtk.ScrolledWindow):
    def __init__(self, on_verify, on_settings, toast):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._on_verify = on_verify
        self._on_settings = on_settings
        self._toast = toast
        self._shown = None
        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=700)
        self.set_child(clamp)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                            margin_top=18, margin_bottom=32, margin_start=16, margin_end=16)
        clamp.set_child(self._box)

        self._card = Gtk.Box(spacing=18, css_classes=["card", "hero", "verify-status"])
        self._icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER, css_classes=["hero-icon"])
        self._card.append(self._icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self._title = label("", ("title-2",), wrap=True)
        self._desc = label("", (), wrap=True)
        self._failure = label("", ("caption",), wrap=True, selectable=True, visible=False)
        for w in (self._title, self._desc, self._failure):
            text.append(w)
        self._card.append(text)
        self._verify_btn = Gtk.Button(label="Verify Now", valign=Gtk.Align.CENTER, css_classes=["pill"])
        self._verify_btn.connect("clicked", lambda *_: self._on_verify())
        self._card.append(self._verify_btn)
        self._box.append(self._card)
        self._groups = []

    def update(self, settings, verifying, can_verify):
        """Re-render; the sections below the card are only rebuilt when what they show changed."""
        s = settings
        state, icon, title, desc = status(s, verifying)
        for c in STATES:
            self._card.remove_css_class(c)
            self._icon.remove_css_class(c)
        if state in ("error", "warning"):
            self._card.add_css_class(state)
        self._icon.add_css_class(state)
        self._icon.set_from_icon_name(icon)
        self._title.set_label(title)
        self._desc.set_label(desc)
        failure = s.check_failure and not verifying
        self._failure.set_visible(bool(failure))
        self._failure.set_label(f"The last attempt to verify couldn't finish: {s.check_failure}" if failure else "")
        self._verify_btn.set_label("Verify Again" if s.check_damaged or s.check_suspect else "Verify Now")
        self._verify_btn.set_sensitive(can_verify and not verifying)
        for c in ("suggested-action", "destructive-action"):
            self._verify_btn.remove_css_class(c)
        if state not in ("error", "warning"):
            self._verify_btn.add_css_class("suggested-action")

        key = (s.repository, s.check_damaged, s.check_suspect, s.check_errors, tuple(s.check_findings),
               tuple(s.check_broken), tuple(s.check_hints), s.check_read, s.check_days, s.check_parts,
               tuple(e.get("time") for e in s.check_history))
        if key == self._shown:
            return
        self._shown = key
        for group in self._groups:
            self._box.remove(group)
        self._groups = [g for g in (self._findings(s), self._damaged_files(s), self._repair(s),
                                    self._history(s), self._schedule(s)) if g is not None]
        for group in self._groups:
            self._box.append(group)

    # Sections ------------------------------------------------------------------

    def _findings(self, s):
        if not (s.check_damaged or s.check_suspect):
            return None
        where = fmt_read(s.check_read)
        group = Adw.PreferencesGroup(
            title="What the Check Found",
            description=f"{s.check_error}" + (f" Checked {where}." if where else ""))
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["card", "findings"])
        if s.check_findings:
            text = "\n\n".join(s.check_findings)
            if s.check_errors > len(s.check_findings):
                text += f"\n\n… restic counted {s.check_errors} errors in all"
            frame.append(_mono(text))
            group.set_header_suffix(_copy_button(text, self._toast, "Errors copied"))
        else:
            frame.append(label("restic's messages weren't kept for this check. Verify again to see them.",
                               ("dim-label",), wrap=True))
        group.add(frame)
        return group

    def _damaged_files(self, s):
        if not s.check_broken:
            return None
        n = len(s.check_broken)
        group = Adw.PreferencesGroup(
            title=f"Damaged Data Files ({n})",
            description="restic stores your files' contents in data files called packs. These couldn't be read "
                        "back intact; files whose contents are in them can't be restored.")
        group.set_header_suffix(_copy_button("\n".join(s.check_broken), self._toast, "File IDs copied"))
        for pack in s.check_broken:
            row = Adw.ActionRow(title=pack, title_selectable=True, css_classes=["monospace", "property"])
            row.set_subtitle(f"data/{pack[:2]}/{pack}")
            group.add(row)
        return group

    def _repair(self, s):
        if not s.check_damaged and not s.check_broken:
            return None
        group = Adw.PreferencesGroup(
            title="How to Repair It",
            description="Repairing changes the backup and can't be undone. If you can, copy the backup "
                        "folder first, and don't run backups (here or in Déjà Dup) until it's done.")
        guide = Adw.ActionRow(title="Read restic's Troubleshooting Guide", activatable=True,
                              subtitle="Explains each step and what it removes")
        guide.add_prefix(Gtk.Image(icon_name="help-browser-symbolic"))
        guide.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        guide.connect("activated", lambda *_: Gtk.UriLauncher.new(TROUBLESHOOTING).launch(
            self.get_root(), None, None))
        group.add(guide)
        for i, (title, command) in enumerate(repair_steps(s), 1):
            row = Adw.ActionRow(title=f"{i}. {title}", subtitle=command, subtitle_selectable=True,
                                css_classes=["repair-step"])
            row.add_suffix(_copy_button(command, self._toast, "Command copied"))
            group.add(row)
        after = Adw.ActionRow(title="Then back up and verify again",
                              subtitle="The next backup stores the files that were lost again, from this "
                                       "computer, as long as you still have them.")
        group.add(after)
        return group

    def _history(self, s):
        if not s.check_history:
            return None
        group = Adw.PreferencesGroup(title="Recent Checks")
        for entry in s.check_history:
            icon, css = RESULTS.get(entry.get("result"), RESULTS["failed"])
            when = parse_time(entry.get("time"))
            row = Adw.ActionRow(title=fmt_date(when) if when else "—", subtitle=entry.get("text", ""),
                                subtitle_lines=3)
            row.add_prefix(Gtk.Image(icon_name=icon, css_classes=[css]))
            read = fmt_read(entry.get("read"))
            if read:
                row.add_suffix(label(read, ("dim-label", "caption")))
            group.add(row)
        return group

    def _schedule(self, s):
        group = Adw.PreferencesGroup()
        if s.check_days > 0:
            amount = "all of the data" if s.check_parts <= 1 else f"{100 / s.check_parts:g}% of the data"
            subtitle = f"Every {fmt_days(s.check_days)}, {amount} after a backup"
        else:
            subtitle = "Off; Verify Now reads all of the data. Turn it on to find damage early"
        row = Adw.ActionRow(title="Verify Automatically", subtitle=subtitle, activatable=True)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self._on_settings())
        group.add(row)
        return group

