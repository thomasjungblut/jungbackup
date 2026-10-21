"""Watch running backup processes through /proc.

Déjà Dup does not expose its progress to other apps, but the backup engine
(restic or duplicity) is an ordinary process, so we can sample its CPU time,
memory, I/O counters and open files to show what it is doing right now.
"""

import os
import time
from collections import deque
from dataclasses import dataclass, field

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
ENGINES = {"restic", "duplicity", "rclone"}
RESTIC_COMMANDS = {"backup", "restore", "forget", "prune", "check", "snapshots", "stats", "ls",
                   "find", "dump", "init", "unlock", "rebuild-index", "repair", "copy", "mount", "diff"}
QUERY_OPS = {"snapshots", "stats", "ls", "find", "diff", "cat", "version", "dump", "mount", "rclone"}
HISTORY = 90
SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/", "/memfd:", "/run/", "/app/", "/usr/", "/etc/")


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
    cpu_percent: float
    rss: int
    read_rate: float
    write_rate: float
    read_total: int
    write_total: int
    current_file: str | None
    read_history: deque = field(default_factory=deque)
    write_history: deque = field(default_factory=deque)


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
        current = None
        for pid in sorted(engines):
            current = self._open_file(pid) or current

        return Activity(
            pids=sorted(pids),
            ours=self._is_ours(table[main], table),
            operation=self._operation(main, engines[main]),
            engine=engines[main],
            started=self._boot + table[main].start_ticks / CLK_TCK,
            cpu_percent=cpu_rate * 100,
            rss=rss,
            read_rate=read_rate,
            write_rate=write_rate,
            read_total=read_total,
            write_total=write_total,
            current_file=current,
            read_history=self._read_hist,
            write_history=self._write_hist,
        )
