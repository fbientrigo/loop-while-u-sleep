from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re


class TaskContractError(ValueError):
    pass


@dataclass(frozen=True)
class TaskContract:
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    verification_expectations: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def load_task(path: Path) -> tuple[TaskContract, str]:
    text = path.read_text(encoding="utf-8")
    return parse_task(text), text


def parse_task(text: str) -> TaskContract:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    headings = {"objective", "acceptance criteria", "constraints", "verification"}
    for line in text.splitlines():
        match = re.fullmatch(r"#\s+(.+?)\s*", line)
        if match:
            candidate = match.group(1).strip().lower()
            current = candidate if candidate in headings else None
            if current is not None:
                sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    objective = " ".join(line.strip() for line in sections.get("objective", []) if line.strip())
    criteria = _bullets(sections.get("acceptance criteria", []))
    if not objective:
        raise TaskContractError("task contract requires a non-empty # Objective section")
    if not criteria:
        raise TaskContractError("task contract requires at least one Acceptance Criteria bullet")
    return TaskContract(objective, criteria, _bullets(sections.get("constraints", [])), _bullets(sections.get("verification", [])))


def _bullets(lines: list[str]) -> tuple[str, ...]:
    return tuple(
        text
        for line in lines
        if (match := re.fullmatch(r"\s*-\s+(.+?)\s*", line)) and (text := match.group(1).strip())
    )
