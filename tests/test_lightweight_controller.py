from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from collab import git_evidence
from collab.common import CollabError
from collab.controller import Controller
from collab.gateway import Gateway


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


class Desktop:
    def status(self):
        return {"state": "CONNECTED"}

    def execution_configuration(self, preset):
        return {"agent_preset": preset, "preset_sha256": "fixture-preset"}

    def close(self):
        pass


class ReconcilingDesktop(Desktop):
    def __init__(self, *, running=False, terminal="completed"):
        self.running = running
        self.terminal = terminal
        self.cancelled = []

    def task_state(self, session_id):
        return {"running": self.running, "queue": [], "pendingInput": [], "cursor": 9}

    def task_events(self, session_id):
        records = [{"event": {"seq": 9, "type": "turn/end",
                              "data": {"reason": {"kind": self.terminal}}}}] if self.terminal else []
        return {"cursor": 9, "records": records, "hasMore": False}

    def cancel_task(self, session_id):
        self.cancelled.append(session_id)
        return {"accepted": True}


class DownDesktop(Desktop):
    def status(self):
        return {"state": "UNPAIRED"}


class FakeScheduler:
    def __init__(self):
        self.closing = False
        self.kicks = 0

    def kick(self):
        self.kicks += 1

    def close(self):
        self.closing = True


