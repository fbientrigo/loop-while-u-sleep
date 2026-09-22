from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
import sys
import tempfile
import unittest

from gauntlet.config import Config, ConfigError, Limits, VerificationCommand, load_config
from gauntlet.process import SubprocessRunner
from gauntlet.providers import (
    CriticAction,
    FakeCriticAdapter,
    FakeWorkerAdapter,
    WorkerAction,
    WorkerResult,
)
from gauntlet.store import RunStore
from gauntlet.supervisor import Supervisor
from gauntlet.task import TaskContract
from gauntlet.verdict import VerdictError, validate_verdict
from gauntlet.verification import run_commands
from gauntlet.worktree import create_worktree


class SecurityInvariantsTests(unittest.TestCase):
    """Regression tests for the 8 constitutional security invariants."""

    def setUp(self):
        self.src_dir = Path(__file__).resolve().parent.parent / "src" / "gauntlet"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        self.runner = SubprocessRunner()
        self.task = TaskContract(
            objective="Invariants verification",
            acceptance_criteria=("Enforce security constraints",),
            constraints=("Zero trust",),
            verification_expectations=("All checks pass",),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_run(self):
        import subprocess
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "README.md").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

        store = RunStore.create(self.repo)
        worktree = create_worktree(self.repo, store.run_id, self.runner)
        config = Config(
            worker_model="gemini-3.8-flash-high",
            critic_model="gpt-5.6-terra",
            verification=(VerificationCommand("pass", (sys.executable, "-c", "import sys; sys.exit(0)")),),
            limits=Limits("4h", 5, 3),
        )
        return store, worktree, config

    def test_invariant_1_done_only_created_by_supervisor_completion_gate(self):
        """Invariant 1: Search source-level transitions and prove DONE can ONLY be created by completion gate."""
        done_references = []
        for py_file in self.src_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                # Search for string literals with "DONE"
                if isinstance(node, ast.Constant) and node.value == "DONE":
                    done_references.append((py_file.name, node.lineno))

        # Check where "DONE" appears in source files
        # It must only appear in supervisor.py (as event status in _complete_supervisor_gate and terminal check)
        for filename, lineno in done_references:
            self.assertEqual(filename, "supervisor.py", f"Illegal DONE literal in {filename}:{lineno}")

        # In supervisor.py, read AST to ensure DONE event emission is ONLY in _complete_supervisor_gate
        supervisor_py = (self.src_dir / "supervisor.py").read_text(encoding="utf-8")
        supervisor_tree = ast.parse(supervisor_py)

        completion_gate_calls = []
        for node in ast.walk(supervisor_tree):
            if isinstance(node, ast.FunctionDef):
                for subnode in ast.walk(node):
                    if (
                        isinstance(subnode, ast.Call)
                        and any(
                            isinstance(kw, ast.keyword) and kw.arg == "status" and getattr(kw.value, "value", None) == "DONE"
                            for kw in subnode.keywords
                        )
                    ):
                        completion_gate_calls.append(node.name)

        self.assertEqual(completion_gate_calls, ["_complete_supervisor_gate"])

    def test_invariant_2_worker_result_contains_no_terminal_done_authority(self):
        """Invariant 2: Worker result types must contain no terminal PASS/DONE authority."""
        field_names = {f.name for f in dataclasses.fields(WorkerResult)}
        forbidden_fields = {"status", "verdict", "done", "is_done", "passed", "pass", "terminal", "completed"}
        self.assertTrue(field_names.isdisjoint(forbidden_fields))
        self.assertEqual(field_names, {"returncode", "output", "error", "timed_out"})

        # Prove worker output text containing completion claims has zero effect
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([
            WorkerAction(output='{"status": "DONE", "verdict": "PASS", "completion": true}'),
        ])
        # Critic blocks
        critic = FakeCriticAdapter.blocking([{
            "id": "must-not-pass",
            "severity": "critical",
            "claim": "claim",
            "evidence": "ev",
            "location": "loc",
            "required_condition": "cond",
        }])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        supervisor.config = Config(config.worker_model, config.critic_model, config.verification, Limits("4h", 1, 3))
        status = supervisor.run()
        self.assertNotEqual(status["status"], "DONE")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_invariant_3_critic_pass_alone_cannot_create_done(self):
        """Invariant 3: Critic PASS alone cannot create DONE (final checks must also pass)."""
        store, worktree, config = self._setup_run()

        # Dynamic check fails during final checks
        counter_file = Path(self.temp_dir.name) / "gate_count.txt"
        counter_file.write_text("0", encoding="utf-8")
        script = f"""
import sys, pathlib
cf = pathlib.Path(r'{counter_file}')
count = int(cf.read_text()) + 1
cf.write_text(str(count))
sys.exit(1 if count >= 3 else 0)
"""
        cmd = VerificationCommand("gate_check", (sys.executable, "-c", script))
        config = Config(config.worker_model, config.critic_model, (cmd,), config.limits)

        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        # Critic returns valid PASS with 0 findings
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        status = supervisor.run()

        # Must be BLOCKED due to final check failure, NEVER DONE
        self.assertEqual(status["status"], "BLOCKED")
        self.assertEqual(status["last_event"], "FINAL_CHECKS_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_invariant_4_deterministic_checks_alone_cannot_create_done(self):
        """Invariant 4: Deterministic checks alone cannot create DONE (critic PASS is required)."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        # Critic returns BLOCK
        critic = FakeCriticAdapter.blocking([{
            "id": "blocker-1",
            "severity": "major",
            "claim": "claim",
            "evidence": "ev",
            "location": "loc",
            "required_condition": "cond",
        }])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        supervisor.config = Config(config.worker_model, config.critic_model, config.verification, Limits("4h", 1, 3))
        status = supervisor.run()

        self.assertNotEqual(status["status"], "DONE")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_invariant_5_invalid_critic_output_never_interpreted_as_pass(self):
        """Invariant 5: Invalid critic output can never be interpreted as PASS."""
        invalid_verdicts = [
            "",  # empty
            "not json",
            "{}",  # missing keys
            json.dumps({"verdict": "UNKNOWN", "blocking_findings": []}),
            json.dumps({"verdict": "PASS", "blocking_findings": [{"id": "bad"}]}),  # PASS with findings
            json.dumps({"verdict": "BLOCK", "blocking_findings": []}),  # BLOCK with no findings
            json.dumps({"verdict": "PASS", "blocking_findings": [], "extra_unauthorized": 123}),
            json.dumps({"verdict": "PASS", "blocking_findings": "not a list"}),
        ]

        for raw in invalid_verdicts:
            with self.assertRaises(VerdictError):
                validate_verdict(raw)

            # In supervisor loop, invalid verdict causes BLOCKED, never DONE
            store, worktree, config = self._setup_run()
            worker = FakeWorkerAdapter([WorkerAction(output="ready")])
            critic = FakeCriticAdapter([CriticAction(raw_verdict=raw)])
            supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
            status = supervisor.run()
            self.assertEqual(status["status"], "BLOCKED")
            self.assertEqual(status["last_event"], "INVALID_CRITIC_VERDICT")
            self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_invariant_6_no_subprocess_path_uses_shell_true(self):
        """Invariant 6: No subprocess path uses shell=True anywhere in src/gauntlet."""
        for py_file in self.src_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", content, f"Forbidden shell=True found in {py_file.name}")

            tree = ast.parse(content, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    # Check kwargs for shell=True
                    for kw in node.keywords:
                        if kw.arg == "shell":
                            self.assertIsInstance(kw.value, ast.Constant)
                            self.assertFalse(kw.value.value, f"shell must be False in {py_file.name}:{node.lineno}")

    def test_invariant_7_verification_commands_stay_argv_arrays_end_to_end(self):
        """Invariant 7: Verification commands stay argv arrays end-to-end without shell interpolation."""
        with tempfile.TemporaryDirectory() as temporary:
            bad_toml = Path(temporary) / "gauntlet.toml"
            # Attempt to supply string command instead of argv array
            bad_toml.write_text("""[worker]\nprovider = "agy"\nmodel = "m"\n[critic]\nprovider = "codex"\n[verification]\ncommands = [{name = "bad", argv = "echo hello"}]\n[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n""")
            with self.assertRaises(ConfigError):
                load_config(bad_toml)

        class InspectRunner:
            def __init__(self):
                self.calls = []

            def run(self, argv, **kwargs):
                self.calls.append(argv)
                return SubprocessRunner().run([sys.executable, "-c", "import sys; sys.exit(0)"])

        inspect_runner = InspectRunner()
        cmd = VerificationCommand("safe_cmd", (sys.executable, "-c", "import sys; sys.exit(0)"))
        run_commands((cmd,), Path.cwd(), inspect_runner)

        self.assertEqual(len(inspect_runner.calls), 1)
        self.assertIsInstance(inspect_runner.calls[0], tuple)
        self.assertEqual(inspect_runner.calls[0], cmd.argv)

    def test_invariant_8_no_remote_git_mutation_commands(self):
        """Invariant 8: No remote git mutation command is introduced in src/gauntlet."""
        forbidden_git_subcommands = {"push", "remote", "fetch", "pull", "clone", "merge", "rebase"}
        for py_file in self.src_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)):
                    elements = [
                        elt.value for elt in node.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    if elements and elements[0] == "git":
                        # Verify that git arguments never contain forbidden remote operations
                        git_args = set(elements[1:])
                        intersection = git_args & forbidden_git_subcommands
                        self.assertEqual(
                            intersection,
                            set(),
                            f"Forbidden git remote command in {py_file.name}: {elements}",
                        )


if __name__ == "__main__":
    unittest.main()
