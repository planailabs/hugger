"""Tell "the store disk is slow" apart from "the download is wedged".

Stores can live on FUSE/network mounts (sshfs, SMB) where a single write, fsync
or SQLite commit can block for minutes. The stall watchdogs must not restart a
worker for that: a fresh worker would block on the same disk.

The worker wraps its store I/O in `busy()`. A heartbeat thread mirrors the age
of the oldest in-flight disk call to a LOCAL file (the temp dir, never the store
volume, which would block too) named by $HUGGER_DISK_HEARTBEAT. The parent reads
it with `lagging()`. A blocked syscall releases the GIL, so the heartbeat keeps
beating through disk lag, but NOT through a GIL-holding hang (e.g. hf_xet <1.7),
which therefore still counts as a stall.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ENV = "HUGGER_DISK_HEARTBEAT"
# A disk call blocked this long is lag, not normal I/O.
LAG_SECS = 10.0
BEAT_SECS = 2.0

_lock = threading.Lock()
_ids = itertools.count()
_inflight: dict[int, float] = {}  # call id -> monotonic start


@contextmanager
def busy():
    """Mark a store disk call as in flight for the duration of the block."""
    i = next(_ids)
    with _lock:
        _inflight[i] = time.monotonic()
    try:
        yield
    finally:
        with _lock:
            _inflight.pop(i, None)


def oldest_age() -> float:
    """Seconds the oldest in-flight disk call has been running (0 if none)."""
    with _lock:
        t0 = min(_inflight.values(), default=None)
    return 0.0 if t0 is None else time.monotonic() - t0


def start() -> None:
    """Start the heartbeat thread, if the parent asked for one."""
    path = os.environ.get(ENV)
    if not path:
        return

    def beat():
        tmp = path + ".tmp"
        for seq in itertools.count(1):
            try:
                Path(tmp).write_text(f"{seq} {oldest_age():.1f}")
                os.replace(tmp, path)
            except OSError:
                pass
            time.sleep(BEAT_SECS)

    threading.Thread(target=beat, daemon=True, name="hugger-diskbeat").start()


class Watch:
    """Parent side: reads the worker heartbeat and times its own store reads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._seq = None
        self._seq_t = 0.0  # when the heartbeat last advanced
        self._read_t0: float | None = None

    @contextmanager
    def reading(self):
        """Wrap the parent's own store reads (progress polling)."""
        self._read_t0 = time.monotonic()
        try:
            yield
        finally:
            self._read_t0 = None

    def lagging(self) -> float:
        """Seconds of disk lag seen right now (0 if none). Counts the parent's
        own blocked store read, or a worker disk call blocked >= LAG_SECS while
        the worker's heartbeat is still fresh."""
        now = time.monotonic()
        t0 = self._read_t0
        if t0 is not None and now - t0 >= LAG_SECS:
            return now - t0
        try:
            seq, age = self.path.read_text().split()
            seq, age = int(seq), float(age)
        except (OSError, ValueError):
            return 0.0
        if seq != self._seq:
            self._seq, self._seq_t = seq, now
        fresh = now - self._seq_t < 3 * BEAT_SECS
        return age if fresh and age >= LAG_SECS else 0.0
