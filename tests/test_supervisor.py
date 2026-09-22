from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gauntlet.config import Config, Limits, VerificationCommand
from gauntlet.process import SubprocessRunner
from gauntlet.providers import (
    CriticAction,
    FakeCriticAdapter,
    FakeWorkerAdapter,
    WorkerAction,
)
from gauntlet.store import RunStore
from gauntlet.supervisor import Supervisor, snapshot_manifest
from gauntlet.task import TaskContract
from gauntlet.worktree import create_worktree


class SupervisorScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.primary_repo = Path(self.temp_dir.name) / "repo"
        self.primary_repo.mkdir()
        self._git_init(self.primary_repo)
        self.runner = SubprocessRunner()
        self.task = TaskContract(
            objective="Implement target functionality",
            acceptance_criteria=("File feature.txt must exist with correct content",),
            constraints=("Do not touch remotes",),
            verification_expectations=("python verification check passes",),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git_init(self, repo: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@gauntlet.invalid"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GauntletTest"], cwd=repo, check=True, capture_output=True)
        (repo / "README.md").write_text("initial repository content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)

    def _setup_run(self, verification_commands: tuple[VerificationCommand, ...] | None = None, limits: Limits | None = None):
        store = RunStore.create(self.primary_repo)
        worktree = create_worktree(self.primary_repo, store.run_id, self.runner)

        v_commands = verification_commands if verification_commands is not None else (
            VerificationCommand("pass_check", (sys.executable, "-c", "import sys; sys.exit(0)")),
        )
        lim = limits or Limits(wall_time="4h", max_worker_turns=10, same_blocker_limit=3)

        config = Config(
            worker_model="gemini-3.8-flash-high",
            critic_model="gpt-5.6-terra",
            verification=v_commands,
            limits=lim,
        )

        config_toml = f"""[worker]\nprovider = "agy"\nmodel = "gemini-3.8-flash-high"\neffort = "high"\n\n[critic]\nprovider = "codex"\nmodel = "gpt-5.6-terra"\neffort = "high"\n\n[limits]\nwall_time = "{lim.wall_time}"\nmax_worker_turns = {lim.max_worker_turns}\nsame_blocker_limit = {lim.same_blocker_limit}\n"""
        for cmd in v_commands:
            argv_json = json.dumps(list(cmd.argv))
            config_toml += f"\n[[verification.commands]]\nname = \"{cmd.name}\"\nargv = {argv_json}\n"

        store.freeze_text("config.toml", config_toml)
        store.freeze_text("task.md", "# Objective\nImplement target functionality\n# Acceptance Criteria\n- File feature.txt must exist\n")
        store.freeze_json("task-contract.json", self.task.as_dict())
        store.event("CREATED", status="CREATED")
        store.event("WORKTREE_CREATED", status="READY", detail={"path": str(worktree)})

        return store, worktree, config

    def test_scenario_a_successful_flow_creates_done(self):
        """SCENARIO A: worker edit -> checks pass -> critic PASS -> final checks pass -> DONE."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([
            WorkerAction(edits={"feature.txt": "implemented"}, output="created feature.txt"),
        ])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        self.assertTrue((worktree / "feature.txt").exists())
        self.assertEqual((worktree / "feature.txt").read_text(), "implemented")

        # Verify artifacts
        self.assertTrue((store.root / "baseline-checks.json").exists())
        self.assertTrue((store.root / "worker-turn-1-output.txt").exists())
        self.assertTrue((store.root / "verification-post-worker-turn-1.json").exists())
        self.assertTrue((store.root / "critic-turn-1-verdict.json").exists())
        self.assertTrue((store.root / "verification-final-turn-1.json").exists())

    def test_scenario_b_multi_round_critic_block_then_fix(self):
        """SCENARIO B: worker incomplete -> checks pass -> critic BLOCK -> worker fixes -> checks pass -> critic PASS -> DONE."""
        store, worktree, config = self._setup_run()

        finding = {
            "id": "missing-error-handling",
            "severity": "critical",
            "claim": "feature.txt lacks required error handling section",
            "evidence": "feature.txt only contains draft",
            "location": "feature.txt:1",
            "required_condition": "include [ErrorHandling] section",
        }

        worker = FakeWorkerAdapter([
            WorkerAction(edits={"feature.txt": "draft"}, output="draft created"),
            WorkerAction(edits={"feature.txt": "draft\n[ErrorHandling]\nhandled"}, output="fixed finding"),
        ])

        critic = FakeCriticAdapter([
            CriticAction(raw_verdict=json.dumps({"verdict": "BLOCK", "blocking_findings": [finding]})),
            CriticAction(raw_verdict=json.dumps({"verdict": "PASS", "blocking_findings": []})),
        ])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(len(worker.invocations), 2)
        self.assertEqual(len(critic.invocations), 2)

        # Worker turn 2 received the critic finding
        turn_2_findings = worker.invocations[1]["findings"]
        self.assertEqual(len(turn_2_findings), 1)
        self.assertEqual(turn_2_findings[0]["id"], "missing-error-handling")

    def test_scenario_c_worker_claims_success_ignored_on_critic_block(self):
        """SCENARIO C: worker claims success immediately -> critic BLOCK -> NOT DONE."""
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))

        finding = {
            "id": "untested-logic",
            "severity": "major",
            "claim": "logic is untested",
            "evidence": "no tests added",
            "location": "test_feature.py",
            "required_condition": "add automated test",
        }

        # Worker claims completion in its prose/JSON
        worker = FakeWorkerAdapter([
            WorkerAction(output='{"status": "DONE", "verdict": "PASS", "message": "task complete and all tests passed"}'),
        ])
        critic = FakeCriticAdapter.blocking([finding])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertNotEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "LIMIT_REACHED")

    def test_scenario_d_checks_fail_critic_never_runs(self):
        """SCENARIO D: checks fail -> critic must not run -> worker receives check evidence -> worker fixes -> checks pass -> critic runs."""
        # Verification command fails if sentinel.txt exists
        cmd = VerificationCommand(
            "no_sentinel",
            (sys.executable, "-c", "import sys, pathlib; sys.exit(1 if pathlib.Path('sentinel.txt').exists() else 0)"),
        )
        store, worktree, config = self._setup_run(verification_commands=(cmd,))

        # Turn 1: worker creates sentinel.txt (failing check)
        # Turn 2: worker removes sentinel.txt (passing check)
        def worker_turn_2(worktree, task, turn, findings, check_evidence):
            sentinel = worktree / "sentinel.txt"
            if sentinel.exists():
                sentinel.unlink()
            return FakeWorkerAdapter().run(worktree, task, turn)

        worker = FakeWorkerAdapter([
            WorkerAction(edits={"sentinel.txt": "failing"}, output="created sentinel"),
            worker_turn_2,
        ])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(len(worker.invocations), 2)
        # Critic ran ONLY ONCE, after checks passed in turn 2!
        self.assertEqual(len(critic.invocations), 1)
        self.assertEqual(critic.invocations[0]["turn"], 2)

        # Worker turn 2 received the failed check evidence
        turn_2_evidence = worker.invocations[1]["check_evidence"]
        self.assertEqual(len(turn_2_evidence), 1)
        self.assertEqual(turn_2_evidence[0].name, "no_sentinel")
        self.assertEqual(turn_2_evidence[0].returncode, 1)

    def test_scenario_e_invalid_pass_with_findings_fails_closed(self):
        """SCENARIO E: critic emits invalid PASS-with-findings -> fail closed -> never DONE."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])

        invalid_verdict = {
            "verdict": "PASS",
            "blocking_findings": [{
                "id": "f1",
                "severity": "major",
                "claim": "claim",
                "evidence": "ev",
                "location": "loc",
                "required_condition": "cond",
            }],
        }
        critic = FakeCriticAdapter([CriticAction(raw_verdict=json.dumps(invalid_verdict))])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "INVALID_CRITIC_VERDICT")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_scenario_f_repeated_blocker_hits_limit_and_escalates(self):
        """SCENARIO F: same blocker reaches configured repetition limit -> ESCALATED -> never DONE."""
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=10, same_blocker_limit=2))

        recurring_finding = {
            "id": "unhandled-null",
            "severity": "critical",
            "claim": "null check missing",
            "evidence": "foo.py:10",
            "location": "foo.py:10",
            "required_condition": "guard against None",
        }

        worker = FakeWorkerAdapter(default_action=WorkerAction(output="attempted fix"))
        critic = FakeCriticAdapter(default_action=CriticAction(raw_verdict=json.dumps({
            "verdict": "BLOCK",
            "blocking_findings": [recurring_finding],
        })))

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "SAME_BLOCKER_LIMIT_REACHED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_scenario_g_limits_reached_wall_time_and_max_turns(self):
        """SCENARIO G: wall-clock / worker-turn limit reached -> BLOCKED -> never DONE."""
        # 1. Test max_worker_turns
        store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=2, same_blocker_limit=5))

        # Different finding each turn so same_blocker_limit is not triggered
        def critic_varying(worktree, task, turn, check_results):
            f = {
                "id": f"finding-{turn}",
                "severity": "major",
                "claim": f"claim {turn}",
                "evidence": f"ev {turn}",
                "location": f"loc:{turn}",
                "required_condition": f"fix {turn}",
            }
            return FakeCriticAdapter.blocking([f]).run(worktree, task, turn, check_results)

        worker = FakeWorkerAdapter(default_action=WorkerAction(output="working"))
        critic = FakeCriticAdapter([critic_varying, critic_varying, critic_varying])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "LIMIT_REACHED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

        # 2. Test wall_time limit
        store2, worktree2, config2 = self._setup_run(limits=Limits("10s", max_worker_turns=10, same_blocker_limit=5))
        simulated_time = [1000.0]

        def fake_clock():
            simulated_time[0] += 20.0  # Advance past 10s limit
            return simulated_time[0]

        worker2 = FakeWorkerAdapter(default_action=WorkerAction(output="working"))
        critic2 = FakeCriticAdapter.passing()
        supervisor2 = Supervisor(store2, config2, self.task, worktree2, self.runner, worker2, critic2, clock=fake_clock)
        final_status2 = supervisor2.run()

        self.assertEqual(final_status2["status"], "BLOCKED")
        self.assertEqual(final_status2["last_event"], "LIMIT_REACHED")

    def test_scenario_h_worker_process_crash_fails_closed(self):
        """SCENARIO H: worker process crashes -> evidence recorded -> never false PASS."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(returncode=137, error="SIGKILL: OOM killed")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "WORKER_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])
        self.assertTrue((store.root / "worker-turn-1-stderr.txt").exists())

    def test_scenario_i_critic_process_crash_fails_closed(self):
        """SCENARIO I: critic process crashes -> evidence recorded -> never false PASS."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        critic = FakeCriticAdapter([CriticAction(returncode=1, error="Codex CLI crashed with exit code 1")])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "CRITIC_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_scenario_j_resume_uses_frozen_config_and_task(self):
        """SCENARIO J: resume an interrupted run -> use frozen task/config -> do not reread mutable repository config."""
        store, worktree, config = self._setup_run()

        finding = {
            "id": "initial-finding",
            "severity": "critical",
            "claim": "claim",
            "evidence": "ev",
            "location": "loc",
            "required_condition": "cond",
        }

        # Round 1: critic blocks
        worker1 = FakeWorkerAdapter([WorkerAction(output="turn 1")])
        critic1 = FakeCriticAdapter.blocking([finding])
        supervisor1 = Supervisor(store, config, self.task, worktree, self.runner, worker1, critic1)

        # Stop after turn 1 block by using max_worker_turns=1 temporarily in the run instance
        supervisor1.config = Config(config.worker_model, config.critic_model, config.verification, Limits("4h", 1, 3))
        status1 = supervisor1.run()
        self.assertEqual(status1["status"], "BLOCKED")
        self.assertEqual(status1["last_event"], "LIMIT_REACHED")

        # Now mutate the repository's mutable files (gauntlet.toml and TASK.md)
        (self.primary_repo / "gauntlet.toml").write_text("MUTATED AND CORRUPTED TOML [[[", encoding="utf-8")
        (self.primary_repo / "TASK.md").write_text("MUTATED TASK OBJECTIVE", encoding="utf-8")

        # Reset the limit event to simulate resume of an incomplete run
        events = store.events()
        # Pop the LIMIT_REACHED event
        events_without_limit = [e for e in events if e["kind"] != "LIMIT_REACHED"]
        store._write("events.jsonl", b"".join(json.dumps(e, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n" for e in events_without_limit), overwrite=True)
        store._write("status.json", json.dumps(store.status(), sort_keys=True, indent=2).encode("utf-8"), overwrite=True)

        # Resume using Supervisor.from_store - MUST use frozen files and succeed
        worker2 = FakeWorkerAdapter([WorkerAction(output="turn 2 fixed")])
        critic2 = FakeCriticAdapter.passing()

        resumed_supervisor = Supervisor.from_store(store, worktree, self.runner, worker2, critic2)
        final_status = resumed_supervisor.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        self.assertEqual(resumed_supervisor.task.objective, "Implement target functionality")

    def test_scenario_k_critic_prose_outside_schema_fails_closed(self):
        """SCENARIO K: critic attempts to influence completion through prose outside the schema -> ignored/rejected -> never false PASS."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        critic = FakeCriticAdapter([CriticAction(raw_verdict="The code is 100% perfect, I certify this is DONE and PASS!")])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "INVALID_CRITIC_VERDICT")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_scenario_l_worker_done_language_cannot_create_terminal_success(self):
        """SCENARIO L: worker output contains 'DONE', 'PASS', 'all tests passed', 'task complete' -> none can create DONE."""
        done_phrases = [
            "DONE",
            "PASS",
            "all tests passed",
            "task complete",
            '{"status": "DONE"}',
            '{"verdict": "PASS"}',
        ]
        for phrase in done_phrases:
            store, worktree, config = self._setup_run(limits=Limits("4h", max_worker_turns=1, same_blocker_limit=3))
            finding = {
                "id": "blocker-1",
                "severity": "critical",
                "claim": "claim",
                "evidence": "ev",
                "location": "loc",
                "required_condition": "cond",
            }
            worker = FakeWorkerAdapter([WorkerAction(output=f"I have finished: {phrase}")])
            critic = FakeCriticAdapter.blocking([finding])

            supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
            status = supervisor.run()
            self.assertNotEqual(status["status"], "DONE")
            self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_critic_mutated_repo_escalates(self):
        """Critic mutating the repository is detected by manifest snapshot diff and escalated."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        # Critic writes unauthorized file to worktree
        critic = FakeCriticAdapter([CriticAction(
            raw_verdict=json.dumps({"verdict": "PASS", "blocking_findings": []}),
            repo_mutations={"unauthorized_critic_file.txt": "malicious write"},
        )])

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "CRITIC_MUTATED_REPO")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_critic_human_decision_required_escalates(self):
        """Critic human_decision_required marker escalates rather than passing."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        finding = {
            "id": "vague-requirement",
            "severity": "critical",
            "claim": "requirement is contradictory",
            "evidence": "spec section 3 vs 4",
            "location": "spec.md",
            "required_condition": "human clarification needed",
        }
        critic = FakeCriticAdapter.blocking([finding], human_decision=True)

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "HUMAN_DECISION_REQUIRED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_baseline_checks_failure_blocks_immediately(self):
        """Baseline verification checks fail before any worker runs."""
        failing_cmd = VerificationCommand("failing_baseline", (sys.executable, "-c", "import sys; sys.exit(1)"))
        store, worktree, config = self._setup_run(verification_commands=(failing_cmd,))
        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "BASELINE_CHECKS_FAILED")
        self.assertEqual(len(worker.invocations), 0)
        self.assertEqual(len(critic.invocations), 0)

    def test_post_critic_final_checks_failure_blocks_immediately(self):
        """If final deterministic checks fail after critic PASS, supervisor fails closed to BLOCKED."""
        # Verification command passes initially and post-worker, but fails on final check
        # We can simulate this by having worker create a file, and verification fails if a sentinel exists
        # Or a command that fails after 2 successful invocations
        counter_file = Path(self.temp_dir.name) / "run_count.txt"
        counter_file.write_text("0", encoding="utf-8")

        script = f"""
import sys, pathlib
cf = pathlib.Path(r'{counter_file}')
count = int(cf.read_text()) + 1
cf.write_text(str(count))
# Fail on invocation 3 (final check: 1 is baseline, 2 is post-worker, 3 is final)
sys.exit(1 if count >= 3 else 0)
"""
        cmd = VerificationCommand("dynamic_check", (sys.executable, "-c", script))
        store, worktree, config = self._setup_run(verification_commands=(cmd,))
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "FINAL_CHECKS_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_resume_restores_pending_findings_after_administrative_events(self):
        """Resume after CRITIC_BLOCKED + administrative events restores pending_findings for the worker."""
        store, worktree, config = self._setup_run()
        finding = {
            "id": "missing-error-handling",
            "severity": "critical",
            "claim": "unhandled exception in parse",
            "evidence": "traceback on line 12",
            "location": "src/parser.py:12",
            "required_condition": "catch ValueError and log warning",
        }
        worker1 = FakeWorkerAdapter([WorkerAction(output="first attempt")])
        critic1 = FakeCriticAdapter.blocking([finding])

        # Run turn 1 with max_worker_turns=1 so it halts cleanly on limit
        supervisor1 = Supervisor(store, config, self.task, worktree, self.runner, worker1, critic1)
        supervisor1.config = Config(config.worker_model, config.critic_model, config.verification, Limits("4h", 1, 3))
        status1 = supervisor1.run()
        self.assertEqual(status1["status"], "BLOCKED")
        self.assertEqual(status1["last_event"], "LIMIT_REACHED")

        # Simulate administrative events appended to the event log
        # First reset the LIMIT_REACHED event so it's resumable
        events = [e for e in store.events() if e["kind"] != "LIMIT_REACHED"]
        store._write("events.jsonl", b"".join(json.dumps(e, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n" for e in events), overwrite=True)
        store.event("RUN_RESUMED", status="RUNNING", detail={"previous_status": "BLOCKED"})
        store.event("OPERATOR_NOTE", status="RUNNING", detail={"note": "operator inspected state"})

        # Resume with fresh supervisor
        worker2 = FakeWorkerAdapter([WorkerAction(output="fixed error handling")])
        critic2 = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker2, critic2)
        resumed.run()

        # Verify that worker2 received the exact findings from turn 1
        self.assertEqual(len(worker2.invocations), 1)
        inv_findings = worker2.invocations[0]["findings"]
        self.assertEqual(len(inv_findings), 1)
        self.assertEqual(inv_findings[0]["id"], "missing-error-handling")
        self.assertEqual(inv_findings[0]["location"], "src/parser.py:12")

    def test_resume_restores_pending_check_evidence_after_administrative_events(self):
        """Resume after POST_WORKER_CHECKS_FAILED + administrative events restores pending_check_evidence for worker."""
        counter_file = Path(self.temp_dir.name) / "post_worker_check_count.txt"
        counter_file.write_text("0", encoding="utf-8")
        script = f"""
import pathlib, sys
cf = pathlib.Path(r'{counter_file}')
count = int(cf.read_text())
cf.write_text(str(count + 1))
if count == 0:
    sys.exit(0)
sys.exit(0 if pathlib.Path('fixed.txt').exists() else 1)
"""
        failing_check = VerificationCommand("check_file", (sys.executable, "-c", script))
        store, worktree, config = self._setup_run(verification_commands=(failing_check,))

        worker1 = FakeWorkerAdapter([WorkerAction(output="didn't create fixed.txt")])
        critic1 = FakeCriticAdapter.passing()

        # Run turn 1 with max_worker_turns=1 so it halts on limit after post worker checks fail
        supervisor1 = Supervisor(store, config, self.task, worktree, self.runner, worker1, critic1)
        supervisor1.config = Config(config.worker_model, config.critic_model, config.verification, Limits("4h", 1, 3))
        status1 = supervisor1.run()
        self.assertEqual(status1["status"], "BLOCKED")
        self.assertEqual(status1["last_event"], "LIMIT_REACHED")

        # Verify POST_WORKER_CHECKS_FAILED was logged
        self.assertIn("POST_WORKER_CHECKS_FAILED", [e["kind"] for e in store.events()])

        # Remove LIMIT_REACHED and add administrative events
        events = [e for e in store.events() if e["kind"] != "LIMIT_REACHED"]
        store._write("events.jsonl", b"".join(json.dumps(e, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n" for e in events), overwrite=True)
        store.event("RUN_RESUMED", status="RUNNING", detail={"previous_status": "BLOCKED"})
        store.event("ADMIN_AUDIT", status="RUNNING")

        # Resume with worker that creates fixed.txt
        worker2 = FakeWorkerAdapter([WorkerAction(edits={"fixed.txt": "created"}, output="fixed")])
        critic2 = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker2, critic2)
        final_status = resumed.run()

        # Verify worker received the check evidence from the failed check
        self.assertEqual(len(worker2.invocations), 1)
        evidence = worker2.invocations[0]["check_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].name, "check_file")
        self.assertNotEqual(evidence[0].returncode, 0)
        self.assertEqual(final_status["status"], "DONE")

    def test_resume_mid_cycle_stages(self):
        """Interrupted run at intermediate stages resumes from the correct stage without re-running prior stages."""
        store, worktree, config = self._setup_run()
        # Execute baseline checks
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        # Simulate worker turn 1 finished
        store.freeze_text("worker-turn-1-output.txt", "simulated worker output")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1, "returncode": 0})
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run in turn 1")])
        critic = FakeCriticAdapter.passing()
        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "DONE")
        # Worker was not invoked because turn 1 was already finished; it proceeded straight to checks -> critic -> done
        self.assertEqual(len(worker.invocations), 0)
        self.assertEqual(len(critic.invocations), 1)

    def test_resume_final_checks_interruption_does_not_recreate_critic_artifacts(self):
        """Resuming after CRITIC_PASSED or FINAL_CHECKS_STARTED must not re-run critic or re-freeze critic manifests."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.freeze_text("worker-turn-1-output.txt", "simulated worker output")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1, "returncode": 0})
        store.freeze_json("verification-post-worker-turn-1.json", [])
        store.event("POST_WORKER_CHECKS_PASSED", status="RUNNING", detail={"turn": 1})

        # Pre-freeze critic artifacts as if critic ran and passed
        manifest = snapshot_manifest(worktree)
        store.freeze_json("manifest-before-critic-turn-1.json", manifest)
        store.freeze_json("manifest-after-critic-turn-1.json", manifest)
        store.freeze_text("critic-turn-1-raw.txt", json.dumps({"verdict": "PASS", "blocking_findings": []}))
        store.freeze_json("critic-turn-1-verdict.json", {"verdict": "PASS", "blocking_findings": []})
        store.event("CRITIC_PASSED", status="RUNNING", detail={"turn": 1})
        store.event("FINAL_CHECKS_STARTED", status="RUNNING", detail={"turn": 1})
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter([CriticAction(raw_verdict="should not run")])

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        self.assertEqual(len(worker.invocations), 0)
        self.assertEqual(len(critic.invocations), 0)

    def test_resume_rejects_stale_critic_pass_if_worktree_changed(self):
        """If worktree is modified after critic PASS before resume, supervisor escalates and refuses to declare DONE."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.freeze_text("worker-turn-1-output.txt", "simulated worker output")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1, "returncode": 0})
        store.freeze_json("verification-post-worker-turn-1.json", [])
        store.event("POST_WORKER_CHECKS_PASSED", status="RUNNING", detail={"turn": 1})

        manifest = snapshot_manifest(worktree)
        store.freeze_json("manifest-before-critic-turn-1.json", manifest)
        store.freeze_json("manifest-after-critic-turn-1.json", manifest)
        store.freeze_text("critic-turn-1-raw.txt", json.dumps({"verdict": "PASS", "blocking_findings": []}))
        store.freeze_json("critic-turn-1-verdict.json", {"verdict": "PASS", "blocking_findings": []})
        store.event("CRITIC_PASSED", status="RUNNING", detail={"turn": 1})
        store.event("FINAL_CHECKS_STARTED", status="RUNNING", detail={"turn": 1})
        store.event("RUN_RESUMED", status="RUNNING")

        # Mutate worktree outside supervisor before resuming
        (worktree / "tampered.txt").write_text("unreviewed change", encoding="utf-8")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter([CriticAction(raw_verdict="should not run")])

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "WORKTREE_MUTATED_OUTSIDE_SUPERVISOR")
        self.assertNotEqual(final_status["status"], "DONE")
        latest_event = store.events()[-1]
        self.assertIn("tampered.txt", latest_event["detail"]["diff"]["added"])
        self.assertEqual(len(worker.invocations), 0)
        self.assertEqual(len(critic.invocations), 0)

    def test_resume_critic_stage_rejects_mutated_worktree(self):
        """If worktree is modified outside supervisor when reusing critic artifacts, supervisor escalates."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.freeze_text("worker-turn-1-output.txt", "simulated worker output")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1, "returncode": 0})
        store.freeze_json("verification-post-worker-turn-1.json", [])
        store.event("POST_WORKER_CHECKS_PASSED", status="RUNNING", detail={"turn": 1})
        store.event("CRITIC_STARTED", status="RUNNING", detail={"turn": 1})

        manifest = snapshot_manifest(worktree)
        store.freeze_json("manifest-before-critic-turn-1.json", manifest)
        store.freeze_json("manifest-after-critic-turn-1.json", manifest)
        store.freeze_text("critic-turn-1-raw.txt", json.dumps({"verdict": "PASS", "blocking_findings": []}))
        store.freeze_json("critic-turn-1-verdict.json", {"verdict": "PASS", "blocking_findings": []})

        # Mutate worktree outside supervisor
        (worktree / "README.md").write_text("tampered readme", encoding="utf-8")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter([CriticAction(raw_verdict="should not run")])

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "WORKTREE_MUTATED_OUTSIDE_SUPERVISOR")
        self.assertNotEqual(final_status["status"], "DONE")
        latest_event = store.events()[-1]
        self.assertIn("README.md", latest_event["detail"]["diff"]["modified"])

    def test_critic_mutating_git_metadata_escalates(self):
        """Critic mutating git metadata (such as worktree .git file or linked repo HEAD) is caught and escalated."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])

        # Simulate critic committing changes into git or modifying git metadata
        def mutate_git(**kwargs):
            git_target = worktree / ".git"
            if git_target.is_file():
                content = git_target.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    gitdir = Path(content.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (worktree / gitdir).resolve()
                    (gitdir / "mutated_git_meta.txt").write_text("mutation by critic", encoding="utf-8")
            return FakeCriticAdapter.passing().run(worktree, kwargs.get("task"), kwargs.get("turn"))

        critic = FakeCriticAdapter([mutate_git])
        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "CRITIC_MUTATED_REPO")
        latest_event = store.events()[-1]
        self.assertIn(".git-meta/mutated_git_meta.txt", latest_event["detail"]["diff"]["added"])

    def test_verification_command_times_out_with_wall_time_deadline(self):
        """A hanging verification command is bounded by the wall-time limit and fails rather than hanging."""
        hanging_cmd = VerificationCommand(
            "hang",
            (sys.executable, "-c", "import time; time.sleep(10)"),
        )
        store, worktree, config = self._setup_run(
            verification_commands=(hanging_cmd,),
            limits=Limits("1s", 5, 3),
        )
        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "BASELINE_CHECKS_FAILED")
        self.assertEqual(len(worker.invocations), 0)

    def test_resume_worker_turn_interrupted_after_output_written(self):
        """If interrupted after worker output artifact is written but before WORKER_FINISHED is logged, resume proceeds safely."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        # Worker started and froze output, but was interrupted before WORKER_FINISHED event
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_json("worker-turn-1-result.json", {"returncode": 0, "timed_out": False})
        store.freeze_text("worker-turn-1-output.txt", "completed work before crash")
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        self.assertEqual(len(worker.invocations), 0)

    def test_wall_time_deadline_enforced_with_empty_verification_commands(self):
        """Worker or critic exceeding wall-time limit fails closed to BLOCKED even if verification commands is empty."""
        store, worktree, config = self._setup_run(
            verification_commands=(),
            limits=Limits("10s", 5, 3),
        )

        # Custom clock that jumps forward past wall time after worker runs
        current_time = [1000.0]
        def advancing_clock():
            val = current_time[0]
            current_time[0] += 15.0  # Advances by 15s each call (exceeding 10s limit)
            return val

        worker = FakeWorkerAdapter([WorkerAction(output="finished")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic, clock=advancing_clock)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "LIMIT_REACHED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])

    def test_critic_mutating_nested_git_metadata_escalates(self):
        """Critic mutating nested gitdir files (e.g. refs/heads/branch) is detected and escalated."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])

        def mutate_nested_git(**kwargs):
            git_target = worktree / ".git"
            if git_target.is_file():
                content = git_target.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    gitdir = Path(content.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (worktree / gitdir).resolve()
                    nested_ref = gitdir / "refs" / "heads" / "critic-injected-branch"
                    nested_ref.parent.mkdir(parents=True, exist_ok=True)
                    nested_ref.write_text("0123456789abcdef", encoding="utf-8")
            return FakeCriticAdapter.passing().run(worktree, kwargs.get("task"), kwargs.get("turn"))

        critic = FakeCriticAdapter([mutate_nested_git])
        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "CRITIC_MUTATED_REPO")
        latest_event = store.events()[-1]
        self.assertIn(".git-meta/refs/heads/critic-injected-branch", latest_event["detail"]["diff"]["added"])

    def test_critic_mutating_common_git_metadata_escalates(self):
        """Critic mutating the primary repository's common gitdir (e.g. .git/config) is detected and escalated."""
        store, worktree, config = self._setup_run()
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])

        def mutate_common_git(**kwargs):
            git_target = worktree / ".git"
            if git_target.is_file():
                content = git_target.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    gitdir = Path(content.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (worktree / gitdir).resolve()
                    commondir_file = gitdir / "commondir"
                    if commondir_file.is_file():
                        c_text = commondir_file.read_text(encoding="utf-8").strip()
                        c_path = Path(c_text)
                        commondir = (gitdir / c_path).resolve() if not c_path.is_absolute() else c_path.resolve()
                        config_file = commondir / "config"
                        if config_file.exists():
                            with config_file.open("a", encoding="utf-8") as f:
                                f.write("\n# critic injected common config comment\n")
            return FakeCriticAdapter.passing().run(worktree, kwargs.get("task"), kwargs.get("turn"))

        critic = FakeCriticAdapter([mutate_common_git])
        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic)
        final_status = supervisor.run()

        self.assertEqual(final_status["status"], "ESCALATED")
        self.assertEqual(final_status["last_event"], "CRITIC_MUTATED_REPO")
        latest_event = store.events()[-1]
        self.assertIn(".git-common/config", latest_event["detail"]["diff"]["modified"])

    def test_resume_failed_worker_turn_preserves_failure_status(self):
        """Interrupted worker that timed out or returned non-zero must not be converted to success on resume."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        # Persist structured exit result indicating non-zero failure
        store.freeze_json("worker-turn-1-result.json", {"returncode": 1, "timed_out": False})
        store.freeze_text("worker-turn-1-output.txt", "failed work before crash")
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        # Must fail closed to BLOCKED with WORKER_FAILED, NEVER DONE
        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "WORKER_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])
        self.assertEqual(len(worker.invocations), 0)

    def test_resume_worker_output_without_result_fails_closed(self):
        """If worker output exists without a recorded exit result, resume must fail closed rather than inferring success."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_text("worker-turn-1-output.txt", "unrecorded exit outcome")
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "BLOCKED")
        self.assertEqual(final_status["last_event"], "WORKER_FAILED")
        self.assertNotIn("DONE", [e["status"] for e in store.events()])
        self.assertEqual(len(worker.invocations), 0)

    def test_resume_interrupted_after_post_worker_checks_artifact_written(self):
        """Interruption after post-worker checks artifact is frozen reuses the artifact without StoreError."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_json("worker-turn-1-result.json", {"returncode": 0, "timed_out": False})
        store.freeze_text("worker-turn-1-output.txt", "worker finished")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1})

        # Post-worker checks started and artifact frozen, but interrupted before POST_WORKER_CHECKS_PASSED
        store.event("POST_WORKER_CHECKS_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_json("verification-post-worker-turn-1.json", [
            {"name": "pass", "argv": ["python", "-c", "import sys; sys.exit(0)"], "returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        ])
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")
        self.assertEqual(len(worker.invocations), 0)

    def test_resume_interrupted_after_critic_manifest_before_written(self):
        """Interruption after manifest-before-critic is frozen reuses the manifest without StoreError."""
        store, worktree, config = self._setup_run()
        store.event("BASELINE_CHECKS_PASSED", status="READY")
        store.event("WORKER_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_json("worker-turn-1-result.json", {"returncode": 0, "timed_out": False})
        store.freeze_text("worker-turn-1-output.txt", "worker finished")
        store.event("WORKER_FINISHED", status="RUNNING", detail={"turn": 1})
        store.event("POST_WORKER_CHECKS_STARTED", status="RUNNING", detail={"turn": 1})
        store.freeze_json("verification-post-worker-turn-1.json", [
            {"name": "pass", "argv": ["python", "-c", "import sys; sys.exit(0)"], "returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        ])
        store.event("POST_WORKER_CHECKS_PASSED", status="RUNNING", detail={"turn": 1})

        # Critic started and manifest-before frozen, but interrupted before manifest-after or verdict
        store.event("CRITIC_STARTED", status="RUNNING", detail={"turn": 1})
        from gauntlet.supervisor import snapshot_manifest
        store.freeze_json("manifest-before-critic-turn-1.json", snapshot_manifest(worktree))
        store.event("RUN_RESUMED", status="RUNNING")

        worker = FakeWorkerAdapter([WorkerAction(output="should not run")])
        critic = FakeCriticAdapter.passing()

        resumed = Supervisor.from_store(store, worktree, self.runner, worker, critic)
        final_status = resumed.run()

        self.assertEqual(final_status["status"], "DONE")
        self.assertEqual(final_status["last_event"], "COMPLETED")

    def test_verification_command_sequence_shares_deadline(self):
        """A sequence of verification commands shares the total wall-clock budget."""
        current_time = [100.0]
        def advancing_clock():
            val = current_time[0]
            current_time[0] += 12.0  # Advances by 12s per call (exceeding 10s timeout)
            return val

        from gauntlet.verification import run_commands

        cmd1 = VerificationCommand("cmd1", (sys.executable, "-c", "import sys; sys.exit(0)"))
        cmd2 = VerificationCommand("cmd2", (sys.executable, "-c", "import sys; sys.exit(0)"))

        # Total timeout 10s: cmd1 runs (time becomes 106), cmd2 has 4s left. But next call advances past 10s
        results = run_commands((cmd1, cmd2), cwd=self.primary_repo, runner=self.runner, timeout=10.0, clock=advancing_clock)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].returncode, 0)
        self.assertEqual(results[1].returncode, 124)
        self.assertTrue(results[1].timed_out)

    def test_baseline_checks_included_in_wall_time_budget(self):
        """Time consumed by baseline checks counts against the wall_time budget."""
        current_time = [100.0]
        def advancing_clock():
            val = current_time[0]
            current_time[0] += 6.0  # Advances 6s per call
            return val

        store, worktree, config = self._setup_run(limits=Limits("10s", 5, 3))
        worker = FakeWorkerAdapter([WorkerAction(output="ready")])
        critic = FakeCriticAdapter.passing()

        supervisor = Supervisor(store, config, self.task, worktree, self.runner, worker, critic, clock=advancing_clock)
        status = supervisor.run()

        self.assertEqual(status["status"], "BLOCKED")
        self.assertEqual(status["last_event"], "LIMIT_REACHED")
        self.assertEqual(len(worker.invocations), 0)


if __name__ == "__main__":
    unittest.main()
