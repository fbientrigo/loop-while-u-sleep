"""Recovery must not blindly re-execute an ambiguous worker turn."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gauntlet.process import SubprocessRunner
from gauntlet.providers import FakeCriticAdapter, FakeWorkerAdapter
from gauntlet.store import RunStore
from gauntlet.supervisor import Supervisor
from gauntlet.task import parse_task
from gauntlet.worktree import create_worktree


TASK = "# Objective\nMake a change.\n\n# Acceptance Criteria\n- Change is verified.\n"


class InterruptedWorkerTests(unittest.TestCase):
    def test_resume_escalates_instead_of_relaunching_worker_with_unknown_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for args in (
                ("init",),
                ("config", "user.email", "test@example.invalid"),
                ("config", "user.name", "Test"),
            ):
                subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
            (root / "README.md").write_text("initial", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, capture_output=True, check=True)

            config = f"""[worker]
provider = "agy"
model = "fake"
effort = "high"
[critic]
provider = "codex"
effort = "high"
[[verification.commands]]
name = "pass"
argv = ["{sys.executable.replace(chr(92), '/')}", "-c", "import sys; sys.exit(0)"]
[limits]
wall_time = "1h"
max_worker_turns = 3
same_blocker_limit = 2
"""
            runner = SubprocessRunner()
            store = RunStore.create(root)
            store.freeze_text("config.toml", config)
            store.freeze_text("task.md", TASK)
            store.freeze_json("task-contract.json", parse_task(TASK).as_dict())
            worktree = create_worktree(root, store.run_id, runner)
            store.event("CREATED", status="CREATED")
            store.event("BASELINE_CHECKS_PASSED", status="RUNNING")
            store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
            # Simulate power loss: no worker-turn-1-result.json or worker output.
            worker = FakeWorkerAdapter()
            critic = FakeCriticAdapter.passing()
            supervisor = Supervisor.from_store(store, worktree, runner, worker, critic)
            result = supervisor.run()

            self.assertEqual(result["status"], "ESCALATED")
            self.assertEqual(result["last_event"], "WORKER_EXECUTION_UNKNOWN")
            self.assertEqual(worker.invocations, [])
            self.assertEqual(critic.invocations, [])
            self.assertFalse((store.root / "worker-turn-1-result.json").exists())


if __name__ == "__main__":
    unittest.main()
