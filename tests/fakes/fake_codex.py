from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def main() -> None:
    argv = sys.argv[1:]

    def finish(code: int) -> None:
        if "FAKE_CODEX_STDERR" in os.environ:
            sys.stderr.write(os.environ["FAKE_CODEX_STDERR"])
            sys.stderr.flush()
        sys.stdout.flush()
        sys.exit(code)

    def write_log(stdin_str: str) -> None:
        log_file = os.environ.get("FAKE_CODEX_LOG")
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "stdin": stdin_str}
                        )
                        + "\n"
                    )
            except Exception:
                pass

    if "--version" in argv:
        write_log("")
        sys.stdout.write("codex-cli 0.156.1-fake\n")
        finish(0)

    if "--help" in argv and "exec" not in argv:
        write_log("")
        help_text = (
            "Usage: codex [options] [command]\n"
            "Options:\n"
            "  -a, --ask-for-approval <mode>\n"
            "  --help\n"
        )
        sys.stdout.write(help_text)
        finish(0)

    if "exec" in argv and "--help" in argv:
        write_log("")
        flags = ["--output-schema <path>", "-o <path>", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-s <sandbox>", "-C <dir>"]
        omit = os.environ.get("FAKE_CODEX_EXEC_HELP_OMIT")
        if omit:
            flags = [f for f in flags if omit not in f]
        exec_help = "Usage: codex exec [options] [prompt]\nOptions:\n" + "".join(f"  {f}\n" for f in flags)
        sys.stdout.write(exec_help)
        finish(0)

    if "debug" in argv and "models" in argv:
        write_log("")
        models_data = {
            "models": [
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                        {"effort": "high"},
                        {"effort": "xhigh"},
                    ],
                },
                {
                    "slug": "gpt-5.6-terra-lowonly",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                    ],
                },
            ]
        }
        sys.stdout.write(json.dumps(models_data) + "\n")
        finish(0)

    # Otherwise (a critic review call, argv contains "exec"):
    stdin_text = ""
    if "exec" in argv and not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""

    write_log(stdin_text)

    output_file: str | None = None
    if "-o" in argv:
        try:
            idx = argv.index("-o")
            if idx + 1 < len(argv):
                output_file = argv[idx + 1]
        except ValueError:
            pass

    workdir: str | None = None
    if "-C" in argv:
        try:
            idx = argv.index("-C")
            if idx + 1 < len(argv):
                workdir = argv[idx + 1]
        except ValueError:
            pass

    if "FAKE_CODEX_SLEEP_MS" in os.environ:
        try:
            time.sleep(float(os.environ["FAKE_CODEX_SLEEP_MS"]) / 1000.0)
        except (ValueError, TypeError):
            pass

    exit_code = 0
    if "FAKE_CODEX_EXIT" in os.environ:
        try:
            exit_code = int(os.environ["FAKE_CODEX_EXIT"])
        except (ValueError, TypeError):
            exit_code = 0

    if not output_file:
        finish(exit_code)

    if "FAKE_CODEX_WRITE_IN_WORKTREE" in os.environ and workdir:
        try:
            target = Path(workdir) / os.environ["FAKE_CODEX_WRITE_IN_WORKTREE"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("mutated by fake codex", encoding="utf-8")
        except Exception:
            pass

    if "FAKE_CODEX_NO_OUTPUT_FILE" not in os.environ:
        verdict_content = '{"verdict":"PASS","blocking_findings":[]}'
        if "FAKE_CODEX_VERDICT_QUEUE_FILE" in os.environ:
            try:
                q_file = Path(os.environ["FAKE_CODEX_VERDICT_QUEUE_FILE"])
                if q_file.exists():
                    lines = [line.strip() for line in q_file.read_text(encoding="utf-8").split("\n---\n") if line.strip()]
                    if lines:
                        verdict_content = lines.pop(0)
                        if lines:
                            q_file.write_text("\n---\n".join(lines), encoding="utf-8")
                        else:
                            q_file.unlink()
            except Exception:
                verdict_content = '{"verdict":"PASS","blocking_findings":[]}'
        elif "FAKE_CODEX_VERDICT_JSON" in os.environ:
            verdict_content = os.environ["FAKE_CODEX_VERDICT_JSON"]

        try:
            out_path = Path(output_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(verdict_content, encoding="utf-8")
        except Exception:
            pass

    if "FAKE_CODEX_STDOUT_EXTRA" in os.environ:
        sys.stdout.write(os.environ["FAKE_CODEX_STDOUT_EXTRA"])

    finish(exit_code)


if __name__ == "__main__":
    main()
