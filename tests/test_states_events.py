"""The state table and the event log: small, pure, and the contract everything else is checked against."""

from __future__ import annotations

import json

import pytest

from conductor import states
from conductor.events import CATALOGUE, EventError, EventLog, follow, read_events


def test_every_row_names_known_states():
    for (source, target), (trigger, attempt) in states.TRANSITIONS.items():
        assert source in states.STATES and target in states.STATES
        assert trigger and attempt in {"unchanged", "spends if new", "new attempt", "continuation"}


def test_terminal_states_have_no_way_out():
    assert {"accepted", "stopped", "budget_spent"} == states.TERMINAL
    assert not [pair for pair in states.TRANSITIONS if pair[0] in states.TERMINAL]


def test_the_landing_path_and_its_exits():
    for pair in [
        ("ready", "running"),
        ("running", "verifying"),
        ("verifying", "queued_for_merge"),
        ("queued_for_merge", "merging"),
        ("merging", "accepted"),
    ]:
        assert states.allowed(*pair)
    assert states.check("merging", "failed")[1] == "new attempt"
    assert states.check("held", "ready") == ("Resume continues the same attempt", "continuation")
    assert not states.allowed("verifying", "accepted")  # nothing lands without the queue
    assert not states.allowed("running", "queued_for_merge")  # nothing queues without its checks


def test_stop_holds_only_holdable_states():
    held_from = {source for source, target in states.TRANSITIONS if target == "held"}
    assert held_from == states.HOLDABLE
    assert "waiting" not in held_from


def test_refusals():
    with pytest.raises(states.TransitionRefused, match="cannot move from accepted"):
        states.check("accepted", "ready")
    with pytest.raises(states.TransitionRefused, match="Unknown package state"):
        states.check("ready", "flying")


def test_event_log_appends_in_order_and_continues_after_reopening(tmp_path):
    path = str(tmp_path / "run" / "events.ndjson")
    log = EventLog(path, clock=lambda: 12.5)
    seen = []
    log.subscribe(seen.append)
    log.subscribe(lambda event: 1 / 0)  # a failing listener is ignored
    log.emit("run.resumed", run_id="r", packages=["A"])
    log.emit("merge.queued", package_id="A", position=1)
    log.close()
    again = EventLog(path)
    again.emit("merge.stale", package_id="A", head_sha="abc")
    again.close()
    events = list(read_events(path))
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[0] == {"seq": 1, "ts": 12.5, "kind": "run.resumed", "run_id": "r", "packages": ["A"]}
    assert [event["seq"] for event in seen] == [1, 2]
    assert [event["seq"] for event in read_events(path, since=2)] == [3]


def test_event_log_refuses_what_the_catalogue_does_not_name(tmp_path):
    log = EventLog(str(tmp_path / "events.ndjson"))
    with pytest.raises(EventError, match="Unknown event kind"):
        log.emit("made.up")
    with pytest.raises(EventError, match="does not carry extra"):
        log.emit("merge.queued", package_id="A", extra=1)
    log.close()


def test_a_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / "events.ndjson"
    path.write_text(json.dumps({"seq": 1, "kind": "x"}) + "\n" + '{"seq": 2, "ki', encoding="utf-8")
    assert [event["seq"] for event in read_events(str(path))] == [1]
    assert list(read_events(str(tmp_path / "missing.ndjson"))) == []
    assert [event["seq"] for event in follow(str(path), stop=lambda: True)] == [1]


def test_catalogue_fields_never_shadow_the_envelope():
    for kind, fields in CATALOGUE.items():
        assert not {"seq", "ts", "kind"} & set(fields), kind
