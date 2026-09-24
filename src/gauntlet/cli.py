from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from .config import ConfigError, init_config, load_config
from .doctor import critic_read_only_status, doctor
from .process import SubprocessRunner
from .providers import get_adapters
from .store import RunStore, StoreError
from .supervisor import STATUS_DONE, Supervisor, TERMINAL_STATUSES
from .task import TaskContractError, load_task
from .worktree import WorktreeError, create_worktree, primary_root, worktree_path


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_config(Path("gauntlet.toml"))
            print("created gauntlet.toml")
        elif args.command == "doctor":
            report = doctor(load_config(Path(args.config)), SubprocessRunner())
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
                agy_cmd = (shutil.which("agy") or "agy",)
                codex_cmd = (shutil.which("codex") or "codex",)
                report = doctor(frozen_config, runner, agy_cmd, codex_cmd)
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
            store.event("RUN_RESUMED", status="RUNNING", detail={"previous_status": current_status})
            supervisor = Supervisor.from_store(store, worktree, runner, worker, critic)
            final_status = supervisor.run()
            print(json.dumps(final_status, indent=2, sort_keys=True))
            return 0 if final_status.get("status") == STATUS_DONE else 1
        else:
            if bool(args.objective) == bool(args.task):
                raise ValueError("provide exactly one of a short objective or --task TASK.md")
            if args.objective:
                raise TaskContractError("short objectives require explicit acceptance criteria; use --task TASK.md")
            _prepare(args.task)
    except (ConfigError, FileExistsError, TaskContractError, WorktreeError, ValueError, NotImplementedError) as exc:
        print(f"gauntlet: {exc}", file=sys.stderr)
        return 2
    return 0


def _prepare(task_path: Path) -> None:
    root = primary_root(Path.cwd(), SubprocessRunner())
    config_path = root / "gauntlet.toml"
    config_text = config_path.read_text(encoding="utf-8")
    load_config(config_path)
    contract, task_text = load_task(task_path)
    runs_dir = root / ".gauntlet" / "runs"
    if runs_dir.is_dir():
        for run_path in sorted(runs_dir.iterdir()):
            if run_path.is_dir() and (run_path / "events.jsonl").exists():
                try:
                    existing_store = RunStore(root, run_path.name)
                    st = existing_store.status().get("status")
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
    print(json.dumps(store.status(), indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
