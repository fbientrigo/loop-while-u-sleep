from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from .config import VerificationCommand
from .process import ProcessRunner


@dataclass(frozen=True)
class VerificationResult:
    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_commands(
    commands: tuple[VerificationCommand, ...],
    cwd: Path,
    runner: ProcessRunner,
    timeout: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[VerificationResult, ...]:
    results = []
    if not commands:
        return ()
    start_time = clock() if timeout is not None else 0.0
    deadline = start_time + timeout if timeout is not None else None
    for i, command in enumerate(commands):
        kwargs: dict[str, object] = {"cwd": cwd}
        if deadline is not None:
            now = start_time if i == 0 else clock()
            remaining = deadline - now
            if remaining <= 0:
                results.append(
                    VerificationResult(
                        command.name,
                        command.argv,
                        124,
                        "",
                        f"verification command {command.name!r} timed out before execution",
                        timed_out=True,
                    )
                )
                continue
            kwargs["timeout"] = remaining
        outcome = runner.run(command.argv, **kwargs)
        returncode = outcome.returncode
        stderr = outcome.stderr
        if outcome.timed_out and returncode == 0:
            returncode = 124
            if not stderr:
                stderr = f"verification command {command.name!r} timed out after {timeout}s"
        results.append(
            VerificationResult(
                command.name,
                command.argv,
                returncode,
                outcome.stdout,
                stderr,
                timed_out=outcome.timed_out,
            )
        )
    return tuple(results)
