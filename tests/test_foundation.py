from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gauntlet.cli import main
from gauntlet.config import ConfigError, VerificationCommand, init_config, load_config
from gauntlet.doctor import doctor
from gauntlet.process import ProcessResult, SubprocessRunner
from gauntlet.store import RunStore, StoreError
from gauntlet.supervisor import FoundationSupervisor
from gauntlet.task import TaskContractError, parse_task
from gauntlet.verdict import VerdictError, validate_verdict
from gauntlet.verification import run_commands
from gauntlet.worktree import WorktreeError, create_worktree, worktree_path


TASK = """# Objective
Ship a testable change.

# Acceptance Criteria
- The command exits successfully.

# Constraints
- Do not touch remotes.
"""


class FakeRunner:
    def __init__(self, result: ProcessResult = ProcessResult(0, "ok", "")):
        self.result = result
        self.calls: list[tuple[tuple[str, ...], Path | None, str | None]] = []

    def run(self, argv, *, cwd=None, input_text=None):
        self.calls.append((tuple(argv), cwd, input_text))
        return self.result


class FoundationTests(unittest.TestCase):
    def test_worker_output_never_creates_done(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary))
            FoundationSupervisor(store).worker_finished('{"status":"DONE"}')
            self.assertEqual(store.status()["status"], "WORKER_FINISHED")
            self.assertNotIn("DONE", (store.root / "events.jsonl").read_text())

    def test_invalid_verdicts_fail_closed(self):
        with self.assertRaises(VerdictError):
            validate_verdict("not json")
        with self.assertRaises(VerdictError):
            validate_verdict({"verdict": "PASS", "blocking_findings": [{"id": "x"}]})
        with self.assertRaises(VerdictError):
            validate_verdict({"verdict": "BLOCK", "blocking_findings": []})

    def test_frozen_task_and_config_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore.create(root)
            store.freeze_text("config.toml", "old")
            store.freeze_text("task.md", TASK)
            with self.assertRaises(FileExistsError):
                store.freeze_text("config.toml", "new")
            self.assertEqual((store.root / "config.toml").read_text(), "old")
            self.assertEqual(parse_task((store.root / "task.md").read_text()).objective, "Ship a testable change.")

    def test_status_is_projected_from_events_not_status_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary))
            store.event("CREATED", status="CREATED")
            (store.root / "status.json").write_text('{"status":"DONE"}')
            self.assertEqual(store.status()["status"], "CREATED")

    def test_atomic_overwrite_leaves_old_artifact_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary))
            store.freeze_text("item.txt", "old")
            with patch("gauntlet.store.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store._write("item.txt", b"new", overwrite=True)
            self.assertEqual((store.root / "item.txt").read_text(), "old")
            self.assertEqual(list(store.root.glob(".item.txt.*")), [])

    def test_worktree_is_sibling_not_primary_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.invalid")
            self._git(root, "config", "user.name", "test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")
            path = create_worktree(root, "run-1", SubprocessRunner())
            self.assertTrue(path.is_dir())
            self.assertNotIn(root.resolve(), path.resolve().parents)
            self.assertEqual(path, worktree_path(root, "run-1"))

    def test_verification_remains_argv_and_never_shell(self):
        runner = FakeRunner()
        command = VerificationCommand("literal", ("python", "-c", "print('x'); rm -rf /"))
        result = run_commands((command,), Path.cwd(), runner)
        self.assertEqual(runner.calls[0][0], command.argv)
        self.assertEqual(result[0].argv, command.argv)
        with patch("gauntlet.process.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            SubprocessRunner().run(["echo", "safe"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_missing_executable_is_a_nonzero_result(self):
        result = SubprocessRunner().run(["gauntlet-command-that-does-not-exist"])
        self.assertEqual(result.returncode, 127)

    def test_config_has_no_command_string_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gauntlet.toml"
            path.write_text("""[worker]\nprovider = 'agy'\nmodel = 'x'\n[critic]\nprovider = 'codex'\n[verification]\ncommands = ['echo injected; touch bad']\n""")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_config_limits_numeric_validation_rejects_booleans_and_non_positives(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gauntlet.toml"
            base_toml = """[worker]\nprovider = "agy"\nmodel = "m"\neffort = "high"\n[critic]\nprovider = "codex"\neffort = "high"\n[[verification.commands]]\nname = "test"\nargv = ["echo", "test"]\n[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n"""

            # Valid configuration should pass
            path.write_text(base_toml)
            cfg = load_config(path)
            self.assertEqual(cfg.limits.max_worker_turns, 20)
            self.assertEqual(cfg.limits.same_blocker_limit, 3)

            # Rejects boolean True for max_worker_turns
            path.write_text(base_toml.replace("max_worker_turns = 20", "max_worker_turns = true"))
            with self.assertRaises(ConfigError):
                load_config(path)

            # Rejects boolean False for same_blocker_limit
            path.write_text(base_toml.replace("same_blocker_limit = 3", "same_blocker_limit = false"))
            with self.assertRaises(ConfigError):
                load_config(path)

            # Rejects 0 or negative integers
            path.write_text(base_toml.replace("max_worker_turns = 20", "max_worker_turns = 0"))
            with self.assertRaises(ConfigError):
                load_config(path)

            path.write_text(base_toml.replace("same_blocker_limit = 3", "same_blocker_limit = -1"))
            with self.assertRaises(ConfigError):
                load_config(path)

            # Rejects invalid durations
            path.write_text(base_toml.replace('wall_time = "4h"', 'wall_time = "0s"'))
            with self.assertRaises(ConfigError):
                load_config(path)

            path.write_text(base_toml.replace('wall_time = "4h"', 'wall_time = "-1h"'))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_doctor_is_unsafe_without_proven_read_only(self):
        config = self._config()
        with patch("gauntlet.doctor.shutil.which", return_value="tool"), patch("gauntlet.doctor._codex_smoke", return_value="UNPROVEN"):
            report = doctor(config, FakeRunner())
        self.assertFalse(report.safe)
        self.assertTrue(report.codex_read_only.startswith("UNPROVEN"))

    def test_codex_smoke_requires_sandbox_denial_evidence(self):
        from gauntlet.doctor import _codex_smoke

        # Output without denial signal fails (e.g. model declined or did nothing)
        runner_no_denial = FakeRunner(ProcessResult(0, "I did not run the write command", ""))
        self.assertIn("UNPROVEN", _codex_smoke(runner_no_denial, "model"))

        # Sentinel created fails even if words appear
        class CreateSentinelRunner:
            def run(self, argv, *, cwd=None, input_text=None, **kwargs):
                if cwd:
                    (Path(cwd) / "gauntlet-write-sentinel").write_text("forbidden")
                return ProcessResult(0, "permission denied", "")

        self.assertIn("UNPROVEN", _codex_smoke(CreateSentinelRunner(), "model"))

        # Output with denial signal and no sentinel created proves read-only
        runner_proven = FakeRunner(ProcessResult(0, "tool error: Read-only file system", ""))
        self.assertEqual(_codex_smoke(runner_proven, "model"), "PROVEN")

    def test_run_refuses_short_or_invalid_contract_before_worker(self):
        self.assertEqual(main(["run", "do a thing"]), 2)
        with self.assertRaises(TaskContractError):
            parse_task("# Objective\nVague\n")

    def test_parse_task_rejects_whitespace_only_bullets(self):
        with self.assertRaises(TaskContractError):
            parse_task("# Objective\nValid objective\n# Acceptance Criteria\n-    \n- \t \n")
        contract = parse_task("# Objective\nValid\n# Acceptance Criteria\n-    \n- Concrete outcome   \n")
        self.assertEqual(contract.acceptance_criteria, ("Concrete outcome",))

    def test_init_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gauntlet.toml"
            init_config(path)
            with self.assertRaises(FileExistsError):
                init_config(path)

    def test_run_freezes_task_and_config_before_creating_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.invalid")
            self._git(root, "config", "user.name", "test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")
            config = root / "gauntlet.toml"
            task = root / "TASK.md"
            init_config(config)
            task.write_text(TASK)
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["run", "--task", str(task)]), 0)
            run = next((root / ".gauntlet" / "runs").iterdir())
            frozen_config = (run / "config.toml").read_text()
            frozen_task = (run / "task-contract.json").read_text()
            config.write_text("changed")
            task.write_text("changed")
            self.assertIn("gemini-3.8-flash-high", frozen_config)
            self.assertIn("Ship a testable change.", frozen_task)
            self.assertEqual(json.loads((run / "status.json").read_text())["status"], "READY")

    def test_resume_cli_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.invalid")
            self._git(root, "config", "user.name", "test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")

            # 1. Non-existent run
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["resume", "nonexistent-run"]), 2)

            # Create a real run using main(["run", "--task", ...])
            config = root / "gauntlet.toml"
            task = root / "TASK.md"
            init_config(config)
            task.write_text(TASK)
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["run", "--task", str(task)]), 0)
            run = next((root / ".gauntlet" / "runs").iterdir())
            run_id = run.name

            # 2. Live provider doctor check fails closed (unproven read-only sandbox /
            # unconfigured critic model) -> exit code 2. Point agy/codex resolution at a
            # nonexistent path (never a bare "agy"/"codex" that the OS could still find
            # on PATH) so this never launches a real installed provider executable.
            missing_exe = str(root / "no-such-provider-executable")
            with (
                patch("gauntlet.cli.Path.cwd", return_value=root),
                patch("gauntlet.cli.shutil.which", return_value=missing_exe),
                patch("gauntlet.doctor.shutil.which", return_value=missing_exe),
            ):
                self.assertEqual(main(["resume", run_id]), 2)

            # 3. Update frozen config to use worker model "fake" and a passing verification command
            frozen_config_path = run / "config.toml"
            py_exec = sys.executable.replace("\\", "/")
            fake_config = f"""[worker]\nprovider = "agy"\nmodel = "fake"\neffort = "high"\n\n[critic]\nprovider = "codex"\neffort = "high"\n\n[[verification.commands]]\nname = "pass"\nargv = ["{py_exec}", "-c", "import sys; sys.exit(0)"]\n\n[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n"""
            frozen_config_path.write_text(fake_config, encoding="utf-8")

            # 4. Resume with fake model runs supervisor and reaches DONE -> exit code 0
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["resume", run_id]), 0)

            # Check status is now DONE
            status = json.loads((run / "status.json").read_text())
            self.assertEqual(status["status"], "DONE")

            # 5. Resuming an already DONE run fails with code 2
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["resume", run_id]), 2)

    def test_run_rejects_when_active_run_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")

            config = root / "gauntlet.toml"
            task = root / "TASK.md"
            init_config(config)
            task.write_text(TASK)

            # First run creates an active run in READY
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["run", "--task", str(task)]), 0)

            # Second run must reject because the first run is still active
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["run", "--task", str(task)]), 2)

    def test_store_rejects_path_traversal_run_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_ids = ["..", "../foo", "foo/bar", "foo\\bar", "foo/../bar", "", "   ", "a*b", "a?b"]
            for bad_id in invalid_ids:
                with self.assertRaises(StoreError):
                    RunStore(root, bad_id)

    def test_worktree_rejects_path_traversal_run_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_ids = ["..", "../foo", "foo/bar", "foo\\bar", "foo/../bar", ""]
            for bad_id in invalid_ids:
                with self.assertRaises(WorktreeError):
                    worktree_path(root, bad_id)

    def test_doctor_rejects_smoke_test_filesystem_mutation(self):
        from gauntlet.doctor import _codex_smoke

        def mutate_smoke(argv, *, cwd=None, input_text=None):
            if cwd:
                (cwd / "unauthorized_file.txt").write_text("injected")
            return ProcessResult(0, "permission denied: read-only file system", "")

        runner = FakeRunner()
        runner.run = mutate_smoke
        result = _codex_smoke(runner, "model")
        self.assertIn("filesystem mutation detected", result)

    def test_failed_worktree_setup_leaves_run_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")

            config = root / "gauntlet.toml"
            task = root / "TASK.md"
            init_config(config)
            task.write_text(TASK)

            # First run fails during worktree creation
            with patch("gauntlet.cli.Path.cwd", return_value=root), \
                 patch("gauntlet.cli.create_worktree", side_effect=WorktreeError("simulated worktree failure")):
                self.assertEqual(main(["run", "--task", str(task)]), 2)

            # Second run must NOT be blocked by an active run error and succeed
            with patch("gauntlet.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["run", "--task", str(task)]), 0)

    def test_status_from_subdirectory_locates_repo_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test")
            (root / "file.txt").write_text("x")
            self._git(root, "add", "file.txt")
            self._git(root, "commit", "-m", "initial")

            store = RunStore.create(root)
            store.event("CREATED", status="CREATED")

            subdir = root / "sub" / "dir"
            subdir.mkdir(parents=True)

            import io
            from contextlib import redirect_stdout

            # Test status with run_id from subdirectory
            buf = io.StringIO()
            with patch("gauntlet.cli.Path.cwd", return_value=subdir), redirect_stdout(buf):
                self.assertEqual(main(["status", store.run_id]), 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["run_id"], store.run_id)
            self.assertEqual(data["status"], "CREATED")

            # Test status listing all runs from subdirectory
            buf_all = io.StringIO()
            with patch("gauntlet.cli.Path.cwd", return_value=subdir), redirect_stdout(buf_all):
                self.assertEqual(main(["status"]), 0)
            data_all = json.loads(buf_all.getvalue())
            self.assertEqual(len(data_all), 1)
            self.assertEqual(data_all[0]["run_id"], store.run_id)

    def _config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gauntlet.toml"
            init_config(path)
            return load_config(path)

    def _git(self, cwd: Path, *argv: str) -> None:
        result = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
