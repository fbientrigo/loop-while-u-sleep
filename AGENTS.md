# AGENTS.md

AutoLoop (package `gauntlet`) is a bounded executor: an `agy` worker edits a Git worktree, deterministic checks run, a read-only Codex critic reviews, and only the supervisor may declare DONE.

## Invariants (do not weaken)

- `DONE` is written only by the supervisor, after final checks pass and a valid critic `PASS` exists for the same turn.
- The critic must be proven read-only (`gauntlet doctor` -> `codex_read_only: PROVEN`) before a live run.
- A worker turn with `WORKER_STARTED` and no durable result is `WORKER_EXECUTION_UNKNOWN` -> `ESCALATED`. It is never relaunched. Only `gauntlet reconcile` (operator, with a note) moves it on.
- Run artifacts are frozen: never overwrite or edit events or `*-result.json`. Corrections are new events or a new run.
- Files in `verification.protected` must not change during a run (`EVALUATOR_TAMPERED` -> `BLOCKED`).
- One active run per repository. One supervisor per run (`run.lock`).
- AutoLoop never pushes, merges, or deletes branches.
- No policy features (task selection, quota optimization, routing). See `docs/integration-contract.md`.

## Commands

```sh
python -m pip install -e .
python -m unittest discover -s tests -v   # fake agents, no network
python -m pytest -q
ruff check src tests
gauntlet doctor
```

Tests must not call real providers. Live runs are manual and recorded in `docs/validation.md`.
