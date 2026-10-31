# Architecture

conductor is a library with a command line on top. Every module is small and does one thing, and only
`scheduler.py` knows about all of them.

| Module | What it owns |
|---|---|
| `contract.py` | The workflow file: validation, defaults, the dependency graph, and the fingerprint an approval binds to. |
| `scheduler.py` | The run: which package does what next, worker threads for stages, the merge queue's turn, Stop and Resume. |
| `states.py` | The package state machine as one table. Pure, no I/O. |
| `worktrees.py` | Package worktrees and branches, stage commits, the base refresh, removal, and the lease manager. |
| `merge_queue.py` | The integration worktree, the squash, the re-check bound to the landing SHA, and the fast-forward. |
| `checks.py` | Running acceptance checks and judging a check receipt against the commit it claims. |
| `receipts.py` | Receipt refs that keep stage commits reachable, and the JSON receipt files. |
| `events.py` | The NDJSON event log and its catalogue. |
| `permissions.py` | Dispatch roots, grants that survive a restart, and the approvers. |
| `store.py` | The run directory, atomic state writes, the stop request file, and the run and repository locks. |
| `procs.py` | The allow-listed child environment, process groups, and stopping a process tree. |
| `agents/` | The adapter contract (`base.py`), the scripted agents, and the Codex CLI and Claude Code adapters. |
| `cli.py`, `demo.py` | The `conductor` command and the scripted demo. |

There are no runtime dependencies. Everything above is the standard library and the `git` binary.

## A run, start to finish

```
conductor run workflow.toml
  contract.load            validate the whole file, refuse unknown fields
  permissions              the repository must be inside a granted root
  approver.approve(run)    bound to the plan's SHA-256 fingerprint; refused means no run directory
  store.RunDir             .conductor/runs/<run-id>/state.json, events.ndjson, receipts/
  scheduler.Conductor      loop until every package has landed, the run is stopped, or nothing can move
```

The loop runs on one thread. Each pass it:

1. Moves a waiting package to ready when every package it depends on has been accepted.
2. Starts stages for ready packages, up to `max_parallel` at a time: implement, then verify (and review from T1).
   A T3 package then waits for the approver.
3. Gives the merge queue its turn: if nothing is landing, the package that joined the queue first starts its
   landing (build, re-check, land, one phase per pass).
4. Collects finished stages and applies their results through the state table.

Stages run on worker threads and return a plain result. Only the loop thread changes the run state, every change
goes through `states.check`, and the state is written to disk after each change. A crash therefore loses at most
the stage that was running, and that stage is found again on resume.

The loop saves the implement result as `stage_head` before verification. Every verification first proves that the
worktree is still at that head or its recorded refresh merge, and repeats the package's scope check. Refresh intent
records both parents before the merge; its result is recorded afterwards, so a crash between them is recoverable.
An unexpected reviewer commit is kept under `refs/conductor/discarded/` with a receipt before reset and rework.
Before every resumed dispatch, refresh or reset, the worktree must be a registered linked worktree of this repository,
at its exact canonical `.conductor/worktrees/<id>` path, with no linked parent, and inside a granted root.

A conflict in the queue or refresh starts a new attempt from the current tip.
A failed check, review or re-check continues in the same worktree as a new attempt, refreshed before verification.

## Package states

```
waiting -> ready -> running -> verifying -> queued_for_merge -> merging -> accepted
                       |           |      \-> awaiting_approval (T3) -/     |   \-> merge_held -> merging
                       v           v                                         v
                     failed  <-----+-----------------------------------------+
                       |-> ready (a new attempt, within max_attempts)
                       \-> budget_spent

Stop:   ready | running | verifying | awaiting_approval | queued_for_merge | merging | merge_held -> held
Resume: held -> ready (the same attempt)        a run that died: running ... -> uncertain -> ready
        a package a crash left failed or uncertain is settled first (retry or budget, then ready)
Cancel: anything unfinished -> stopped
```

The full table, with the trigger for every move and what it does to the attempt counter, is `TABLE` in
`src/conductor/states.py`.

## Stages and keys

Every stage run has a dispatch key, `<run-id>:<package>:<stage>:<n>`, where `n` counts that stage's runs for the
package. The key is written into the stage commit's message (`Dispatch-Key:`), which makes a stage commit
idempotent: a stage that runs again after a lost result finds its own commit instead of committing twice. The
landing re-check is a `verify` key too, so its receipt can be told apart from the package's own checks.

Attempts are counted separately from runs. The first start and every rework spend an attempt. A stage that runs
again after Stop and Resume, or after a crash, is the same attempt: it is recorded as a continuation, and the
agent is told to finish the work rather than start over. An adapter with a known session id resumes the session the
stopped stage was in, for the implementer and the reviewer alike. A rework that must start from the new tip keeps that
intent until a worktree has actually been provisioned, so a crash in between cannot lose it. The worktree's metadata
also records the run and fresh attempt before the agent starts. A repeated dispatch for that attempt reuses the
replacement, so a crash after provisioning cannot delete partial work. The attempt's pre-agent head is kept under
`refs/conductor/stage-base/<run>/<package>/<attempt>` before dispatch, for scope cleanup after a lost commit result.

