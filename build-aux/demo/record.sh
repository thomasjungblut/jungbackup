#!/bin/sh
# Record the README demo: data/screenshots/demo.gif (and an .mp4 next to the
# intermediate files).
#
# Nothing of yours is shown or touched. The app runs in a bubblewrap sandbox
# with a made-up home folder (/home/alex), host name and backup drive, on its
# own D-Bus session and a headless compositor, so no window appears either.
# gen.py makes up the files and a few weeks of backups (kept in the work
# folder for a few hours), play.py drives the app and records it.
#
# Needs: bwrap, mutter, ffmpeg, dbus-run-session, restic.
#   JUNGBACKUP_DEMO_DIR   work folder (default ~/.cache/jungbackup-demo)
#   JUNGBACKUP_RESTIC     restic binary to use
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
work=${JUNGBACKUP_DEMO_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/jungbackup-demo}
out=$work/out
gif=$root/data/screenshots/demo.gif

restic=${JUNGBACKUP_RESTIC:-$(command -v restic || true)}
[ -n "$restic" ] || restic=$root/build-dir/files/bin/restic     # left there by `make flatpak`
[ -x "$restic" ] || { echo "restic not found: install it, run 'make flatpak' or set JUNGBACKUP_RESTIC" >&2; exit 1; }
for tool in bwrap mutter ffmpeg dbus-run-session; do
    command -v "$tool" >/dev/null || { echo "$tool is needed to record the demo" >&2; exit 1; }
done

demo_home=/home/alex
drive=/run/media/alex/Backup
uid=$(id -u)

# sandbox <extra bwrap arguments> -- <command>
sandbox() {
    bwrap --unshare-uts --hostname laptop --unshare-pid --unshare-ipc --die-with-parent \
        --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
        --ro-bind /etc /etc --dev-bind /dev /dev --ro-bind /sys /sys --proc /proc --tmpfs /tmp --tmpfs /run \
        --perms 0700 --dir "/run/user/$uid" --dir /home \
        --ro-bind "$restic" /app/bin/restic --ro-bind "$root" /src --bind "$out" /out \
        --clearenv --setenv PATH /app/bin:/usr/bin --setenv HOME "$demo_home" --setenv USER alex \
        --setenv LANG "${LANG:-C.UTF-8}" --setenv XDG_RUNTIME_DIR "/run/user/$uid" \
        --setenv PYTHONPATH /src/src --setenv PYTHONDONTWRITEBYTECODE 1 \
        --chdir / "$@"
}

mkdir -p "$out"
# Made once and reused while it's fresh: after a few hours the newest backup
# would no longer be "just now", and after a day the app would call it overdue.
if [ -z "$(find "$work/ready" -mmin -360 2>/dev/null)" ]; then
    echo "Making up files and backups (takes a couple of minutes)…"
    rm -f "$work/ready"
    rm -rf "$work/home" "$work/drive"
    mkdir -p "$work/home" "$work/drive"
    sandbox --bind "$work/home" "$demo_home" --bind "$work/drive" "$drive" -- \
        python3 /src/build-aux/demo/gen.py "$drive/jungbackup"
    touch "$work/ready"
fi

echo "Recording…"
rm -f "$out/demo.mkv"
# The home folder and the drive are throwaway copies, so every take starts the same;
# the drive is a (mostly empty) 2 TB one.
sandbox --ro-bind "$work" /pristine --tmpfs "$demo_home" --size 2000000000000 --tmpfs "$drive" \
    --setenv GDK_BACKEND wayland --setenv WAYLAND_DISPLAY jungbackup-demo \
    --setenv GDK_DEBUG no-portals --setenv GTK_A11Y none --setenv ADW_DEBUG_COLOR_SCHEME prefer-light \
    --setenv JUNGBACKUP_RESTIC "/app/bin/restic --limit-upload=${JUNGBACKUP_DEMO_RATE:-36000}" -- \
    dbus-run-session -- sh -euc '
        cp -a /pristine/home/. "$HOME/"
        cp -a /pristine/drive/. "$1/"
        mutter --headless --no-x11 --wayland-display "$WAYLAND_DISPLAY" --virtual-monitor 1400x1100 \
            >/out/compositor.log 2>&1 &
        for _ in $(seq 50); do [ -e "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break; sleep 0.2; done
        python3 /src/build-aux/demo/play.py /out/demo.mkv
    ' sh "$drive" 2>"$out/session.log" || { tail -n 30 "$out/session.log" >&2; exit 1; }

echo "Encoding…"
ffmpeg -loglevel error -y -i "$out/demo.mkv" -pix_fmt yuv420p -c:v libx264 -crf 20 -movflags +faststart "$out/demo.mp4"
ffmpeg -loglevel error -y -i "$out/demo.mkv" -filter_complex \
    "fps=${JUNGBACKUP_DEMO_FPS:-8},scale=${JUNGBACKUP_DEMO_WIDTH:-880}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
    "$gif"
ls -lh "$gif" "$out/demo.mp4"
