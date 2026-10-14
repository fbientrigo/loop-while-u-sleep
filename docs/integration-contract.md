# I/O contract for a future Research Policy Engine

Contract only. Nothing here is implemented as an integration, and AutoLoop does not import or call any policy engine.

## Input

- `TASK.md` with `# Objective`, `# Acceptance Criteria` (at least one bullet), optional `# Constraints`, `# Verification`.
- `gauntlet.toml`: worker/critic model and effort, `[[verification.commands]]`, optional `verification.protected`, `[limits]` (`wall_time`, `max_worker_turns`, `same_blocker_limit`). These are the whole resource allocation.
- Invocation: `gauntlet run --task TASK.md` then `gauntlet resume RUN_ID`, or `gauntlet run --task TASK.md --execute`.

## Output

- `RUN_ID` (printed by `run`, directory `.gauntlet/runs/RUN_ID`).
- Terminal status from `gauntlet status RUN_ID`: `DONE`, `BLOCKED`, or `ESCALATED` (needs a human: `reconcile`).
- `.gauntlet/runs/RUN_ID/summary.json`: per-turn worker launches, changes, checks and verdict references.
- Evidence: `events.jsonl`, `worker-turn-N-*`, `critic-turn-N-verdict.json`, `verification-*.json`, `protected-manifest.json`.
- Exit codes: `0` DONE, `1` run ended not DONE, `2` usage/config/lock error.

AutoLoop never pushes or merges; any such step belongs to the caller.
