# Gauntlet v1 architecture

## Decision

Build a small Python core, informed by `kamtS/gauntlet-loop`, rather than fork
it.  That project already demonstrates several sound ideas: direct
`subprocess` argument vectors (no shell interpolation), bounded passes,
per-run artifacts, and before/after repository checks for read-only phases.
Its client-neutral planner/integrator phases and JSON configuration are outside
the v1 need.  More importantly, its critic prompt includes the worker report
and attempt number; Gauntlet must not do either.

Gauntlet owns the sole `DONE` transition.  A successful worker exit is only a
`WORKER_FINISHED` event.  A critic `PASS` is necessary, but the supervisor
creates `DONE` only after it validates a schema-conformant empty finding list
and the final deterministic checks pass.

## Scope and boundaries

The supervisor is a local foreground Python CLI.  It is invoked from the
repository that the user selected, but every run creates one dedicated local
Git worktree outside that primary checkout.  The worker can write only in that
run worktree; the primary checkout remains the user's inspection surface.  The
worktree has an ordinary Git diff, is never pushed, merged, or automatically
deleted on failure, and is cleaned up only by an explicit future cleanup
command.  It uses ordinary `git` argument vectors, not a general Git layer.
Gauntlet uses locally authenticated provider CLIs and has no
service, database, Docker daemon, API key handling, remote Git action, or
background authority.

The v1 route is fixed by config to one writable worker (`agy`) and one fresh,
read-only critic (`codex`).  Provider adapters construct argument arrays and
parse their own output; the state machine never parses prose to decide a
terminal state.

## Verified provider contract (2026-09-17)

The installed `agy` is 1.2.5.  Its `--help` and `agy models` confirm:

- `agy -p` is headless; JSON and NDJSON are available through
  `--output-format json|stream-json`.
- `--json-schema` enforces structured final output, and the requested worker
  slug is exactly `gemini-3.8-flash-high`.
- `--conversation ID` and `--continue` continue an Agy conversation.  v1 uses
  an individual worker conversation only across that run's worker revisions;
  it never reuses a conversation across runs.
- `--effort high`, `--mode accept-edits`, `--sandbox`, and an explicit
  `--dangerously-skip-permissions` flag exist.  Gauntlet never supplies the
  dangerous bypass flag.  The worker permission policy must be diagnosed and
  recorded before a live run.
- `agy plugin` supports install, enable, disable, validate, and list.  Agy
  documentation says a plugin can bundle skills, agents, rules, MCP, and
  hooks.

The installed Codex CLI is 0.154.0.  `codex exec` supports `-m`,
`-s read-only`, `-a never`, `--ephemeral`, `--ignore-user-config`,
`--ignore-rules`, `--output-schema FILE`, and JSONL `--json`.  The local
configuration selects `gpt-5.6-terra`; that is a local configured slug, not a
portable hard-coded default.  `codex debug models` confirms that this model
accepts high reasoning; the adapter supplies it as
`-c model_reasoning_effort="high"`.  `codex review` does not expose a sandbox flag,
so it is not the v1 critic launch surface.

The intended critic command is conceptually:

```text
codex exec -C <repo> -m <detected-model> -c model_reasoning_effort="high" \
  -s read-only -a never --ephemeral \
  --ignore-user-config --ignore-rules --output-schema <verdict-schema.json> --json -
```

The supervisor writes artifacts itself from captured stdout; it does not give
the critic a writable artifact directory.  A before/after content manifest of
the repository is a second guard, but it is detection, not a substitute for a
working sandbox.  The current Windows `codex doctor` reports unrestricted
filesystem/sandbox-provisioning failure, so v1 must refuse live critic runs on
that machine.  `gauntlet doctor` on Debian will run a disposable write-attempt
smoke test and require both CLI enforcement and an unchanged manifest before
it reports the critic safe.

