from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .config import Config, parse_config
from .process import ProcessRunner
from .providers import CriticAdapter, CriticResult, WorkerAdapter, WorkerResult
from .store import RunStore
from .task import TaskContract
from .verdict import VerdictError, validate_verdict
from .verification import VerificationResult, run_commands


STATUS_DONE = "DONE"
TERMINAL_STATUSES = frozenset({STATUS_DONE, "BLOCKED", "ESCALATED"})


class FoundationSupervisor:
    """Phase 1 evidence recorder; it deliberately cannot declare DONE."""

    def __init__(self, store: RunStore):
        self.store = store

    def worker_finished(self, output: str) -> None:
        self.store.freeze_text("worker-output.txt", output)
        self.store.event("WORKER_FINISHED", status="WORKER_FINISHED")

    def critic_recorded(self, raw_verdict: str) -> None:
        verdict = validate_verdict(raw_verdict)
        self.store.freeze_json("critic-verdict.json", verdict)
        self.store.event("CRITIC_RECORDED", status="CRITIC_RECORDED", detail={"verdict": verdict["verdict"]})


HEARTBEAT_SECONDS = 15.0
HEARTBEAT_FILE = "alive.json"


class Heartbeat:
    """Writes alive.json while a run executes, so work lost to a crash can be charged on resume.

    A clean exit removes the file; only a crash leaves it behind.
    """

    def __init__(self, store: RunStore, interval: float = HEARTBEAT_SECONDS):
        self.store = store
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="gauntlet-heartbeat", daemon=True)

    def _beat(self) -> None:
        try:
            self.store.write_json(HEARTBEAT_FILE, {"at": datetime.now().astimezone().isoformat(), "pid": os.getpid()})
        except OSError:
            pass  # a missed beat only makes the crash charge smaller

    def _loop(self) -> None:
        self._beat()
        while not self._stop.wait(self.interval):
            self._beat()

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            (self.store.root / HEARTBEAT_FILE).unlink()
        except OSError:
            pass


