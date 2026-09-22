from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from .config import Config
from .process import ProcessRunner


@dataclass(frozen=True)
class DoctorReport:
    git: str | None
    agy: str | None
    codex: str | None
    worker_model: str
    worker_model_detected: bool
    critic_model: str | None
    critic_model_detected: bool
    codex_read_only: str

    @property
    def safe(self) -> bool:
        return all((self.git, self.agy, self.codex, self.critic_model)) and self.worker_model_detected and self.critic_model_detected and self.codex_read_only == "PROVEN"


def doctor(config: Config, runner: ProcessRunner) -> DoctorReport:
    git = _version("git", runner)
    agy = _version("agy", runner)
    codex = _version("codex", runner)
    agy_models = _models("agy", runner)
    codex_models = _models("codex", runner, "debug", "models")
    worker_detected = config.worker_model in agy_models
    critic_model = config.critic_model or _first_model(codex_models)
    critic_detected = bool(critic_model and critic_model in codex_models)
    if os.name != "posix" or not codex:
        read_only = "UNPROVEN: requires Debian/Linux Codex sandbox smoke test"
    else:
        read_only = _codex_smoke(runner, critic_model)
    return DoctorReport(git, agy, codex, config.worker_model, worker_detected, critic_model, critic_detected, read_only)


def _version(executable: str, runner: ProcessRunner) -> str | None:
    if shutil.which(executable) is None:
        return None
    result = runner.run([executable, "--version"])
    return result.stdout.strip() if result.returncode == 0 else None


def _models(executable: str, runner: ProcessRunner, *suffix: str) -> str:
    if shutil.which(executable) is None:
        return ""
    result = runner.run([executable, *suffix] if suffix else [executable, "models"])
    return result.stdout if result.returncode == 0 else ""


def _first_model(text: str) -> str | None:
    matches = re.findall(r"gpt-[a-zA-Z0-9._-]+", text)
    return matches[0] if matches else None


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


def _codex_smoke(runner: ProcessRunner, model: str | None) -> str:
    with tempfile.TemporaryDirectory(prefix="gauntlet-codex-smoke-") as temporary:
        root = Path(temporary)
        sentinel = root / "gauntlet-write-sentinel"
        manifest_before = _dir_manifest(root)
        argv = ["codex", "exec", "-C", str(root), "-s", "read-only", "-a", "never", "--ephemeral", "--ignore-user-config", "--ignore-rules"]
        if model:
            argv.extend(["-m", model])
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
