"""Small file helpers shared by run stores and campaigns."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

import psutil


def replace_with_retry(source: str | os.PathLike, target: str | os.PathLike, *, attempts: int = 40, delay: float = 0.05) -> None:
    """os.replace that tolerates a reader briefly holding the target open (Windows WinError 5/32)."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


class LockHeld(RuntimeError):
    pass


def _owner_alive(path: Path) -> bool:
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        pid, created = int(owner["pid"]), float(owner["create_time"])
    except (OSError, ValueError, KeyError, TypeError):
        # Unreadable usually means the owner is between creating the file and writing it.
        try:
            return time.time() - path.stat().st_mtime < 5
        except OSError:
            return False
    try:
        return abs(psutil.Process(pid).create_time() - created) < 0.01
    except psutil.Error:
        return False


@contextmanager
def exclusive_lock(path: Path, what: str = "resource"):
    """Cross-process lock file. A lock whose owning process is gone is taken over.

    Taking over a stale lock is not atomic: two processes that both find the same dead owner at the same
    instant could both proceed. The window is a few microseconds and needs a prior crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _owner_alive(path):
                raise LockHeld(f"{what} is already in use by another gauntlet process ({path})") from None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    else:
        raise LockHeld(f"could not take {what} lock {path}")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "create_time": psutil.Process().create_time()}, handle)
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line and fsync. A crash can leave only a partial last line, which readers skip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("ab") as handle:
        if handle.tell() > 0:
            with path.open("rb") as probe:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    handle.write(b"\n")  # a crash left a torn last line; keep the new record on its own line
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict]:
    """Read JSON lines, skipping blank and unparseable (partial) lines."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:] if limit else records
