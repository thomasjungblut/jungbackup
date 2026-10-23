"""Include/exclude entries: Déjà Dup's $HOME-style tokens and their labels."""

import os

from gi.repository import GLib

SPECIAL_DIRS = {
    "$DESKTOP": GLib.UserDirectory.DIRECTORY_DESKTOP,
    "$DOCUMENTS": GLib.UserDirectory.DIRECTORY_DOCUMENTS,
    "$DOWNLOAD": GLib.UserDirectory.DIRECTORY_DOWNLOAD,
    "$MUSIC": GLib.UserDirectory.DIRECTORY_MUSIC,
    "$PICTURES": GLib.UserDirectory.DIRECTORY_PICTURES,
    "$PUBLIC_SHARE": GLib.UserDirectory.DIRECTORY_PUBLIC_SHARE,
    "$TEMPLATES": GLib.UserDirectory.DIRECTORY_TEMPLATES,
    "$VIDEOS": GLib.UserDirectory.DIRECTORY_VIDEOS,
}

SPECIAL_LABELS = {
    "$HOME": "Home",
    "$TRASH": "Trash",
    "$DESKTOP": "Desktop",
    "$DOCUMENTS": "Documents",
    "$DOWNLOAD": "Downloads",
    "$MUSIC": "Music",
    "$PICTURES": "Pictures",
    "$PUBLIC_SHARE": "Public",
    "$TEMPLATES": "Templates",
    "$VIDEOS": "Videos",
}


def expand_path(token):
    home = GLib.get_home_dir()
    if token == "$HOME":
        return home
    if token == "$TRASH":
        return os.path.join(GLib.get_user_data_dir(), "Trash")
    if token in SPECIAL_DIRS:
        return GLib.get_user_special_dir(SPECIAL_DIRS[token])   # None if not set up
    if token.startswith("$HOME/"):
        return os.path.join(home, token[6:])
    return token


def describe_path(token):
    """Human label for an include/exclude entry."""
    if token in SPECIAL_LABELS:
        return SPECIAL_LABELS[token]
    path = expand_path(token) or token
    home = GLib.get_home_dir()
    if path == home:
        return "Home"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1:]
    return path


def collapse_path(path):
    """Store paths the same way Déjà Dup does."""
    home = GLib.get_home_dir()
    if path == home:
        return "$HOME"
    for token, kind in SPECIAL_DIRS.items():
        if GLib.get_user_special_dir(kind) == path:
            return token
    return path