A cancelled check restores its clean starting head and keeps cleanup evidence in the check receipt. Implement
work is kept on Stop; reviewer edits are rejected even after a Stop or crash. Claude session ids are saved before
dispatch and passed with `--session-id`; continuation uses `--resume`. If Claude answers that it has no such
session (the stop came before it saved one), the stage runs once more with `--session-id` and the same id, so the
Stop still costs no attempt. Other adapters need to supply an id before
they can resume a session, and a later missing id never erases a known one.

The driver reads the run's state again once it holds the repository's and the run's locks, and refuses a run that
another process cancelled or finished in the meantime. One unfinished run owns each package id: a second run with
the same id is refused when it is created, since the worktree and branch are named after the id alone.

## The merge queue

The integration worktree is `.conductor/worktrees/@integration-<branch>`, detached at the branch's tip. A landing
has three phases, each its own pass of the loop and each safe to repeat:

- **build**: `git merge --squash` the package's verified head (the commit its checks, review and approval saw)
  onto the tip, and commit once with `Conductor-Landing:` and `Conductor-Package:` trailers. If the branch has moved
  past the verified head, nothing is built and the package is reworked. A repeat finds the same commit and reuses
  it. A conflict returns the paths and resets the worktree to the tip.
- **check**: run the package's checks in the integration worktree. They run only on a clean tree, the tree is looked
  at again after each check, and the receipt must show HEAD equal to the landing commit throughout and the exact
  contract fingerprint. Assume-unchanged and skip-worktree flags are refused before checks and after every command.
- **land**: if main has moved since the build, the landing is stale and goes back to build. If main is checked out
  in the main checkout, `git merge --ff-only` under guards (nothing staged, no edited or untracked file in the
  way). If it is checked out nowhere, `git update-ref refs/heads/main <landing> <parent>`, which moves the branch
  only if it still points at the parent. Anything in the way is a hold, never a force. A land step that errors
  may already have moved main, so it is never reworked: it is retried after a pause that doubles each time, and
  after three errors in a row the package is held and the run ends held, for `conductor resume` to retry. A landing
  held by the checkout records what was in the way and is tried again when any of it changes (a commit, the index,
  a moved or deleted file or ancestor, `index.lock`, the branch checked out elsewhere). Refusals without a known
  watched cause also retry every five seconds while `hold_wait` lasts. Repeated holds do not restart that deadline.
  Packages queued behind a held landing wait with it, and
  the run settles as held rather than polling forever.

After a landing the package's worktree is removed, `refs/conductor/receipts/<package>/tip` is pointed at the
branch's last stage commit, and the branch itself is deleted only when that ref holds its tip.

## Leases

`LeaseManager` gives one holder at a time a writer lease on a directory tree. Two leases conflict when one path
contains the other. A package's stages lease its worktree; a landing's build and re-check lease the integration
worktree; the fast-forward leases the main checkout's `.git` directory. Sibling packages therefore never wait on
each other, and a landing never waits on an agent.

## On disk

```
<repo>/.conductor/                     excluded through .git/info/exclude
  worktrees/<package>/                 one linked worktree per package, on conductor/<package>
  worktrees/@integration-main/         the merge queue's worktree, detached
  runs/<run-id>/state.json             the run state, rewritten whole and renamed into place
  runs/<run-id>/events.ndjson          the event log
  runs/<run-id>/receipts/<package>/    001-stage.json, 002-checks.json, 003-recheck.json, 004-landing.json ...
  runs/<run-id>/control/stop           a stop request from any terminal
  runs/<run-id>/run.lock               held by the process driving the run (an OS lock)
  conductor.lock                       the repository's lock: one process creates, drives or cancels a run
<git-common-dir>/conductor-worktrees/  which worktree conductor made for which package, and from which base
refs/conductor/receipts/<package>/     the stage commits of every package that landed
~/.conductor/grants.json               recorded grants (CONDUCTOR_HOME moves it)
~/.conductor/projects.json             repositories conductor has run in
```

## Events

`events.py` holds the catalogue: every event kind and its fields. An event outside it, or with a field it does not
list, raises instead of being written. One event per line, with a sequence number that is never reused:

```json
{"seq": 41, "ts": 1793446151.402, "kind": "merge.landed", "package_id": "A", "sha": "264b805...",
 "parent_sha": "14dfee4...", "receipts_ref": "refs/conductor/receipts/A/tip", "already": false}
```

`conductor log --json` prints the raw stream; `conductor log --follow` tails a live run. On reopening, a complete
last record without a newline gets its delimiter; only an unparsable tail is dropped. The next sequence is above
the highest surviving number, including logs with gaps.

Grant restoration reads the project list and grants record inside the same records lock, so a concurrent project's
new grant cannot be dropped by a stale snapshot.

## Processes

Agents and checks are started by `procs.spawn`: an argv list, no shell, no terminal (stdin is empty, or an agent's
brief followed by end of input), the allow-listed environment, and their own process group. A timeout or a Stop calls
`procs.stop_tree`, which signals the whole group on POSIX and sends CTRL_BREAK then terminates on Windows
(grandchildren too when `psutil` is installed). Every git call goes through `worktrees._git`: an argv list, a
timeout, and the same environment with prompts turned off.
