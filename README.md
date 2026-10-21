# Jung Backup

A GNOME backup app built around a dashboard: how big the backup is, how old it
is, how long the last run took, how many files changed, and live throughput,
CPU and memory use while a backup runs. Configuration sits behind an edit
button, out of the way.

It runs its own [restic](https://restic.net) backups and stays compatible with
Déjà Dup: snapshots use the same tag (`deja-dup`), host grouping and retention
flags, so either app can restore or prune the other's backups.

## First start

* **Import from Déjà Dup**: reads Déjà Dup's settings (dconf, or the Flatpak
  keyfile in `~/.var/app/org.gnome.DejaDup`), including location, folders,
  schedule and retention. Déjà Dup's Flatpak keeps the password in its own
  sandboxed keyring, so you type it once and it's stored in your keyring. The
  app offers to switch off Déjà Dup's schedule so the two don't both back up.
* **Set up new backup**: choose a folder (local disk, USB drive or a mounted
  network share), pick a password, done.

## Development

The app uses the system's PyGObject, GTK 4, libadwaita and libsecret (on
Fedora: `python3-gobject gtk4 libadwaita libsecret`), so the venv needs
system site-packages:

```sh
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
uv run jungbackup
uv run ruff check src
```

restic is looked up in this order: `$JUNGBACKUP_RESTIC`, `restic` on `PATH`, then the
copy bundled inside the Déjà Dup Flatpak.

Settings: `~/.config/jungbackup/settings.json`. Stats cache:
`~/.cache/jungbackup/`.

## Layout

| Module | Purpose |
|---|---|
| `window.py` | Dashboard, welcome page, scheduler |
| `engine.py` | Backup job: init → backup → forget/prune (Déjà Dup-compatible flags) |
| `restic.py` | Running restic (queries and streamed JSON jobs; password via stdin) |
| `monitor.py` | `/proc` sampling of running restic/duplicity (CPU, RSS, I/O rates, open file) |
| `settings.py` | This app's settings and the Déjà Dup import |
| `dejadup.py` | Reading/writing Déjà Dup's GSettings / Flatpak keyfile |
| `dialogs.py`, `prefs.py` | Setup flow, password prompt, settings dialog |
| `charts.py` | Cairo bar charts, sparklines, stacked bar |

## Road to Flathub

- [ ] Flatpak manifest bundling restic (and later rclone), `--filesystem=host`
      or portal-based folder access, `--talk-name=org.freedesktop.secrets`
- [ ] Background portal: autostart with `--background` so scheduled backups run
      without the window open (the flag already exists)
- [ ] App ID, icon, `.desktop` and AppStream metainfo with screenshots
- [x] Name: Jung Backup (`io.jungblut.JungBackup`)
- [ ] Restore UI (browse snapshots, restore files)
- [ ] Remote backends (SFTP, rclone: Google Drive, OneDrive)
- [ ] Translations (gettext)
