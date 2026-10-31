"""The run's event log: one JSON object per line (NDJSON), appended in order and never rewritten.

Every state change, stage, check, approval and landing is an event with a sequence number that is never reused.
The catalogue below names each kind and its fields; an event outside it is refused, so the log stays a contract
that ``conductor log``, a dashboard or a test can read without guessing. Paths in events are relative to the
repository, so a log can be shared without leaking a machine's layout.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator

CATALOGUE: dict[str, tuple[str, ...]] = {
    "run.started": ("run_id", "workflow", "workflow_sha256", "fingerprint", "packages", "integration_branch"),
    "run.resumed": ("run_id", "packages"),
    "run.stopping": ("run_id", "by"),
    "run.held": ("run_id", "packages", "reason"),
    "run.finished": ("run_id", "status", "accepted", "unfinished"),
    "run.cancelled": ("run_id", "packages"),
    "package.state_changed": ("package_id", "from", "to", "cause", "attempt"),
    "stage.started": ("package_id", "key", "stage", "attempt", "continuation"),
    "stage.ended": ("package_id", "key", "stage", "attempt", "outcome", "detail"),
    "stage.committed": ("package_id", "key", "sha", "files"),
    "agent.progress": ("package_id", "key", "message"),
    "worktree.provisioned": ("package_id", "path", "branch", "base_sha", "created", "preserved_branch"),
    "worktree.refreshed": ("package_id", "old_base", "new_base", "head_sha"),
    "worktree.removed": ("package_id", "path"),
    "check.result": ("package_id", "key", "index", "argv", "exit_code", "duration_ms", "output_sha256"),
    "review.verdict": ("package_id", "key", "verdict", "summary"),
    "approval.requested": ("approval", "subject", "fingerprint"),
    "approval.decided": ("approval", "subject", "fingerprint", "approved", "approver", "reason"),
    "merge.queued": ("package_id", "position"),
    "merge.built": ("package_id", "landing_sha", "parent_sha", "package_sha", "files", "replayed"),
    "merge.conflict": ("package_id", "paths", "integration_sha", "package_sha"),
    "merge.rechecked": ("package_id", "landing_sha", "passed", "reason"),
    "merge.landed": ("package_id", "sha", "parent_sha", "receipts_ref", "already"),
    "merge.stale": ("package_id", "head_sha"),
    "merge.held": ("package_id", "reason"),
    "receipt.recorded": ("package_id", "receipt", "path", "sha256"),
}


class EventError(ValueError):
    pass


class EventLog:
    """Append-only, thread-safe. Listeners see each event after it is written; a failing listener is ignored."""

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _drop_torn_tail(path)
        # The highest sequence number written, not the count of events: a number is never handed out twice.
        self._seq = max((int(event.get("seq", 0)) for event in read_events(path)), default=0)
        self._handle = open(path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115 - closed by close()

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        self._listeners.append(listener)

    def emit(self, kind: str, /, **data) -> dict:
        fields = CATALOGUE.get(kind)
        if fields is None:
            raise EventError(f"Unknown event kind {kind!r}.")
        unknown = set(data) - set(fields)
        if unknown:
            raise EventError(f"{kind} does not carry {', '.join(sorted(unknown))}.")
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": round(self._clock(), 3), "kind": kind, **data}
            self._handle.write(json.dumps(event, ensure_ascii=False, sort_keys=False) + "\n")
            self._handle.flush()
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass
        return event

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


def _drop_torn_tail(path: str) -> None:
    """Keep a complete final record, even without its delimiter. Drop only a tail that cannot parse."""
    try:
        with open(path, "rb+") as handle:
            data = handle.read()
            if data and not data.endswith(b"\n"):
                start = data.rfind(b"\n") + 1
                try:
                    json.loads(data[start:])
                except (ValueError, UnicodeDecodeError):
                    handle.truncate(start)
                else:
                    handle.write(b"\n")
    except FileNotFoundError:
        pass


def read_events(path: str, since: int = 0) -> Iterator[dict]:
    """The events after sequence number ``since``. A torn last line (a crash mid-write) is skipped."""
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed below
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and int(event.get("seq", 0)) > since:
                yield event


def follow(path: str, since: int = 0, *, poll: float = 0.2, stop: Callable[[], bool] = lambda: False) -> Iterator[dict]:
    """Like ``tail -f``: yields new events until ``stop()`` is true."""
    cursor = since
    while True:
        for event in read_events(path, cursor):
            cursor = int(event["seq"])
            yield event
        if stop():
            return
        time.sleep(poll)
