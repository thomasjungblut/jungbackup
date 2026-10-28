"""Watch running backup processes through /proc.

Déjà Dup does not expose its progress to other apps, but the backup engine
(restic or duplicity) is an ordinary process, so we can sample its CPU time,
memory, I/O counters and open files to show what it is doing right now.

Low CPU with little I/O usually means the backup is waiting on something. The
kernel shows what: threads in uninterruptible sleep (D) are blocked on I/O, and
their wait channel says whether it's a local disk or an NFS/SMB server. For an
NFS repository, /proc/self/mountstats gives the round trip time per request,
and TCP retransmits in /proc/net/snmp point at a lossy network.
"""

import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NUM_CPUS = os.cpu_count() or 1
ENGINES = {"restic", "duplicity", "rclone"}
RESTIC_COMMANDS = {"backup", "restore", "forget", "prune", "check", "snapshots", "stats", "ls",
                   "find", "dump", "init", "unlock", "rebuild-index", "repair", "copy", "mount", "diff"}
QUERY_OPS = {"snapshots", "stats", "ls", "find", "diff", "cat", "version", "dump", "mount", "rclone"}
HISTORY = 90
SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/", "/memfd:", "/run/", "/app/", "/usr/", "/etc/")
WAIT_WINDOW = 15         # samples (seconds) behind the wait breakdown and NFS/TCP rates
RATE_WINDOW = 30         # samples (seconds) behind the averaged read/write rates
NETWORK_WCHANS = ("rpc_", "nfs", "cifs", "smb", "fuse", "sk_wait", "tcp_", "inet_", "unix_stream")


@dataclass
class _Sample:
    t: float
    cpu_ticks: int
    rchar: int
    wchar: int


@dataclass
class Activity:
    """Aggregated view of the running backup engine."""
    pids: list
    ours: bool               # started by this app (vs. Déjà Dup or a terminal)
    operation: str
    engine: str
    started: float           # unix time the oldest process started
    cpu_percent: float       # share of all cores, 0-100
    load: tuple              # system load average over 1, 5 and 15 minutes
    rss: int
    read_rate: float
    write_rate: float
    read_avg: float          # read_rate averaged over the last RATE_WINDOW seconds
    write_avg: float
    read_total: int
    write_total: int
    current_file: str | None
    waits: dict              # share of recent samples spent in "cpu", "disk", "network", "idle"
    io_pressure: float | None  # % of time all non-idle tasks stalled on I/O (PSI "full" avg10)
    nfs: dict | None         # recent NFS requests, only when the repository is on an NFS mount
    tcp_retrans: float | None  # % of TCP segments resent system-wide, only for a remote repository
    read_history: deque = field(default_factory=deque)
    write_history: deque = field(default_factory=deque)
    rtt_history: deque = field(default_factory=deque)


def moving_average(values, window=RATE_WINDOW):
    """Trailing mean of `values`, one per value, over up to `window` values.

    Writes to a network share come in bursts: the engine fills a pack file in
    memory, then blocks while the kernel flushes it, so per-second rates swing
    between full speed and zero. The average shows the throughput actually
    sustained.
    """
    out, total = [], 0.0
    values = list(values)
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        out.append(total / min(i + 1, window))
    return out


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _boot_time():
    data = _read("/proc/stat") or b""
    for line in data.splitlines():
        if line.startswith(b"btime "):
            return int(line.split()[1])
    return time.time() - time.monotonic()


def _io_pressure():
    for line in (_read("/proc/pressure/io") or b"").splitlines():
        if line.startswith(b"full "):
            return float(line.split(b"avg10=")[1].split()[0])
    return None


def _tcp_counters():
    """(segments sent, segments retransmitted) since boot, or None."""
    rows = [line.split() for line in (_read("/proc/net/snmp") or b"").splitlines() if line.startswith(b"Tcp:")]
    if len(rows) < 2:
        return None
    tcp = dict(zip(rows[0][1:], rows[1][1:], strict=True))
    return int(tcp[b"OutSegs"]), int(tcp[b"RetransSegs"])


def parse_mountstats(data):
    """{mount point: (fstype, {op: [ops, trans, timeouts, sent, recv, queue_ms, rtt_ms, exec_ms, ...]})}"""
    mounts = {}
    for block in data.split(b"\ndevice "):
        head, _, body = block.partition(b"\n")
        m = re.search(rb" mounted on (\S+) with fstype (\S+)", head)
        if not m:
            continue
        ops = {}
        if b"per-op statistics" in body:
            for line in body.split(b"per-op statistics", 1)[1].splitlines():
                name, sep, values = line.strip().partition(b": ")
                if sep and name.isalpha():
                    ops[name.decode()] = [int(v) for v in values.split()]
        point = m[1].decode(errors="replace").replace("\\040", " ")
        mounts[point] = (m[2].decode(), ops)
    return mounts


