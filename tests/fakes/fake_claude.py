"""Stands in for ``claude -p --output-format json``: reads the brief on stdin, edits the worktree, prints a result.

Directives are the same as ``fake_codex.py``'s. With ``FAKE_SESSIONS`` set to a directory, a session is saved there
only when the fake finishes, and ``--resume`` of an unsaved id fails as the real CLI does. A stop during ``sleep``
then leaves nothing to resume, like a Claude stopped before it saved its session.
"""

import json
import os
import sys
import time

argv = sys.argv[1:]
brief = sys.stdin.read()
session_flag = "--resume" if "--resume" in argv else "--session-id"
session = argv[argv.index(session_flag) + 1]
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "brief": brief}) + "\n")
saved = os.environ.get("FAKE_SESSIONS")
if saved and session_flag == "--resume" and not os.path.exists(os.path.join(saved, session)):
    print(f"No conversation found with session ID: {session}", file=sys.stderr)
    sys.exit(1)
result = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": session}
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
        result.update(subtype="error_during_execution", is_error=True, result=line[5:])
    elif words[0] == "verdict":
        result["result"] = f"Reviewed the change.\nVERDICT: {words[1]}"
if saved:
    open(os.path.join(saved, session), "w").close()
print(json.dumps(result))
sys.exit(1 if result["is_error"] else 0)
