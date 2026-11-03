"""Search a folder in a snapshot and everything below it by name, like `find | grep`.

restic has no index of names, so every search walks the snapshot's trees.
`restic find` with a pattern anchored at the folder skips everything outside
it, and only reads trees, which restic keeps in its local cache, so it's
quick even for remote backups. Searching all snapshots at once is not: find
walks each one separately.
"""

import fnmatch
import os

from .restic import pattern_literal

GLOB = ("*", "?", "[")
MIN_LENGTH = 2      # one letter matches nearly everything


class Query:
    """What the user typed, searched for in `folder` and below.

    Plain text matches anywhere in a name, ignoring case ("resume" finds
    Resume-2024.pdf). With a "/" it matches the end of the path below the
    folder ("jobs/resume"). Text with glob characters is a pattern for the
    whole name (or path end): "*.pdf".
    """

    def __init__(self, text, folder):
        self.text = text.strip()
        self.folder = folder
        glob = self.text if any(c in self.text for c in GLOB) else f"*{self.text}*"
        self._glob = glob.casefold()
        self._in_path = "/" in self.text
        root = "" if folder == "/" else pattern_literal(folder)
        self.pattern = f"{root}/**/{glob}"

    @property
    def valid(self):
        return len(self.text) >= MIN_LENGTH or any(c in self.text for c in GLOB)

    def matches(self, path):
        """Whether `path` itself matches.

        restic also reports everything inside a matching folder, the way an
        exclude pattern covers a folder's contents; those aren't results.
        """
        prefix = self.folder.rstrip("/") + "/"
        if not path.startswith(prefix) or path == prefix:
            return False
        below = path[len(prefix):]
        if self._in_path:
            below = below.casefold()
            return fnmatch.fnmatchcase(below, self._glob) or fnmatch.fnmatchcase(below, "*/" + self._glob)
        return fnmatch.fnmatchcase(os.path.basename(path).casefold(), self._glob)

    def results(self, data):
        """The matching nodes from `restic find --json` output for one snapshot, named like `ls` nodes."""
        nodes = []
        for group in data or []:
            for node in group.get("matches") or []:
                path = node.get("path", "")
                if self.matches(path):
                    nodes.append({**node, "name": os.path.basename(path)})
        return nodes


def bad_pattern(error):
    return "syntax error in pattern" in str(error).lower()