def nfs_summary(before, after, seconds):
    """Average cost of the NFS requests made between two per-op snapshots."""
    total = [0] * 8
    for op, counters in after.items():
        prev = before.get(op, [0] * len(counters))
        for i in range(min(8, len(counters))):
            total[i] += counters[i] - prev[i]
    ops = max(total[0], 0)
    per_op = max(ops, 1)
    return {"ops": ops, "ops_rate": ops / seconds, "retrans": max(0, total[1] - ops), "timeouts": total[2],
            "bytes": total[3] + total[4], "queue_ms": total[5] / per_op,
            "rtt_ms": total[6] / per_op, "exec_ms": total[7] / per_op}


def classify(states):
    """Reduce (state, wchan) of every thread in the tree to what it's waiting on.

    Being blocked wins over running: a single thread stuck on I/O is usually
    what the rest are waiting for.
    """
    blocked = [wchan for state, wchan in states if state == "D"]
    if any(w.startswith(NETWORK_WCHANS) for w in blocked):
        return "network"
    if blocked:
        return "disk"
    if any(state == "R" for state, _ in states):
        return "cpu"
    return "idle"


def _thread_states(pid):
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    states = []
    for tid in tids:
        raw = _read(f"/proc/{pid}/task/{tid}/stat")
        if raw is None:
            continue
        state = raw[raw.rfind(b")") + 2:raw.rfind(b")") + 3].decode()
        wchan = (_read(f"/proc/{pid}/task/{tid}/wchan") or b"").decode(errors="replace")
        states.append((state, wchan))
    return states


class _Proc:
    __slots__ = ("pid", "ppid", "comm", "start_ticks", "cpu_ticks", "rss_pages")

    @classmethod
    def load(cls, pid):
        raw = _read(f"/proc/{pid}/stat")
        if raw is None:
            return None
        # comm can contain spaces and parentheses; split around the last ')'
        lpar, rpar = raw.find(b"("), raw.rfind(b")")
        fields = raw[rpar + 2:].split()
        p = cls()
        p.pid = pid
        p.comm = raw[lpar + 1:rpar].decode(errors="replace")
        p.ppid = int(fields[1])
        p.cpu_ticks = int(fields[11]) + int(fields[12])
        p.start_ticks = int(fields[19])
        p.rss_pages = int(fields[21])
        return p


