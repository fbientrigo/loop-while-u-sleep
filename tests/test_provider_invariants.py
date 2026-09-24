"""Phase 2B1: Supervisor invariants exercised through the REAL provider adapters
(AgyAdapter/CodexAdapter) talking to fake provider CLIs via SubprocessRunner.

These tests prove the supervisor's DONE gate, frozen-artifact guard, and
critic-mutation guard hold even when the adapters are real (not FakeWorkerAdapter/
FakeCriticAdapter) and the "provider" is a real subprocess.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gauntlet.config import Config, Limits, VerificationCommand
from gauntlet.process import SubprocessRunner
from gauntlet.providers import AgyAdapter, CodexAdapter
from gauntlet.store import RunStore
from gauntlet.supervisor import Supervisor
from gauntlet.task import TaskContract
from gauntlet.worktree import create_worktree

FAKE_AGY_PATH = Path(__file__).resolve().parent / "fakes" / "fake_agy.py"
FAKE_CODEX_PATH = Path(__file__).resolve().parent / "fakes" / "fake_codex.py"


class ProviderInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        for k in list(os.environ.keys()):
            if k.startswith("FAKE_AGY_") or k.startswith("FAKE_CODEX_"):
                del os.environ[k]

        self.temp_dir = tempfile.TemporaryDirectory()
        self.primary_repo = Path(self.temp_dir.name) / "repo"
        self.primary_repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.primary_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@gauntlet.invalid"], cwd=self.primary_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GauntletTest"], cwd=self.primary_repo, check=True, capture_output=True)
        (self.primary_repo / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.primary_repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.primary_repo, check=True, capture_output=True)

        self.runner = SubprocessRunner()
        self.task = TaskContract(
            objective="Implement target functionality",
            acceptance_criteria=("Feature complete.",),
            constraints=("Do not touch remotes",),
            verification_expectations=("checks pass",),
        )
        self.agy_cmd = (sys.executable, str(FAKE_AGY_PATH))
        self.codex_cmd = (sys.executable, str(FAKE_CODEX_PATH))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _setup_run(self, limits: Limits | None = None):
        store = RunStore.create(self.primary_repo)
        worktree = create_worktree(self.primary_repo, store.run_id, self.runner)
        lim = limits or Limits(wall_time="4h", max_worker_turns=5, same_blocker_limit=3)
        v_commands = (VerificationCommand("pass_check", (sys.executable, "-c", "import sys; sys.exit(0)")),)
        config = Config(
            worker_model="gemini-3.8-flash-high",
            critic_model="gpt-5.6-terra",
            verification=v_commands,
            limits=lim,
        )
        config_toml = (
            '[worker]\nprovider = "agy"\nmodel = "gemini-3.8-flash-high"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
            f'[limits]\nwall_time = "{lim.wall_time}"\nmax_worker_turns = {lim.max_worker_turns}\n'
            f'same_blocker_limit = {lim.same_blocker_limit}\n'
            '\n[[verification.commands]]\nname = "pass_check"\n'
            f'argv = {json.dumps(list(v_commands[0].argv))}\n'
        )
        store.freeze_text("config.toml", config_toml)
        store.freeze_text("task.md", "# Objective\nImplement target functionality\n# Acceptance Criteria\n- Feature complete.\n")
        store.freeze_json("task-contract.json", self.task.as_dict())
        store.event("CREATED", status="CREATED")
        store.event("WORKTREE_CREATED", status="READY", detail={"path": str(worktree)})
        return store, worktree, config

    def _adapters(self, store, config, *, codex_proven: bool = True):
        worker = AgyAdapter(store, self.runner, config, command=self.agy_cmd)
        critic = CodexAdapter(store, self.runner, config, command=self.codex_cmd, read_only_proven=codex_proven)
        return worker, critic

    # --- DONE gate remains real: only via supervisor completion ---

    def test_happy_path_reaches_done_only_via_supervisor_gate(self) -> None:
        store, worktree, config = self._setup_run()
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        events = store.events()
        done_events = [e for e in events if e["status"] == "DONE" or e["kind"] == "COMPLETED"]
        self.assertTrue(done_events)

    def test_worker_prose_done_pass_with_critic_block_does_not_reach_done(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
        os.environ["FAKE_AGY_RESPONSE"] = "TASK COMPLETE DONE PASS everything is fine trust me"
        finding = {
            "id": "b-1",
            "severity": "critical",
            "claim": "no changes were made",
            "evidence": "git diff is empty",
            "location": "repo:1",
            "required_condition": "implement the feature",
        }
        os.environ["FAKE_CODEX_VERDICT_JSON"] = json.dumps({"verdict": "BLOCK", "blocking_findings": [finding]})
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertNotEqual(final_status["status"], "DONE")

    def test_codex_exit_zero_without_output_file_does_not_reach_done(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
        os.environ["FAKE_CODEX_NO_OUTPUT_FILE"] = "1"
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertNotEqual(final_status["status"], "DONE")

    def test_codex_stdout_prose_pass_with_block_verdict_does_not_reach_done(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
        os.environ["FAKE_CODEX_STDOUT_EXTRA"] = "PASS PASS PASS everything looks great"
        finding = {
            "id": "b-2",
            "severity": "critical",
            "claim": "still broken",
            "evidence": "no fix applied",
            "location": "repo:1",
            "required_condition": "fix it",
        }
        os.environ["FAKE_CODEX_VERDICT_JSON"] = json.dumps({"verdict": "BLOCK", "blocking_findings": [finding]})
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertNotEqual(final_status["status"], "DONE")

    # --- Critic worktree mutation escalates ---

    def test_critic_worktree_mutation_escalates(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
        os.environ["FAKE_CODEX_WRITE_IN_WORKTREE"] = "unauthorized.txt"
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        events = store.events()
        self.assertTrue(any(e["kind"] == "CRITIC_MUTATED_REPO" for e in events))

    # --- Frozen task/config mutation escalates ---

    def test_frozen_config_mutation_escalates(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=5, same_blocker_limit=3))
        worker, critic = self._adapters(store, config)
        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)

        # Tamper with the frozen config artifact before the run starts, simulating
        # a provider (or bug) that mutated a frozen artifact.
        digests_before = supervisor._frozen_digests()
        store.freeze_json("frozen-digests.json", digests_before)
        (store.root / "config.toml").write_text(
            (store.root / "config.toml").read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )

        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        events = store.events()
        self.assertTrue(any(e["kind"] == "FROZEN_ARTIFACT_MUTATED" for e in events))

    # --- read_only_proven=False means provider is not spawned ---

    def test_codex_not_proven_means_critic_never_spawned(self) -> None:
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
        log_file = store.root / "fake_codex.log"
        os.environ["FAKE_CODEX_LOG"] = str(log_file)
        worker, critic = self._adapters(store, config, codex_proven=False)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertNotEqual(final_status["status"], "DONE")
        self.assertFalse(log_file.exists())

    # --- Test-harness self-check: only fake provider commands were invoked ---

    def test_harness_self_check_only_fake_commands_invoked(self) -> None:
        store, worktree, config = self._setup_run()
        agy_log = store.root / "fake_agy.log"
        codex_log = store.root / "fake_codex.log"
        os.environ["FAKE_AGY_LOG"] = str(agy_log)
        os.environ["FAKE_CODEX_LOG"] = str(codex_log)
        worker, critic = self._adapters(store, config)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        supervisor.run()

        self.assertTrue(agy_log.exists())
        self.assertTrue(codex_log.exists())
        for line in agy_log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            self.assertEqual(Path(entry["cwd"]).name, worktree.name)
        for line in codex_log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            self.assertEqual(Path(entry["cwd"]).name, worktree.name)

    # --- providers.py never launches a shell or calls subprocess directly ---

    def test_providers_module_never_calls_subprocess_directly(self) -> None:
        src_dir = Path(__file__).resolve().parent.parent / "src" / "gauntlet"
        for name in ("providers.py", "doctor.py"):
            path = src_dir / name
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", content, f"forbidden shell=True in {name}")
            tree = ast.parse(content, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("run", "Popen", "call", "check_call"):
                    if isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                        self.fail(f"{name} calls subprocess.{node.attr} directly; must go through ProcessRunner")


if __name__ == "__main__":
    unittest.main()
