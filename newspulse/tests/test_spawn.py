"""A worker that cannot start must not leave its lock held.

Five routes take a module-level lock in the request thread and let the worker's
``finally`` give it back. If ``Thread.start()`` raises there is no worker, so
nothing releases — and because the lock is module-level and the process is long
lived, every later run of that feature is refused until a restart, with the page
saying, correctly and uselessly, that one is already in progress.
"""

from __future__ import annotations

import threading

import pytest

from newspulse.web import spawn


def _refuses_to_start(*args, **kwargs):
    raise RuntimeError("can't start new thread")


def test_the_lock_comes_back_when_the_thread_will_not_start(monkeypatch):
    lock = threading.Lock()
    assert lock.acquire(blocking=False)
    monkeypatch.setattr(threading.Thread, "start", _refuses_to_start)

    with pytest.raises(RuntimeError):
        spawn.start_or_release(lambda: None, name="test", release=lock.release)

    assert lock.acquire(blocking=False), "the next request may run"


def test_the_callers_own_undo_runs_not_just_a_release(monkeypatch):
    """A route that also recorded "a run is in progress" has to take that back,
    or the page keeps announcing work that no longer exists."""
    undone: list[str] = []
    monkeypatch.setattr(threading.Thread, "start", _refuses_to_start)

    with pytest.raises(RuntimeError):
        spawn.start_or_release(
            lambda: None, name="test", release=lambda: undone.append("state+lock")
        )

    assert undone == ["state+lock"]


def test_a_thread_that_starts_is_left_alone():
    """The undo must not fire on the happy path: it would hand the lock away
    from the worker that is holding it."""
    done = threading.Event()

    spawn.start_or_release(
        done.set, name="test-ok", release=lambda: pytest.fail("released a live worker")
    )

    assert done.wait(timeout=2)
