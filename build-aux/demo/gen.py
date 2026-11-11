"""Make up a home folder and a few weeks of backups of it, for the README demo.

Runs inside the sandbox record.sh sets up (so the paths, the host name and
the backup drive are the demo's, not this machine's). Each backup is slowed
down a little so the dashboard has durations worth charting.
"""

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta

HOME = os.environ["HOME"]
REPO = sys.argv[1]
CACHE = os.path.join(HOME, ".cache", "jungbackup", "restic")
HOST = os.uname().nodename
MB = 1024 * 1024
# Days before today each backup was made; the newest one is made "now".
DAYS_AGO = [34, 32, 30, 29, 27, 25, 24, 22, 20, 18, 17, 15, 13, 11, 10, 8, 6, 4, 2, 0]
LOST_FILE = "Documents/jobs/Resume-2026.pdf"
LOST_AT = len(DAYS_AGO) - 2          # gone from the last two backups

rng = random.Random(2026)
now = datetime.now().replace(microsecond=0)


def write(rel, size, when):
    path = os.path.join(HOME, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(rng.randbytes(int(size)))
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def before(when, days):
    return when - timedelta(days=rng.uniform(0, days), minutes=rng.uniform(0, 600))


def photos(album, count, when, spread=3):
    for _ in range(count):
        write(f"Pictures/{album}/IMG_{rng.randrange(1000, 9999)}.jpg", rng.uniform(2.6, 5.2) * MB,
              before(when, spread))


def restic(*args, limit=None):
    cmd = ["restic", "--repo", REPO, "--insecure-no-password", "--cache-dir", CACHE, "--quiet"]
    if limit:
        cmd.append(f"--limit-upload={limit}")
    subprocess.run(cmd + list(args), check=True)


def backup(when=None, limit=None):
    args = ["backup", f"--host={HOST}", "--tag=deja-dup", "--group-by=host,tags", "--exclude-caches",
            f"--exclude={HOME}/Downloads"]
    if when is not None:
        args.append(f"--time={when:%Y-%m-%d %H:%M:%S}")
    restic(*args, HOME, limit=limit)


def first_day(when):
    for name, size in (("Resume-2026.pdf", 0.3), ("Cover-Letter.odt", 0.05), ("References.pdf", 0.4),
                       ("Portfolio.pdf", 6.5)):
        write(f"Documents/jobs/{name}", size * MB, before(when, 60))
    for year in (2024, 2025):
        for name in ("Tax-Return.pdf", "Payslips.pdf", "Health-Insurance.pdf", "Receipts.ods"):
            write(f"Documents/taxes/{year}/{name}", rng.uniform(0.1, 1.8) * MB, before(when, 300))
    for name in ("Rental-Contract.pdf", "Passport-Scan.pdf", "Car-Insurance.pdf", "Recipes.odt", "Budget.ods",
                 "Reading-List.md"):
        write(f"Documents/{name}", rng.uniform(0.02, 2.5) * MB, before(when, 200))
    for chapter in range(1, 7):
        write(f"Documents/thesis/chapter-{chapter}.tex", rng.uniform(0.03, 0.2) * MB, before(when, 90))
    write("Documents/thesis/thesis.pdf", 4.2 * MB, before(when, 20))
    photos("2025/Family", 14, when, 200)
    photos("2026/Lisbon", 16, when, 40)
    for album, tracks in (("Kind of Blue", 5), ("Moon Safari", 6)):
        for n in range(1, tracks + 1):
            write(f"Music/{album}/{n:02d} Track {n}.flac", rng.uniform(4, 7) * MB, before(when, 500))
    write("Videos/Lisbon-Tram.mp4", 38 * MB, before(when, 40))
    for name in ("index.html", "about.html", "style.css", "main.js", "README.md", "Makefile"):
        write(f"Projects/website/{name}", rng.uniform(0.002, 0.06) * MB, before(when, 120))
    for n in range(1, 13):
        write(f"Projects/website/posts/2026-{n:02d}-post.md", rng.uniform(0.002, 0.02) * MB, before(when, 300))
    write("Downloads/fedora-workstation.iso", 3 * MB, before(when, 5))   # not backed up


def another_day(step, when):
    for _ in range(rng.randint(1, 3)):
        write(f"Documents/notes/{before(when, 2):%Y-%m-%d}-{rng.choice(['meeting', 'ideas', 'todo', 'journal'])}.md",
              rng.uniform(0.002, 0.03) * MB, before(when, 1))
    for chapter in rng.sample(range(1, 7), rng.randint(1, 3)):
        write(f"Documents/thesis/chapter-{chapter}.tex", rng.uniform(0.03, 0.2) * MB, before(when, 1))
    write("Documents/thesis/thesis.pdf", rng.uniform(4, 5) * MB, before(when, 1))
    write(f"Projects/website/posts/{when:%Y-%m-%d}-post.md", rng.uniform(0.002, 0.02) * MB, before(when, 1))
    if step % 3 != 1:
        photos("2026/Hiking" if step < 12 else "2026/Garden", rng.randint(2, 9), when)
    if step == LOST_AT:
        os.unlink(os.path.join(HOME, LOST_FILE))    # the file the demo goes looking for


start = now - timedelta(days=DAYS_AGO[0])
first_day(start)
restic("init", "--repository-version=2")
for step, days in enumerate(DAYS_AGO):
    when = (now - timedelta(days=days)).replace(hour=rng.choice((2, 9, 12, 20, 21)), minute=rng.randrange(60))
    if step:
        another_day(step, when)
    print(f"backup {step + 1}/{len(DAYS_AGO)}", flush=True)
    backup(when if days else None, limit=40000 if not step else 9000)

# Something new for the demo's own backup to do.
photos("2026/Iceland", 44, now, 6)
write("Videos/Iceland-Waterfall.mp4", 46 * MB, before(now, 3))

config = os.path.join(HOME, ".config")
os.makedirs(os.path.join(config, "jungbackup"), exist_ok=True)
with open(os.path.join(config, "user-dirs.dirs"), "w") as f:
    for name in ("Desktop", "Documents", "Download", "Music", "Pictures", "Videos"):
        f.write(f'XDG_{name.upper()}_DIR="$HOME/{name + "s" if name == "Download" else name}"\n')
with open(os.path.join(config, "jungbackup", "settings.json"), "w") as f:
    json.dump({"repository": REPO, "encrypted": False, "includes": ["$HOME"], "excludes": ["$TRASH", "$DOWNLOAD"],
               "periodic": True, "period_days": 1, "keep_days": 0}, f)
