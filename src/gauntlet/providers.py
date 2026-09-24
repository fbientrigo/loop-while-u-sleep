from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Protocol, Sequence

from .config import Config, ConfigError
from .prompts import build_critic_prompt, build_worker_prompt
from .task import TaskContract
from .verdict import schema_path
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


class AdapterError(RuntimeError):
    """Raised when a live provider adapter cannot be constructed or safely invoked."""


_DISALLOWED_EXEC_SUFFIXES = (".cmd", ".bat")
_CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _reject_shell_executable(executable: str) -> None:
    """Refuse executables that Windows implicitly launches through cmd.exe.

    subprocess with shell=False still invokes cmd.exe internally for .cmd/.bat
    targets, which reintroduces shell quoting/interpolation risk for argv values
    such as prompts and model slugs. Real installs (a compiled .exe or a POSIX
    binary/script with a shebang) are unaffected.
    """
    if executable.lower().endswith(_DISALLOWED_EXEC_SUFFIXES):
        raise AdapterError(
            f"refusing to launch provider executable through an implicit shell wrapper: {executable!r}"
        )


def _next_attempt(store: Any, pattern: str) -> int:
    return len(list(store.root.glob(pattern))) + 1


def _atomic_write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_agy_payload(stdout: str) -> tuple[dict | None, str | None]:
    stripped = stdout.strip()
    if not stripped:
        return None, "agy produced no output"
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"agy output is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "agy output is not a JSON object"
    return payload, None


class AgyAdapter:
    """Real writable worker adapter for the Antigravity (agy) CLI.

    CONSTITUTIONAL INVARIANT: this adapter never interprets worker prose or exit
    status as a completion signal; it only ever returns a WorkerResult, which has
    no DONE field.
    """

    def __init__(
        self,
        store: Any,
        runner: Any,
        config: Config,
        command: Sequence[str] = ("agy",),
        timeout_seconds: float | None = None,
    ):
        self.store = store
        self.runner = runner
        self.config = config
        self.command = tuple(command)
        if not self.command or not self.command[0]:
            raise AdapterError("agy executable path is not resolved")
        _reject_shell_executable(self.command[0])
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        findings: Sequence[dict] = (),
        check_evidence: Sequence[VerificationResult] = (),
    ) -> WorkerResult:
        conversation_id, state_error = self._load_conversation_state()
        if state_error:
            return WorkerResult(returncode=70, output="", error=state_error, timed_out=False)
        if conversation_id is None and turn > 1:
            return WorkerResult(
                returncode=70,
                output="",
                error="agy continuation state missing for turn > 1 of this run; refusing to guess",
                timed_out=False,
            )

        prompt = build_worker_prompt(task, findings, check_evidence)

        timeout_arg = f"{int(self.timeout_seconds)}s" if self.timeout_seconds else "0s"
        argv = [
            *self.command,
            "-p",
            prompt,
            "--model",
            self.config.worker_model,
            "--effort",
            self.config.worker_effort,
            "--output-format",
            "json",
            "--print-timeout",
            timeout_arg,
            "--mode",
            "accept-edits",
            "--disable-slash-commands",
            "--add-dir",
            str(worktree),
        ]
        if conversation_id:
            argv.extend(["--conversation", conversation_id])

        attempt = _next_attempt(self.store, f"worker-turn-{turn}-attempt-*-command.json")
        self.store.freeze_text(f"worker-turn-{turn}-attempt-{attempt}-prompt.txt", prompt)
        self.store.freeze_json(f"worker-turn-{turn}-attempt-{attempt}-command.json", {"argv": argv})

        result = self.runner.run(argv, cwd=worktree, timeout=self.timeout_seconds)

        if result.timed_out:
            return WorkerResult(returncode=result.returncode or 124, output=result.stdout, error=result.stderr, timed_out=True)
        if result.returncode != 0:
            return WorkerResult(returncode=result.returncode, output=result.stdout, error=result.stderr, timed_out=False)

        payload, parse_error = _parse_agy_payload(result.stdout)
        if parse_error:
            return WorkerResult(returncode=70, output=result.stdout, error=parse_error, timed_out=False)

        new_conversation_id = payload.get("conversation_id")
        if not isinstance(new_conversation_id, str) or not _CONVERSATION_ID_PATTERN.fullmatch(new_conversation_id):
            return WorkerResult(
                returncode=70,
                output=result.stdout,
                error="agy result is missing a well-formed conversation_id",
                timed_out=False,
            )
        if conversation_id is not None and new_conversation_id != conversation_id:
            return WorkerResult(
                returncode=70,
                output=result.stdout,
                error="agy returned a different conversation_id than the one supplied for this run; refusing",
                timed_out=False,
            )

        if payload.get("status") != "SUCCESS":
            return WorkerResult(
                returncode=70,
                output=result.stdout,
                error=f"agy reported status={payload.get('status')!r} error={payload.get('error')!r}",
                timed_out=False,
            )

        self._save_conversation_state(new_conversation_id)
        return WorkerResult(returncode=0, output=result.stdout, error=result.stderr, timed_out=False)

    def _state_path(self) -> Path:
        return self.store.root / "worker-conversation.json"

    def _load_conversation_state(self) -> tuple[str | None, str | None]:
        path = self._state_path()
        if not path.exists():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"agy continuation state is malformed: {exc}"
        if not isinstance(data, dict):
            return None, "agy continuation state is not a JSON object"
        if data.get("provider") != "agy" or data.get("run_id") != self.store.run_id:
            return None, "agy continuation state does not match this provider/run"
        conversation_id = data.get("conversation_id")
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
            return None, "agy continuation state has an invalid conversation_id"
        return conversation_id, None

    def _save_conversation_state(self, conversation_id: str) -> None:
        _atomic_write_json(
            self._state_path(),
            {"provider": "agy", "run_id": self.store.run_id, "conversation_id": conversation_id},
        )


