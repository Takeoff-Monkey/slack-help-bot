"""Thread-safe registry of in-flight bot tasks, so a follow-up message (handled on a
different Slack-Bolt worker thread) can cancel a task that's still running.

Keyed by Slack (channel, thread). Each running task registers a threading.Event; the agent
loop polls it, and a cancel request sets it. A thread can have more than one task in flight,
so cancel() trips them all — "stop" means stop whatever is running here.
"""

import threading

_active: dict[str, set] = {}
_lock = threading.Lock()


def key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def register(k: str) -> threading.Event:
    """Create + register a cancel Event for a task starting on key `k`."""
    ev = threading.Event()
    with _lock:
        _active.setdefault(k, set()).add(ev)
    return ev


def deregister(k: str, ev: threading.Event) -> None:
    with _lock:
        s = _active.get(k)
        if s is not None:
            s.discard(ev)
            if not s:
                _active.pop(k, None)


def has_active(k: str) -> bool:
    with _lock:
        return bool(_active.get(k))


def cancel(k: str) -> int:
    """Signal cancellation to every task running on key `k`. Returns how many were tripped."""
    with _lock:
        evs = list(_active.get(k, ()))
    for ev in evs:
        ev.set()
    return len(evs)
