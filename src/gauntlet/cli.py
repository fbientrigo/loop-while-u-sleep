from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from contextlib import ExitStack

from .config import ConfigError, init_config, load_config
from .fsutil import LockHeld, exclusive_lock
from .doctor import DOCTOR_OUTPUT_CHARS, critic_read_only_status, doctor
from .process import SubprocessRunner
from .providers import get_adapters, resolve_executable
from .store import RunStore, StoreError
from .supervisor import STATUS_DONE, Supervisor, TERMINAL_STATUSES, crash_charge_seconds
from .task import TaskContractError, load_task
from .worktree import WorktreeError, create_worktree, primary_root, worktree_path


def main(argv: list[str] | None = None) -> int:
    with ExitStack() as stack:
        return _main(argv, stack)


def _main(argv: list[str] | None, stack: ExitStack) -> int:
    parser = argparse.ArgumentParser(prog="gauntlet", description="Auditable external supervisor foundation")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="write gauntlet.toml without overwriting")
    doctor_parser = commands.add_parser("doctor", help="inspect local prerequisites")
    doctor_parser.add_argument("--config", default="gauntlet.toml")
    status_parser = commands.add_parser("status", help="derive status from run events")
    status_parser.add_argument("run_id", nargs="?")
    resume_parser = commands.add_parser("resume", help="continue an incomplete run using frozen contract")
    resume_parser.add_argument("run_id")
    run_parser = commands.add_parser("run", help="freeze a contract and create an isolated worktree; no worker is launched")
    run_parser.add_argument("objective", nargs="?")
    run_parser.add_argument("--task", type=Path)
    run_parser.add_argument("--execute", action="store_true", help="launch the bounded supervisor after preparing the run")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_config(Path("gauntlet.toml"))
            print("created gauntlet.toml")
        elif args.command == "doctor":
            report = doctor(load_config(Path(args.config)), SubprocessRunner(max_output_chars=DOCTOR_OUTPUT_CHARS))
            print(json.dumps(report.__dict__, indent=2, sort_keys=True))
            return 0 if report.safe else 1
        elif args.command == "status":
            root = primary_root(Path.cwd(), SubprocessRunner())
            if args.run_id:
                print(json.dumps(RunStore(root, args.run_id).status(), indent=2, sort_keys=True))
            else:
                runs = root / ".gauntlet" / "runs"
                statuses = [RunStore(root, path.name).status() for path in sorted(runs.iterdir())] if runs.is_dir() else []
                print(json.dumps(statuses, indent=2, sort_keys=True))
        elif args.command == "resume":
            root = primary_root(Path.cwd(), SubprocessRunner())
            store = RunStore(root, args.run_id)
            if not store.root.exists():
                raise ValueError(f"run not found: {args.run_id}")
            # One supervisor per run: a second resume would double-launch the worker and lose events.
            stack.enter_context(exclusive_lock(store.root / "run.lock", f"run {args.run_id}"))
            current_status = store.status().get("status")
            if current_status in TERMINAL_STATUSES:
                raise ValueError(f"cannot resume run {args.run_id} in terminal status: {current_status}")
            worktree = worktree_path(root, args.run_id)
            if not worktree.is_dir():
                raise WorktreeError(f"worktree not found for run {args.run_id}: {worktree}")
            config_path = store.root / "config.toml"
            task_path = store.root / "task-contract.json"
            if not config_path.exists() or not task_path.exists():
                raise ValueError(f"run {args.run_id} is missing frozen configuration or task contract")
            frozen_config = load_config(config_path)
            runner = SubprocessRunner()
            is_fake = frozen_config.worker_model == "fake" or frozen_config.worker_model.startswith("fake")
            if is_fake:
                worker, critic = get_adapters(frozen_config)
            else:
                agy_cmd = (resolve_executable("agy"),)
                codex_cmd = (resolve_executable("codex"),)
                report = doctor(frozen_config, SubprocessRunner(max_output_chars=DOCTOR_OUTPUT_CHARS), agy_cmd, codex_cmd)
                if not report.safe:
                    raise ValueError(
                        f"doctor reports live providers are not safe/ready to resume run {args.run_id}: "
                        f"codex_read_only={report.codex_read_only!r} "
                        f"worker_model_detected={report.worker_model_detected} "
                        f"critic_model_detected={report.critic_model_detected}"
                    )
                worker, critic = get_adapters(
                    frozen_config,
                    store=store,
                    runner=runner,
                    critic_read_only_proven=critic_read_only_status(report),
                    agy_command=agy_cmd,
                    codex_command=codex_cmd,
                )
            store.event(
                "RUN_RESUMED",
                status="RUNNING",
                detail={"previous_status": current_status, "charged_seconds": crash_charge_seconds(store)},
            )
            supervisor = Supervisor.from_store(store, worktree, runner, worker, critic)
            final_status = supervisor.run()
            print(json.dumps(final_status, indent=2, sort_keys=True))
            return 0 if final_status.get("status") == STATUS_DONE else 1
        else:
            if bool(args.objective) == bool(args.task):
                raise ValueError("provide exactly one of a short objective or --task TASK.md")
            if args.objective:
                raise TaskContractError("short objectives require explicit acceptance criteria; use --task TASK.md")
            store = _prepare(args.task, execute=args.execute)
            if args.execute:
                return main(["resume", store.run_id])
    except (ConfigError, FileExistsError, TaskContractError, WorktreeError, ValueError, NotImplementedError, LockHeld) as exc:
        print(f"gauntlet: {exc}", file=sys.stderr)
        return 2
    return 0


