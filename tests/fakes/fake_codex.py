"""Stands in for ``codex exec --json``: reads the brief on stdin, edits the worktree, prints Codex's JSONL events.

The brief's instruction says what to do, one directive per line:
``write <path> <text>``, ``verdict <approve|request_changes>``, ``fail <message>``, ``sleep <seconds>``,
``env <NAME>`` (writes the variable's value, or "absent", to env.txt).
"""

import json
import os
import sys
import time

argv = sys.argv[1:]
brief = sys.stdin.read()
resumed = argv[:2] == ["exec", "resume"]
session = argv[2] if resumed else "11111111-2222-3333-4444-555555555555"
print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "brief": brief}) + "\n")
message = "done"
for line in brief.splitlines():
    words = line.split(" ", 2)
    if words[0] == "write" and len(words) == 3:
        with open(words[1], "w", encoding="utf-8") as handle:
            handle.write(words[2])
    elif words[0] == "env":
        with open("env.txt", "w", encoding="utf-8") as handle:
            handle.write(os.environ.get(words[1], "absent"))
    elif words[0] == "sleep":
        time.sleep(float(words[1]))
    elif words[0] == "fail":
        print(json.dumps({"type": "turn.failed", "error": {"message": line[5:]}}), flush=True)
        sys.exit(1)
    elif words[0] == "verdict":
        message = f"Reviewed the change.\nVERDICT: {words[1]}"
print(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": message}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