class ProcessMonitor:
    def __init__(self, exclude_paths=()):
        self._boot = _boot_time()
        self._self = os.getpid()
        self._prev = {}
        self._read_hist = deque(maxlen=HISTORY)
        self._write_hist = deque(maxlen=HISTORY)
        self._waits = deque(maxlen=WAIT_WINDOW)
        self._counters = deque(maxlen=WAIT_WINDOW)   # (time, nfs per-op snapshot, tcp counters)
        self._rtt_hist = deque(maxlen=HISTORY)
        self.exclude_paths = tuple(exclude_paths)

    def _is_ours(self, proc, table):
        seen = 0
        while proc is not None and seen < 64:
            if proc.ppid == self._self or proc.pid == self._self:
                return True
            proc = table.get(proc.ppid)
            seen += 1
        return False

    @staticmethod
    def _cmdline(pid):
        raw = _read(f"/proc/{pid}/cmdline") or b""
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a]

    def _engine(self, proc):
        if proc.comm in ENGINES:
            return proc.comm
        if proc.comm.startswith("python") or proc.comm == "duplicity":
            argv = self._cmdline(proc.pid)
            if any(os.path.basename(a) == "duplicity" for a in argv[:3]):
                return "duplicity"
        return None

    def _operation(self, pid, engine):
        argv = self._cmdline(pid)
        if engine == "restic":
            for arg in argv[1:]:
                if arg in RESTIC_COMMANDS:
                    return arg
        if engine == "duplicity":
            for arg in argv[1:]:
                if arg in ("full", "incremental", "restore", "verify", "collection-status",
                           "list-current-files", "remove-older-than", "cleanup"):
                    return arg
            return "backup"
        return engine

    def _repository(self, pid):
        argv = self._cmdline(pid)
        for i, arg in enumerate(argv):
            if arg in ("--repo", "-r") and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith("--repo="):
                return arg.split("=", 1)[1]
        for var in (_read(f"/proc/{pid}/environ") or b"").split(b"\0"):
            if var.startswith(b"RESTIC_REPOSITORY="):
                return var.split(b"=", 1)[1].decode(errors="replace")
        return None

    @staticmethod
    def _nfs_ops(repo):
        """Per-op NFS counters of the mount holding a local repository path, if it's NFS."""
        repo = os.path.realpath(repo)
        mounts = parse_mountstats(_read("/proc/self/mountstats") or b"")
        best = max((p for p in mounts if repo == p or repo.startswith(p.rstrip("/") + "/")), key=len, default=None)
        if best is None or not mounts[best][0].startswith("nfs"):
            return None
        return mounts[best][1]

    def _open_file(self, pid):
        """Most interesting regular file the process has open (what it's reading)."""
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            return None
        best = None
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if not target.startswith("/") or target.startswith(SKIP_PREFIXES):
                continue
            if "/.cache/" in target or "/cache/deja-dup/" in target or target.startswith(self.exclude_paths):
                continue
            if best is None or int(fd) > best[0]:
                best = (int(fd), target)
        return best[1] if best else None

    def sample(self):
        """Return an Activity if a backup engine is running, else None."""
        table = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                proc = _Proc.load(int(name))
                if proc is not None:
                    table[proc.pid] = proc

        engines = {}
        for proc in table.values():
            engine = self._engine(proc)
            if engine and engine != "rclone" and self._operation(proc.pid, engine) not in QUERY_OPS:
                engines[proc.pid] = engine

        if not engines:
            self._prev.clear()
            self._read_hist.clear()
            self._write_hist.clear()
            self._waits.clear()
            self._counters.clear()
            self._rtt_hist.clear()
            return None

        # Count the whole process tree (e.g. ssh or rclone spawned by restic).
        children = {}
        for proc in table.values():
            children.setdefault(proc.ppid, []).append(proc.pid)
        pids, stack = set(), list(engines)
        while stack:
            pid = stack.pop()
            if pid not in pids:
                pids.add(pid)
                stack.extend(children.get(pid, ()))

        t = time.monotonic()
        cpu_rate = read_rate = write_rate = 0.0
        rss = read_total = write_total = 0
        samples = {}
        for pid in pids:
            proc = table[pid]
            io = _read(f"/proc/{pid}/io") or b""
            counters = dict(line.split(b": ") for line in io.splitlines() if b": " in line)
            s = _Sample(t, proc.cpu_ticks, int(counters.get(b"rchar", 0)), int(counters.get(b"wchar", 0)))
            samples[pid] = s
            rss += proc.rss_pages * PAGE_SIZE
            read_total += s.rchar
            write_total += s.wchar
            prev = self._prev.get(pid)
            if prev is not None and t > prev.t:
                dt = t - prev.t
                cpu_rate += (s.cpu_ticks - prev.cpu_ticks) / CLK_TCK / dt
                read_rate += max(0, s.rchar - prev.rchar) / dt
                write_rate += max(0, s.wchar - prev.wchar) / dt
        first_sample = not any(pid in self._prev for pid in pids)
        self._prev = samples
        if not first_sample:
            self._read_hist.append(read_rate)
            self._write_hist.append(write_rate)

        main = min(engines, key=lambda pid: table[pid].start_ticks)
        self._waits.append(classify([st for pid in pids for st in _thread_states(pid)]))
        waits = {k: self._waits.count(k) / len(self._waits) for k in ("cpu", "disk", "network", "idle")}

        # Only look at the transport the repository actually uses: NFS counters
        # for a path on an NFS mount, TCP for anything that isn't a local disk.
        repo = self._repository(main) if engines[main] == "restic" else None
        nfs_ops = self._nfs_ops(repo) if repo and repo.startswith("/") else None
        remote = nfs_ops is not None or bool(repo) and not repo.startswith("/")
        self._counters.append((t, nfs_ops, _tcp_counters() if remote else None))
        (old_t, old_nfs, old_tcp), (_, new_nfs, new_tcp) = self._counters[0], self._counters[-1]
        nfs = None
        if old_nfs is not None and new_nfs is not None and t > old_t:
            nfs = nfs_summary(old_nfs, new_nfs, t - old_t)
            self._rtt_hist.append(nfs["rtt_ms"])
        tcp_retrans = None
        if old_tcp and new_tcp and new_tcp[0] > old_tcp[0]:
            tcp_retrans = (new_tcp[1] - old_tcp[1]) / (new_tcp[0] - old_tcp[0]) * 100

        current = None
        for pid in sorted(engines):
            current = self._open_file(pid) or current

        return Activity(
            pids=sorted(pids),
            ours=self._is_ours(table[main], table),
            operation=self._operation(main, engines[main]),
            engine=engines[main],
            started=self._boot + table[main].start_ticks / CLK_TCK,
            cpu_percent=cpu_rate / NUM_CPUS * 100,
            load=os.getloadavg(),
            rss=rss,
            read_rate=read_rate,
            write_rate=write_rate,
            read_avg=(moving_average(self._read_hist) or [read_rate])[-1],
            write_avg=(moving_average(self._write_hist) or [write_rate])[-1],
            read_total=read_total,
            write_total=write_total,
            current_file=current,
            waits=waits,
            io_pressure=_io_pressure(),
            nfs=nfs,
            tcp_retrans=tcp_retrans,
            read_history=self._read_hist,
            write_history=self._write_hist,
            rtt_history=self._rtt_hist,
        )
