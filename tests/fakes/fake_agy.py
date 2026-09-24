from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def main() -> None:
    # Ensure stdout/stderr use utf-8
    try:
        if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = sys.argv[1:]

    # Always: append JSON {"argv":argv,"cwd":os.getcwd(),"pid":os.getpid()}
    # as one line to os.environ["FAKE_AGY_LOG"] if set (best-effort, ignore errors).
    # agy receives the prompt via -p, never stdin; no stdin is read here (subprocess
    # does not pipe stdin for agy calls, so reading it would risk blocking).
    log_file = os.environ.get("FAKE_AGY_LOG")
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "pid": os.getpid()}) + "\n")
        except Exception:
            pass

    # Helper to flush and exit
    def finish(code: int) -> None:
        if "FAKE_AGY_STDERR" in os.environ:
            sys.stderr.write(os.environ["FAKE_AGY_STDERR"])
            sys.stderr.flush()
        sys.stdout.flush()
        sys.exit(code)

    if "--version" in argv:
        sys.stdout.write("1.2.9-fake\n")
        finish(0)

    if "models" in argv:
        models = (
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
            "gemini-3.8-pro-high\tGemini 3.8 Pro (High)\n"
        )
        sys.stdout.write(models)
        finish(0)

    if "--help" in argv:
        flags = ["--model <slug>", "--effort <level>", "--output-format <format>", "--conversation <id>", "--add-dir <path>"]
        omit = os.environ.get("FAKE_AGY_HELP_OMIT")
        if omit:
            flags = [f for f in flags if omit not in f]
        help_text = "Usage: agy [options]\nOptions:\n" + "".join(f"  {f}\n" for f in flags)
        sys.stdout.write(help_text)
        finish(0)

    # Sleep if requested
    if "FAKE_AGY_SLEEP_MS" in os.environ:
        try:
            time.sleep(float(os.environ["FAKE_AGY_SLEEP_MS"]) / 1000.0)
        except (ValueError, TypeError):
            pass

    exit_code = 0
    if "FAKE_AGY_EXIT" in os.environ:
        try:
            exit_code = int(os.environ["FAKE_AGY_EXIT"])
        except (ValueError, TypeError):
            exit_code = 0

    if "FAKE_AGY_NO_JSON" in os.environ:
        sys.stdout.write(os.environ["FAKE_AGY_NO_JSON"])
        finish(exit_code)

    if "FAKE_AGY_STDOUT_OVERRIDE" in os.environ:
        val = os.environ["FAKE_AGY_STDOUT_OVERRIDE"]
        try:
            if Path(val).is_file():
                val = Path(val).read_text(encoding="utf-8")
        except Exception:
            pass
        sys.stdout.write(val)
        finish(0)

    # Normal worker-turn call
    conv_arg = None
    if "--conversation" in argv:
        try:
            idx = argv.index("--conversation")
            if idx + 1 < len(argv):
                conv_arg = argv[idx + 1]
        except ValueError:
            pass

    default_conv_id = conv_arg if conv_arg is not None else "conv-1"
    conversation_id = os.environ.get("FAKE_AGY_CONVERSATION_ID", default_conv_id)

    payload: dict[str, object] = {
        "status": os.environ.get("FAKE_AGY_STATUS", "SUCCESS"),
        "response": os.environ.get("FAKE_AGY_RESPONSE", "fake worker output"),
        "conversation_id": conversation_id,
    }

    if "FAKE_AGY_ERROR" in os.environ:
        payload["error"] = os.environ["FAKE_AGY_ERROR"]

    if "FAKE_AGY_OMIT_CONVERSATION_ID" in os.environ:
        payload.pop("conversation_id", None)

    if "FAKE_AGY_CONVERSATION_MISMATCH" in os.environ:
        payload["conversation_id"] = os.environ["FAKE_AGY_CONVERSATION_MISMATCH"]

    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    finish(exit_code)


if __name__ == "__main__":
    main()
