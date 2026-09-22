from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .task import TaskContract
from .verification import VerificationResult


@dataclass(frozen=True)
class WorkerResult:
    """Worker execution outcome.

    CONSTITUTIONAL INVARIANT: This type contains NO terminal status,
    completion flag, or DONE authority. Only exit status and textual streams.
    """
    returncode: int
    output: str = ""
    error: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class CriticResult:
    """Critic execution outcome.

    Raw verdict string must be validated by the supervisor against the JSON schema.
    Critic exit status and stderr are preserved for auditing.
    """
    returncode: int
    raw_verdict: str = ""
    error: str = ""
    timed_out: bool = False


class WorkerAdapter(Protocol):
    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        findings: Sequence[dict] = (),
        check_evidence: Sequence[VerificationResult] = (),
    ) -> WorkerResult: ...


class CriticAdapter(Protocol):
    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        check_results: Sequence[VerificationResult] = (),
    ) -> CriticResult: ...


@dataclass
class WorkerAction:
    edits: dict[str, str] = field(default_factory=dict)
    returncode: int = 0
    output: str = "worker turn completed"
    error: str = ""
    timed_out: bool = False


@dataclass
class CriticAction:
    raw_verdict: str = '{"verdict": "PASS", "blocking_findings": []}'
    returncode: int = 0
    error: str = ""
    timed_out: bool = False
    repo_mutations: dict[str, str] = field(default_factory=dict)


class FakeWorkerAdapter:
    """Deterministic fake worker for exercising supervisor orchestration.

    Supports simulating:
    - successful edits
    - no edits
    - partial edits
    - zero exit code
    - non-zero exit code
    - process timeout
    - malformed output
    - repeated responses
    - fixing critic findings / failed checks
    - explicitly claiming "DONE" or "task complete" (ignored by supervisor)
    """

    def __init__(
        self,
        actions: Sequence[WorkerAction | Callable[..., WorkerResult]] | None = None,
        default_action: WorkerAction | None = None,
    ):
        self._actions = list(actions) if actions is not None else []
        self._default_action = default_action or WorkerAction()
        self.invocations: list[dict[str, Any]] = []

    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        findings: Sequence[dict] = (),
        check_evidence: Sequence[VerificationResult] = (),
    ) -> WorkerResult:
        self.invocations.append({
            "turn": turn,
            "worktree": worktree,
            "task": task,
            "findings": tuple(findings),
            "check_evidence": tuple(check_evidence),
        })

        if self._actions:
            current = self._actions.pop(0)
            if callable(current):
                return current(worktree=worktree, task=task, turn=turn, findings=findings, check_evidence=check_evidence)
            action = current
        else:
            action = self._default_action

        # Apply simulated file edits into the worktree
        for rel_path, content in action.edits.items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        return WorkerResult(
            returncode=action.returncode,
            output=action.output,
            error=action.error,
            timed_out=action.timed_out,
        )


class FakeCriticAdapter:
    """Deterministic fake critic for exercising supervisor orchestration.

    Supports simulating:
    - PASS with zero findings
    - BLOCK with one or multiple findings
    - Malformed JSON output
    - PASS with findings (invalid)
    - BLOCK with no findings (invalid)
    - Timeout
    - Non-zero exit code
    - Repeated identical blockers
    - Different blockers across rounds
    - Human decision required / escalation
    - Repository mutation (simulating broken sandbox)
    """

    def __init__(
        self,
        actions: Sequence[CriticAction | Callable[..., CriticResult]] | None = None,
        default_action: CriticAction | None = None,
    ):
        self._actions = list(actions) if actions is not None else []
        self._default_action = default_action or CriticAction()
        self.invocations: list[dict[str, Any]] = []

    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        check_results: Sequence[VerificationResult] = (),
    ) -> CriticResult:
        self.invocations.append({
            "turn": turn,
            "worktree": worktree,
            "task": task,
            "check_results": tuple(check_results),
        })

        if self._actions:
            current = self._actions.pop(0)
            if callable(current):
                return current(worktree=worktree, task=task, turn=turn, check_results=check_results)
            action = current
        else:
            action = self._default_action

        # Simulate unauthorized repository mutation if configured
        for rel_path, content in action.repo_mutations.items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        return CriticResult(
            returncode=action.returncode,
            raw_verdict=action.raw_verdict,
            error=action.error,
            timed_out=action.timed_out,
        )

    @classmethod
    def passing(cls) -> "FakeCriticAdapter":
        return cls(default_action=CriticAction(raw_verdict=json.dumps({"verdict": "PASS", "blocking_findings": []})))

    @classmethod
    def blocking(cls, findings: Sequence[dict], human_decision: bool = False) -> "FakeCriticAdapter":
        payload: dict[str, Any] = {"verdict": "BLOCK", "blocking_findings": list(findings)}
        if human_decision:
            payload["human_decision_required"] = True
        return cls(default_action=CriticAction(raw_verdict=json.dumps(payload)))


def get_adapters(config: Any) -> tuple[WorkerAdapter, CriticAdapter]:
    """Resolve adapters for the given configuration.

    In Phase 2A, live adapters are deliberately omitted to ensure deterministic tests.
    """
    model = getattr(config, "worker_model", "")
    if model == "fake" or str(model).startswith("fake"):
        return FakeWorkerAdapter(), FakeCriticAdapter.passing()
    raise NotImplementedError(
        f"Live provider adapters for worker model {model!r} are scheduled for Phase 2B. "
        "Use FakeWorkerAdapter and FakeCriticAdapter for Phase 2A deterministic runs."
    )
