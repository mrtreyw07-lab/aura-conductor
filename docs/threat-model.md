# Threat model

conductor runs programs other people (or models) wrote, in repositories the operator cares about. This page says
what it protects, from whom, and where the protection ends.

## What is protected

- **The integration branch.** It only ever fast-forwards to a commit whose checks passed on that exact commit.
- **The operator's checkout.** Staged changes, edited files and untracked or ignored files are never overwritten.
- **Credentials in the operator's environment.** A child inherits an allowlist of variables, never tokens, keys,
  passwords, git config injected through the environment, askpass helpers or the ssh-agent socket. Credentials
  stored in files are not protected (see the end of this page).
- **The record of what happened.** Stage commits, check receipts and the event log.
- **The approval.** Nothing runs or lands under an approval it was not given.

## Who could do harm

| Actor | Trusted with | Not trusted with |
|---|---|---|
| The operator | everything: they grant, approve and run | |
| An agent | writing files in its own worktree | its scope, its own review, approval, landing, credentials |
| A reviewer agent | reading its package's worktree | writing anything |
| The workflow file | describing work | running anything before the operator approves its fingerprint |
| Check commands | running once approved | changing the tree they check |

## Threats and what stops them

| Threat | Mitigation | Where |
|---|---|---|
| An agent writes outside its package | Scope is checked after implementation and again before verification. The pre-agent base survives a crash; rejected commits and patches are kept before reset | `scheduler._implement_job`, `scheduler._verify_job` |
| An agent or check reads a credential from the environment | Children inherit an allowlist of variables only; secret-shaped names are dropped even when a workflow adds them; AST spawn guard on every spawn site | `procs.scrubbed_env`, `tests/test_spawn_guard.py` |
| An agent pushes with the operator's credentials | Through the environment: `SSH_AUTH_SOCK`, `*ASKPASS` and `GIT_CONFIG_*` never reach a child, and conductor itself never pushes. Not through files: an unsandboxed agent can still use `~/.ssh`, a git credential helper, or its CLI's login, as the operator could | `procs.py`, `tests/test_guarantees.py` |
| An agent approves its own work | Children get no terminal, only their brief and then end of input; the CLI approver refuses without one; a policy file inside a repository is refused | `procs.spawn`, `permissions.py` |
| A reviewer edits the code it reviews | Verification accepts only the saved implement head or a recorded refresh, including on Resume. Reviewer changes are kept as discarded evidence, reset and reworked | `scheduler._verify_job`, `worktrees.verification_base` |
| A check passes by changing what it checks | Checks reject assume-unchanged and skip-worktree flags before starting and after each command. HEAD and the clean tree are compared after every check. Not covered: one check command that changes files and restores them before it exits | `checks.run_checks`, `checks.bound` |
| Commits nobody verified land | The landing is built from the head the checks, review and approval saw; a branch that moved since is reworked, not landed | `merge_queue.build`, `scheduler._built` |
| A stale verification lands | The re-check runs on the landing commit; the fast-forward requires main at the landing's parent | `merge_queue.recheck`, `merge_queue.land` |
| Two runs work on one package | A package id belongs to one unfinished run; cancelling removes only the worktrees a run made; one process works in a repository at a time | `scheduler.claimed_packages`, `store.repository_lock` |
| A git command run in the wrong directory resets the main checkout | Every worktree operation proves canonical containment and registration in its owning repository first. Resumed dispatches also recheck grants | `worktrees.linked_worktree`, `scheduler._require_worktree` |
| Removing a worktree deletes through a link | Nothing is created, run or deleted when `.conductor` or `.conductor/worktrees` is a link; a worktree is removed only when its real path is exactly `.conductor/worktrees/<id>` in its repository, and links inside it are unlinked first | `worktrees.worktree_parent`, `worktrees.remove` |
| A path smuggles work outside the granted folders | Grants and dispatch paths must already be canonical; a path through a symlink is refused | `permissions.canonical_directory` |
| A changed workflow resumes under an old approval | The plan's fingerprint and the workflow file's hash are checked on resume | `scheduler.check_resumable` |
| A crash leaves a half-applied merge | Interrupted merges are aborted before a refresh, and the integration worktree is reset before a build | `worktrees.refresh_base`, `merge_queue.ensure` |

## Where the protection ends

conductor is a landing engine, not a sandbox. Agents run as the operator's OS user. An agent that can execute
arbitrary code can read files outside its worktree, write to `.conductor/` or `~/.conductor`, or install a git hook,
and conductor does not stop it while it runs. The environment allowlist does not change that: `HOME`, `~/.ssh`, git's
credential helpers and the agent CLIs' own login files are all files the operator's user can read. What conductor
guarantees is about what lands: an agent's work reaches
the integration branch only through the declared files, the checks, the re-check on the landing commit and, by tier,
a review and an approval. For isolation while agents run, use the agent's own sandbox (Codex's `--sandbox`, Claude
Code's permission modes) or a container, and grant conductor only the repository.

Other limits worth knowing:

- Approved check commands run arbitrary code. The approval is the control: read the plan summary before saying yes.
- Checks are only as strong as the code they run. When a package's declared files include its own tests or check
  scripts, its agent writes what it is judged by, and a test can change a file and restore it within one command.
  Keep the checks outside the files a package may change, or review it (T1 and up).
- On Windows, stopping a process tree reaches grandchildren only when `psutil` is installed; without it a grandchild
  can outlive a Stop.
- The event log and receipts are evidence, not signatures. Anyone with write access to the repository can edit them.
- Git hooks in the repository run on conductor's commits, as they would on the operator's.
