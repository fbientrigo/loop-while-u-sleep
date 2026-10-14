# Running unattended

AutoLoop has no daemon. Use tmux or `systemd --user` to keep `gauntlet resume` alive after you disconnect.

## tmux (Linux/WSL)

```sh
gauntlet doctor                              # must say safe: true
gauntlet run --task TASK.md                  # prepare; note RUN_ID
tmux new -d -s gauntlet-RUN_ID 'gauntlet resume RUN_ID >> .gauntlet/runs/RUN_ID/session.log 2>&1'
tmux attach -t gauntlet-RUN_ID               # reconnect (detach: Ctrl-b d)
tail -f .gauntlet/runs/RUN_ID/session.log
gauntlet status RUN_ID                       # supervisor.pid_alive and heartbeat_age_seconds
cat .gauntlet/runs/RUN_ID/summary.json       # final state and evidence
```

## systemd --user (alternative)

```sh
loginctl enable-linger "$USER"               # keep it alive after logout
systemd-run --user --unit gauntlet-RUN_ID --working-directory "$PWD" \
  gauntlet resume RUN_ID
journalctl --user -u gauntlet-RUN_ID -f
```

## Windows

```powershell
Start-Process -WindowStyle Hidden gauntlet -ArgumentList 'resume','RUN_ID' `
  -RedirectStandardOutput .gauntlet\runs\RUN_ID\session.log -RedirectStandardError .gauntlet\runs\RUN_ID\session.err
```

## What happens when...

| Event | Result |
|---|---|
| SSH disconnect | tmux/systemd keeps running. |
| Laptop suspend | Heartbeat stalls. On wake the run continues; time asleep is not charged. |
| Reboot or kill during a worker turn | `resume` ends `ESCALATED` `WORKER_EXECUTION_UNKNOWN`. Inspect the worktree, then `gauntlet reconcile`. |
| Reboot or kill during checks or critic | `resume` continues from the last durable step. |
| Provider quota or login expiry | `BLOCKED`, reason `provider_quota` / `provider_auth`. Fix, then start a new run. |
| Wall time or turn limit | `BLOCKED` `LIMIT_REACHED`. |

Before `reconcile --accept`, confirm no agent process from the dead run is still alive (`pgrep -af agy`); a hard kill can leave one editing the worktree.

`gauntlet resume RUN_ID` is also the reconnect command after a crash. It refuses to start if another supervisor holds `run.lock`.
