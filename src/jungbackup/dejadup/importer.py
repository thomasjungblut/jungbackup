"""Fill this app's Settings from Déjà Dup's configuration."""

from dataclasses import dataclass

from ..core.settings import Settings


@dataclass
class ImportResult:
    settings: Settings
    notes: list


def import_from_deja_dup(dd):
    """Build Settings from a DejaDupConfig. Returns ImportResult or None."""
    if not dd.available:
        return None
    notes = []
    loc = dd.location()
    s = Settings(
        includes=dd.includes,
        excludes=dd.excludes,
        periodic=dd.periodic,
        period_days=dd.period_days,
        keep_days=dd.keep_days,
        imported_from="deja-dup",
    )
    if loc.path:
        s.repository = loc.path
    elif loc.repository:
        s.repository = loc.repository
    elif loc.backend == "local":
        s.repository = loc.label
        notes.append("The backup folder isn't reachable right now; mount it before the next backup.")
    else:
        notes.append(f"{loc.label} can't be used yet — choose a folder for your backups.")
    tool = dd.detect_tool(loc)
    if tool == "duplicity":
        s.repository = ""
        notes.append("Your Déjà Dup backups use the older Duplicity format, which this app can't read. "
                     "Choose a new folder to start a fresh restic backup.")
    last = dd.last_backup
    if last is not None:
        s.last_backup = last.isoformat()
    return ImportResult(s, notes)