def crash_charge_seconds(store: RunStore) -> float:
    """Seconds of work a crashed session did after its last event, from its last heartbeat.

    Zero when there is no heartbeat file (the previous session ended cleanly, or predates heartbeats).
    The error is at most one heartbeat interval.
    """
    try:
        alive = json.loads((store.root / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        alive_at = datetime.fromisoformat(alive["at"]).timestamp()
        events = store.events()
        last_at = datetime.fromisoformat(events[-1]["at"]).timestamp()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return 0.0
    return round(max(0.0, min(alive_at - last_at, time.time() - last_at)), 1)


def active_seconds(events: list[dict[str, Any]]) -> float:
    """Wall time the run was actually being worked on.

    The gap before a RUN_RESUMED event is idle time (the machine was off, or nobody had resumed yet)
    and must not use up the wall-time budget. Only the part a crashed session provably worked
    (detail.charged_seconds, taken from its heartbeat) is charged.
    """
    total = 0.0
    for previous, current in zip(events, events[1:]):
        if current.get("kind") == "RUN_RESUMED":
            try:
                total += max(0.0, float(current.get("detail", {}).get("charged_seconds", 0)))
            except (TypeError, ValueError):
                pass
            continue
        try:
            gap = datetime.fromisoformat(current["at"]).timestamp() - datetime.fromisoformat(previous["at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        total += max(0.0, gap)
    return total


def grant_time(adapter: Any, remaining: float) -> None:
    """Cap an adapter's per-call timeout at the wall time that is still left (never below one second)."""
    if not hasattr(adapter, "timeout_seconds"):
        return
    base = getattr(adapter, "_base_timeout_seconds", adapter.timeout_seconds)
    adapter._base_timeout_seconds = base
    allowance = max(1.0, remaining)
    adapter.timeout_seconds = allowance if not base else min(base, allowance)


def compute_fingerprint(finding: dict[str, Any]) -> str:
    norm_id = str(finding.get("id", "")).strip().lower()
    norm_loc = str(finding.get("location", "")).strip().lower()
    norm_req = str(finding.get("required_condition", "")).strip().lower()
    payload = f"{norm_id}:{norm_loc}:{norm_req}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


_PROVIDER_MARKERS = (
    ("provider_quota", ("429", "resource_exhausted", "usage limit", "quota")),
    ("provider_auth", ("unauthenticated", "not logged in", "please log in", "login required", "401")),
)


def classify_provider_failure(*texts: str) -> str | None:
    """Name a quota or login failure so the operator knows to wait or log in; never a reason to retry."""
    haystack = " ".join(texts).lower()
    for reason, markers in _PROVIDER_MARKERS:
        if any(marker in haystack for marker in markers):
            return reason
    return None


def reconcile_run(store: RunStore, worktree: Path, runner: Any, *, accept: bool, note: str) -> dict[str, Any]:
    """Operator decision for a worker turn whose outcome is unknown. Never launches anything.

    accept: keep the worktree as it is. The next resume still runs the checks and the critic.
    abandon: end the run as BLOCKED. A corrected task starts a new run.
    """
    if not note.strip():
        raise ValueError("reconcile requires a non-empty --note explaining the decision")
    last = store.events()[-1] if store.events() else {}
    if last.get("kind") != "WORKER_EXECUTION_UNKNOWN":
        raise ValueError(f"nothing to reconcile: last event is {last.get('kind')!r}, not WORKER_EXECUTION_UNKNOWN")
    turn = last["detail"]["turn"]
    if not accept:
        store.event("RUN_ABANDONED", status="BLOCKED", detail={"turn": turn, "note": note})
        return store.status()
    patch = runner.run(["git", "--no-optional-locks", "diff", "--binary", "HEAD"], cwd=worktree, timeout=60)
    store.freeze_json(
        f"reconcile-turn-{turn}.json",
        {"turn": turn, "decision": "accept", "note": note, "diff_sha256": hashlib.sha256(str(patch.stdout).encode()).hexdigest()},
    )
    output = f"worker-turn-{turn}-output.txt"
    if not (store.root / output).exists():
        store.freeze_text(output, "(worker output unavailable: turn reconciled by operator)")
    store.freeze_json(f"worker-turn-{turn}-result.json", {"returncode": 0, "timed_out": False, "reconciled": True})
    store.event("WORKER_RECONCILED", status="RUNNING", detail={"turn": turn, "note": note})
    return store.status()


def snapshot_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    if not root.is_dir():
        return manifest

    def _hash_file(p: Path) -> str:
        hasher = hashlib.sha256()
        try:
            with p.open("rb") as handle:
                while chunk := handle.read(65536):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            return ""

    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel_parts = path.relative_to(root).parts
            if len(rel_parts) > 1 and rel_parts[0] == ".git":
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            manifest[rel] = _hash_file(path)

    # Detect git metadata mutations in linked worktree or git directory
    git_target = root / ".git"
    if git_target.is_file():
        try:
            content = git_target.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir_str = content.split(":", 1)[1].strip()
                gitdir = Path(gitdir_str)
                if not gitdir.is_absolute():
                    gitdir = (root / gitdir).resolve()
                if gitdir.is_dir():
                    for meta_file in sorted(gitdir.rglob("*")):
                        if meta_file.is_file():
                            rel_meta = str(meta_file.relative_to(gitdir)).replace("\\", "/")
                            manifest[f".git-meta/{rel_meta}"] = _hash_file(meta_file)

                    # Also snapshot the primary repository's common git directory
                    commondir_file = gitdir / "commondir"
                    commondir = None
                    if commondir_file.is_file():
                        c_text = commondir_file.read_text(encoding="utf-8").strip()
                        c_path = Path(c_text)
                        commondir = (gitdir / c_path).resolve() if not c_path.is_absolute() else c_path.resolve()
                    elif gitdir.parent.name == "worktrees" and gitdir.parent.parent.is_dir():
                        commondir = gitdir.parent.parent.resolve()

                    if commondir and commondir.is_dir():
                        for meta_file in sorted(commondir.rglob("*")):
                            if meta_file.is_file():
                                parts = meta_file.relative_to(commondir).parts
                                if parts and parts[0] == "worktrees":
                                    continue
                                rel_common = str(meta_file.relative_to(commondir)).replace("\\", "/")
                                manifest[f".git-common/{rel_common}"] = _hash_file(meta_file)
        except OSError:
            pass
    elif git_target.is_dir():
        for meta_file in sorted(git_target.rglob("*")):
            if meta_file.is_file():
                rel_meta = str(meta_file.relative_to(git_target)).replace("\\", "/")
                manifest[f".git-meta/{rel_meta}"] = _hash_file(meta_file)

    return manifest


def manifest_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    modified = sorted(k for k in before if k in after and before[k] != after[k])
    return {"added": added, "removed": removed, "modified": modified}


class Supervisor:
    """Deterministic state machine coordinator.

    Transitions:
        CREATED
          -> BASELINE_CHECKS
          -> WORKER
          -> POST_WORKER_CHECKS
          -> CRITIC
          -> DONE | WORKER | BLOCKED | ESCALATED

    CONSTITUTIONAL INVARIANT: DONE can only be created by the supervisor's
    completion gate after:
    1. post-worker deterministic checks pass;
    2. a fresh critic returns schema-valid PASS;
    3. PASS contains zero blocking findings;
    4. final deterministic checks still pass.
    """

    def __init__(
        self,
        store: RunStore,
        config: Config,
        task: TaskContract,
        worktree: Path,
        runner: ProcessRunner,
        worker: WorkerAdapter,
        critic: CriticAdapter,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.config = config
        self.task = task
        self.worktree = worktree
        self.runner = runner
        self.worker = worker
        self.critic = critic
        self.clock = clock

    @classmethod
    def from_store(
        cls,
        store: RunStore,
        worktree: Path,
        runner: ProcessRunner,
        worker: WorkerAdapter,
        critic: CriticAdapter,
        clock: Callable[[], float] = time.time,
    ) -> "Supervisor":
        """Reconstruct supervisor using FROZEN task and FROZEN config from the store."""
        config_path = store.root / "config.toml"
        if not config_path.exists():
            raise ValueError(f"frozen config not found in run store: {config_path}")
        config = parse_config(config_path.read_text(encoding="utf-8"))

        task_path = store.root / "task-contract.json"
        if not task_path.exists():
            raise ValueError(f"frozen task contract not found in run store: {task_path}")
        task_data = json.loads(task_path.read_text(encoding="utf-8"))
        task = TaskContract(
            objective=task_data["objective"],
            acceptance_criteria=tuple(task_data["acceptance_criteria"]),
            constraints=tuple(task_data.get("constraints", ())),
            verification_expectations=tuple(task_data.get("verification_expectations", ())),
        )
        return cls(store, config, task, worktree, runner, worker, critic, clock=clock)

    FROZEN_ARTIFACTS = ("config.toml", "task.md", "task-contract.json")

    def _frozen_digests(self) -> dict[str, str]:
        digests = {}
        for name in self.FROZEN_ARTIFACTS:
            path = self.store.root / name
            if path.exists():
                digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digests

    def _check_frozen_artifacts(self) -> bool:
        """CONSTITUTIONAL PROPERTY: providers cannot mutate frozen task/config artifacts."""
        digests_file = self.store.root / "frozen-digests.json"
        current = self._frozen_digests()
        if not digests_file.exists():
            self.store.freeze_json("frozen-digests.json", current)
            return True
        recorded = json.loads(digests_file.read_text(encoding="utf-8"))
        if recorded != current:
            self.store.event(
                "FROZEN_ARTIFACT_MUTATED",
                status="ESCALATED",
                detail={"recorded": recorded, "current": current},
            )
            return False
        return True

    def run(self) -> dict[str, Any]:
        """Execute the supervisor state machine until a terminal state is reached."""
        try:
            with Heartbeat(self.store):
                return self._run()
        finally:
            try:
                self._write_summary()
            except Exception:  # the summary describes the run; it must never change its outcome
                pass

    def _run(self) -> dict[str, Any]:
        status = self.store.status()
        current_status = status.get("status")
        if current_status in {"DONE", "BLOCKED", "ESCALATED"}:
            return status

        if not self._check_frozen_artifacts():
            return self.store.status()

        events = self.store.events()
        event_kinds = [e["kind"] for e in events]

        recorded_elapsed = active_seconds(events)
        start_clock = self.clock()

        # Check wall-time deadline before baseline checks
        elapsed = recorded_elapsed + (self.clock() - start_clock)
        if elapsed >= self.config.limits.wall_time_seconds:
            self.store.event(
                "LIMIT_REACHED",
                status="BLOCKED",
                detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
            )
            return self.store.status()

        # 1. BASELINE CHECKS
        if "BASELINE_CHECKS_PASSED" not in event_kinds:
            remaining_baseline = max(0.001, self.config.limits.wall_time_seconds - elapsed)
            if not self._run_baseline_checks(timeout=remaining_baseline):
                return self.store.status()

            # Check wall-time deadline immediately after baseline checks
            elapsed = recorded_elapsed + (self.clock() - start_clock)
            if elapsed >= self.config.limits.wall_time_seconds:
                self.store.event(
                    "LIMIT_REACHED",
                    status="BLOCKED",
                    detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
                )
                return self.store.status()

        # Reconstruct loop state from events for resume support
        worker_turns = sum(1 for e in events if e["kind"] == "WORKER_FINISHED")
        blocker_counts: dict[str, int] = {}
        for e in events:
            if e["kind"] == "CRITIC_BLOCKED":
                for fp in e.get("detail", {}).get("fingerprints", []):
                    blocker_counts[fp] = blocker_counts.get(fp, 0) + 1

        pending_findings: tuple[dict[str, Any], ...] = ()
        pending_check_evidence: tuple[VerificationResult, ...] = ()

        # Scan backwards to locate the latest phase event and restore remediation context
        latest_phase_kind: str | None = None
        for e in reversed(events):
            kind = e.get("kind")
            if kind in {
                "BASELINE_CHECKS_PASSED",
                "WORKER_STARTED",
                "WORKER_FINISHED",
                "POST_WORKER_CHECKS_STARTED",
                "POST_WORKER_CHECKS_PASSED",
                "POST_WORKER_CHECKS_FAILED",
                "CRITIC_STARTED",
                "CRITIC_BLOCKED",
                "CRITIC_PASSED",
                "FINAL_CHECKS_STARTED",
                "FINAL_CHECKS_PASSED",
            }:
                if latest_phase_kind is None:
                    latest_phase_kind = kind
                if kind == "CRITIC_BLOCKED":
                    turn = e.get("detail", {}).get("turn", worker_turns)
                    verdict_file = self.store.root / f"critic-turn-{turn}-verdict.json"
                    if verdict_file.exists():
                        saved_verdict = json.loads(verdict_file.read_text(encoding="utf-8"))
                        pending_findings = tuple(saved_verdict.get("blocking_findings", []))
                    break
                elif kind == "POST_WORKER_CHECKS_FAILED":
                    turn = e.get("detail", {}).get("turn", worker_turns)
                    checks_file = self.store.root / f"verification-post-worker-turn-{turn}.json"
                    if checks_file.exists():
                        saved_checks = json.loads(checks_file.read_text(encoding="utf-8"))
                        pending_check_evidence = tuple(
                            VerificationResult(
                                name=c["name"],
                                argv=tuple(c["argv"]),
                                returncode=c["returncode"],
                                stdout=c["stdout"],
                                stderr=c["stderr"],
                            )
                            for c in saved_checks
                            if c["returncode"] != 0
                        )
                    break
                elif kind in {"WORKER_FINISHED", "POST_WORKER_CHECKS_PASSED", "CRITIC_PASSED", "FINAL_CHECKS_PASSED", "BASELINE_CHECKS_PASSED"}:
                    break

        # Handle mid-cycle resumption if interrupted after worker finished
        resume_stage = "WORKER"
        if latest_phase_kind in {"WORKER_FINISHED", "POST_WORKER_CHECKS_STARTED"}:
            resume_stage = "POST_WORKER_CHECKS"
        elif latest_phase_kind in {"POST_WORKER_CHECKS_PASSED", "CRITIC_STARTED"}:
            resume_stage = "CRITIC"
        elif latest_phase_kind in {"CRITIC_PASSED", "FINAL_CHECKS_STARTED", "FINAL_CHECKS_PASSED"}:
            resume_stage = "FINAL_CHECKS"

        # Main orchestration loop
        while True:
            # Check limits before worker turn
            elapsed = recorded_elapsed + (self.clock() - start_clock)
            if elapsed >= self.config.limits.wall_time_seconds:
                self.store.event(
                    "LIMIT_REACHED",
                    status="BLOCKED",
                    detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
                )
                return self.store.status()

            if resume_stage == "WORKER":
                if worker_turns >= self.config.limits.max_worker_turns:
                    self.store.event(
                        "LIMIT_REACHED",
                        status="BLOCKED",
                        detail={"reason": "max_worker_turns_exceeded", "turn": worker_turns, "limit": self.config.limits.max_worker_turns},
                    )
                    return self.store.status()

                # 2. WORKER TURN
                turn = worker_turns + 1
                worker_result_file = self.store.root / f"worker-turn-{turn}-result.json"
                output_file = self.store.root / f"worker-turn-{turn}-output.txt"
                if worker_result_file.exists():
                    result_data = json.loads(worker_result_file.read_text(encoding="utf-8"))
                    output_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
                    err_file = self.store.root / f"worker-turn-{turn}-stderr.txt"
                    err_text = err_file.read_text(encoding="utf-8") if err_file.exists() else ""
                    worker_result = WorkerResult(
                        returncode=result_data.get("returncode", 1),
                        output=output_text,
                        error=err_text,
                        timed_out=bool(result_data.get("timed_out", False)),
                    )
                elif output_file.exists() or any(
                    e["kind"] == "WORKER_STARTED" and e.get("detail", {}).get("turn") == turn for e in self.store.events()
                ):
                    # The former process may have changed the worktree or reached external tools.
                    # A missing durable result is UNKNOWN, not permission to launch it again.
                    # Only `gauntlet reconcile` (operator decision) can move this run forward.
                    self.store.event(
                        "WORKER_EXECUTION_UNKNOWN",
                        status="ESCALATED",
                        detail={"turn": turn, "reason": "started_without_durable_result"},
                    )
                    return self.store.status()
                else:
                    self._freeze_manifest_before_worker(turn)
                    self.store.event("WORKER_STARTED", status="RUNNING", detail={"turn": turn})
                    elapsed = recorded_elapsed + (self.clock() - start_clock)
                    grant_time(self.worker, self.config.limits.wall_time_seconds - elapsed)

                    worker_result = self.worker.run(
                        worktree=self.worktree,
                        task=self.task,
                        turn=turn,
                        findings=pending_findings,
                        check_evidence=pending_check_evidence,
                    )

                    # Freeze worker structured result and output artifacts
                    self.store.freeze_json(
                        f"worker-turn-{turn}-result.json",
                        {"returncode": worker_result.returncode, "timed_out": bool(worker_result.timed_out)},
                    )
                    self.store.freeze_text(f"worker-turn-{turn}-output.txt", worker_result.output)
                    if worker_result.error:
                        self.store.freeze_text(f"worker-turn-{turn}-stderr.txt", worker_result.error)

                    if not self._check_frozen_artifacts():
                        return self.store.status()

                self._record_worker_changes(turn)

                protected_file = self.store.root / "protected-manifest.json"
                if protected_file.exists():
                    before = json.loads(protected_file.read_text(encoding="utf-8"))
                    after = self._protected_hashes()
                    if before != after:
                        self.store.event(
                            "EVALUATOR_TAMPERED",
                            status="BLOCKED",
                            detail={"turn": turn, "changed": sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))},
                        )
                        return self.store.status()

                # Check wall-time deadline immediately after worker execution
                elapsed = recorded_elapsed + (self.clock() - start_clock)
                if elapsed >= self.config.limits.wall_time_seconds:
                    self.store.event(
                        "LIMIT_REACHED",
                        status="BLOCKED",
                        detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
                    )
                    return self.store.status()

                # Worker crash / timeout / non-zero exit handling
                if worker_result.timed_out:
                    self.store.event(
                        "WORKER_FAILED",
                        status="BLOCKED",
                        detail={"turn": turn, "reason": "timeout", "returncode": worker_result.returncode},
                    )
                    return self.store.status()

                if worker_result.returncode != 0:
                    reason = classify_provider_failure(worker_result.error, worker_result.output) or "non_zero_exit"
                    self.store.event(
                        "WORKER_FAILED",
                        status="BLOCKED",
                        detail={"turn": turn, "reason": reason, "returncode": worker_result.returncode},
                    )
                    return self.store.status()

                # Worker success exit 0 -> records WORKER_FINISHED
                worker_turns += 1
                if "WORKER_FINISHED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": turn})
                pending_findings = ()
                pending_check_evidence = ()
            else:
                turn = worker_turns

            # Calculate remaining wall time before running commands
            elapsed = recorded_elapsed + (self.clock() - start_clock)
            remaining_time = max(0.001, self.config.limits.wall_time_seconds - elapsed)

            if resume_stage in {"WORKER", "POST_WORKER_CHECKS"}:
                # 3. POST-WORKER DETERMINISTIC CHECKS
                post_worker_file = self.store.root / f"verification-post-worker-turn-{turn}.json"
                if post_worker_file.exists():
                    saved_checks = json.loads(post_worker_file.read_text(encoding="utf-8"))
                    check_results = tuple(
                        VerificationResult(
                            name=c["name"],
                            argv=tuple(c["argv"]),
                            returncode=c["returncode"],
                            stdout=c["stdout"],
                            stderr=c["stderr"],
                            timed_out=c.get("timed_out", False),
                        )
                        for c in saved_checks
                    )
                else:
                    if "POST_WORKER_CHECKS_STARTED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                        self.store.event("POST_WORKER_CHECKS_STARTED", status="RUNNING", detail={"turn": turn})
                    check_results = run_commands(self.config.verification, cwd=self.worktree, runner=self.runner, timeout=remaining_time, clock=self.clock)
                    self.store.freeze_json(f"verification-post-worker-turn-{turn}.json", [asdict(r) for r in check_results])

                failed_checks = [r for r in check_results if r.returncode != 0]
                if failed_checks:
                    if "POST_WORKER_CHECKS_FAILED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                        self.store.event(
                            "POST_WORKER_CHECKS_FAILED",
                            status="RUNNING",
                            detail={"turn": turn, "failures": [r.name for r in failed_checks]},
                        )
                    pending_check_evidence = tuple(failed_checks)
                    resume_stage = "WORKER"
                    continue

                if "POST_WORKER_CHECKS_PASSED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event("POST_WORKER_CHECKS_PASSED", status="RUNNING", detail={"turn": turn})
            else:
                post_worker_file = self.store.root / f"verification-post-worker-turn-{turn}.json"
                if post_worker_file.exists():
                    saved_checks = json.loads(post_worker_file.read_text(encoding="utf-8"))
                    check_results = tuple(
                        VerificationResult(
                            name=c["name"],
                            argv=tuple(c["argv"]),
                            returncode=c["returncode"],
                            stdout=c["stdout"],
                            stderr=c["stderr"],
                            timed_out=c.get("timed_out", False),
                        )
                        for c in saved_checks
                    )
                else:
                    check_results = run_commands(self.config.verification, cwd=self.worktree, runner=self.runner, timeout=remaining_time, clock=self.clock)

            if resume_stage != "FINAL_CHECKS":
                # 4. CRITIC TURN
                if "CRITIC_STARTED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event("CRITIC_STARTED", status="RUNNING", detail={"turn": turn})

                manifest_before_file = self.store.root / f"manifest-before-critic-turn-{turn}.json"
                if manifest_before_file.exists():
                    manifest_before = json.loads(manifest_before_file.read_text(encoding="utf-8"))
                else:
                    manifest_before = snapshot_manifest(self.worktree)
                    self.store.freeze_json(f"manifest-before-critic-turn-{turn}.json", manifest_before)

                raw_file = self.store.root / f"critic-turn-{turn}-raw.txt"
                manifest_after_file = self.store.root / f"manifest-after-critic-turn-{turn}.json"
                verdict_file = self.store.root / f"critic-turn-{turn}-verdict.json"

                if raw_file.exists() and manifest_after_file.exists():
                    raw_verdict = raw_file.read_text(encoding="utf-8")
                    critic_result = CriticResult(raw_verdict=raw_verdict, returncode=0, timed_out=False)
                    manifest_after = json.loads(manifest_after_file.read_text(encoding="utf-8"))
                    current_manifest = snapshot_manifest(self.worktree)
                    if current_manifest != manifest_after:
                        diff = manifest_diff(manifest_after, current_manifest)
                        self.store.event(
                            "WORKTREE_MUTATED_OUTSIDE_SUPERVISOR",
                            status="ESCALATED",
                            detail={"turn": turn, "diff": diff},
                        )
                        return self.store.status()
                else:
                    elapsed = recorded_elapsed + (self.clock() - start_clock)
                    grant_time(self.critic, self.config.limits.wall_time_seconds - elapsed)
                    critic_result = self.critic.run(
                        worktree=self.worktree,
                        task=self.task,
                        turn=turn,
                        check_results=check_results,
                    )

                    manifest_after = snapshot_manifest(self.worktree)
                    if not manifest_after_file.exists():
                        self.store.freeze_json(f"manifest-after-critic-turn-{turn}.json", manifest_after)

                    # Check if critic mutated the repository
                    if manifest_before != manifest_after:
                        diff = manifest_diff(manifest_before, manifest_after)
                        self.store.event(
                            "CRITIC_MUTATED_REPO",
                            status="ESCALATED",
                            detail={"turn": turn, "diff": diff},
                        )
                        return self.store.status()

                    if not self._check_frozen_artifacts():
                        return self.store.status()

                    # Check wall-time deadline immediately after critic execution
                    elapsed = recorded_elapsed + (self.clock() - start_clock)
                    if elapsed >= self.config.limits.wall_time_seconds:
                        self.store.event(
                            "LIMIT_REACHED",
                            status="BLOCKED",
                            detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
                        )
                        return self.store.status()

                    # Check critic process reliability
                    if critic_result.timed_out:
                        self.store.event(
                            "CRITIC_FAILED",
                            status="BLOCKED",
                            detail={"turn": turn, "reason": "timeout", "returncode": critic_result.returncode},
                        )
                        return self.store.status()

                    if critic_result.returncode != 0:
                        self.store.event(
                            "CRITIC_FAILED",
                            status="BLOCKED",
                            detail={"turn": turn, "reason": "non_zero_exit", "returncode": critic_result.returncode},
                        )
                        return self.store.status()

                    if not raw_file.exists():
                        self.store.freeze_text(f"critic-turn-{turn}-raw.txt", critic_result.raw_verdict)

                # Validate critic verdict schema
                if verdict_file.exists():
                    verdict_data = json.loads(verdict_file.read_text(encoding="utf-8"))
                else:
                    try:
                        verdict_data = validate_verdict(critic_result.raw_verdict)
                    except VerdictError as exc:
                        self.store.event(
                            "INVALID_CRITIC_VERDICT",
                            status="BLOCKED",
                            detail={"turn": turn, "error": str(exc)},
                        )
                        return self.store.status()

                    self.store.freeze_json(f"critic-turn-{turn}-verdict.json", verdict_data)

                # Check for human decision escalation marker
                if verdict_data.get("human_decision_required"):
                    self.store.event(
                        "HUMAN_DECISION_REQUIRED",
                        status="ESCALATED",
                        detail={"turn": turn, "reason": "human_decision_required"},
                    )
                    return self.store.status()

                # Handle BLOCK verdict
                if verdict_data["verdict"] == "BLOCK":
                    findings = verdict_data["blocking_findings"]
                    fps = [compute_fingerprint(f) for f in findings]

                    # Update and track blocker fingerprints
                    limit_reached = False
                    triggering_fp = None
                    for fp in fps:
                        blocker_counts[fp] = blocker_counts.get(fp, 0) + 1
                        if blocker_counts[fp] >= self.config.limits.same_blocker_limit:
                            limit_reached = True
                            triggering_fp = fp
                            break

                    # Save fingerprints artifact for auditability
                    fingerprints_file = self.store.root / f"blocker-fingerprints-turn-{turn}.json"
                    if not fingerprints_file.exists():
                        self.store.freeze_json(f"blocker-fingerprints-turn-{turn}.json", {
                            "turn": turn,
                            "fingerprints": fps,
                            "counts": dict(blocker_counts),
                        })

                    if limit_reached:
                        self.store.event(
                            "SAME_BLOCKER_LIMIT_REACHED",
                            status="ESCALATED",
                            detail={
                                "turn": turn,
                                "fingerprint": triggering_fp,
                                "count": blocker_counts[triggering_fp],
                                "limit": self.config.limits.same_blocker_limit,
                            },
                        )
                        return self.store.status()

                    if "CRITIC_BLOCKED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                        self.store.event(
                            "CRITIC_BLOCKED",
                            status="RUNNING",
                            detail={"turn": turn, "finding_count": len(findings), "fingerprints": fps},
                        )
                    pending_findings = tuple(findings)
                    resume_stage = "WORKER"
                    continue

                # Handle PASS verdict
                # validate_verdict guaranteed blocking_findings is empty []
                if "CRITIC_PASSED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event("CRITIC_PASSED", status="RUNNING", detail={"turn": turn})
            else:
                manifest_after_file = self.store.root / f"manifest-after-critic-turn-{turn}.json"
                if not manifest_after_file.exists():
                    self.store.event(
                        "CRITIC_FAILED",
                        status="BLOCKED",
                        detail={"turn": turn, "reason": "missing_manifest_after_critic"},
                    )
                    return self.store.status()
                manifest_after = json.loads(manifest_after_file.read_text(encoding="utf-8"))
                current_manifest = snapshot_manifest(self.worktree)
                if current_manifest != manifest_after:
                    diff = manifest_diff(manifest_after, current_manifest)
                    self.store.event(
                        "WORKTREE_MUTATED_OUTSIDE_SUPERVISOR",
                        status="ESCALATED",
                        detail={"turn": turn, "diff": diff},
                    )
                    return self.store.status()

            resume_stage = "WORKER"

            # 5. FINAL DETERMINISTIC CHECKS
            final_file = self.store.root / f"verification-final-turn-{turn}.json"
            if final_file.exists():
                saved_final = json.loads(final_file.read_text(encoding="utf-8"))
                final_results = [
                    VerificationResult(
                        name=r["name"],
                        argv=tuple(r["argv"]),
                        returncode=r["returncode"],
                        stdout=r["stdout"],
                        stderr=r["stderr"],
                        timed_out=r.get("timed_out", False),
                    )
                    for r in saved_final
                ]
            else:
                if "FINAL_CHECKS_STARTED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event("FINAL_CHECKS_STARTED", status="RUNNING", detail={"turn": turn})
                elapsed = recorded_elapsed + (self.clock() - start_clock)
                remaining_time = max(0.001, self.config.limits.wall_time_seconds - elapsed)
                final_results = run_commands(self.config.verification, cwd=self.worktree, runner=self.runner, timeout=remaining_time, clock=self.clock)
                self.store.freeze_json(f"verification-final-turn-{turn}.json", [asdict(r) for r in final_results])

            failed_final = [r for r in final_results if r.returncode != 0]
            if failed_final:
                if "FINAL_CHECKS_FAILED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                    self.store.event(
                        "FINAL_CHECKS_FAILED",
                        status="BLOCKED",
                        detail={"turn": turn, "failures": [r.name for r in failed_final]},
                    )
                return self.store.status()

            if "FINAL_CHECKS_PASSED" not in [e["kind"] for e in self.store.events() if e.get("detail", {}).get("turn") == turn]:
                self.store.event("FINAL_CHECKS_PASSED", status="RUNNING", detail={"turn": turn})

            # Check wall-time deadline before completion gate
            elapsed = recorded_elapsed + (self.clock() - start_clock)
            if elapsed >= self.config.limits.wall_time_seconds:
                self.store.event(
                    "LIMIT_REACHED",
                    status="BLOCKED",
                    detail={"reason": "wall_time_exceeded", "elapsed": elapsed, "limit": self.config.limits.wall_time_seconds},
                )
                return self.store.status()

            # 6. SUPERVISOR COMPLETION GATE (the SOLE creator of DONE)
            return self._complete_supervisor_gate(turn)

    def _run_baseline_checks(self, timeout: float | None = None) -> bool:
        baseline_file = self.store.root / "baseline-checks.json"
        if baseline_file.exists():
            saved_baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
            results = [
                VerificationResult(
                    name=r["name"],
                    argv=tuple(r["argv"]),
                    returncode=r["returncode"],
                    stdout=r["stdout"],
                    stderr=r["stderr"],
                    timed_out=r.get("timed_out", False),
                )
                for r in saved_baseline
            ]
        else:
            if "BASELINE_CHECKS_STARTED" not in [e["kind"] for e in self.store.events()]:
                self.store.event("BASELINE_CHECKS_STARTED", status="RUNNING")
            effective_timeout = timeout if timeout is not None else self.config.limits.wall_time_seconds
            results = run_commands(
                self.config.verification,
                cwd=self.worktree,
                runner=self.runner,
                timeout=effective_timeout,
                clock=self.clock,
            )
            self.store.freeze_json("baseline-checks.json", [asdict(r) for r in results])
        failed = [r for r in results if r.returncode != 0]
        if failed:
            if "BASELINE_CHECKS_FAILED" not in [e["kind"] for e in self.store.events()]:
                self.store.event(
                    "BASELINE_CHECKS_FAILED",
                    status="BLOCKED",
                    detail={"failures": [r.name for r in failed]},
                )
            return False
        if not (self.store.root / "protected-manifest.json").exists():
            self.store.freeze_json("protected-manifest.json", self._protected_hashes())
        if "BASELINE_CHECKS_PASSED" not in [e["kind"] for e in self.store.events()]:
            self.store.event("BASELINE_CHECKS_PASSED", status="RUNNING")
        return True

    def _protected_hashes(self) -> dict[str, str]:
        """Hashes of the evaluator files the worker must not touch (verification.protected globs)."""
        found = {
            path.relative_to(self.worktree).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for pattern in self.config.protected
            for path in self.worktree.glob(pattern)
            if path.is_file()
        }
        return dict(sorted(found.items()))

    def _gate_problem(self, turn: int) -> str | None:
        """Re-read the evidence on disk; DONE is refused unless it still shows PASS and green final checks."""
        verdict_file = self.store.root / f"critic-turn-{turn}-verdict.json"
        final_file = self.store.root / f"verification-final-turn-{turn}.json"
        try:
            verdict = json.loads(verdict_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "critic verdict evidence is missing or unreadable"
        if verdict.get("verdict") != "PASS" or verdict.get("blocking_findings"):
            return "recorded critic verdict is not an empty-finding PASS"
        try:
            finals = json.loads(final_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "final check evidence is missing or unreadable"
        if not isinstance(finals, list) or any(not isinstance(r, dict) or r.get("returncode") != 0 for r in finals):
            return "recorded final checks did not all pass"
        return None

    def _freeze_manifest_before_worker(self, turn: int) -> None:
        name = f"worker-turn-{turn}-manifest-before.json"
        if not (self.store.root / name).exists():
            self.store.freeze_json(name, snapshot_manifest(self.worktree))

    def _record_worker_changes(self, turn: int) -> None:
        """Freeze what a worker turn changed: file lists with hashes, plus the Git patch against HEAD."""
        changes_name = f"worker-turn-{turn}-changes.json"
        before_file = self.store.root / f"worker-turn-{turn}-manifest-before.json"
        if (self.store.root / changes_name).exists() or not before_file.exists():
            return
        before = json.loads(before_file.read_text(encoding="utf-8"))
        after = snapshot_manifest(self.worktree)
        diff = manifest_diff(before, after)

        def visible(paths: list[str]) -> list[str]:
            return [p for p in paths if not p.startswith(".git-")]

        record: dict[str, Any] = {
            "turn": turn,
            "added": {p: after[p] for p in visible(diff["added"])},
            "modified": {p: after[p] for p in visible(diff["modified"])},
            "removed": visible(diff["removed"]),
            "patch": None,
        }
        try:
            patch = self.runner.run(["git", "--no-optional-locks", "diff", "--binary", "HEAD"], cwd=self.worktree, timeout=60)
            if patch.returncode == 0 and isinstance(patch.stdout, str):
                self.store.freeze_text(f"worker-turn-{turn}-diff.patch", patch.stdout)
                record["patch"] = f"worker-turn-{turn}-diff.patch"
                record["patch_truncated"] = "[truncated" in patch.stdout
        except Exception:  # evidence is best effort; the manifest lists above are the authority
            pass
        self.store.freeze_json(changes_name, record)

    def _write_summary(self) -> None:
        """One-file answer to 'did the job run and what did it create?', rewritten whenever a run stops."""
        events = self.store.events()
        status = self.store.status()
        turns = []
        for turn in sorted({e.get("detail", {}).get("turn") for e in events if isinstance(e.get("detail", {}).get("turn"), int)}):
            changes_file = self.store.root / f"worker-turn-{turn}-changes.json"
            changes = json.loads(changes_file.read_text(encoding="utf-8")) if changes_file.exists() else None
            verdict_file = self.store.root / f"critic-turn-{turn}-verdict.json"
            verdict = json.loads(verdict_file.read_text(encoding="utf-8")) if verdict_file.exists() else None
            turns.append(
                {
                    "turn": turn,
                    "worker_launches": sum(1 for e in events if e["kind"] == "WORKER_STARTED" and e.get("detail", {}).get("turn") == turn),
                    "worker_finished": any(e["kind"] == "WORKER_FINISHED" and e.get("detail", {}).get("turn") == turn for e in events),
                    "files_added": sorted(changes["added"]) if changes else None,
                    "files_modified": sorted(changes["modified"]) if changes else None,
                    "files_removed": changes["removed"] if changes else None,
                    "patch": changes.get("patch") if changes else None,
                    "critic_verdict": verdict["verdict"] if verdict else None,
                    "critic_findings": len(verdict["blocking_findings"]) if verdict else None,
                }
            )
        summary = {
            "run_id": self.store.run_id,
            "status": status.get("status"),
            "last_event": status.get("last_event"),
            "created_at": events[0]["at"] if events else None,
            "last_event_at": events[-1]["at"] if events else None,
            "active_seconds": round(active_seconds(events), 1),
            "worktree": str(self.worktree),
            "turns": turns,
        }
        self.store.write_json("summary.json", summary)

    def _complete_supervisor_gate(self, turn: int) -> dict[str, Any]:
        """CONSTITUTIONAL PROPERTY: The only place where terminal DONE is created."""
        problem = self._gate_problem(turn)
        if problem:
            self.store.event(
                "COMPLETION_GATE_REFUSED",
                status="ESCALATED",
                detail={"turn": turn, "reason": problem},
            )
            return self.store.status()
        output_file = self.store.root / f"worker-turn-{turn}-output.txt"
        output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        verdict_file = self.store.root / f"critic-turn-{turn}-verdict.json"
        verdict = json.loads(verdict_file.read_text(encoding="utf-8"))

        if not (self.store.root / "worker-output.txt").exists():
            self.store.freeze_text("worker-output.txt", output)
        if not (self.store.root / "critic-verdict.json").exists():
            self.store.freeze_json("critic-verdict.json", verdict)

        if "COMPLETED" not in [e["kind"] for e in self.store.events()]:
            self.store.event(
                "COMPLETED",
                status="DONE",
                detail={"turn": turn, "reason": "all_deterministic_checks_and_critic_passed"},
            )
        return self.store.status()
