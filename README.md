# AutoLoop (Gauntlet)

A bounded, auditable local executor for coding tasks. **Gemini through the `agy` CLI edits a dedicated Git worktree; Codex independently reviews the result.** The supervisor, not either model, is the sole authority for declaring completion.

This is the **execution layer**, not a general policy engine. [Research Policy Engine](https://github.com/fbientrigo/research-policy-engine) is a separate, higher-level project that may later select tasks, agents, resource budgets and stopping strategies. AutoLoop must work without it.

## Status

Implemented: supervisor, worktrees, fake and live (`agy`, Codex) providers, artifact store, structured critic gate, `resume`, `reconcile`, `status` with heartbeat, protected evaluator files. Real-run evidence is listed in `docs/validation.md`; anything not listed there has **not** been validated. A passing fake-provider test does not establish live operation. There is no daemon and no automatic push or merge.

## Install and configure

Linux/WSL and Windows are supported. `gauntlet doctor` decides, per machine, whether the Codex critic is provably read-only; if not, live runs refuse to start.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
gauntlet init
agy models
codex debug models
```

Edit `gauntlet.toml` before a live run. Set `worker.model` to a slug actually listed by `agy models`; **add `critic.model` explicitly** under `[critic]` to a model/effort combination returned by `codex debug models`. The `init` template intentionally does **not** guess the Codex model. Configure deterministic verification commands as argv arrays for the target repository (for example, Python tests).

```toml
[worker]
provider = "agy"
model = "REPLACE_WITH_AGY_MODEL"
effort = "high"

[critic]
provider = "codex"
model = "REPLACE_WITH_CODEX_MODEL"
effort = "high"
```

Run `gauntlet doctor`. It must report `safe: true` and `codex_read_only: "PROVEN"`. If it fails, do not bypass the guard.

## One-command execution (opt in)

Create a task with explicit acceptance criteria:

```md
# Objective
Implement a small, testable feature.

# Acceptance Criteria
- The relevant command behaves as documented.
- Existing regression tests pass.

# Constraints
- Do not push or merge changes automatically.
```

From the **target repository** (not the AutoLoop source checkout unless AutoLoop itself is the target):

```sh
gauntlet run --task TASK.md --execute
```

`--execute` prepares a frozen task/config snapshot and worktree, then invokes the existing bounded `resume` supervisor in the same foreground process. The default `gauntlet run --task TASK.md` remains prepare-only for compatibility. Inspect a prepared run using `gauntlet status` and start it with `gauntlet resume RUN_ID`.

The supervisor runs baseline checks, Gemini worker turns, deterministic checks, independent Codex reviews and final checks. Reaching a worker exit code 0 is **not** completion. Failed checks, unsafe sandbox diagnosis, exceeded limits and malformed reviewer output cannot produce `DONE`. Artifacts are stored under `.gauntlet/runs/RUN_ID`; the detached worktree is a sibling of the target repository. No push, merge or automatic cleanup occurs.

### Evaluator you can trust

The loop is: your natural-language constraints (`TASK.md`) plus a fixed evaluator (`[[verification.commands]]`) that runs after every worker turn. To keep the evaluator fixed:

1. Write the evaluator first (for example `tests/eval_*.py`). A model may draft it from `TASK.md`, but a human reviews and commits it **before** `gauntlet run`.
2. List it in `gauntlet.toml` so the worker cannot change it:

```toml
[verification]
protected = ["tests/eval_*.py"]
```

If a worker edits, adds or deletes a protected file, the run ends `BLOCKED` with `EVALUATOR_TAMPERED`.

### Unattended runs and recovery

`--execute` runs in the foreground. To survive a disconnect, use tmux (details in [docs/unattended.md](docs/unattended.md)):

```sh
gauntlet run --task TASK.md                 # prints RUN_ID, prepares only
tmux new -d -s gauntlet-RUN_ID 'gauntlet resume RUN_ID >> .gauntlet/runs/RUN_ID/session.log 2>&1'
gauntlet status RUN_ID                      # state, plus supervisor pid and heartbeat age
```

If the process dies during a worker turn, the next `resume` ends `ESCALATED` (`WORKER_EXECUTION_UNKNOWN`). **The worker is never launched again automatically**, because it may have changed files. Inspect the worktree, then decide:

```sh
gauntlet reconcile RUN_ID --accept  --note "diff reviewed, keep it"   # resume still runs checks + critic
gauntlet reconcile RUN_ID --abandon --note "discard"                  # run ends BLOCKED; start a new run
```

Provider quota or login failures end the run `BLOCKED` with reason `provider_quota` or `provider_auth`. Fix the cause and start a new run.

## Tests

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use deterministic fake agents and subprocess fixtures. Live system integration is a separate acceptance gate: start a disposable Git repository on Debian, prove `doctor`, run one real bounded task, verify actual edits/checks/verdict/evidence and inspect the resulting worktree. Keep that pilot reviewable and never enable automatic push/merge.

## Planned boundary with Research Policy Engine

A future integration may submit a frozen task contract plus a bounded resource allocation and receive a machine-readable run ID, terminal state and evidence references. AutoLoop should **not** implement policy learning, quota optimization, task selection, multi-agent routing or scientific decision logic.
