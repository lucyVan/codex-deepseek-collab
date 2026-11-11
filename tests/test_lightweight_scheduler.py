from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from collab import git_evidence
from collab.scheduler import Scheduler
from collab.store import Store


DRIVER = r'''import argparse,json,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--state-root',type=Path);p.add_argument('--run-id');p.add_argument('--workspace',type=Path);a=p.parse_args()
r=a.state_root/'runs'/a.run_id
sp=r/'execution-spec.json'
s=json.loads((sp if sp.is_file() else r/'spec.json').read_text())
if s['goal']=='sleep':
    time.sleep(.4)
    (Path(s['root'])/'out.txt').write_text('after\n')
    result={'status':'EXECUTION_COMPLETED','error':None}
elif s['goal']=='cancel':
    deadline=time.monotonic()+10
    while not (r/'cancel.requested').exists() and time.monotonic()<deadline: time.sleep(.05)
    result={'status':'CANCELLED','error':'CANCEL_REQUESTED'}
else:
    (Path(s['root'])/'out.txt').write_text('after\n')
    result={'status':'EXECUTION_COMPLETED','error':None}
(r/'delivery.json').write_text(json.dumps(result))
'''


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@unittest.skipUnless(__import__("shutil").which("git"), "git is unavailable")
class LightweightSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "input.txt").write_text("input\n")
        git(self.workspace, "init", "-q")
        git(self.workspace, "config", "user.email", "fixture@example.invalid")
        git(self.workspace, "config", "user.name", "fixture")
        git(self.workspace, "add", "input.txt")
        git(self.workspace, "commit", "-qm", "baseline")
        self.driver = self.base / "fake_driver.py"
        self.driver.write_text(DRIVER)
        self.store = Store(self.base / "state")
        self.scheduler = Scheduler(self.base / "state", self.workspace, self.store,
                                   lambda: None, driver_path=self.driver)
        self.addCleanup(self.scheduler.close)

    def submit(self, goal, request_id, user_root=None, user_workspace=None):
        root = Path(user_root) if user_root else self.workspace
        workspace = Path(user_workspace) if user_workspace else self.workspace
        spec = {"goal": goal, "root": str(root), "workspace": str(workspace),
                "read_inputs": ["input.txt"], "write_paths": ["out.txt"], "constraints": [],
                "acceptance": ["driver finishes"], "agent_preset": "collab-worker",
                "timeout_seconds": 60}
        def prepare(run_id):
            run_dir = self.base / "state/runs" / run_id
            run_dir.mkdir(parents=True)
            import json
            (run_dir / "spec.json").write_text(json.dumps(spec))
        return self.store.submit(spec, request_id, prepare=prepare)["run"]

    def wait_state(self, run_id, states, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.store.get(run_id)
            if run["state"] in states:
                return run
            time.sleep(.05)
        self.fail(f"state did not reach {states}: {self.store.get(run_id)}")

    def test_scheduler_survives_unexpected_loop_error(self):
        store = Store(self.base / "flaky-state")
        original = store.claim_next
        calls = {"count": 0}

        def flaky_claim():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("fixture failure")
            return original()

        store.claim_next = flaky_claim
        scheduler = Scheduler(self.base / "flaky-state", self.workspace, store,
                              lambda: None, driver_path=self.driver)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and calls["count"] < 2:
                time.sleep(.05)
            self.assertTrue(scheduler.thread.is_alive())
            self.assertGreaterEqual(calls["count"], 2)
        finally:
            scheduler.close()

    def test_different_roots_run_concurrently(self):
        second = self.base / "workspace2"
        second.mkdir()
        (second / "input.txt").write_text("input2\n")
        git(second, "init", "-q")
        git(second, "config", "user.email", "fixture2@example.invalid")
        git(second, "config", "user.name", "fixture2")
        git(second, "add", "input.txt")
        git(second, "commit", "-qm", "baseline2")
        first = self.submit("sleep", "conc-1")
        other = self.submit("sleep", "conc-2", user_root=second)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(self.scheduler.active) < 2:
            time.sleep(.02)
        self.assertEqual(len(self.scheduler.active), 2)
        self.wait_state(first["id"], {"DELIVERED"})
        self.wait_state(other["id"], {"DELIVERED"})

    def test_driver_start_failure_marks_run_failed(self):
        with patch("collab.scheduler.NativeJob.start", side_effect=RuntimeError("fixture")):
            run = self.submit("complete", "start-failure")
            final = self.wait_state(run["id"], {"FAILED"})
        self.assertEqual(final["result"]["delivery"]["error"], "DRIVER_START_FAILED")
        self.assertEqual(self.scheduler.active, {})

    def test_worktree_delivery_requires_repo_root(self):
        plain = self.base / "plain"
        plain.mkdir()
        run = self.submit("complete", "no-repo", user_root=plain)
        final = self.wait_state(run["id"], {"FAILED"})
        self.assertEqual(final["result"]["delivery"]["error"], "GIT_EVIDENCE_REQUIRED")

    def test_fake_driver_delivers_worktree_and_apply(self):
        run = self.submit("complete", "worktree-complete")
        try:
            final = self.wait_state(run["id"], {"DELIVERED"})
            manifest = final["result"]["manifest"]
            self.assertEqual(manifest["evidence_mode"], "worktree")
            self.assertEqual([item["path"] for item in manifest["changes"]], ["out.txt"])
            self.assertFalse((self.workspace / "out.txt").exists())
            self.assertTrue(Path(manifest["worktree"]).is_dir())
            git_evidence.apply_worktree(self.base / "state/runs" / run["id"])
            self.assertEqual((self.workspace / "out.txt").read_text(encoding="utf-8"), "after\n")
            self.assertFalse(Path(manifest["worktree"]).exists())
        finally:
            git_evidence.cleanup_worktree(self.base / "state/runs" / run["id"])

    def test_run_uses_the_spec_workspace_for_worktree_and_driver(self):
        caller = self.base / "caller-workspace"
        caller.mkdir()
        (caller / "input.txt").write_text("caller\n")
        git(caller, "init", "-q")
        git(caller, "config", "user.email", "fixture@example.invalid")
        git(caller, "config", "user.name", "fixture")
        git(caller, "add", "input.txt")
        git(caller, "commit", "-qm", "caller baseline")
        run = self.submit("copy", "caller-workspace", user_root=caller, user_workspace=caller)
        final = self.wait_state(run["id"], {"DELIVERED"})
        manifest = final["result"]["manifest"]
        self.assertEqual(manifest["root"], str(caller.resolve()))
        self.assertEqual(Path(manifest["worktree"]).parent, (caller / ".worktrees").resolve())
        git_evidence.apply_worktree(self.base / "state/runs" / run["id"])
        self.assertEqual((caller / "out.txt").read_text(), "after\n")
        self.assertFalse((self.workspace / "out.txt").exists())

    def test_cancel_file_reaches_driver_and_cleans_worktree(self):
        run = self.submit("cancel", "cancel-run")
        self.wait_state(run["id"], {"RUNNING"})
        run_dir = self.base / "state/runs" / run["id"]
        (run_dir / "cancel.requested").write_text("requested\n", encoding="utf-8")
        final = self.wait_state(run["id"], {"CANCELLED"})
        self.assertEqual(final["result"]["delivery"]["error"], "CANCEL_REQUESTED")
        self.assertFalse((self.workspace / "out.txt").exists())
        base = git_evidence.read_worktree(run_dir)
        self.assertIsNotNone(base)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and Path(base["worktree"]).exists():
            time.sleep(.05)
        self.assertFalse(Path(base["worktree"]).exists())


if __name__ == "__main__":
    unittest.main()
