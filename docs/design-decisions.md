# Design decisions

Each entry is a choice that shaped the engine, what it costs, and what was turned down. Most of them were made the
hard way in Aura, the orchestrator this engine comes from, after the alternative broke something.

## 1. Main only fast-forwards

The integration branch moves by `git merge --ff-only` in the main checkout, or by
`git update-ref <branch> <new> <old>` when it is checked out nowhere. Both refuse to move unless the branch is
exactly where the landing was built. Anything else is a hold or a stale landing that gets built again.

Cost: a landing can wait on the operator's checkout (a staged file, an untracked file in the way). That is the
point, as the alternative is overwriting someone's work. Turned down: merge commits on main (main's history becomes
a record of the queue's timing rather than of the packages) and any `--force` or reset of the branch.

## 2. One squash commit per package, built in a separate worktree

A package's stage commits stay on its branch as receipts; main gets one commit per package, built on the current tip
in `.conductor/worktrees/@integration-<branch>`. The operator's checkout keeps its branch checked out and is never
used as scratch space.

Cost: main's commits do not carry the stage history. It stays reachable under `refs/conductor/receipts/`, and each
landing commit names the package commit it squashed (`Conductor-Package:`). Turned down: landing the package branch
itself (its history includes refresh merges and abandoned attempts).

## 3. The re-check is bound to the landing SHA

A package's checks passing in its worktree says little about the commit that lands, as main may have moved. So the
checks run again on the landing commit, on a clean tree that is looked at again after every check, and the receipt
must show that commit as HEAD throughout and the contract's exact fingerprint. The landing itself is built from the
head the package's checks, review and approval saw, never from wherever the branch is when its turn comes. The fast-forward then moves to that commit and no other: if main
moved in between, the landing is built and checked again.

Cost: every package's checks run at least twice. Turned down: trusting the worktree run, and batching landings
without re-checking each one (see "What's next" in the README for merge trains).

## 4. Refresh by merge, never rebase

Before its checks, a package branch is brought up to date by merging the integration branch into it. A rebase would
rewrite the stage commits whose SHAs the receipts name. Nothing in the source can rebase: `tests/test_guarantees.py`
reads every git argv conductor builds.

## 5. One landing at a time

The queue is serial. It is the simplest order in which "what landed is what was re-checked" is true without
qualification, and a held landing blocks the queue because main cannot move past it anyway. The order holds across
processes too: creating, driving and cancelling a run take the repository's lock (`.conductor/conductor.lock`), so a
second `conductor run` in the same repository is refused rather than landing beside the first.

Cost: throughput is bounded by the slowest re-check. For the package counts an operator supervises this has not
been the bottleneck; agents are.

## 6. A conflict is rework, from the new tip

When a package no longer squashes onto main (or its refresh conflicts), it goes back to its agent as a new attempt.
Its branch is renamed `conductor/<id>@attempt-<n>` (kept, never reset) and the new attempt starts from the current
tip, with the conflicting paths as feedback. A failed check, review or re-check continues in the same worktree
as a new attempt. A fresh provisioning marker names the run and attempt, so repeating dispatch after a crash
keeps the replacement's partial work while a crash before provisioning still replaces the conflicted tree.

Turned down: resolving conflicts in the merge queue. The queue would be writing code nobody reviewed.

## 7. Packages declare their files

Every package lists the files or directories it may change. The attempt's base is saved before the agent runs.
A stage that changes anything else is reset to that base, even after a crash lost its commit result. The commit
stays under `refs/conductor/discarded/` and the patch is kept as a receipt. It counts as a failed attempt.
Verification repeats the scope check and accepts only the saved implement head or a recorded refresh merge.
This is what makes parallel work reviewable: you can tell from the
workflow file which packages can collide, and `conductor validate` warns about the overlaps.

## 8. Stop holds; Resume continues the same attempt

