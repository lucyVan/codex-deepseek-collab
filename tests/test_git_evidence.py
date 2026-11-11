"""Worktree evidence tests use local temporary repositories only."""
import json
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from collab import git_evidence
from collab.common import CollabError


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@unittest.skipUnless(shutil.which("git"), "git is unavailable")
class WorktreeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "fixture")
        (self.repo / "out.txt").write_text("before\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("other-before\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "baseline")

    def test_worktree_round_trip_with_dirty_main(self):
        run_dir = self.base / "state/runs/wt-1"
        (self.repo / "other.txt").write_text("user-uncommitted\n", encoding="utf-8")
        base = git_evidence.prepare_worktree(self.repo, run_dir, "wt-1", self.base)
        worktree = Path(base["worktree"])
        try:
            (worktree / "out.txt").write_text("after\n", encoding="utf-8")
            (worktree / "new.txt").write_text("new\n", encoding="utf-8")
            manifest = git_evidence.deliver_worktree(run_dir, ["out.txt", "new.txt"])
            self.assertEqual(manifest["evidence_mode"], "worktree")
            changes = {item["path"]: item for item in manifest["changes"]}
            self.assertEqual(set(changes), {"out.txt", "new.txt"})
            diff = git_evidence.worktree_change_diff(worktree, run_dir, changes["out.txt"])
            self.assertIn("+after", diff["diff"])
            self.assertTrue(diff["after_matches_current"])
            current = git_evidence.worktree_current_changes(run_dir, ["out.txt", "new.txt"])
            self.assertTrue(current["available"])
            self.assertEqual(current["changes"], [])

            git_evidence.apply_worktree(run_dir)
            self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"), "after\n")
            self.assertTrue((self.repo / "new.txt").is_file())
            self.assertEqual((self.repo / "other.txt").read_text(encoding="utf-8"),
                             "user-uncommitted\n")
            self.assertFalse(worktree.exists())
        finally:
            git_evidence.cleanup_worktree(run_dir)

    def prepare_delivered(self, name, content="after\n"):
        run_dir = self.base / "state/runs" / name
        base = git_evidence.prepare_worktree(self.repo, run_dir, name, self.base)
        worktree = Path(base["worktree"])
        (worktree / "out.txt").write_text(content, encoding="utf-8")
        git_evidence.deliver_worktree(run_dir, ["out.txt"])
        return run_dir, worktree

    def test_dirty_main_overlapping_is_refused(self):
        run_dir, worktree = self.prepare_delivered("wt-dirty-conflict")
        try:
            (self.repo / "out.txt").write_text("user-working-copy\n", encoding="utf-8")
            with self.assertRaises(CollabError) as caught:
                git_evidence.apply_worktree(run_dir)
            self.assertEqual(caught.exception.code, "WORKTREE_MAIN_DIRTY_CONFLICT")
            self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"),
                             "user-working-copy\n")
            self.assertTrue(worktree.exists())
        finally:
            git_evidence.cleanup_worktree(run_dir)

    def test_main_head_advance_requires_re_review_then_applies(self):
        run_dir, worktree = self.prepare_delivered("wt-rebase-ok")
        try:
            (self.repo / "other.txt").write_text("main-advanced\n", encoding="utf-8")
            git(self.repo, "add", "other.txt")
            git(self.repo, "commit", "-qm", "main advanced")
            with self.assertRaises(CollabError) as caught:
                git_evidence.apply_worktree(run_dir)
            self.assertEqual(caught.exception.code, "REVIEW_BASE_CHANGED")
            self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"), "before\n")
            self.assertTrue(worktree.exists())
            manifest = json.loads((run_dir / "delivery-manifest.json").read_text(encoding="utf-8"))
            diff = git_evidence.worktree_change_diff(
                worktree, run_dir, manifest["changes"][0])
            self.assertIn("+after", diff["diff"])
            git_evidence.apply_worktree(run_dir)
            self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual((self.repo / "other.txt").read_text(encoding="utf-8"),
                             "main-advanced\n")
            self.assertFalse(worktree.exists())
        finally:
            git_evidence.cleanup_worktree(run_dir)

    def test_main_head_advance_conflict_is_reported(self):
        run_dir, worktree = self.prepare_delivered("wt-rebase-conflict")
        try:
            (self.repo / "out.txt").write_text("main-conflict\n", encoding="utf-8")
            git(self.repo, "add", "out.txt")
            git(self.repo, "commit", "-qm", "main conflict")
            with self.assertRaises(CollabError) as caught:
                git_evidence.apply_worktree(run_dir)
            self.assertEqual(caught.exception.code, "WORKTREE_CONFLICT")
            self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"),
                             "main-conflict\n")
            self.assertTrue(worktree.exists())
            conflicts = (run_dir / "worktree-conflicts.json").read_text(encoding="utf-8")
            self.assertIn("out.txt", conflicts)
        finally:
            git_evidence.cleanup_worktree(run_dir)

    def test_worktree_base_is_ignored_automatically(self):
        self.assertFalse((self.repo / ".gitignore").exists())
        self.assertTrue(git_evidence.ensure_worktree_ignored(self.repo))
        check = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "-q", ".worktrees/"],
                               capture_output=True)
        self.assertEqual(check.returncode, 0)
        exclude = (self.repo / ".git/info/exclude").read_text(encoding="utf-8")
        self.assertIn("/.worktrees/", exclude.splitlines())
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--short"],
                                capture_output=True, text=True, check=True)
        self.assertNotIn(".worktrees", status.stdout)

    def test_main_changed_is_reported_in_manifest(self):
        run_dir = self.base / "state/runs/wt-main-changed"
        base = git_evidence.prepare_worktree(self.repo, run_dir, "wt-main-changed", self.base)
        worktree = Path(base["worktree"])
        try:
            (worktree / "out.txt").write_text("after\n", encoding="utf-8")
            (self.repo / "other.txt").write_text("human-edit\n", encoding="utf-8")
            manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"])
            self.assertTrue(manifest["main_changed"])
            self.assertIn("other.txt", manifest["main_changes"])
        finally:
            git_evidence.cleanup_worktree(run_dir)

    def test_cleanup_orphan_terminal_worktree(self):
        from collab.store import Store
        state = self.base / "state"
        store = Store(state)
        run = store.submit({"root": str(self.repo), "workspace": str(self.base),
                            "goal": "gc", "read_inputs": [], "write_paths": [],
                            "constraints": [], "acceptance": ["gc"],
                            "agent_preset": "collab-worker", "timeout_seconds": 60},
                           "gc-run", prepare=lambda rid: (state / "runs" / rid).mkdir(parents=True))["run"]
        store.claim_next()
        run_dir = state / "runs" / run["id"]
        base = git_evidence.prepare_worktree(self.repo, run_dir, run["id"], self.base)
        worktree = Path(base["worktree"])
        store.finish(run["id"], {"status": "FAILED", "error": "fixture"},
                     {"changes": [], "out_of_scope": []})
        removed = git_evidence.cleanup_orphan_worktrees(state, self.base)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(worktree.exists())

    def test_excluded_changes_never_enter_rebased_patch_or_main(self):
        (self.repo / "ignored.txt").write_text("ignored-before\n", encoding="utf-8")
        (self.repo / ".cache").mkdir()
        (self.repo / ".cache/state.txt").write_text("cache-before\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "tracked exclusions")
        run_dir = self.base / "state/runs/wt-excludes"
        base = git_evidence.prepare_worktree(self.repo, run_dir, "wt-excludes", self.base)
        worktree = Path(base["worktree"])
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        (worktree / "out.txt").write_text("included-after\n", encoding="utf-8")
        (worktree / "ignored.txt").write_text("ignored-after\n", encoding="utf-8")
        (worktree / ".cache/state.txt").write_text("cache-after\n", encoding="utf-8")
        manifest = git_evidence.deliver_worktree(run_dir, ["out.txt"], excludes=["ignored.txt"])
        self.assertEqual([item["path"] for item in manifest["changes"]], ["out.txt"])
        patch = (run_dir / "worktree.patch").read_bytes()
        self.assertNotIn(b"ignored.txt", patch)
        self.assertNotIn(b".cache/state.txt", patch)

        (self.repo / "other.txt").write_text("main-advanced\n", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-qm", "main advanced")
        with self.assertRaises(CollabError) as caught:
            git_evidence.apply_worktree(run_dir)
        self.assertEqual(caught.exception.code, "REVIEW_BASE_CHANGED")
        git_evidence.apply_worktree(run_dir)
        self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"), "included-after\n")
        self.assertEqual((self.repo / "ignored.txt").read_text(encoding="utf-8"), "ignored-before\n")
        self.assertEqual((self.repo / ".cache/state.txt").read_text(encoding="utf-8"), "cache-before\n")

    def test_post_delivery_mutation_blocks_apply_without_touching_main(self):
        run_dir, worktree = self.prepare_delivered("wt-post-delivery", "delivered-v1\n")
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        (worktree / "out.txt").write_text("post-delivery-v2\n", encoding="utf-8")
        with self.assertRaises(CollabError) as caught:
            git_evidence.apply_worktree(run_dir)
        self.assertEqual(caught.exception.code, "WORKTREE_CHANGED_AFTER_DELIVERY")
        self.assertEqual((self.repo / "out.txt").read_text(encoding="utf-8"), "before\n")

    def test_cleanup_makes_current_match_unobservable(self):
        run_dir, worktree = self.prepare_delivered("wt-cleaned")
        manifest = json.loads((run_dir / "delivery-manifest.json").read_text(encoding="utf-8"))
        git_evidence.apply_worktree(run_dir)
        (self.repo / "out.txt").write_text("changed-after-accept\n", encoding="utf-8")
        diff = git_evidence.worktree_change_diff(worktree, run_dir, manifest["changes"][0])
        current = git_evidence.worktree_current_changes(run_dir, ["out.txt"])
        self.assertIsNone(diff["after_matches_current"])
        self.assertEqual(diff["reason"], "WORKTREE_CLEANED_UP")
        self.assertFalse(current["available"])

    def test_binary_patch_round_trip_preserves_exact_bytes(self):
        before = b"\x00\xffbefore\r\n"
        after = b"\x00\xffafter\x00\r\n"
        (self.repo / "binary.bin").write_bytes(before)
        git(self.repo, "add", "binary.bin")
        git(self.repo, "commit", "-qm", "binary baseline")
        run_dir = self.base / "state/runs/wt-binary"
        base = git_evidence.prepare_worktree(self.repo, run_dir, "wt-binary", self.base)
        worktree = Path(base["worktree"])
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        (worktree / "binary.bin").write_bytes(after)
        git_evidence.deliver_worktree(run_dir, ["binary.bin"])
        git_evidence.apply_worktree(run_dir)
        self.assertEqual((self.repo / "binary.bin").read_bytes(), after)

    def test_git_add_failure_fails_delivery(self):
        run_dir = self.base / "state/runs/wt-add-failure"
        base = git_evidence.prepare_worktree(self.repo, run_dir, "wt-add-failure", self.base)
        worktree = Path(base["worktree"])
        self.addCleanup(git_evidence.cleanup_worktree, run_dir)
        (worktree / "out.txt").write_text("after\n", encoding="utf-8")
        original = git_evidence._git

        def fail_add(root, *args, **kwargs):
            if args[:2] == ("add", "-A"):
                return SimpleNamespace(returncode=1)
            return original(root, *args, **kwargs)

        with mock.patch("collab.git_evidence._git", side_effect=fail_add):
            with self.assertRaises(CollabError) as caught:
                git_evidence.deliver_worktree(run_dir, ["out.txt"])
        self.assertEqual(caught.exception.code, "GIT_WORKTREE_FAILED")

    def test_worktree_requires_repo_root(self):
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(CollabError) as caught:
            git_evidence.prepare_worktree(plain, self.base / "state/runs/plain", "plain", self.base)
        self.assertEqual(caught.exception.code, "GIT_EVIDENCE_REQUIRED")

    def test_worktree_path_is_contained(self):
        path = git_evidence.worktree_path("run-1", self.base)
        self.assertEqual(path.parent, (self.base / ".worktrees").resolve())
        with self.assertRaises(CollabError) as caught:
            git_evidence.worktree_path("../../escape", self.base)
        self.assertEqual(caught.exception.code, "INVALID_WORKTREE_ID")


if __name__ == "__main__":
    unittest.main()