def _prepare(task_path: Path, *, execute: bool = False) -> RunStore:
    root = primary_root(Path.cwd(), SubprocessRunner())
    config_path = root / "gauntlet.toml"
    config_text = config_path.read_text(encoding="utf-8")
    config = load_config(config_path)
    if execute and not config.worker_model.startswith("fake") and not config.critic_model:
        raise ConfigError("live --execute requires [critic] model in gauntlet.toml; choose a slug from codex debug models")
    contract, task_text = load_task(task_path)
    runs_dir = root / ".gauntlet" / "runs"
    # The check for an active run and the creation of the next one must be a single step.
    with exclusive_lock(root / ".gauntlet" / "prepare.lock", "run preparation"):
        if runs_dir.is_dir():
            for run_path in sorted(runs_dir.iterdir()):
                if run_path.is_dir() and (run_path / "events.jsonl").exists():
                    try:
                        existing_store = RunStore(root, run_path.name)
                        st = existing_store.status().get("status")
                        if st == "CREATED":
                            # Only a crash between the CREATED event and the worktree can leave this behind,
                            # because preparation holds the lock the whole time.
                            existing_store.event(
                                "SETUP_ABANDONED", status="BLOCKED", detail={"reason": "preparation was interrupted"}
                            )
                            continue
                        if st not in TERMINAL_STATUSES:
                            raise ValueError(f"an active run already exists for this repository: {run_path.name} (status: {st})")
                    except StoreError:
                        continue
        store = RunStore.create(root)
        store.freeze_text("config.toml", config_text)
        store.freeze_text("task.md", task_text)
        store.freeze_json("task-contract.json", contract.as_dict())
        store.event("CREATED", status="CREATED")
        try:
            worktree = create_worktree(root, store.run_id, SubprocessRunner())
        except Exception as exc:
            store.event("SETUP_FAILED", status="BLOCKED", detail={"error": str(exc)})
            raise
        store.event("WORKTREE_CREATED", status="READY", detail={"path": str(worktree)})
    if not execute:
        print(json.dumps(store.status(), indent=2, sort_keys=True))
        print(f"next: gauntlet resume {store.run_id}   # runs the worker and critic; evidence in .gauntlet/runs/{store.run_id}/summary.json", file=sys.stderr)
    return store


if __name__ == "__main__":
    raise SystemExit(main())
