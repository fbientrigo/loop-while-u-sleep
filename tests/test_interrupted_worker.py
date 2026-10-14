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
from gauntlet.supervisor import Supervisor, classify_provider_failure, reconcile_run
from gauntlet.task import parse_task
from gauntlet.worktree import create_worktree


TASK = "# Objective\nMake a change.\n\n# Acceptance Criteria\n- Change is verified.\n"


CONFIG = """[worker]
provider = "agy"
model = "fake"
effort = "high"
[critic]
provider = "codex"
effort = "high"
[[verification.commands]]
name = "pass"
argv = ["{python}", "-c", "import sys; sys.exit(0)"]
[limits]
wall_time = "1h"
max_worker_turns = 3
same_blocker_limit = 2
"""


class InterruptedWorkerTests(unittest.TestCase):
    def _unknown_run(self):
        """A run whose process died right after WORKER_STARTED and was then resumed once."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repo"
        root.mkdir()
        for args in (("init",), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
        (root / "README.md").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, capture_output=True, check=True)
        runner = SubprocessRunner()
        store = RunStore.create(root)
        store.freeze_text("config.toml", CONFIG.format(python=sys.executable.replace(chr(92), "/")))
        store.freeze_text("task.md", TASK)
        store.freeze_json("task-contract.json", parse_task(TASK).as_dict())
        worktree = create_worktree(root, store.run_id, runner)
        store.event("CREATED", status="CREATED")
        store.event("BASELINE_CHECKS_PASSED", status="RUNNING")
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        worker = FakeWorkerAdapter()
        result = Supervisor.from_store(store, worktree, runner, worker, FakeCriticAdapter.passing()).run()
        return store, worktree, runner, worker, result

    def test_resume_escalates_instead_of_relaunching_worker_with_unknown_result(self):
        store, _worktree, _runner, worker, result = self._unknown_run()
        self.assertEqual(result["status"], "ESCALATED")
        self.assertEqual(result["last_event"], "WORKER_EXECUTION_UNKNOWN")
        self.assertEqual(worker.invocations, [])
        self.assertFalse((store.root / "worker-turn-1-result.json").exists())

    def test_accept_then_resume_still_needs_checks_and_critic_pass(self):
        store, worktree, runner, worker, _ = self._unknown_run()
        status = reconcile_run(store, worktree, runner, accept=True, note="looked at the diff, fine")
        self.assertEqual(status["status"], "RUNNING")
        self.assertTrue((store.root / "reconcile-turn-1.json").exists())
        critic = FakeCriticAdapter.passing()
        final = Supervisor.from_store(store, worktree, runner, worker, critic).run()
        self.assertEqual(final["status"], "DONE")
        self.assertEqual(len(critic.invocations), 1)
        self.assertEqual(worker.invocations, [])

    def test_accept_with_blocking_critic_never_reaches_done(self):
        store, worktree, runner, worker, _ = self._unknown_run()
        reconcile_run(store, worktree, runner, accept=True, note="try it")
        critic = FakeCriticAdapter.blocking([{"id": "x", "title": "bad", "evidence": "e", "required_fix": "f"}], human_decision=True)
        final = Supervisor.from_store(store, worktree, runner, worker, critic).run()
        self.assertNotEqual(final["status"], "DONE")

    def test_abandon_is_terminal_and_resume_is_refused_by_status(self):
        store, worktree, runner, _worker, _ = self._unknown_run()
        status = reconcile_run(store, worktree, runner, accept=False, note="discard")
        self.assertEqual(status["status"], "BLOCKED")
        self.assertEqual(status["last_event"], "RUN_ABANDONED")

    def test_reconcile_refused_when_nothing_is_unknown_or_note_missing(self):
        store, worktree, runner, _worker, _ = self._unknown_run()
        with self.assertRaises(ValueError):
            reconcile_run(store, worktree, runner, accept=True, note="  ")
        reconcile_run(store, worktree, runner, accept=False, note="discard")
        with self.assertRaises(ValueError):
            reconcile_run(store, worktree, runner, accept=True, note="again")

    def test_status_reports_supervisor_heartbeat(self):
        import os
        from datetime import datetime

        from gauntlet.cli import _status_with_heartbeat

        store, _worktree, _runner, _worker, _ = self._unknown_run()
        self.assertNotIn("supervisor", _status_with_heartbeat(store))
        store.write_json("alive.json", {"at": datetime.now().astimezone().isoformat(), "pid": os.getpid()})
        info = _status_with_heartbeat(store)["supervisor"]
        self.assertTrue(info["pid_alive"])
        self.assertLess(info["heartbeat_age_seconds"], 60)

    def test_provider_failure_reasons(self):
        self.assertEqual(classify_provider_failure("RESOURCE_EXHAUSTED: 429"), "provider_quota")
        self.assertEqual(classify_provider_failure("", "You have hit your usage limit"), "provider_quota")
        self.assertEqual(classify_provider_failure("UNAUTHENTICATED"), "provider_auth")
        self.assertIsNone(classify_provider_failure("segfault"))


if __name__ == "__main__":
    unittest.main()
