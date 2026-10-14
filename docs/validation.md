# Validation record

Only runs that actually happened are listed.

## Automated (fake agents)

`python -m unittest discover -s tests` and `pytest`: 159+ tests. They cover kill points, reconcile accept and abandon, protected evaluator files, provider failure reasons, heartbeat status.

## Real processes, fake agents (2026-10-14, Windows)

A supervisor was started with a hanging worker and killed with `kill()` while the worker was running. Then:
`resume` -> `ESCALATED` `WORKER_EXECUTION_UNKNOWN`; a second `resume` was refused; `reconcile --accept --note ...` -> `RUNNING`; `resume` -> `DONE`. The worker was launched once. `status` showed the supervisor pid and heartbeat age while it ran.

## Live Gemini (agy) + Codex critic: NOT validated in this branch

- An earlier live Windows run reached `DONE` (run `20261004T220151Z-8d514f05`, worker `gemini-3.8-flash-medium`, critic `gpt-5.6-luna`). It ran on a previous local build, not on this branch's final code.
- On 2026-10-14 `gauntlet doctor` on this Windows host reports `codex_read_only: UNPROVEN`: `codex sandbox` fails with `windows sandbox failed: helper_unknown_error: setup refresh had errors` (Codex sandbox log: access denied on `...\OpenAI\Codex\runtimes\cua_node\...\node_repl.exe`). Doctor correctly refuses live runs. Not changed by AutoLoop.
- WSL Ubuntu 24.04: native `codex` 0.162.1 installed in `~/.local`; `agy models` works, `tmux` is present. `codex exec` fails: `refresh token was revoked ... sign in again` (401). Needs `codex login` inside WSL by the account owner.
- So no live Linux/tmux run and no live detach/reattach run have been done.
