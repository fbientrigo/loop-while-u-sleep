from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


class StoreError(ValueError):
    pass


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class RunStore:
    def __init__(self, root: Path, run_id: str):
        if not isinstance(run_id, str) or not run_id or not RUN_ID_PATTERN.fullmatch(run_id) or ".." in run_id:
            raise StoreError(f"invalid run ID: {run_id!r}")
        runs_dir = (root / ".gauntlet" / "runs").resolve()
        target = (runs_dir / run_id).resolve()
        try:
            target.relative_to(runs_dir)
        except ValueError:
            raise StoreError(f"run ID path escapes runs directory: {run_id!r}")
        self.root = root / ".gauntlet" / "runs" / run_id
        self.run_id = run_id

    @classmethod
    def create(cls, root: Path) -> "RunStore":
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        store = cls(root, run_id)
        store.root.mkdir(parents=True, exist_ok=False)
        return store

    def freeze_text(self, name: str, text: str) -> None:
        self._write(name, text.encode("utf-8"), overwrite=False)

    def freeze_json(self, name: str, value: object) -> None:
        self._write(name, _json(value), overwrite=False)

    def event(self, kind: str, *, status: str, detail: dict | None = None) -> dict:
        record = {"at": datetime.now(UTC).isoformat(), "kind": kind, "status": status, "detail": detail or {}}
        events = self.events()
        events.append(record)
        self._write("events.jsonl", b"".join(_event_json(event) + b"\n" for event in events), overwrite=True)
        self._write("status.json", _json(self.status()), overwrite=True)
        return record

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def status(self) -> dict:
        events = self.events()
        if not events:
            return {"run_id": self.run_id, "status": "UNKNOWN", "event_count": 0}
        latest = events[-1]
        return {"run_id": self.run_id, "status": latest["status"], "event_count": len(events), "last_event": latest["kind"]}

    def _write(self, name: str, data: bytes, *, overwrite: bool) -> None:
        target = self.root / name
        if target.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite run artifact: {target}")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2).encode("utf-8")


def _event_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
