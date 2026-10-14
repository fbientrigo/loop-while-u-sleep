from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Protocol, Sequence

import psutil


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


MAX_PROCESS_OUTPUT_CHARS = 500_000
_NEW_PROCESS_GROUP = 0x00000200  # subprocess.CREATE_NEW_PROCESS_GROUP, spelled out so POSIX imports stay clean


def kill_process_tree(process: subprocess.Popen) -> None:
    """Kill a process and every descendant; a timed-out agent must not leave children running."""
    try:
        if os.name != "nt":
            os.killpg(process.pid, 9)
            return
        parent = psutil.Process(process.pid)
        for child in reversed(parent.children(recursive=True)):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
    except (ProcessLookupError, psutil.Error, OSError):
        pass


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
            # Own process group so a timeout can stop the whole tree, not only the direct child.
            with subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=run_env,
                start_new_session=(os.name != "nt"),
                creationflags=_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            ) as process:
                try:
                    stdout, stderr = process.communicate(input=input_text, timeout=timeout)
                except BaseException as interruption:
                    # Ctrl-C reaches only our own process group; the child must not keep editing unattended.
                    kill_process_tree(process)
                    if not isinstance(interruption, subprocess.TimeoutExpired):
                        raise
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired as late:
                        stdout, stderr = late.stdout, late.stderr
                    raise subprocess.TimeoutExpired(list(argv), timeout, output=stdout, stderr=stderr) from None
                completed = subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
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
