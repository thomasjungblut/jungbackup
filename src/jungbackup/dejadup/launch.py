"""Start Déjà Dup, whether installed natively or as a Flatpak."""

import subprocess


def open_deja_dup(*args):
    for argv in (["deja-dup", *args], ["flatpak", "run", "org.gnome.DejaDup", *args]):
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            continue
    return False
