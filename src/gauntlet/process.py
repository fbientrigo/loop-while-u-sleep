from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


MAX_PROCESS_OUTPUT_CHARS = 500_000


def bound_output(text: str, max_chars: int = MAX_PROCESS_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    omitted = len(text) - (keep * 2)
    return f"{text[:keep]}\n... [truncated {omitted} characters] ...\n{text[-keep:]}"


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> ProcessResult: ...


class SubprocessRunner:
    def __init__(self, max_output_chars: int = MAX_PROCESS_OUTPUT_CHARS):
        self.max_output_chars = max_output_chars

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        run_env = dict(os.environ) if env is None else dict(env)
        run_env.setdefault("PYTHONUTF8", "1")
        run_env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                shell=False,
                check=False,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired as exc:
            raw_stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
            raw_stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            timeout_msg = f"command timed out after {timeout} seconds"
            stderr = f"{raw_stderr}\n{timeout_msg}".strip() if raw_stderr else timeout_msg
            return ProcessResult(
                124,
                bound_output(raw_stdout or "", self.max_output_chars),
                bound_output(stderr, self.max_output_chars),
                timed_out=True,
            )
        except OSError as exc:
            return ProcessResult(127, "", str(exc))
        return ProcessResult(
            completed.returncode,
            bound_output(completed.stdout, self.max_output_chars),
            bound_output(completed.stderr, self.max_output_chars),
        )
