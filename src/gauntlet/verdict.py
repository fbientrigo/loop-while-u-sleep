from __future__ import annotations

import json
from pathlib import Path


class VerdictError(ValueError):
    pass


def validate_verdict(raw: str | bytes | dict) -> dict:
    try:
        verdict = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerdictError("critic verdict is not valid JSON") from exc
    if not isinstance(verdict, dict):
        raise VerdictError("critic verdict must be a dictionary")
    allowed_keys = {"verdict", "blocking_findings", "human_decision_required"}
    required_keys = {"verdict", "blocking_findings"}
    if not (required_keys <= set(verdict) <= allowed_keys):
        raise VerdictError("critic verdict must contain only verdict and blocking_findings")
    decision = verdict["verdict"]
    findings = verdict["blocking_findings"]
    if decision not in {"PASS", "BLOCK"} or not isinstance(findings, list):
        raise VerdictError("invalid critic verdict shape")
    if "human_decision_required" in verdict and not isinstance(verdict["human_decision_required"], bool):
        raise VerdictError("human_decision_required must be a boolean")
    if decision == "PASS" and findings:
        raise VerdictError("PASS requires no blocking findings")
    if decision == "BLOCK" and not findings and not verdict.get("human_decision_required"):
        raise VerdictError("BLOCK requires at least one blocking finding")
    fields = {"id", "severity", "claim", "evidence", "location", "required_condition"}
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != fields:
            raise VerdictError("invalid blocking finding shape")
        if finding["severity"] not in {"critical", "major"} or any(not isinstance(finding[key], str) or not finding[key] for key in fields - {"severity"}):
            raise VerdictError("invalid blocking finding values")
    return verdict


def schema_path() -> Path:
    return Path(__file__).with_name("schemas") / "critic.json"
