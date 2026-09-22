# Gauntlet v1 threat model

## Assets and trust boundary

Protected assets are the repository contents and Git history, the truth of the
terminal status, local CLI credentials, and an auditable account of what each
process saw and did.  The supervisor is the trusted decision component.  The
worker and critic are untrusted probabilistic subprocesses, even when their
output is structurally valid.  Repository files, task text, tool output, and
provider prose are untrusted input to those processes.

The worker may modify only its dedicated local run worktree, never the user's
primary checkout.  The critic must have no filesystem write permission and a
fresh context.  Neither subprocess may invoke remote Git mutation through
supervisor-provided arguments.  Failed worktrees are retained as forensic
evidence until a human explicitly cleans them up.

## Premature completion

| Threat | Control | Fail-closed behavior |
| --- | --- | --- |
| Worker says done, exits zero, or emits a success JSON field | Worker adapter has no completion result type | Record `WORKER_FINISHED`; proceed to checks |
| Critic is persuaded by worker optimism | Critic prompt excludes worker report, prior praise, attempt number, and completion claims | Only schema-valid independent evidence can PASS |
| Critic returns malformed prose or contradictory PASS | JSON Schema plus supervisor semantic validation | `BLOCKED: invalid_critic_verdict` |
| Checks pass before an unfinished worker | Checks run after every worker turn and before critic | Critic still required; no direct DONE path |
| A task has no testable acceptance condition | Require frozen acceptance criteria; allow critic `human_decision_required` | `ESCALATED`, never inferred PASS |
| Limit reached | Wall clock, max turns, and repeated-blocker caps | `BLOCKED` with final reason and artifacts |

`DONE` has exactly one predecessor: a supervisor transition after final checks
pass and a valid critic `PASS` contains zero findings.  There is no CLI flag,
worker output field, exit code, hook, or resume path that can bypass it.

## Critic drift and collusion

Critic drift means a reviewer gradually accepts weaker evidence, invents
optional work as blockers, or is primed by earlier rounds.  Countermeasures:

- start a fresh, ephemeral Codex process for every review;
- use the fixed PASS/BLOCK schema and reject ungrounded claims;
- define blockers narrowly: violated criterion, correctness/regression,
  meaningful missing defect-hiding test, relevant security/safety issue,
  required UX failure, or material unnecessary complexity;
- reject style preferences, optional refactors, speculative architecture, and
  unrequested features as blocks;
- fingerprint normalized finding IDs/locations/required conditions.  Repeated
  fingerprints reach `same_blocker_limit` and escalate to a human rather than
  creating an endless rhetorical loop;
- retain the exact critic prompt, provider command, output, input digest, and
  repository/check evidence for later audit.

The supervisor never feeds worker rebuttal prose into the next critic.  It
feeds the changed repository and current check evidence only.  This sacrifices
some conversational efficiency to preserve independence.

## Process and filesystem risks

- **Sandbox failure or provider regression:** `doctor` runs a non-destructive
  controlled write-attempt in a disposable directory and compares manifests.
  A failure means no live critic.  The supervisor also snapshots the actual
  repository before and after every critic; any change is `ESCALATED`.
- **Artifact tampering:** the outer process, not either agent, writes all run
  records through atomic replace.  Events include SHA-256 digests.  Artifacts
  are evidence, not privileged instructions.
- **Prompt injection in repository/task output:** prompts explicitly classify
  repository text as data, constrain tools, and use a minimal context bundle.
  This reduces but cannot eliminate model manipulation; suspicious instructions
  or a human-decision flag escalate rather than silently pass.
- **Unsafe worker permissions:** do not pass Agy's
  `--dangerously-skip-permissions`; record its sandbox/permission diagnosis.
  The worker has write authority only because it is the task executor, not
  because it can decide completion.
- **Remote mutation:** supervisor commands and worker/critic contracts prohibit
  push, merge, force-reset, history rewrite, credential changes, and remote
  configuration changes.  This is contractual prevention, not a claim that a
  writable coding agent is a hostile-code sandbox.
- **Shell injection:** adapters pass an executable plus argv to `subprocess`
  with `shell=False`; verification commands are validated argv arrays at the
  TOML boundary and remain arrays until execution.  There is no command-string
  parser or interpolation feature.
- **Vague task persuasion:** freeze an explicit objective and non-empty,
  testable acceptance criteria before creating a run worktree.  A short
  objective without criteria is refused or escalated; it can never become a
  completion contract by implication.

## Residual risk

A local authenticated CLI can still have provider-specific tool authority, and
repository content can influence an LLM.  Gauntlet is a completion authority
and audit boundary, not a security sandbox for arbitrary malicious code.  Run
it on a reviewable branch/worktree and retain human control over merge and
push.
