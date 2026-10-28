"""Parsing of /proc data behind the live "what is the backup waiting on" view."""

from jungbackup.backup.monitor import classify, moving_average, nfs_summary, parse_mountstats

MOUNTSTATS = b"""device proc mounted on /proc with fstype proc
device 10.0.0.2:/homes mounted on /var/mnt/nas\\040home with fstype nfs4 statvers=1.1
\topts:\trw,vers=4.1
\tper-op statistics
\t        NULL: 0 0 0 0 0 0 0 0 0
\t       WRITE: 10 12 1 100 20 50 3000 4000 0
\t        READ: 2 2 0 10 1000 0 20 30 0
device /dev/sda1 mounted on / with fstype btrfs
"""


def test_parse_mountstats():
    mounts = parse_mountstats(MOUNTSTATS)
    assert mounts["/"] == ("btrfs", {})
    fstype, ops = mounts["/var/mnt/nas home"]
    assert fstype == "nfs4"
    assert ops["WRITE"] == [10, 12, 1, 100, 20, 50, 3000, 4000, 0]


def test_nfs_summary_averages_the_interval():
    _, before = parse_mountstats(MOUNTSTATS)["/var/mnt/nas home"]
    after = {op: [v * 2 for v in counters] for op, counters in before.items()}
    s = nfs_summary(before, after, seconds=2)
    assert s["ops"] == 12
    assert s["ops_rate"] == 6
    assert s["retrans"] == 2
    assert s["rtt_ms"] == 3020 / 12
    assert s["bytes"] == 1130


def test_nfs_summary_without_requests():
    _, ops = parse_mountstats(MOUNTSTATS)["/var/mnt/nas home"]
    s = nfs_summary(ops, ops, seconds=1)
    assert s["ops"] == 0
    assert s["rtt_ms"] == 0


def test_classify_prefers_blocked_threads():
    assert classify([("R", "0"), ("D", "rpc_wait_bit_killable")]) == "network"
    assert classify([("R", "0"), ("D", "folio_wait_bit_common")]) == "disk"
    assert classify([("R", "0"), ("S", "futex_do_wait")]) == "cpu"
    assert classify([("S", "futex_do_wait"), ("S", "ep_poll")]) == "idle"


def test_moving_average_smooths_bursts():
    assert moving_average([]) == []
    assert moving_average([4, 0, 2], window=2) == [4, 2, 1]
    # A burst every third second averages out to the sustained rate.
    assert moving_average([0, 0, 90] * 10, window=30)[-1] == 30