Stop cancels the running stages, stops their process trees, and holds every package where it was. Implement work
is kept. A cancelled check restores its clean starting head and records cleanup evidence, so its scratch files
cannot cost the attempt on Resume. Reviewer edits still fail the attempt and are kept as discarded evidence.
Claude gets a saved UUID with `--session-id` before dispatch, then `--resume` on continuation. A Claude stopped
before it saved that session has nothing to resume, so the adapter starts it under the same id instead of failing.
It matches Claude's exact "No conversation found with session ID: <id>" error; if that wording changes, the stage
fails and costs the attempt, as it did before. An absent result id never replaces a known one; other adapters can
resume only once they have supplied an id.
A process that died without a clean stop is found on resume: its active
packages pass through `uncertain` and continue the same way.

## 9. Approvals bind to fingerprints, and no terminal means no

An approval names the SHA-256 of exactly what it approves: the whole validated plan, a grant, or one package's
verified head. A changed workflow does not resume under an old approval. The command-line approver reads from the
terminal and refuses when there is none, so an agent (whose stdin ends after its brief) cannot approve its own work through
conductor. For unattended runs, the policy approver reads an allow-list file that must live outside every repository
conductor runs in.

This replaces Aura's native approval credential, which tied approvals to a desktop dialog.

## 10. Grants outlive the process only while a project lives in them

A grant is the operator's explicit act, recorded under `~/.conductor`. Each start restores a recorded grant only while
a registered, unarchived project lies inside it, canonicalizes it again, and drops it if the folder vanished or now
resolves through a link. Grants made implicitly during a run are never recorded. In Aura, losing grants on restart
meant a stopped run could not even remove its own worktrees afterwards; bounding them by projects keeps the record
from growing until it refuses new grants. Restoration reads projects and grants inside the same records lock,
so a registration and grant completed by another process cannot be erased by an earlier project snapshot.

## 11. Files and OS locks instead of a daemon

Stop is a file in the run directory, and the process driving a run holds an OS file lock (`fcntl` or `msvcrt`) that
the operating system releases if it dies. The same code runs on Linux and Windows (macOS takes the POSIX path but is
not tested), survives the process that wrote it, and needs no named pipe, socket or service. Links into worktrees are
symlinks rather than Windows junctions; on Windows that needs Developer Mode or an elevated shell, and only when a
workflow uses `environment.link`.

## 12. An allow-listed environment, checked by an AST guard

A child inherits only a fixed list of variables (`PATH`, `HOME`, the temporary directory, the locale, and what
Windows needs to run programs) plus names its workflow adds in `environment.env`. Secret-shaped names (`*_TOKEN`,
`*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_JWT`, `*_DSN`, `*ASKPASS`, `GIT_CONFIG_*`, `SSH_AUTH_SOCK`, `CONDUCTOR_*`) are
dropped even then; an adapter that needs its own credential lists it by name in `env_allow`. An allowlist fails
closed: a credential under a name nobody thought of is not inherited. The cost is that a check needing an unusual
variable must name it. This is about the environment only; credentials in files stay reachable to an unsandboxed
agent (see the threat model).
`tests/test_spawn_guard.py` walks the source's syntax tree and fails the build on any spawn without an explicit
`env=`, including keyword dicts captured by nested scopes and spawn functions invoked through attributes or
resolvable `getattr` calls.

## 13. Events have a catalogue

The event log is NDJSON with a fixed catalogue of kinds and fields. An unknown kind or field raises rather than being
written, so the log stays something a dashboard or a test can depend on. A complete last record without its newline
is kept and delimited on reopening. Only an unparsable tail is dropped, and allocation starts above the maximum sequence.

## 14. No runtime dependencies

The standard library and the `git` binary. It keeps the trust surface small for a tool that runs other programs.

## 15. One unfinished run owns a package

A package's worktree and branch are named after its id alone (`.conductor/worktrees/<id>`, `conductor/<id>`), which
keeps them easy to find and to clean up. The cost is that two runs cannot hold the same package at once: a held run's
partial work would be reused or removed by the other. So a run is refused when it names a package another unfinished
run still holds, and cancelling a run removes only the worktrees it provisioned. Turned down: namespacing every
worktree and branch by run, which keeps more abandoned trees around than it saves.
