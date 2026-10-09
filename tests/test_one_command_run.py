"""Integration tests for the opt-in one-command run path."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gauntlet.cli import main
from gauntlet.config import init_config
from gauntlet.store import RunStore


TASK = """# Objective
Run a bounded task.

# Acceptance Criteria
- Deterministic verification completes.
"""


def make_repo(root: Path) -> None:
    root.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "Gauntlet Tests"),
        ("config", "user.email", "gauntlet@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, capture_output=True, check=True)


def configure(root: Path, *, worker_model: str, exit_code: int) -> Path:
    task = root / "TASK.md"
    task.write_text(TASK, encoding="utf-8")
    python_path = json.dumps(sys.executable.replace("\\", "/"))
    config = f"""[worker]
provider = "agy"
model = "{worker_model}"
effort = "high"

[critic]
provider = "codex"
effort = "high"

[[verification.commands]]
name = "gate"
argv = [{python_path}, "-c", "import sys; sys.exit({exit_code})"]

[limits]
wall_time = "1h"
max_worker_turns = 2
same_blocker_limit = 2
"""
    (root / "gauntlet.toml").write_text(config, encoding="utf-8")
    return task


class OneCommandRunTests(unittest.TestCase):
    def test_execute_runs_supervisor_to_completion_with_fakes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            make_repo(root)
            task = configure(root, worker_model="fake", exit_code=0)
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                result = main(["run", "--task", str(task), "--execute"])
            self.assertEqual(result, 0)
            runs = list((root / ".gauntlet" / "runs").iterdir())
            self.assertEqual(len(runs), 1)
            store = RunStore(root, runs[0].name)
            self.assertEqual(store.status()["status"], "DONE")
            events = [event["kind"] for event in store.events()]
            self.assertIn("WORKER_FINISHED", events)
            self.assertIn("CRITIC_PASSED", events)
            self.assertIn("FINAL_CHECKS_PASSED", events)

    def test_execute_does_not_bypass_failing_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            make_repo(root)
            task = configure(root, worker_model="fake", exit_code=1)
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                result = main(["run", "--task", str(task), "--execute"])
            self.assertEqual(result, 1)
            runs = list((root / ".gauntlet" / "runs").iterdir())
            self.assertEqual(len(runs), 1)
            store = RunStore(root, runs[0].name)
            self.assertEqual(store.status()["status"], "BLOCKED")
            self.assertNotIn("WORKER_FINISHED", [event["kind"] for event in store.events()])

    def test_live_execute_without_critic_model_rejects_before_creating_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            make_repo(root)
            init_config(root / "gauntlet.toml")
            task = root / "TASK.md"
            task.write_text(TASK, encoding="utf-8")
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                result = main(["run", "--task", str(task), "--execute"])
            self.assertEqual(result, 2)
            self.assertFalse((root / ".gauntlet" / "runs").exists())


if __name__ == "__main__":
    unittest.main()