Sources: [Antigravity headless mode](https://www.agy.dev/docs/cli/headless/),
[Antigravity features](https://www.agy.dev/docs/cli/features),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
and the installed CLI help described above.

## Minimal layout

```text
pyproject.toml
src/gauntlet/
  __init__.py
  cli.py              # argparse/Typer-free command boundary
  config.py            # TOML load, validate, snapshot
  supervisor.py        # pure transition coordinator
  providers.py         # AgyAdapter and CodexAdapter only
  verification.py      # argv-only configured commands, captured result records
  store.py             # atomic run artifacts and status projection
  worktree.py          # one git worktree per run
  doctor.py            # local CLI discovery and read-only smoke test
  task.py              # explicit frozen task contract
  schemas/critic.json
tests/
docs/
  architecture.md
  threat-model.md
```

Use the standard library only in v1 (`argparse`, `tomllib`, `subprocess`,
`json`, `pathlib`, `hashlib`).  A console entry point is installed by `pipx` or
`uv tool install`; no system `pip` path is supported.

## CLI and configuration contract

```text
gauntlet init                 # write a conservative gauntlet.toml, never overwrite
gauntlet doctor               # detect CLIs/models and prove critic read-only guard
gauntlet run "task text"      # rejects until explicit criteria are supplied
gauntlet run --task TASK.md   # freeze file contents at run creation
gauntlet status [run-id]      # read status.json only
gauntlet resume <run-id>      # continue an incomplete, same-repo run
gauntlet setup                # install/validate optional Agy global skill
```

`run` rejects both task sources together, a non-Git directory, a task lacking
explicit acceptance criteria, an unresolved worker/critic adapter, missing
command, unsafe critic diagnosis, malformed limits, or an existing active run
for the same repository.  A short objective is therefore only a convenience
for a future explicit-contract flow; it cannot begin work or imply criteria.
`resume` reads the frozen config and frozen task; it does not reread mutable
configuration.

```toml
[worker]
provider = "agy"
model = "gemini-3.8-flash-high" # doctor validates this with `agy models`
effort = "high"

[critic]
provider = "codex"
# model omitted: doctor detects/configures it; snapshot stores resolved slug
effort = "high"

[[verification.commands]]
name = "tests"
argv = ["python", "-m", "pytest", "-q"]

[[verification.commands]]
name = "lint"
argv = ["ruff", "check", "."]

[limits]
wall_time = "4h"
max_worker_turns = 20
same_blocker_limit = 3
```

Commands are explicit argv arrays in TOML and in the internal model.  Gauntlet
has no shell parser, interpolation, or `shell=True` path.  All resolved values,
executable paths, CLI versions, working-tree baseline, and command results are
frozen under `.gauntlet/runs/<run-id>/`; the run worktree lives beside, never
inside, the primary checkout.

## Task contract

Every run freezes a JSON task contract and the source task text before the
worktree is created.  It contains an `objective`, non-empty
`acceptance_criteria`, `constraints`, and optional verification expectations.
For v1, `gauntlet run --task TASK.md` accepts this minimal Markdown shape:

```markdown
# Objective
Implement the described behavior.

# Acceptance Criteria
- A concrete, testable outcome.

# Constraints
- Do not change public API X.

# Verification
- `python -m pytest -q`
```

Only Objective and Acceptance Criteria are required; each criterion must be a
non-empty bullet.  There is deliberately no planner that turns vague prose
into criteria.  A malformed or vague short-objective invocation escalates
before a worker can run.

## State machine and evidence isolation

```text
CREATED -> BASELINE_CHECKS -> WORKER -> POST_WORKER_CHECKS -> CRITIC
  ^             |                 |              |               |
  |             v                 v              v               v
  |          BLOCKED        BLOCKED/ESCALATED  WORKER       DONE or WORKER
  |                                                        (only supervisor)
  +----------------------- resume -------------------------------+
```

The critic receives a fresh process and only: frozen task, explicit acceptance
criteria, a bounded repository/diff snapshot, and deterministic-check records.
It receives neither worker prose, prior critic output, an attempt count, nor a
completion claim.  On `BLOCK`, the supervisor gives the worker the structured
findings and applicable check evidence.  The worker must verify each finding,
fix valid ones, rebut invalid ones with evidence, and run relevant checks; it
cannot emit any terminal state.

The final critic response must conform to this schema and must have an empty
array for PASS:

```json
{"verdict":"PASS | BLOCK","blocking_findings":[{"id":"string","severity":"critical | major","claim":"string","evidence":"string","location":"string","required_condition":"string"}]}
```

Malformed output, a PASS with findings, a BLOCK with no finding, provider
failure, timeout, changed critic manifest, repeated blocker fingerprint, or a
human-decision marker produces `BLOCKED` or `ESCALATED`, never `DONE`.

Each run contains immutable-ish numbered files: `task.md`, `config.toml`,
`baseline.json`, `events.jsonl`, worker prompts/results, check stdout/stderr
and exit status, critic prompt/verdict, a file manifest before/after every
read-only phase, and final `status.json`.  Writes use temp-file then atomic
rename.  Status is a projection of the event log, so a claimed result cannot
replace its evidence.

## Antigravity integration

`gauntlet setup` installs a small global Agy plugin/skill.  The skill maps
`/gauntlet <task>` to the installed external `gauntlet run "<task>"` process,
then reports its run ID/status.  It must say that the outer interactive Agy
session has no completion authority.  The supervisor launches a separate
headless Agy process for the worker.  No Stop hook and no MCP server are in v1:
they add lifecycle ambiguity without improving the simple subprocess boundary.

## Deliberately excluded from v1

No planner or final integrator role, multiple concurrent writers, MCP server,
daemon, database, web service, Docker, remote execution, Git mutation,
automatic credential/permission installation, or arbitrary provider routing.
Those features either expand authority or obscure the only authority that
matters here: the local supervisor's state transition to `DONE`.

## Phased implementation

1. Package and contracts: TOML validation, run store, schemas, `init`, and
   `status`, with tests for frozen input and atomic records.
2. `doctor`: CLI/version/model discovery plus the Debian read-only smoke test;
   do not permit `run` until it passes.
3. Agy worker adapter and verification runner, including bounded subprocess
   capture and worker continuation within one run.
4. Fresh Codex critic adapter, manifest guard, verdict validation, and the
   bounded supervisor loop.
5. `resume` and the optional Agy global skill; test it invokes the external
   executable rather than recursively orchestrating in-session.
