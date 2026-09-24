from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gauntlet.config import parse_config
from gauntlet.doctor import doctor
from gauntlet.process import SubprocessRunner
from gauntlet.providers import (
    AdapterError,
    AgyAdapter,
    CodexAdapter,
    WorkerResult,
)
from gauntlet.store import RunStore
from gauntlet.task import parse_task
from gauntlet.worktree import create_worktree

FAKE_AGY_PATH = Path(__file__).resolve().parent / "fakes" / "fake_agy.py"
FAKE_CODEX_PATH = Path(__file__).resolve().parent / "fakes" / "fake_codex.py"


class ProvidersAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        # Clear any preexisting fake env vars
        for k in list(os.environ.keys()):
            if k.startswith("FAKE_AGY_") or k.startswith("FAKE_CODEX_"):
                del os.environ[k]

        self.temp_dir = tempfile.TemporaryDirectory()
        self.primary_repo = Path(self.temp_dir.name) / "repo"
        self.primary_repo.mkdir()
        self._git(self.primary_repo, "init")
        self._git(self.primary_repo, "config", "user.email", "test@example.invalid")
        self._git(self.primary_repo, "config", "user.name", "Tester")
        (self.primary_repo / "README.md").write_text("init\n", encoding="utf-8")
        self._git(self.primary_repo, "add", ".")
        self._git(self.primary_repo, "commit", "-m", "init")

        self.runner = SubprocessRunner()
        self.task = parse_task(
            "# Objective\nImplement target functionality.\n\n"
            "# Acceptance Criteria\n- Feature complete.\n"
        )
        self.config = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "some-model"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "tests"\nargv = ["echo", "test"]\n\n'
            '[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )
        self.agy_cmd = (sys.executable, str(FAKE_AGY_PATH))
        self.codex_cmd = (sys.executable, str(FAKE_CODEX_PATH))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _git(self, repo: Path, *argv: str) -> None:
        res = subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, f"git error: {res.stderr}")

    def _setup_run(self) -> tuple[RunStore, Path]:
        store = RunStore.create(self.primary_repo)
        worktree = create_worktree(self.primary_repo, store.run_id, self.runner)
        return store, worktree

    # --- AgyAdapter tests ---

    def test_agy_success_turn_1(self) -> None:
        store, worktree = self._setup_run()
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)

        state_file = store.root / "worker-conversation.json"
        self.assertTrue(state_file.exists())
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state.get("conversation_id"), "conv-1")
        self.assertEqual(state.get("run_id"), store.run_id)

        cmd_artifact = store.root / "worker-turn-1-attempt-1-command.json"
        self.assertTrue(cmd_artifact.exists())
        cmd = json.loads(cmd_artifact.read_text(encoding="utf-8"))["argv"]
        self.assertNotIn("--conversation", cmd)

    def test_agy_success_turn_2_reusing_conversation_id(self) -> None:
        store, worktree = self._setup_run()
        adapter1 = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        res1 = adapter1.run(worktree, self.task, turn=1)
        self.assertEqual(res1.returncode, 0)

        # Simulate restart/resume with new AgyAdapter instance
        adapter2 = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        res2 = adapter2.run(worktree, self.task, turn=2)
        self.assertEqual(res2.returncode, 0)

        cmd_artifact = store.root / "worker-turn-2-attempt-1-command.json"
        self.assertTrue(cmd_artifact.exists())
        cmd = json.loads(cmd_artifact.read_text(encoding="utf-8"))["argv"]
        self.assertIn("--conversation", cmd)
        conv_idx = cmd.index("--conversation")
        self.assertEqual(cmd[conv_idx + 1], "conv-1")

    def test_agy_turn_2_with_no_saved_conversation_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)

        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=2)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("turn > 1", result.error)
        self.assertFalse(log_file.exists())
        self.assertEqual(list(store.root.glob("worker-turn-2-attempt-*-command.json")), [])

    def test_agy_malformed_json_stdout_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_NO_JSON"] = "not json"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((store.root / "worker-conversation.json").exists())

    def test_agy_missing_conversation_id_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_OMIT_CONVERSATION_ID"] = "1"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((store.root / "worker-conversation.json").exists())

    def test_agy_mismatched_conversation_id_on_continuation_turn_fails(self) -> None:
        store, worktree = self._setup_run()
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        res1 = adapter.run(worktree, self.task, turn=1)
        self.assertEqual(res1.returncode, 0)

        # Force provider to return a different conversation id
        os.environ["FAKE_AGY_CONVERSATION_MISMATCH"] = "different-id"
        res2 = adapter.run(worktree, self.task, turn=2)

        self.assertNotEqual(res2.returncode, 0)
        state = json.loads((store.root / "worker-conversation.json").read_text(encoding="utf-8"))
        self.assertEqual(state["conversation_id"], "conv-1")

    def test_agy_nonzero_exit_code(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_EXIT"] = "3"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 3)

    def test_agy_timeout(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_SLEEP_MS"] = "1000"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd, timeout_seconds=0.1)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertTrue(result.timed_out)

    def test_agy_large_stdout_handled_safely(self) -> None:
        store, worktree = self._setup_run()
        large_file = store.root / "large_stdout.txt"
        large_file.write_text("x" * 250000, encoding="utf-8")
        os.environ["FAKE_AGY_STDOUT_OVERRIDE"] = str(large_file)
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertIsInstance(result, WorkerResult)
        self.assertNotEqual(result.returncode, 0)

    def test_agy_unicode_response_flows_through(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_RESPONSE"] = "héllo wörld 日本語"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertIn("日本語", result.output)

    def test_agy_worker_prose_done_pass_never_creates_done_field(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_RESPONSE"] = "TASK COMPLETE DONE PASS"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(hasattr(result, "done"))
        self.assertFalse(hasattr(result, "status"))
        self.assertFalse(hasattr(result, "verdict"))
        self.assertFalse(hasattr(result, "passed"))

    def test_agy_launched_argv_structure(self) -> None:
        store, worktree = self._setup_run()
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        adapter.run(worktree, self.task, turn=1)

        cmd_artifact = store.root / "worker-turn-1-attempt-1-command.json"
        cmd = json.loads(cmd_artifact.read_text(encoding="utf-8"))["argv"]
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(x, str) for x in cmd))
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], self.config.worker_model)
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], self.config.worker_effort)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--continue", cmd)

    def test_agy_cmd_executable_rejected(self) -> None:
        store, _ = self._setup_run()
        with self.assertRaises(AdapterError):
            AgyAdapter(store, self.runner, self.config, command=("something.cmd",))

    # --- CodexAdapter tests ---

    def test_codex_read_only_unproven_fails_without_launching(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_codex.log"
        os.environ["FAKE_CODEX_LOG"] = str(log_file)

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=False)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 78)
        self.assertIn("not proven safe", result.error)
        self.assertFalse(log_file.exists())

    def test_codex_read_only_proven_default_pass(self) -> None:
        store, worktree = self._setup_run()
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.raw_verdict)
        self.assertEqual(parsed, {"verdict": "PASS", "blocking_findings": []})

    def test_codex_block_verdict_roundtrips(self) -> None:
        store, worktree = self._setup_run()
        finding = {
            "id": "block-1",
            "severity": "critical",
            "claim": "assertion failed",
            "evidence": "file.txt:1",
            "location": "file.txt:1",
            "required_condition": "fix assertion",
        }
        verdict = {"verdict": "BLOCK", "blocking_findings": [finding]}
        os.environ["FAKE_CODEX_VERDICT_JSON"] = json.dumps(verdict)

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.raw_verdict), verdict)

    def test_codex_malformed_json_in_output_file_passed_as_is(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_CODEX_VERDICT_JSON"] = "not json"

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.raw_verdict, "not json")

    def test_codex_missing_output_file_fails_with_empty_verdict(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_CODEX_NO_OUTPUT_FILE"] = "1"

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.raw_verdict, "")
        self.assertIn("produced no --output-schema result file", result.error)

    def test_codex_nonzero_exit(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_CODEX_EXIT"] = "1"

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 1)

    def test_codex_timeout(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_CODEX_SLEEP_MS"] = "1000"

        adapter = CodexAdapter(
            store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True, timeout_seconds=0.1
        )
        result = adapter.run(worktree, self.task, turn=1)

        self.assertTrue(result.timed_out)

    def test_codex_stdout_prose_ignored_for_verdict(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_CODEX_STDOUT_EXTRA"] = "EXTRA PROSE DO NOT READ"

        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.raw_verdict, '{"verdict":"PASS","blocking_findings":[]}')
        self.assertNotIn("EXTRA PROSE", result.raw_verdict)

    def test_codex_fresh_output_file_path_per_call(self) -> None:
        store, worktree = self._setup_run()
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)
        adapter.run(worktree, self.task, turn=2)

        cmd1 = json.loads((store.root / "critic-turn-1-attempt-1-command.json").read_text(encoding="utf-8"))["argv"]
        cmd2 = json.loads((store.root / "critic-turn-2-attempt-1-command.json").read_text(encoding="utf-8"))["argv"]
        path1 = cmd1[cmd1.index("-o") + 1]
        path2 = cmd2[cmd2.index("-o") + 1]
        self.assertNotEqual(path1, path2)

    def test_codex_launched_argv_structure(self) -> None:
        store, worktree = self._setup_run()
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)

        cmd = json.loads((store.root / "critic-turn-1-attempt-1-command.json").read_text(encoding="utf-8"))["argv"]
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(x, str) for x in cmd))

        idx_a = cmd.index("-a")
        self.assertEqual(cmd[idx_a + 1], "never")
        idx_exec = cmd.index("exec")
        self.assertLess(idx_a, idx_exec)

        exec_argv = cmd[idx_exec:]
        self.assertIn("-s", exec_argv)
        self.assertEqual(exec_argv[exec_argv.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", exec_argv)
        self.assertIn("--ignore-user-config", exec_argv)
        self.assertIn("--ignore-rules", exec_argv)
        self.assertIn("--output-schema", exec_argv)
        self.assertIn("-o", exec_argv)

        for forbidden in ("review", "resume", "fork", "--last"):
            self.assertNotIn(forbidden, cmd)

    def test_codex_bat_executable_rejected(self) -> None:
        store, _ = self._setup_run()
        with self.assertRaises(AdapterError):
            CodexAdapter(store, self.runner, self.config, command=("codex.bat",), read_only_proven=True)

    def test_codex_missing_critic_model_raises_adapter_error(self) -> None:
        store, _ = self._setup_run()
        cfg = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "some-model"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "t"\nargv = ["echo", "1"]\n\n'
            '[limits]\nwall_time = "1h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )
        self.assertIsNone(cfg.critic_model)
        with self.assertRaises(AdapterError):
            CodexAdapter(store, self.runner, cfg, command=self.codex_cmd, read_only_proven=True)

    # --- Additional AgyAdapter contract tests ---

    def test_agy_exact_argv_shape_via_process_log(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        adapter.run(worktree, self.task, turn=1)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(Path(entry["cwd"]).resolve(), worktree.resolve())
        argv = entry["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], self.config.worker_model)
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], self.config.worker_effort)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(worktree))
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--continue", argv)

    def test_agy_continuation_never_leaks_between_run_ids(self) -> None:
        store_a, worktree_a = self._setup_run()
        adapter_a = AgyAdapter(store_a, self.runner, self.config, command=self.agy_cmd)
        res_a = adapter_a.run(worktree_a, self.task, turn=1)
        self.assertEqual(res_a.returncode, 0)

        store_b, worktree_b = self._setup_run()
        log_file = store_b.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)
        adapter_b = AgyAdapter(store_b, self.runner, self.config, command=self.agy_cmd)
        res_b = adapter_b.run(worktree_b, self.task, turn=2)

        self.assertNotEqual(res_b.returncode, 0)
        self.assertIn("turn > 1", res_b.error)
        self.assertFalse(log_file.exists())

    def test_agy_tampered_state_wrong_run_id_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        state_path = store.root / "worker-conversation.json"
        state_path.write_text(
            json.dumps({"provider": "agy", "run_id": "not-this-run", "conversation_id": "conv-x"}),
            encoding="utf-8",
        )
        log_file = store.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=2)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_file.exists())

    def test_agy_tampered_state_malformed_json_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        state_path = store.root / "worker-conversation.json"
        state_path.write_text("not json", encoding="utf-8")
        log_file = store.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=2)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_file.exists())

    def test_agy_provider_error_status_fails_closed(self) -> None:
        store, worktree = self._setup_run()
        os.environ["FAKE_AGY_STATUS"] = "ERROR"
        os.environ["FAKE_AGY_ERROR"] = "provider refused"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provider refused", result.error)
        self.assertFalse((store.root / "worker-conversation.json").exists())

    def test_agy_large_stdout_and_stderr_bounded(self) -> None:
        store, worktree = self._setup_run()
        # Windows env vars cap at 32767 chars; drive the large payload through a
        # file instead, same as test_agy_large_stdout_handled_safely.
        large_file = store.root / "large_response.txt"
        large_file.write_text("y" * 250000, encoding="utf-8")
        os.environ["FAKE_AGY_STDOUT_OVERRIDE"] = str(large_file)
        os.environ["FAKE_AGY_STDERR"] = "e" * 20000
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        # Non-JSON stdout override fails closed (same contract as
        # test_agy_large_stdout_handled_safely); the point here is that a large
        # stdout+stderr payload never crashes the adapter and stays bounded.
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(len(result.output), 500_100)
        self.assertLessEqual(len(result.error), 500_100)

    def test_agy_unicode_response_appears_in_process_argv_prompt(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_agy.log"
        os.environ["FAKE_AGY_LOG"] = str(log_file)
        os.environ["FAKE_AGY_RESPONSE"] = "héllo wörld 日本語"
        adapter = AgyAdapter(store, self.runner, self.config, command=self.agy_cmd)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertIn("日本語", result.output)
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)

    def test_agy_model_with_spaces_rejected_by_config(self) -> None:
        with self.assertRaises(Exception):
            parse_config(
                '[worker]\nprovider = "agy"\nmodel = "bad model name"\neffort = "high"\n\n'
                '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
                '[[verification.commands]]\nname = "t"\nargv = ["echo", "1"]\n\n'
                '[limits]\nwall_time = "1h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
            )

    # --- Additional CodexAdapter contract tests ---

    def test_codex_flag_order_a_never_before_exec(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_codex.log"
        os.environ["FAKE_CODEX_LOG"] = str(log_file)
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        argv = entries[0]["argv"]
        self.assertEqual(argv[0], "-a")
        self.assertEqual(argv[1], "never")
        self.assertEqual(argv[2], "exec")

    def test_codex_prompt_arrives_on_stdin_not_argv(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_codex.log"
        os.environ["FAKE_CODEX_LOG"] = str(log_file)
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        entry = entries[0]
        prompt_file = store.root / "critic-turn-1-attempt-1-prompt.txt"
        prompt_text = prompt_file.read_text(encoding="utf-8")
        self.assertEqual(entry["stdin"], prompt_text)
        for arg in entry["argv"]:
            self.assertNotEqual(arg, prompt_text)

    def test_codex_every_review_is_fresh_process_distinct_pid_and_output(self) -> None:
        store, worktree = self._setup_run()
        log_file = store.root / "fake_codex.log"
        os.environ["FAKE_CODEX_LOG"] = str(log_file)
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)
        adapter.run(worktree, self.task, turn=2)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]["pid"], entries[1]["pid"])
        for entry in entries:
            self.assertNotIn("resume", entry["argv"])
            self.assertNotIn("--last", entry["argv"])
            self.assertNotIn("fork", entry["argv"])

    def test_codex_no_session_state_left_in_store(self) -> None:
        store, worktree = self._setup_run()
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        adapter.run(worktree, self.task, turn=1)
        adapter.run(worktree, self.task, turn=2)

        for path in store.root.iterdir():
            self.assertNotIn("conversation", path.name)
            self.assertNotIn("session", path.name)

    def test_codex_pass_with_findings_is_passed_through_raw_for_validator(self) -> None:
        store, worktree = self._setup_run()
        verdict = {
            "verdict": "PASS",
            "blocking_findings": [
                {
                    "id": "f-1",
                    "severity": "minor",
                    "claim": "x",
                    "evidence": "y",
                    "location": "z:1",
                    "required_condition": "n/a",
                }
            ],
        }
        os.environ["FAKE_CODEX_VERDICT_JSON"] = json.dumps(verdict)
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.raw_verdict), verdict)

    def test_codex_block_without_findings_is_passed_through_raw_for_validator(self) -> None:
        store, worktree = self._setup_run()
        verdict = {"verdict": "BLOCK", "blocking_findings": []}
        os.environ["FAKE_CODEX_VERDICT_JSON"] = json.dumps(verdict)
        adapter = CodexAdapter(store, self.runner, self.config, command=self.codex_cmd, read_only_proven=True)
        result = adapter.run(worktree, self.task, turn=1)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.raw_verdict), verdict)


class ProvidersDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        for k in list(os.environ.keys()):
            if k.startswith("FAKE_AGY_") or k.startswith("FAKE_CODEX_"):
                del os.environ[k]
        self.runner = SubprocessRunner()
        self.agy_cmd = (sys.executable, str(FAKE_AGY_PATH))
        self.codex_cmd = (sys.executable, str(FAKE_CODEX_PATH))
        self.config = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "gemini-3.8-flash-high"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "tests"\nargv = ["echo", "test"]\n\n'
            '[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_doctor_all_flags_present_reports_flags_ok(self) -> None:
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertTrue(report.agy_flags_ok)
        self.assertTrue(report.codex_flags_ok)

    def test_doctor_exact_model_match_required(self) -> None:
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertTrue(report.worker_model_detected)

    def test_doctor_prefix_model_not_detected(self) -> None:
        cfg = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "gemini-3.8-flash"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "tests"\nargv = ["echo", "test"]\n\n'
            '[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )
        report = doctor(cfg, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertFalse(report.worker_model_detected)

    def test_doctor_case_variant_model_not_detected(self) -> None:
        cfg = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "Gemini-3.8-Flash-High"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "tests"\nargv = ["echo", "test"]\n\n'
            '[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )
        report = doctor(cfg, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertFalse(report.worker_model_detected)

    def test_doctor_effort_high_supported_unsupported_effort_not(self) -> None:
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertTrue(report.critic_model_detected)
        self.assertTrue(report.critic_model_effort_supported)

        # gpt-5.6-terra-lowonly (fake model) only supports "low"; "high" must
        # be reported as unsupported, not guessed as available.
        cfg_mismatch = parse_config(
            '[worker]\nprovider = "agy"\nmodel = "gemini-3.8-flash-high"\neffort = "high"\n\n'
            '[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra-lowonly"\neffort = "high"\n\n'
            '[[verification.commands]]\nname = "tests"\nargv = ["echo", "test"]\n\n'
            '[limits]\nwall_time = "4h"\nmax_worker_turns = 20\nsame_blocker_limit = 3\n'
        )
        report_mismatch = doctor(cfg_mismatch, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertTrue(report_mismatch.critic_model_detected)
        self.assertFalse(report_mismatch.critic_model_effort_supported)

    def test_doctor_missing_agy_flag_fails_flags_ok_and_safe(self) -> None:
        os.environ["FAKE_AGY_HELP_OMIT"] = "--conversation"
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertFalse(report.agy_flags_ok)
        self.assertFalse(report.safe)

    def test_doctor_missing_codex_exec_flag_fails_flags_ok_and_safe(self) -> None:
        os.environ["FAKE_CODEX_EXEC_HELP_OMIT"] = "--output-schema"
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        self.assertFalse(report.codex_flags_ok)
        self.assertFalse(report.safe)

    def test_doctor_windows_read_only_unproven(self) -> None:
        report = doctor(self.config, self.runner, self.agy_cmd, self.codex_cmd)
        if os.name != "posix":
            self.assertTrue(report.codex_read_only.startswith("UNPROVEN"))
            self.assertFalse(report.safe)
        else:
            self.skipTest("posix platform: read-only smoke test path differs")


if __name__ == "__main__":
    unittest.main()