@unittest.skipUnless(shutil.which("git"), "git is unavailable")
class LightweightControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "input.txt").write_text("input\n", encoding="utf-8")
        git(self.workspace, "init", "-q")
        git(self.workspace, "config", "user.email", "fixture@example.invalid")
        git(self.workspace, "config", "user.name", "fixture")
        git(self.workspace, "add", "input.txt")
        git(self.workspace, "commit", "-qm", "baseline")
        self.scheduler = FakeScheduler()
        self.controller = Controller(self.base / "state", self.workspace,
                                     {"instance_id": "fixture"}, native_enabled=True,
                                     desktop=Desktop(), scheduler_factory=lambda *args: self.scheduler)
        self.addCleanup(self.controller.close)

    def arguments(self, **updates):
        return {"request_id": "submit-1", "root": ".", "goal": "write output",
                "read_inputs": ["input.txt"], "write_paths": ["out.txt"],
                "constraints": [], "acceptance": ["out exists"], **updates}

    def prepare_run(self, request_id="submit-1"):
        run = self.controller.submit(self.arguments(request_id=request_id))["run"]
        self.controller.store.claim_next()
        run_dir = self.base / "state/runs" / run["id"]
        base = git_evidence.prepare_worktree(self.workspace, run_dir, run["id"], self.base)
        worktree = Path(base["worktree"])
        (worktree / "out.txt").write_text("after\n", encoding="utf-8")
        manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"])
        delivered = self.controller.store.finish(
            run["id"], {"status": "EXECUTION_COMPLETED"}, manifest)
        return run["id"], delivered, worktree, run_dir

    def prepare_paused_run(self, request_id="paused-1"):
        run = self.controller.submit(self.arguments(request_id=request_id))["run"]
        self.controller.store.claim_next()
        run_dir = self.base / "state/runs" / run["id"]
        base = git_evidence.prepare_worktree(self.workspace, run_dir, run["id"], self.base)
        worktree = Path(base["worktree"])
        (worktree / "out.txt").write_text("partial\n", encoding="utf-8")
        manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"])
        paused = self.controller.store.finish(run["id"], {
            "status": "OUTCOME_UNKNOWN", "outcome_reason": "MAX_OUTPUT_TOKENS", "partial": True,
            "session_id": "paused-session", "permission_preset": "danger-full-access",
            "prompt_request_id": "desktop-paused", "final_state": {"cursor": 9},
        }, manifest)
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        return run["id"], paused, worktree, run_dir

    def test_invalid_submissions_are_rejected_before_execution(self):
        gateway = Gateway(self.controller)
        missing = gateway.handle("collab_submit", {})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "INVALID_INPUT")
        self.assertTrue(missing["error"].get("message"))
        unknown = gateway.handle("not_a_tool", {})
        self.assertEqual(unknown["error"]["code"], "UNKNOWN_TOOL")
        absent = gateway.handle("collab_get", {"id": "no-such-run"})
        self.assertEqual(absent["error"]["code"], "NOT_FOUND")
        acceptance = ["x" * 16000] * 100
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(acceptance=acceptance))
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.controller.get({})["items"], [])

    def test_dry_run_does_not_create_run(self):
        result = self.controller.submit(self.arguments(dry_run=True))
        self.assertTrue(result["ready"])
        self.assertNotIn("evidence_mode", result["spec"])
        self.assertEqual(self.controller.get({})["items"], [])

    def test_submit_uses_the_callers_execution_workspace(self):
        caller = self.base / "caller-workspace"
        caller.mkdir()
        (caller / "input.txt").write_text("caller\n", encoding="utf-8")
        git(caller, "init", "-q")
        git(caller, "config", "user.email", "fixture@example.invalid")
        git(caller, "config", "user.name", "fixture")
        git(caller, "add", "input.txt")
        git(caller, "commit", "-qm", "caller baseline")
        result = self.controller.submit(self.arguments(
            root=".", execution_workspace=str(caller), dry_run=True))
        self.assertTrue(result["ready"])
        self.assertEqual(result["spec"]["workspace"], str(caller.resolve()))
        self.assertEqual(result["spec"]["root"], str(caller.resolve()))
        self.assertEqual(self.controller.get({})["items"], [])
        for name, value in (("missing", self.base / "missing"), ("contains-state", self.base)):
            with self.subTest(invalid_workspace=name):
                with self.assertRaises(CollabError) as caught:
                    self.controller.submit(self.arguments(execution_workspace=str(value), dry_run=True))
                self.assertEqual(caught.exception.code, "INVALID_WORKSPACE")

    def test_nested_task_spec_reuses_parent_repository(self):
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "input.txt").write_text("nested\n", encoding="utf-8")
        git(self.workspace, "add", "nested/input.txt")
        git(self.workspace, "commit", "-qm", "nested baseline")
        result = self.controller.submit(self.arguments(root="nested", dry_run=True))
        self.assertTrue(result["ready"])
        self.assertEqual(result["spec"]["repository_root"], str(self.workspace.resolve()))
        self.assertEqual(result["spec"]["task_root_relative"], "nested")
        self.assertFalse(result["spec"]["git_init_required"])
        self.assertFalse((nested / ".git").exists())

    def plain_controller(self, name):
        root = self.base / name
        root.mkdir()
        controller = Controller(self.base / (name + "-state"), root, {"instance_id": name},
                                native_enabled=True, desktop=Desktop(),
                                scheduler_factory=lambda *args: self.scheduler)
        self.addCleanup(controller.close)
        return root, controller

    def test_submit_initializes_git_with_a_safe_baseline(self):
        root, controller = self.plain_controller("plain-init")
        (root / "input.txt").write_text("input\n", encoding="utf-8")
        (root / ".env").write_text("DO_NOT_COMMIT=1\n", encoding="utf-8")
        (root / ".venv").mkdir()
        (root / ".venv/dependency.txt").write_text("ignored\n", encoding="utf-8")
        dry = controller.submit(self.arguments(dry_run=True))
        self.assertTrue(dry["ready"])
        self.assertTrue(dry["spec"]["git_init_required"])
        self.assertFalse((root / ".git").exists())
        submitted = controller.submit(self.arguments(request_id="plain-init-submit"))
        self.assertEqual(submitted["run"]["state"], "QUEUED")
        stored = controller.store.get(submitted["run"]["id"])["spec"]
        self.assertTrue(stored["git_initialized"])
        self.assertEqual(stored["baseline_files"], 1)
        self.assertEqual(stored["baseline_excluded"], [".env"])
        tracked = git(root, "ls-files").stdout.splitlines()
        self.assertEqual(tracked, ["input.txt"])
        self.assertEqual((root / ".env").read_text(encoding="utf-8"), "DO_NOT_COMMIT=1\n")
        self.assertEqual(git(root, "log", "-1", "--pretty=%s").stdout.strip(),
                         "Baseline before DeepSeek collaboration")
        run_dir = self.base / "plain-init-state/runs" / submitted["run"]["id"]
        base = git_evidence.prepare_worktree(root, run_dir, submitted["run"]["id"], self.base)
        self.assertTrue((Path(base["worktree"]) / "input.txt").is_file())
        git_evidence.cleanup_worktree(run_dir)

    def test_baseline_rejects_large_file_before_creating_git(self):
        root, controller = self.plain_controller("plain-large")
        (root / "input.txt").write_text("input exceeds test limit\n", encoding="utf-8")
        with mock.patch("collab.git_evidence.MAX_BASELINE_FILE_BYTES", 10):
            with self.assertRaises(CollabError) as caught:
                controller.submit(self.arguments(request_id="plain-large-submit"))
        self.assertEqual(caught.exception.code, "GIT_BASELINE_TOO_LARGE")
        self.assertFalse((root / ".git").exists())

    def test_submit_wait_diff_and_accept(self):
        submitted = self.controller.submit(self.arguments())
        run_id = submitted["run"]["id"]
        self.assertNotIn("spec", submitted["run"])
        self.assertIn("summary", submitted["run"])
        self.scheduler.closing = True
        replay = self.controller.submit(self.arguments())
        self.scheduler.closing = False
        self.assertTrue(replay["idempotent_replay"])
        run_id, delivered, worktree, run_dir = self.prepare_run()
        self.controller._changed()
        waited = self.controller.wait({"id": run_id, "after_version": 2, "max_seconds": 1})
        self.assertEqual(waited["run"]["state"], "DELIVERED")
        summary = self.controller.get({"id": run_id, "view": "summary"})
        self.assertEqual(summary["manifest_summary"]["changed_files"], 1)
        diff = self.controller.get({"id": run_id, "view": "diff"})
        self.assertEqual(diff["changes"][0]["evidence_mode"], "worktree")
        self.assertIn("+after", diff["changes"][0]["diff"])
        self.assertTrue(diff["changes"][0]["after_matches_current"])
        current = self.controller.get({"id": run_id, "current": True})
        self.assertEqual(current["current_changes"]["changes"], [])
        reviewed = self.controller.review({"request_id": "review-1", "id": run_id,
                                           "expected_version": delivered["version"],
                                           "conclusion": "ACCEPTED", "summary": "checked",
                                           "checks": ["diff checked"]})
        self.assertEqual(reviewed["run"]["state"], "ACCEPTED")
        self.assertFalse(worktree.exists())
        self.assertEqual((self.workspace / "out.txt").read_text(encoding="utf-8"), "after\n")
        cleaned = self.controller.get({"id": run_id, "view": "diff"})
        self.assertIsNone(cleaned["changes"][0]["after_matches_current"])
        self.assertEqual(cleaned["changes"][0]["reason"], "WORKTREE_CLEANED_UP")
        replay_review = self.controller.review({"request_id": "review-1", "id": run_id,
                                                "expected_version": delivered["version"],
                                                "conclusion": "ACCEPTED", "summary": "checked",
                                                "checks": ["diff checked"]})
        self.assertTrue(replay_review["idempotent_replay"])
        self.assertEqual(replay_review["run"]["state"], "ACCEPTED")
        self.assertFalse((run_dir / "worktree.patch").read_text(encoding="utf-8").strip() == "")

    def test_desktop_not_ready_is_reported_before_submit(self):
        down = Controller(self.base / "down-state", self.workspace, {"instance_id": "down"},
                          native_enabled=False, desktop=DownDesktop())
        self.addCleanup(down.close)
        status = down.status()
        self.assertEqual(status["desktop"]["state"], "UNPAIRED")
        self.assertTrue(status["desktop"]["repair"]["required"])
        self.assertIn("desktop pair", status["desktop"]["repair"]["command"])
        with self.assertRaises(CollabError) as caught:
            down.submit(self.arguments(request_id="down-submit"))
        self.assertEqual(caught.exception.code, "DESKTOP_NOT_READY")

    def test_root_lease_blocks_second_submit(self):
        first = self.controller.submit(self.arguments(request_id="lease-1"))["run"]
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(request_id="lease-2"))
        self.assertEqual(caught.exception.code, "ROOT_BUSY")
        self.controller.cancel({"request_id": "lease-cancel", "id": first["id"],
                                "expected_version": 1})
        second = self.controller.submit(self.arguments(request_id="lease-2"))["run"]
        self.assertEqual(second["state"], "QUEUED")

    def test_simultaneous_submits_cannot_pass_the_same_root_lease(self):
        barrier = threading.Barrier(2)

        def ready():
            barrier.wait(timeout=5)
            return {"state": "CONNECTED"}

        def submit(index):
            try:
                return self.controller.submit(self.arguments(request_id=f"race-{index}"))["run"]["state"]
            except CollabError as error:
                return error.code

        with mock.patch.object(self.controller.desktop, "status", side_effect=ready):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(submit, range(2)))
        self.assertCountEqual(outcomes, ["QUEUED", "ROOT_BUSY"])
        self.assertEqual(self.controller.store.counts(), {"QUEUED": 1})
        self.assertEqual(len(list((self.base / "state/runs").iterdir())), 1)

    def test_review_requires_re_review_after_main_advance(self):
        run_id, delivered, worktree, run_dir = self.prepare_run("rebase-review")
        (self.workspace / "other.txt").write_text("main advanced\n", encoding="utf-8")
        git(self.workspace, "add", "other.txt")
        git(self.workspace, "commit", "-qm", "main advanced")
        with self.assertRaises(CollabError) as caught:
            self.controller.review({"request_id": "rebase-review-1", "id": run_id,
                                    "expected_version": delivered["version"],
                                    "conclusion": "ACCEPTED", "summary": "stale review",
                                    "checks": ["old diff"]})
        self.assertEqual(caught.exception.code, "REVIEW_BASE_CHANGED")
        self.assertEqual(self.controller.store.get(run_id)["state"], "DELIVERED")
        self.assertFalse((self.workspace / "out.txt").exists())
        diff = self.controller.get({"id": run_id, "view": "diff"})
        self.assertIn("+after", diff["changes"][0]["diff"])
        reviewed = self.controller.review({"request_id": "rebase-review-2", "id": run_id,
                                           "expected_version": delivered["version"],
                                           "conclusion": "ACCEPTED", "summary": "rebased diff reviewed",
                                           "checks": ["new diff reviewed"]})
        self.assertEqual(reviewed["run"]["state"], "ACCEPTED")
        self.assertEqual((self.workspace / "out.txt").read_text(encoding="utf-8"), "after\n")
        self.assertFalse(worktree.exists())

    def test_review_busy_on_same_root(self):
        run = self.controller.submit(self.arguments(request_id="busy-run"))["run"]
        self.controller.store.claim_next()
        with self.assertRaises(CollabError) as caught:
            self.controller.review({"request_id": "busy-review", "id": run["id"],
                                    "expected_version": 2, "conclusion": "ACCEPTED",
                                    "summary": "blocked", "checks": ["none"]})
        self.assertEqual(caught.exception.code, "RUN_BUSY")

    def test_paused_resume_is_idempotent_and_keeps_root_lease(self):
        run_id, paused, worktree, run_dir = self.prepare_paused_run()
        with self.assertRaises(CollabError) as caught:
            self.controller.review({"request_id": "paused-accept", "id": run_id,
                                    "expected_version": paused["version"], "conclusion": "ACCEPTED",
                                    "summary": "must not accept", "checks": ["partial only"]})
        self.assertEqual(caught.exception.code, "INVALID_STATE")
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(request_id="paused-other-submit"))
        self.assertEqual(caught.exception.code, "ROOT_BUSY")
        arguments = {"request_id": "resume-1", "id": run_id,
                     "expected_version": paused["version"], "instructions": "continue"}
        public = Gateway(self.controller).handle("collab_resume", arguments)
        self.assertTrue(public["ok"])
        resumed = public["result"]
        self.assertEqual(resumed["run"]["state"], "QUEUED")
        self.assertEqual(self.scheduler.kicks, 2)
        replay = self.controller.resume(arguments)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.scheduler.kicks, 2)
        with self.assertRaises(CollabError) as caught:
            self.controller.resume({**arguments, "instructions": "different"})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_resume_replay_normalizes_optional_instructions(self):
        for index, instructions in enumerate((None, "  continue  ")):
            with self.subTest(instructions=instructions):
                run_id, paused, _, _ = self.prepare_paused_run(f"normalization-{index}")
                arguments = {"request_id": f"normalized-resume-{index}", "id": run_id,
                             "expected_version": paused["version"]}
                if instructions is not None:
                    arguments["instructions"] = instructions
                public = Gateway(self.controller).handle("collab_resume", arguments)
                self.assertTrue(public["ok"], public)
                first = public["result"]
                for replay_arguments in (arguments, {**arguments, "instructions": (instructions or "").strip()}):
                    replay = self.controller.resume(replay_arguments)
                    self.assertTrue(replay["idempotent_replay"])
                    self.assertEqual(replay["run"], first["run"])
                self.assertEqual(self.controller.store.get(run_id)["result"]["continuation_count"], 1)
                self.controller.cancel({"request_id": f"normalized-cancel-{index}", "id": run_id,
                                        "expected_version": first["run"]["version"]})

    def test_paused_run_can_be_rejected_or_cancelled(self):
        first_id, first, _, _ = self.prepare_paused_run("paused-reject")
        rejected = self.controller.review({"request_id": "paused-reject-review", "id": first_id,
                                           "expected_version": first["version"], "conclusion": "REJECTED",
                                           "summary": "discard partial work", "checks": ["not complete"]})
        self.assertEqual(rejected["run"]["state"], "REJECTED")
        second_id, second, _, second_dir = self.prepare_paused_run("paused-cancel")
        manifest = self.controller.store.get(second_id)["result"]["manifest"]
        cancelled = self.controller.cancel({"request_id": "paused-cancel-request", "id": second_id,
                                            "expected_version": second["version"]})
        self.assertEqual(cancelled["run"]["state"], "CANCELLED")
        self.assertFalse(Path(manifest["worktree"]).exists())

    def test_resume_rejects_worktree_changed_after_pause(self):
        run_id, paused, worktree, _ = self.prepare_paused_run("paused-changed")
        (worktree / "out.txt").write_text("external-change\n", encoding="utf-8")
        with self.assertRaises(CollabError) as caught:
            self.controller.resume({"request_id": "changed-resume", "id": run_id,
                                    "expected_version": paused["version"]})
        self.assertEqual(caught.exception.code, "WORKTREE_CHANGED_AFTER_DELIVERY")
        self.assertEqual(self.controller.store.get(run_id)["state"], "PAUSED")

    def test_resume_count_and_original_deadline_are_bounded(self):
        run_id, paused, worktree, run_dir = self.prepare_paused_run("paused-limits")
        for index in range(2):
            resumed = self.controller.resume({"request_id": f"resume-limit-{index}", "id": run_id,
                                              "expected_version": paused["version"]})
            self.assertEqual(resumed["run"]["state"], "QUEUED")
            self.controller.store.claim_next()
            paused = self.controller.store.finish(run_id, {
                "status": "OUTCOME_UNKNOWN", "outcome_reason": "MAX_OUTPUT_TOKENS", "partial": True,
                "session_id": "paused-session", "permission_preset": "danger-full-access",
                "prompt_request_id": f"desktop-paused-{index}", "final_state": {"cursor": 10 + index},
            }, paused["result"]["manifest"])
        with self.assertRaises(CollabError) as caught:
            self.controller.resume({"request_id": "resume-over-limit", "id": run_id,
                                    "expected_version": paused["version"]})
        self.assertEqual(caught.exception.code, "CONTINUATION_LIMIT")
        self.controller.review({"request_id": "resume-limit-reject", "id": run_id,
                                "expected_version": paused["version"], "conclusion": "REJECTED",
                                "summary": "stop after continuation limit", "checks": ["limit reached"]})
        second_id, second, _, _ = self.prepare_paused_run("paused-expired")
        with mock.patch("collab.controller.time.time", return_value=4_000_000_000):
            with self.assertRaises(CollabError) as caught:
                self.controller.resume({"request_id": "resume-expired", "id": second_id,
                                        "expected_version": second["version"]})
        self.assertEqual(caught.exception.code, "TIME_BUDGET_EXCEEDED")
        self.assertEqual(self.controller.store.get(second_id)["state"], "PAUSED")

    def test_cancel_queued_and_nonnative_rejection(self):
        run = self.controller.submit(self.arguments())["run"]
        cancelled = self.controller.cancel({"request_id": "cancel-1", "id": run["id"],
                                            "expected_version": 1})
        self.assertEqual(cancelled["run"]["state"], "CANCELLED")
        other = Controller(self.base / "other-state", self.workspace, {"instance_id": "other"},
                           desktop=Desktop())
        self.addCleanup(other.close)
        with self.assertRaises(CollabError) as caught:
            other.submit(self.arguments(request_id="not-native"))
        self.assertEqual(caught.exception.code, "NATIVE_DISABLED")

    def restart_with_session(self, request_id, session_id, desktop, *, with_worktree=False):
        run = self.controller.submit(self.arguments(request_id=request_id))["run"]
        self.controller.store.claim_next()
        run_dir = self.base / "state/runs" / run["id"]
        if with_worktree:
            base = git_evidence.prepare_worktree(self.workspace, run_dir, run["id"], self.base)
            (Path(base["worktree"]) / "out.txt").write_text("recovered\n", encoding="utf-8")
            self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        (run_dir / "session.json").write_text(
            json.dumps({"session_id": session_id}), encoding="utf-8")
        restarted = Controller(self.base / "state", self.workspace, {"instance_id": "restart"},
                               desktop=desktop)
        self.addCleanup(restarted.close)
        return restarted.store.get(run["id"])

    def test_restart_reconciles_completed_session(self):
        recovered = self.restart_with_session(
            "reconcile-complete", "s-complete", ReconcilingDesktop(terminal="completed"),
            with_worktree=True)
        self.assertEqual(recovered["state"], "DELIVERED")
        self.assertEqual(recovered["result"]["delivery"]["recovery_reason"], "SESSION_TERMINAL")
        self.assertTrue(recovered["result"]["delivery"]["completion_verified"])
        self.assertEqual([item["path"] for item in recovered["result"]["manifest"]["changes"]], ["out.txt"])

    def test_restart_completed_session_without_manifest_fails_closed(self):
        recovered = self.restart_with_session(
            "reconcile-no-manifest", "s-no-manifest", ReconcilingDesktop(terminal="completed"))
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["result"]["summary"]["outcome_reason"], "RESULT_MANIFEST_FAILED")
        self.assertEqual(recovered["result"]["manifest"]["error"], "GIT_WORKTREE_FAILED")

    def test_restart_cancels_active_session(self):
        desktop = ReconcilingDesktop(running=True, terminal=None)
        recovered = self.restart_with_session("reconcile-active", "s-active", desktop)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertTrue(recovered["result"]["summary"]["outcome_unknown"])
        self.assertEqual(desktop.cancelled, ["s-active"])

    def test_recovered_unknown_run_also_fails_closed(self):
        run = self.controller.submit(self.arguments(request_id="recover-fallback"))["run"]
        self.controller.store.claim_next()
        restarted = Controller(self.base / "state", self.workspace, {"instance_id": "restart"},
                               desktop=Desktop())
        self.addCleanup(restarted.close)
        recovered = restarted.store.get(run["id"])
        self.assertEqual(recovered["state"], "FAILED")
        self.assertTrue(recovered["result"]["summary"]["outcome_unknown"])
        self.assertEqual(recovered["result"]["manifest"]["changes"], [])
        self.assertEqual(recovered["result"]["manifest"]["error"], "GIT_WORKTREE_FAILED")
        self.assertTrue(restarted.get({"id": run["id"]})["outcome_unknown"])

    def test_review_retries_safely_after_apply_before_store_failure(self):
        run_id, delivered, worktree, run_dir = self.prepare_run("review-crash")
        arguments = {"request_id": "review-crash-request", "id": run_id,
                     "expected_version": delivered["version"], "conclusion": "ACCEPTED",
                     "summary": "checked", "checks": ["actual diff checked"]}
        with mock.patch.object(self.controller.store, "review", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.controller.review(arguments)
        self.assertEqual((self.workspace / "out.txt").read_text(encoding="utf-8"), "after\n")
        self.assertEqual(self.controller.store.get(run_id)["state"], "DELIVERED")
        reviewed = self.controller.review(arguments)
        self.assertEqual(reviewed["run"]["state"], "ACCEPTED")
        self.assertFalse(worktree.exists())

    def test_overlapping_parent_and_nested_roots_are_mutually_exclusive(self):
        nested = self.workspace / "nested"
        sibling = self.workspace / "sibling"
        nested.mkdir()
        sibling.mkdir()
        (nested / "input.txt").write_text("nested\n", encoding="utf-8")
        (sibling / "input.txt").write_text("sibling\n", encoding="utf-8")
        git(self.workspace, "add", "nested/input.txt", "sibling/input.txt")
        git(self.workspace, "commit", "-qm", "nested and sibling")
        parent = self.controller.submit(self.arguments(request_id="overlap-parent", root="."))["run"]
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(request_id="overlap-child", root="nested"))
        self.assertEqual(caught.exception.code, "ROOT_BUSY")
        self.controller.cancel({"request_id": "overlap-parent-cancel", "id": parent["id"],
                                "expected_version": parent["version"]})
        child = self.controller.submit(self.arguments(request_id="overlap-child", root="nested"))["run"]
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(request_id="overlap-parent-2", root="."))
        self.assertEqual(caught.exception.code, "ROOT_BUSY")
        sibling_run = self.controller.submit(self.arguments(request_id="overlap-sibling", root="sibling"))["run"]
        self.assertEqual(sibling_run["state"], "QUEUED")
        self.controller.store.claim_next()
        run_dir = self.base / "state/runs" / child["id"]
        base = git_evidence.prepare_worktree(self.workspace, run_dir, child["id"], self.base, "nested")
        (Path(base["worktree"]) / "nested/out.txt").write_text("after\n", encoding="utf-8")
        manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"])
        delivered = self.controller.store.finish(child["id"], {"status": "EXECUTION_COMPLETED"}, manifest)
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        with self.controller.store._db(immediate=True) as db:
            db.execute("UPDATE runs SET state='RUNNING' WHERE id=?", (parent["id"],))
            db.commit()
        with self.assertRaises(CollabError) as caught:
            self.controller.review({"request_id": "overlap-review-blocked", "id": child["id"],
                                    "expected_version": delivered["version"], "conclusion": "ACCEPTED",
                                    "summary": "blocked by ancestor", "checks": ["checked"]})
        self.assertEqual(caught.exception.code, "RUN_BUSY")

    def test_existing_git_repo_rejects_untracked_subdir_and_inputs_at_submit(self):
        untracked_dir = self.workspace / "uncommitted_pkg"
        untracked_dir.mkdir()
        (untracked_dir / "input.txt").write_text("draft\n", encoding="utf-8")
        for dry_run in (True, False):
            with self.subTest(case="untracked_subdir", dry_run=dry_run):
                with self.assertRaises(CollabError) as caught:
                    self.controller.submit(self.arguments(
                        request_id=f"untracked-dir-{dry_run}", root="uncommitted_pkg", dry_run=dry_run))
                self.assertEqual(caught.exception.code, "GIT_TASK_ROOT_UNAVAILABLE")
        tracked_dir = self.workspace / "tracked_pkg"
        tracked_dir.mkdir()
        (tracked_dir / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(self.workspace, "add", "tracked_pkg/tracked.txt")
        git(self.workspace, "commit", "-qm", "tracked subdir")
        (tracked_dir / "untracked_input.txt").write_text("untracked\n", encoding="utf-8")
        for dry_run in (True, False):
            with self.subTest(case="untracked_input", dry_run=dry_run):
                with self.assertRaises(CollabError) as caught:
                    self.controller.submit(self.arguments(
                        request_id=f"untracked-in-{dry_run}", root="tracked_pkg",
                        read_inputs=["untracked_input.txt"], dry_run=dry_run))
                self.assertEqual(caught.exception.code, "MISSING_INPUT")
        self.assertEqual(self.controller.store.counts(), {})

    def test_plain_directory_is_not_initialized_when_desktop_or_native_is_unavailable(self):
        down_root = self.base / "plain-down"
        down_root.mkdir()
        (down_root / "input.txt").write_text("input\n", encoding="utf-8")
        down = Controller(self.base / "plain-down-state", down_root, {"instance_id": "down-plain"},
                          native_enabled=True, desktop=DownDesktop(),
                          scheduler_factory=lambda *args: self.scheduler)
        self.addCleanup(down.close)
        with self.assertRaises(CollabError) as caught:
            down.submit(self.arguments(request_id="plain-down-submit"))
        self.assertEqual(caught.exception.code, "DESKTOP_NOT_READY")
        self.assertFalse((down_root / ".git").exists())

        readonly_root = self.base / "plain-readonly"
        readonly_root.mkdir()
        (readonly_root / "input.txt").write_text("input\n", encoding="utf-8")
        readonly = Controller(self.base / "plain-readonly-state", readonly_root, {"instance_id": "ro-plain"},
                              native_enabled=False, desktop=Desktop())
        self.addCleanup(readonly.close)
        with self.assertRaises(CollabError) as caught:
            readonly.submit(self.arguments(request_id="plain-ro-submit"))
        self.assertEqual(caught.exception.code, "NATIVE_DISABLED")
        self.assertFalse((readonly_root / ".git").exists())

    def test_get_defaults_to_summary_and_review_guards_out_of_scope_changes(self):
        run = self.controller.submit(self.arguments(request_id="scope-guard"))["run"]
        self.controller.store.claim_next()
        run_dir = self.base / "state/runs" / run["id"]
        base = git_evidence.prepare_worktree(self.workspace, run_dir, run["id"], self.base)
        worktree = Path(base["worktree"])
        (worktree / "out.txt").write_text("in-scope\n", encoding="utf-8")
        (worktree / "outside.txt").write_text("out-of-scope\n", encoding="utf-8")
        manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"])
        delivered = self.controller.store.finish(
            run["id"], {"status": "EXECUTION_COMPLETED"}, manifest)
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)

        default_get = self.controller.get({"id": run["id"]})
        self.assertIn("manifest_summary", default_get)
        self.assertNotIn("result", default_get)
        self.assertEqual(default_get["manifest_summary"]["out_of_scope_files"], 1)
        full_result = self.controller.get({"id": run["id"], "view": "result"})
        self.assertIn("result", full_result)
        self.assertEqual(full_result["result"]["manifest"]["out_of_scope"], ["outside.txt"])
        current_result = self.controller.get({"id": run["id"], "current": True})
        self.assertIn("current_changes", current_result)

        with self.assertRaises(CollabError) as caught:
            self.controller.review({"request_id": "scope-reject-default", "id": run["id"],
                                    "expected_version": delivered["version"],
                                    "conclusion": "ACCEPTED", "summary": "accidental accept",
                                    "checks": ["checked"]})
        self.assertEqual(caught.exception.code, "WRITE_SCOPE_EXCEEDED")
        self.assertFalse((self.workspace / "outside.txt").exists())
        self.assertEqual(self.controller.store.get(run["id"])["state"], "DELIVERED")

        accepted = self.controller.review({"request_id": "scope-explicit-allow", "id": run["id"],
                                           "expected_version": delivered["version"],
                                           "conclusion": "ACCEPTED", "summary": "inspected out of scope",
                                           "checks": ["inspected out.txt and outside.txt"],
                                           "allow_out_of_scope": True})
        self.assertEqual(accepted["run"]["state"], "ACCEPTED")
        self.assertTrue(accepted["run"]["review"]["allow_out_of_scope"])
        self.assertEqual((self.workspace / "outside.txt").read_text(encoding="utf-8"), "out-of-scope\n")
        replay = self.controller.review({"request_id": "scope-explicit-allow", "id": run["id"],
                                         "expected_version": delivered["version"],
                                         "conclusion": "ACCEPTED", "summary": "  inspected out of scope  ",
                                         "checks": ["inspected out.txt and outside.txt"],
                                         "allow_out_of_scope": True})
        self.assertTrue(replay["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