class CodexAdapter:
    """Real read-only critic adapter using a fresh `codex exec` process per review.

    CONSTITUTIONAL INVARIANT: every call starts a brand-new process; no
    conversation/session id is ever read, stored, or passed to codex.
    """

    def __init__(
        self,
        store: Any,
        runner: Any,
        config: Config,
        command: Sequence[str] = ("codex",),
        timeout_seconds: float | None = None,
        read_only_proven: bool = False,
    ):
        self.store = store
        self.runner = runner
        self.config = config
        self.command = tuple(command)
        if not self.command or not self.command[0]:
            raise AdapterError("codex executable path is not resolved")
        _reject_shell_executable(self.command[0])
        self.timeout_seconds = timeout_seconds
        self.read_only_proven = read_only_proven
        if not config.critic_model:
            raise AdapterError("critic.model must be configured for the live Codex adapter")

    def run(
        self,
        worktree: Path,
        task: TaskContract,
        turn: int,
        check_results: Sequence[VerificationResult] = (),
    ) -> CriticResult:
        if not self.read_only_proven:
            return CriticResult(
                returncode=78,
                raw_verdict="",
                error="codex read-only sandbox was not proven safe on this platform; refusing to launch the critic",
                timed_out=False,
            )

        repo_state = self._repo_state(worktree)
        prompt = build_critic_prompt(task, check_results, repo_state)

        attempt = _next_attempt(self.store, f"critic-turn-{turn}-attempt-*-command.json")
        self.store.freeze_text(f"critic-turn-{turn}-attempt-{attempt}-prompt.txt", prompt)

        with tempfile.TemporaryDirectory(prefix="gauntlet-codex-out-") as tmp:
            output_file = Path(tmp) / "verdict.json"
            argv = [
                *self.command,
                "-a",
                "never",
                "exec",
                "-C",
                str(worktree),
                "-m",
                self.config.critic_model,
                "-c",
                f'model_reasoning_effort="{self.config.critic_effort}"',
                "-s",
                "read-only",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--output-schema",
                str(schema_path()),
                "-o",
                str(output_file),
                "--color",
                "never",
                "-",
            ]
            self.store.freeze_json(f"critic-turn-{turn}-attempt-{attempt}-command.json", {"argv": argv})

            result = self.runner.run(argv, cwd=worktree, input_text=prompt, timeout=self.timeout_seconds)
            self.store.freeze_text(f"critic-turn-{turn}-attempt-{attempt}-stdout.txt", result.stdout)
            if result.stderr:
                self.store.freeze_text(f"critic-turn-{turn}-attempt-{attempt}-stderr.txt", result.stderr)

            if result.timed_out:
                return CriticResult(returncode=result.returncode or 124, raw_verdict="", error=result.stderr, timed_out=True)
            if result.returncode != 0:
                return CriticResult(returncode=result.returncode, raw_verdict="", error=result.stderr, timed_out=False)

            try:
                raw_verdict = output_file.read_text(encoding="utf-8")
            except OSError as exc:
                return CriticResult(
                    returncode=0,
                    raw_verdict="",
                    error=f"codex exited 0 but produced no --output-schema result file: {exc}",
                    timed_out=False,
                )

        return CriticResult(returncode=0, raw_verdict=raw_verdict, error=result.stderr, timed_out=False)

    def _repo_state(self, worktree: Path) -> str:
        status = self.runner.run(
            ["git", "--no-optional-locks", "status", "--porcelain", "-uall"], cwd=worktree, timeout=30
        )
        diff = self.runner.run(["git", "--no-optional-locks", "diff", "HEAD"], cwd=worktree, timeout=30)
        return "\n".join(
            [
                "## git status --porcelain -uall",
                status.stdout.strip() or "(clean)",
                "",
                "## git diff HEAD",
                diff.stdout.strip() or "(no diff)",
            ]
        )


def get_adapters(
    config: Any,
    *,
    store: Any = None,
    runner: Any = None,
    critic_read_only_proven: bool = False,
    agy_command: Sequence[str] | None = None,
    codex_command: Sequence[str] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[WorkerAdapter, CriticAdapter]:
    """Resolve adapters for the given configuration.

    A `fake`-prefixed worker model always resolves to the deterministic fakes,
    for tests. Any other worker model resolves to the live AgyAdapter/CodexAdapter
    pair, which requires a run store and process runner to record evidence and
    launch subprocesses.
    """
    model = getattr(config, "worker_model", "")
    if model == "fake" or str(model).startswith("fake"):
        return FakeWorkerAdapter(), FakeCriticAdapter.passing()

    if store is None or runner is None:
        raise NotImplementedError(
            f"live provider adapters for worker model {model!r} require a run store and process runner"
        )
    if not config.critic_model:
        raise ConfigError("critic.model must be configured (resolved by doctor) before a live run")

    resolved_timeout = timeout_seconds if timeout_seconds is not None else config.limits.wall_time_seconds

    agy_cmd = tuple(agy_command) if agy_command else (shutil.which("agy") or "agy",)
    codex_cmd = tuple(codex_command) if codex_command else (shutil.which("codex") or "codex",)

    worker: WorkerAdapter = AgyAdapter(
        store=store, runner=runner, config=config, command=agy_cmd, timeout_seconds=resolved_timeout
    )
    critic: CriticAdapter = CodexAdapter(
        store=store,
        runner=runner,
        config=config,
        command=codex_cmd,
        timeout_seconds=resolved_timeout,
        read_only_proven=critic_read_only_proven,
    )
    return worker, critic
