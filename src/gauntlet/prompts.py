from __future__ import annotations

from typing import Any, Sequence

from .task import TaskContract
from .verification import VerificationResult

MAX_PROMPT_BYTES = 100_000
_TRUNCATE_NOTE = "\n... [truncated] ...\n"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max(0, (max_chars - len(_TRUNCATE_NOTE)) // 2)
    return f"{text[:keep]}{_TRUNCATE_NOTE}{text[-keep:]}"


def _fit(sections: list[str], max_bytes: int = MAX_PROMPT_BYTES) -> str:
    """Join sections, then truncate the whole prompt to a UTF-8 byte budget."""
    text = "\n\n".join(sections)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Truncate by characters as a conservative proxy for bytes, then re-check.
    ratio = max_bytes / len(encoded)
    approx_chars = max(0, int(len(text) * ratio))
    truncated = _truncate(text, approx_chars)
    while len(truncated.encode("utf-8")) > max_bytes:
        approx_chars = int(approx_chars * 0.9)
        truncated = _truncate(text, approx_chars)
    return truncated


def _task_section(task: TaskContract) -> str:
    lines = ["# Frozen task", "", "## Objective", task.objective, "", "## Acceptance criteria"]
    lines.extend(f"- {c}" for c in task.acceptance_criteria)
    if task.constraints:
        lines.append("")
        lines.append("## Constraints")
        lines.extend(f"- {c}" for c in task.constraints)
    return "\n".join(lines)


def _check_evidence_section(check_evidence: Sequence[VerificationResult]) -> str | None:
    if not check_evidence:
        return None
    lines = ["# Current deterministic verification failures"]
    for result in check_evidence:
        lines.append(f"\n## {result.name} (`{' '.join(result.argv)}`)")
        lines.append(f"exit code: {result.returncode}")
        lines.append(f"stdout:\n{_truncate(result.stdout, 4000)}")
        lines.append(f"stderr:\n{_truncate(result.stderr, 4000)}")
    return "\n".join(lines)


def _findings_section(findings: Sequence[dict[str, Any]]) -> str | None:
    if not findings:
        return None
    lines = [
        "# Critic BLOCK findings (CANDIDATE blockers, not proven)",
        "",
        "Each finding below is a CLAIM from an independent read-only reviewer, not an "
        "established fact. For every finding:",
        "1. Reproduce/verify it against the actual repository state.",
        "2. If valid, fix it.",
        "3. If invalid, produce concrete evidence (command output, code excerpt) showing "
        "why it does not hold; do not just assert disagreement.",
        "4. Do not perform unrelated improvements while addressing these findings.",
        "5. Run the relevant checks after your changes.",
        "",
    ]
    for finding in findings:
        lines.append(f"## Finding {finding.get('id', '?')} ({finding.get('severity', '?')})")
        lines.append(f"claim: {finding.get('claim', '')}")
        lines.append(f"evidence: {finding.get('evidence', '')}")
        lines.append(f"location: {finding.get('location', '')}")
        lines.append(f"required_condition: {finding.get('required_condition', '')}")
        lines.append("")
    return "\n".join(lines).rstrip()


WORKER_CONTRACT = """# Contract

You are a coding worker operating inside an isolated Git worktree under an external
supervisor. The supervisor alone decides when this task is complete; you have no
authority to declare it done, finished, PASS, or DONE. Any such claim in your output is
ignored by the supervisor.

Rules:
- Work only within the current working directory (the run worktree).
- Never run `git commit`, `git push`, `git merge`, `git rebase`, `git reset`, or any
  remote/history-mutating Git command. Never change Git remotes or credentials.
- Address the objective, acceptance criteria, constraints, and (if present) the current
  verification failures and critic findings below.
- Avoid unrelated improvements or speculative refactors outside the task scope.
- Run the relevant checks yourself when practical before finishing your turn."""


CRITIC_CONTRACT = """# Contract

You are an independent, read-only critic reviewing a candidate completion of the frozen
task below. You have a fresh context: you have not seen any worker self-report, prior
critic output, attempt count, or completion claim, and none of that exists for you to
consider.

Your job is to try to DISPROVE completion, not to improve style. Produce a `BLOCK`
finding only when grounded in a concrete:
- acceptance criterion violation,
- correctness defect or regression,
- meaningful missing defect-hiding test,
- relevant security/safety defect,
- required UX failure, or
- materially harmful unnecessary complexity introduced by this task.

Do NOT block on: optional refactors, style preferences, speculative architecture, or
unrequested features.

You have no filesystem write access and must not attempt to modify any file. Output must
conform exactly to the required JSON verdict schema: `{"verdict":"PASS"|"BLOCK",
"blocking_findings":[...]}`. PASS requires an empty `blocking_findings` array. BLOCK
requires at least one finding with `id`, `severity` (`critical`|`major`), `claim`,
`evidence`, `location`, `required_condition`. Output only the JSON verdict, nothing else."""


def build_worker_prompt(
    task: TaskContract,
    findings: Sequence[dict[str, Any]] = (),
    check_evidence: Sequence[VerificationResult] = (),
) -> str:
    sections = [WORKER_CONTRACT, _task_section(task)]
    check_section = _check_evidence_section(check_evidence)
    if check_section:
        sections.append(check_section)
    findings_section = _findings_section(findings)
    if findings_section:
        sections.append(findings_section)
    return _fit(sections)


def build_critic_prompt(
    task: TaskContract,
    check_results: Sequence[VerificationResult] = (),
    repo_state: str = "",
) -> str:
    sections = [CRITIC_CONTRACT, _task_section(task)]
    if check_results:
        lines = ["# Deterministic verification results"]
        for result in check_results:
            lines.append(f"\n## {result.name} (`{' '.join(result.argv)}`)")
            lines.append(f"exit code: {result.returncode}")
            lines.append(f"stdout:\n{_truncate(result.stdout, 4000)}")
            lines.append(f"stderr:\n{_truncate(result.stderr, 4000)}")
        sections.append("\n".join(lines))
    if repo_state:
        sections.append(f"# Repository state (diff against baseline)\n\n{_truncate(repo_state, 60_000)}")
    return _fit(sections)
