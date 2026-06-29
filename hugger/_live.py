"""Cross-thread change notifications for the live SSE panels.

Jobs run in background threads; the SSE streams run in the asyncio loop. `bump()`
(safe from any thread) wakes every waiting stream, which then re-renders and
pushes a patch only if its own fragment changed. No fixed-interval re-rendering —
idle state means no wakeups and no traffic.
"""
from __future__ import annotations

import asyncio

_loop: asyncio.AbstractEventLoop | None = None
_version = 0
_waiters: set[asyncio.Future] = set()
_shutdown = False


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the running event loop so bump() can hop onto it from worker threads."""
    global _loop
    _loop = loop


def begin_shutdown() -> None:
    """Mark shutdown and wake every stream so they return cleanly (no force-cancel)."""
    global _shutdown
    _shutdown = True
    _apply()


def is_shutting_down() -> bool:
    return _shutdown


def bump() -> None:
    """Signal that some state changed. Safe to call from any thread."""
    loop = _loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_apply)
    else:
        _apply()


def _apply() -> None:
    global _version
    _version += 1
    for fut in list(_waiters):
        if not fut.done():
            fut.set_result(_version)
    _waiters.clear()


def version() -> int:
    return _version


async def wait(since: int, timeout: float) -> int:
    """Block until the next bump after `since` (or `timeout` seconds as a keepalive
    heartbeat). Returns the current version."""
    if _loop is None:
        bind_loop(asyncio.get_running_loop())
    if _version != since:
        return _version
    fut = _loop.create_future()
    _waiters.add(fut)
    try:
        await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        _waiters.discard(fut)
    return _version
