"""conductor: the landing engine for parallel coding agents.

Packages run in their own git worktrees, pass their acceptance checks, and land on the integration branch
one at a time through a merge queue that re-checks the exact commit it fast-forwards to.
"""

__version__ = "0.1.0"
