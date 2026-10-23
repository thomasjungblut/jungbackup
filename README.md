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
  network share) or an SFTP server, pick a password, done.

## Storage

| Backend | Repository | Notes |
|---|---|---|
| Folder | `/run/media/me/disk/backup` | Local disks, USB drives, mounted NFS/SMB/GVfs shares |
| SFTP server | `sftp:user@host:path`, `sftp://user@host:port//abs/path` | restic's own SFTP backend via `ssh` |

SFTP uses your SSH keys, agent and `~/.ssh/config` (a `Host` alias works as
the server name). There's no terminal for a password prompt, so the server must
accept key logins; ssh runs with `BatchMode=yes` and trusts a server's key on
first contact (`StrictHostKeyChecking=accept-new`). A Déjà Dup "Network
server" location with an `sftp://` address is imported as SFTP directly, no
mount needed.

Each backend is a module in `storage/` implementing `storage.base.Storage`.

## Development

The app uses the system's PyGObject, GTK 4, libadwaita and libsecret (on
Fedora: `python3-gobject gtk4 libadwaita libsecret`), so the venv needs
system site-packages:

```sh
make setup    # uv venv --python /usr/bin/python3 --system-site-packages && uv sync
make run      # uv run jungbackup
make check    # ruff + desktop file and metainfo validation
make unit-test         # fast, no restic or podman needed
make integration-test  # real restic, local folder + SFTP container
make test              # both, one after the other
```

Integration tests run the real restic (same lookup as the app, so
`JUNGBACKUP_RESTIC=/path/to/restic make test` works) against a local folder
and an OpenSSH container started with podman, using a throwaway key and
`known_hosts`. They're skipped when restic or podman is missing.

`make help` lists all targets.

restic is looked up in this order: `$JUNGBACKUP_RESTIC`, `restic` on `PATH`, then the
copy bundled inside the Déjà Dup Flatpak.

Settings: `~/.config/jungbackup/settings.json`. Stats cache:
`~/.cache/jungbackup/`.

## Layout

Under `src/jungbackup/`:

| Package | Purpose |
|---|---|
| `app.py` | `Adw.Application`, autostart portal |
| `ui/` | Dashboard and scheduler (`window.py`), setup flow, password prompt, settings dialog, Cairo charts |
| `backup/` | Running restic (`restic.py`; password via stdin), the backup job (`job.py`: init → backup → forget/prune, Déjà Dup-compatible flags), `/proc` sampling of running restic/duplicity (`monitor.py`) |
| `storage/` | Storage backends: `local.py`, `sftp.py`, the shared interface in `base.py` |
| `dejadup/` | Reading/writing Déjà Dup's GSettings / Flatpak keyfile, importing its settings, launching it |
| `core/` | This app's settings, keyring, `$HOME`-style path tokens, formatting helpers |

## Flatpak

The manifest in `build-aux/flatpak/` builds restic from source, using the same
pinned version and vendored Go modules as Déjà Dup's Flathub build
(`restic.go.mod.yml` and `restic.modules.txt`, CC0, copied from
[flathub/org.gnome.DejaDup](https://github.com/flathub/org.gnome.DejaDup)).

```sh
make flatpak-deps     # once: flatpak-builder + GNOME SDK as user installs
make flatpak          # build and install for your user
make flatpak-run      # run the installed build
make flatpak-bundle   # single-file jungbackup.flatpak to share
make flatpak-lint     # Flathub's linter, same checks as its CI
make flatpak-uninstall
```

Inside the sandbox the live view only sees restic processes this app
started. Déjà Dup runs in its own sandbox, so its backups show up after they
finish, not while they run.

