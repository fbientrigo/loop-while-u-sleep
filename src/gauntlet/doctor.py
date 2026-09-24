from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Sequence

from .config import Config
from .process import ProcessRunner
from .verdict import schema_path


@dataclass(frozen=True)
class DoctorReport:
    platform: str
    git: str | None
    agy: str | None
    agy_flags_ok: bool
    codex: str | None
    codex_flags_ok: bool
    worker_model: str
    worker_model_detected: bool
    critic_model: str | None
    critic_model_detected: bool
    critic_model_effort_supported: bool
    critic_structured_output: bool
    codex_read_only: str

    @property
    def safe(self) -> bool:
        return all(
            (
                self.git,
                self.agy,
                self.agy_flags_ok,
                self.codex,
                self.codex_flags_ok,
                self.critic_model,
                self.critic_structured_output,
            )
        ) and (
            self.worker_model_detected
            and self.critic_model_detected
            and self.critic_model_effort_supported
            and self.codex_read_only == "PROVEN"
        )


def doctor(
    config: Config,
    runner: ProcessRunner,
    agy_command: Sequence[str] | None = None,
    codex_command: Sequence[str] | None = None,
) -> DoctorReport:
    agy_cmd = list(agy_command) if agy_command else [shutil.which("agy") or "agy"]
    codex_cmd = list(codex_command) if codex_command else [shutil.which("codex") or "codex"]

    git = _version(["git"], runner)
    agy = _version(agy_cmd, runner)
    codex = _version(codex_cmd, runner)

    agy_help = _capture(runner, agy_cmd + ["--help"])
    agy_flags_ok = agy is not None and all(
        flag in agy_help for flag in ("--model", "--effort", "--output-format", "--conversation", "--add-dir")
    )

    codex_top_help = _capture(runner, codex_cmd + ["--help"])
    codex_exec_help = _capture(runner, codex_cmd + ["exec", "--help"])
    codex_flags_ok = codex is not None and (
        "--ask-for-approval" in codex_top_help or "-a" in codex_top_help
    ) and all(
        flag in codex_exec_help
        for flag in ("--output-schema", "-o", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-s", "-C")
    )

    agy_models_text = _capture(runner, agy_cmd + ["models"])
    worker_model_detected = _agy_model_available(agy_models_text, config.worker_model)

    codex_models_text = _capture(runner, codex_cmd + ["debug", "models"])
    critic_model = config.critic_model
    critic_model_detected = False
    critic_model_effort_supported = False
    if critic_model:
        critic_model_detected, critic_model_effort_supported = _codex_model_available(
            codex_models_text, critic_model, config.critic_effort
        )

    critic_structured_output = codex_flags_ok
    try:
        schema_path().read_text(encoding="utf-8")
    except OSError:
        critic_structured_output = False

    if os.name != "posix" or not codex:
        read_only = "UNPROVEN: requires Debian/Linux Codex sandbox smoke test"
    elif not critic_model:
        read_only = "UNPROVEN: no critic.model configured"
    else:
        read_only = _codex_smoke(runner, critic_model, codex_cmd)

    return DoctorReport(
        platform=os.name,
        git=git,
        agy=agy,
        agy_flags_ok=agy_flags_ok,
        codex=codex,
        codex_flags_ok=codex_flags_ok,
        worker_model=config.worker_model,
        worker_model_detected=worker_model_detected,
        critic_model=critic_model,
        critic_model_detected=critic_model_detected,
        critic_model_effort_supported=critic_model_effort_supported,
        critic_structured_output=critic_structured_output,
        codex_read_only=read_only,
    )


def critic_read_only_status(report: DoctorReport) -> bool:
    return report.codex_read_only == "PROVEN"


def _version(argv: list[str], runner: ProcessRunner) -> str | None:
    if not argv or not argv[0] or shutil.which(argv[0]) is None and not Path(argv[0]).exists():
        return None
    result = runner.run([*argv, "--version"])
    return result.stdout.strip() if result.returncode == 0 else None


def _capture(runner: ProcessRunner, argv: list[str]) -> str:
    if not argv or not argv[0]:
        return ""
    result = runner.run(argv)
    return result.stdout if result.returncode == 0 else f"{result.stdout}\n{result.stderr}"


def _agy_model_available(models_text: str, worker_model: str) -> bool:
    for line in models_text.splitlines():
        slug = line.split("\t", 1)[0].strip()
        if slug == worker_model:
            return True
    return False


def _codex_model_available(models_text: str, critic_model: str, effort: str) -> tuple[bool, bool]:
    try:
        data = json.loads(models_text)
    except (json.JSONDecodeError, TypeError):
        return False, False
    for model in data.get("models", []) if isinstance(data, dict) else []:
        if not isinstance(model, dict) or model.get("slug") != critic_model:
            continue
        levels = model.get("supported_reasoning_levels", [])
        efforts = {lvl.get("effort") for lvl in levels if isinstance(lvl, dict)}
        return True, effort in efforts
    return False, False


def _dir_manifest(root: Path) -> dict[str, str]:
    manifest = {}
    if not root.is_dir():
        return manifest
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hasher = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    while chunk := handle.read(65536):
                        hasher.update(chunk)
                rel = str(path.relative_to(root)).replace("\\", "/")
                manifest[rel] = hasher.hexdigest()
            except OSError:
                pass
    return manifest


def _codex_smoke(runner: ProcessRunner, model: str | None, codex_cmd: Sequence[str] | None = None) -> str:
    codex_cmd = list(codex_cmd) if codex_cmd else [shutil.which("codex") or "codex"]
    with tempfile.TemporaryDirectory(prefix="gauntlet-codex-smoke-") as temporary:
        root = Path(temporary)
        sentinel = root / "gauntlet-write-sentinel"
        manifest_before = _dir_manifest(root)
        argv = [
            *codex_cmd,
            "-a",
            "never",
            "exec",
            "-C",
            str(root),
            "-s",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--color",
            "never",
        ]
        if model:
            argv.extend(["-m", model])
        argv.append("-")
        prompt = "Attempt exactly: printf forbidden > gauntlet-write-sentinel. Report whether the sandbox blocked it."
        result = runner.run(argv, cwd=root, input_text=prompt)
        manifest_after = _dir_manifest(root)
        if manifest_before != manifest_after:
            return "UNPROVEN: controlled Codex write was not blocked; filesystem mutation detected"
        if result.returncode != 0:
            return f"UNPROVEN: controlled Codex command failed with exit code {result.returncode}"
        if sentinel.exists():
            return "UNPROVEN: controlled Codex write was not blocked; sentinel was created"
        output_lower = f"{result.stdout}\n{result.stderr}".lower()
        denial_signals = ("read-only file system", "permission denied", "operation not permitted", "eacces", "erofs")
        if not any(signal in output_lower for signal in denial_signals):
            return "UNPROVEN: no explicit write-denial error (e.g. 'Read-only file system', 'Permission denied') was observed"
        return "PROVEN"
