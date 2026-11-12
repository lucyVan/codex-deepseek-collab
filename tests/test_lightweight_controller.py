from pathlib import Path
import json
import shutil
import subprocess
import tempfile
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

    def test_task_root_must_be_git_repository_root(self):
        plain = self.workspace / "plain"
        plain.mkdir()
        with self.assertRaises(CollabError) as caught:
            self.controller.submit(self.arguments(root="plain", dry_run=True))
        self.assertEqual(caught.exception.code, "GIT_EVIDENCE_REQUIRED")

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


if __name__ == "__main__":
    unittest.main()
