from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gauntlet.process import (
    SubprocessRunner,
    bound_output,
)
from gauntlet.store import RunStore


class ProcessReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.runner = SubprocessRunner(max_output_chars=10_000)

    def test_timeout_kills_process_and_flags_result(self):
        # Run python script that sleeps longer than timeout
        script = "import time; time.sleep(5)"
        result = self.runner.run([sys.executable, "-c", script], timeout=0.2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out after 0.2 seconds", result.stderr)

    def test_killed_process_exit_code_preserved(self):
        # Script exits with a distinct non-zero exit code
        script = "import sys; sys.exit(77)"
        result = self.runner.run([sys.executable, "-c", script])
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 77)

    def test_large_stdout_is_bounded(self):
        # Generate 100,000 characters of stdout
        script = "import sys; sys.stdout.write('A' * 100000)"
        result = self.runner.run([sys.executable, "-c", script])
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 10_000 + 200)
        self.assertIn("[truncated", result.stdout)

    def test_large_stderr_is_bounded(self):
        # Generate 100,000 characters of stderr
        script = "import sys; sys.stderr.write('E' * 100000)"
        result = self.runner.run([sys.executable, "-c", script])
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stderr), 10_000 + 200)
        self.assertIn("[truncated", result.stderr)

    def test_unicode_output_handled_safely(self):
        # Non-ASCII characters: emojis, accents, Cyrillic, CJK
        script = "print('Hello 🌍! Привет мир! こんにちは! Ümläüt')"
        result = self.runner.run([sys.executable, "-c", script])
        self.assertEqual(result.returncode, 0)
        self.assertIn("🌍", result.stdout)
        self.assertIn("Привет", result.stdout)
        self.assertIn("こんにちは", result.stdout)

    def test_exit_code_preservation(self):
        for code in (0, 1, 2, 42, 127):
            result = self.runner.run([sys.executable, "-c", f"import sys; sys.exit({code})"])
            self.assertEqual(result.returncode, code)

    def test_missing_executable_returns_127(self):
        result = self.runner.run(["non_existent_executable_123456789"])
        self.assertEqual(result.returncode, 127)
        self.assertNotEqual(result.stderr, "")

    def test_cwd_correctness(self):
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary).resolve()
            script = "import os; print(os.getcwd())"
            result = self.runner.run([sys.executable, "-c", script], cwd=target_dir)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(Path(result.stdout.strip()).resolve(), target_dir)

    def test_argv_preservation_with_special_characters(self):
        # Special characters that would break shell interpolation
        args = ["first arg", "with; semicolons", "quote's and \"double\"", "--flag=value with spaces", "x&y|z"]
        script = "import sys, json; print(json.dumps(sys.argv[1:]))"
        result = self.runner.run([sys.executable, "-c", script, *args])
        self.assertEqual(result.returncode, 0)
        received = result.stdout.strip()
        import json
        self.assertEqual(json.loads(received), args)

    def test_no_shell_true_anywhere(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
            self.runner.run(["echo", "safe"])
            self.assertFalse(mock_run.call_args.kwargs.get("shell", True))

    def test_bound_output_helper(self):
        short_text = "hello world"
        self.assertEqual(bound_output(short_text, 100), short_text)

        long_text = "x" * 2000
        bounded = bound_output(long_text, 1000)
        self.assertLessEqual(len(bounded), 1100)
        self.assertIn("truncated", bounded)

    def test_interrupted_artifact_write_atomic_preservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore.create(Path(temporary))
            store.freeze_text("important.txt", "original_content")

            with patch("gauntlet.store.os.replace", side_effect=OSError("disk failure during atomic replace")):
                with self.assertRaises(OSError):
                    store._write("important.txt", b"corrupted_content", overwrite=True)

            # Original artifact is completely untouched
            self.assertEqual((store.root / "important.txt").read_text(), "original_content")
            # No temporary files left behind
            temp_files = list(store.root.glob(".important.txt.*"))
            self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
