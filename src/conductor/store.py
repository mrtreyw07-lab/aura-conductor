"""Where a run lives: ``<repo>/.conductor/runs/<run-id>/``, kept out of git through ``info/exclude``.

* ``state.json``: the run's state, rewritten whole and renamed over the last copy, so a crash leaves the old
  state or the new one, never half of either.
* ``events.ndjson``: the event log (``events.py``).
* ``receipts/``: the JSON receipts (``receipts.py``).
* ``control/stop``: a stop request. ``conductor stop`` writes it from any terminal; the running process polls
  for it. A file, so it works the same on every platform and survives the process that wrote it.
* ``run.lock``: held by the process driving the run. The operating system releases it when that process dies,
  so a crashed run never blocks its own resume.

``<repo>/.conductor/conductor.lock`` is the repository's lock: one process creates, drives or cancels a run in a
repository at a time.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import worktrees as wt

RUNS = ".conductor/runs"
STATE_VERSION = 1


class RunError(ValueError):
    pass


class RunBusy(RunError):
    """Another process is driving this run."""


class RepositoryBusy(RunBusy):
    """Another process holds the repository's lock: it is creating, driving or cancelling a run."""


def new_run_id(clock=time.time) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    return f"{stamp}-{secrets.token_hex(2)}"


class FileLock:
    """An exclusive, non-blocking lock on a file, released by the operating system when the holder dies."""

    def __init__(self, path: str):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held until release()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


class RunDir:
    def __init__(self, root: str, run_id: str):
        if not wt.RUN_ID.fullmatch(str(run_id)):
            raise RunError(f"Invalid run id {run_id!r}.")
        self.root = root
        self.run_id = run_id
        self.path = os.path.join(root, *RUNS.split("/"), run_id)

    @property
    def state_path(self) -> str:
        return os.path.join(self.path, "state.json")

    @property
    def events_path(self) -> str:
        return os.path.join(self.path, "events.ndjson")

    @property
    def receipts_path(self) -> str:
        return os.path.join(self.path, "receipts")

    @property
    def stop_path(self) -> str:
        return os.path.join(self.path, "control", "stop")

    def exists(self) -> bool:
        return os.path.isfile(self.state_path)

    def load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                state = json.load(handle)
        except FileNotFoundError:
            raise RunError(f"No run {self.run_id} in this repository.") from None
        except ValueError as exc:
            raise RunError(f"The state of run {self.run_id} is unreadable: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise RunError(f"Run {self.run_id} was written by an incompatible version.")
        return state

    def save(self, state: dict) -> None:
        os.makedirs(self.path, exist_ok=True)
        partial = self.state_path + ".partial"
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, self.state_path)

    def request_stop(self) -> None:
        os.makedirs(os.path.dirname(self.stop_path), exist_ok=True)
        with open(self.stop_path, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))

    def stop_requested(self) -> bool:
        return os.path.exists(self.stop_path)

    def clear_stop(self) -> None:
        try:
            os.remove(self.stop_path)
        except FileNotFoundError:
            pass

    def busy(self) -> bool:
        lock = FileLock(os.path.join(self.path, "run.lock"))
        if lock.acquire():
            lock.release()
            return False
        return True

    @contextmanager
    def lock(self) -> Iterator[None]:
        lock = FileLock(os.path.join(self.path, "run.lock"))
        if not lock.acquire():
            raise RunBusy(f"Run {self.run_id} is being driven by another process.")
        try:
            yield
        finally:
            lock.release()


REPOSITORY_LOCK = ".conductor/conductor.lock"


@contextmanager
def repository_lock(root: str, holder: str) -> Iterator[None]:
    """One conductor process per repository at a time, so "one landing at a time" holds across processes too.

    Creating, driving and cancelling a run take it. The operating system releases it if the holder dies. Who holds
    it is written beside it (``conductor.lock.owner``), for the refusal message.
    """
    path = os.path.join(root, *REPOSITORY_LOCK.split("/"))
    owner = path + ".owner"
    lock = FileLock(path)
    if not lock.acquire():
        try:
            with open(owner, encoding="utf-8") as handle:
                held = json.load(handle)
            who = f" ({held.get('holder')}, process {held.get('pid')})"
        except (OSError, ValueError, AttributeError):
            who = ""
        raise RepositoryBusy(
            f"Another conductor process is working in this repository{who}; one run lands at a "
            "time. Wait for it, or stop it with `conductor stop`."
        )
    try:
        with open(owner, "w", encoding="utf-8") as handle:
            json.dump({"holder": holder, "pid": os.getpid(), "since": time.time()}, handle)
        yield
    finally:
        try:
            os.remove(owner)
        except OSError:
            pass
        lock.release()


def runs(root: str) -> list[str]:
    """Run ids in this repository, oldest first (ids sort by their UTC start time)."""
    folder = os.path.join(root, *RUNS.split("/"))
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted(
        name for name in names if wt.RUN_ID.fullmatch(name) and os.path.isfile(os.path.join(folder, name, "state.json"))
    )


def latest(root: str) -> RunDir:
    ids = runs(root)
    if not ids:
        raise RunError("No conductor run in this repository yet.")
    return RunDir(root, ids[-1])


def open_run(root: str, run_id: str | None = None) -> RunDir:
    return RunDir(root, run_id) if run_id else latest(root)
