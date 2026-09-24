from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Limits:
    wall_time: str
    max_worker_turns: int = 20
    same_blocker_limit: int = 3

    @property
    def wall_time_seconds(self) -> float:
        return parse_duration(self.wall_time)


MODEL_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Config:
    worker_model: str
    critic_model: str | None
    verification: tuple[VerificationCommand, ...]
    limits: Limits = Limits("4h", 20, 3)
    worker_effort: str = "high"
    critic_effort: str = "high"


DEFAULT_CONFIG = '''[worker]
provider = "agy"
model = "gemini-3.8-flash-high"
effort = "high"

[critic]
provider = "codex"
effort = "high"

[[verification.commands]]
name = "tests"
argv = ["python", "-m", "pytest", "-q"]

[limits]
wall_time = "4h"
max_worker_turns = 20
same_blocker_limit = 3
'''


def parse_duration(text: str) -> float:
    stripped = text.strip()
    match = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", stripped, re.IGNORECASE)
    if not match or not any(match.groups()):
        try:
            val = float(stripped)
            if val <= 0:
                raise ConfigError(f"duration must be positive: {text!r}")
            return val
        except ValueError:
            raise ConfigError(f"invalid duration string: {text!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ConfigError(f"duration must be positive: {text!r}")
    return float(total)


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"missing configuration: {path}") from exc
    return parse_config(text)


def parse_config(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    worker = _table(data, "worker")
    critic = _table(data, "critic")
    if worker.get("provider") != "agy" or critic.get("provider") != "codex":
        raise ConfigError("v1 requires worker.provider=agy and critic.provider=codex")
    if worker.get("effort") not in {"low", "medium", "high"} or critic.get("effort") not in {"low", "medium", "high"}:
        raise ConfigError("worker.effort and critic.effort must be low, medium, or high")
    worker_model = _string(worker, "model")
    if worker_model != "fake" and not worker_model.startswith("fake") and not MODEL_SLUG_PATTERN.fullmatch(worker_model):
        raise ConfigError(f"worker.model must be a plain model slug: {worker_model!r}")
    critic_model = critic.get("model")
    if critic_model is not None:
        if not isinstance(critic_model, str) or not critic_model:
            raise ConfigError("critic.model must be a non-empty string")
        if not MODEL_SLUG_PATTERN.fullmatch(critic_model):
            raise ConfigError(f"critic.model must be a plain model slug: {critic_model!r}")
    commands = _table(data, "verification").get("commands", [])
    if not isinstance(commands, list):
        raise ConfigError("verification.commands must be an array of tables")
    verified = tuple(_command(item) for item in commands)
    limits = _table(data, "limits")
    wall_time = _string(limits, "wall_time")
    parse_duration(wall_time)
    for key in ("max_worker_turns", "same_blocker_limit"):
        val = limits.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise ConfigError(f"limits.{key} must be a positive integer")
    parsed_limits = Limits(
        wall_time=wall_time,
        max_worker_turns=limits["max_worker_turns"],
        same_blocker_limit=limits["same_blocker_limit"],
    )
    return Config(
        worker_model,
        critic_model,
        verified,
        limits=parsed_limits,
        worker_effort=worker["effort"],
        critic_effort=critic["effort"],
    )


def init_config(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing configuration: {path}")
    path.write_text(DEFAULT_CONFIG, encoding="utf-8")


def _table(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] table is required")
    return value


def _string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _command(value: object) -> VerificationCommand:
    if not isinstance(value, dict):
        raise ConfigError("verification command must be a table")
    name = _string(value, "name")
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(part, str) or not part for part in argv):
        raise ConfigError(f"verification command {name!r} requires non-empty string argv array")
    return VerificationCommand(name, tuple(argv))
