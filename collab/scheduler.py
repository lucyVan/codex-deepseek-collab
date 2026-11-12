"""Bounded native driver pool: global cap, root lease stays in the Controller."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time

from .common import CollabError, read_json
from . import git_evidence
from .common import write_json
from .limits import MAX_CONCURRENCY
from .runtime_transport import NativeJob, scrub


class Scheduler:
    def __init__(self, root, workspace, store, notify, *, driver_path=None,
                 max_concurrency=MAX_CONCURRENCY):
        self.root, self.workspace, self.store = Path(root), Path(workspace), store
        self.notify = notify
        try:
            git_evidence.ensure_worktree_ignored(self.workspace)
            git_evidence.cleanup_orphan_worktrees(self.root, self.workspace)
        except Exception:
            pass
        self.driver_path = Path(driver_path) if driver_path else None
        self.max_concurrency = max(1, int(max_concurrency))
        self.closing = False
        self.active = {}
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="collab-deepseek-scheduler", daemon=True)
        self.thread.start()

    def kick(self):
        self.wake.set()

    def _drain(self, stream, target):
        try:
            while True:
                chunk = stream.readline(65536)
                if not chunk:
                    return
                target.write(scrub(chunk.decode("utf-8", "replace")))
                target.flush()
        except (OSError, ValueError):
            return

    def _start(self, run):
        run_dir = self.root / "runs" / run["id"]
        logs = run_dir / "logs"
        logs.mkdir(exist_ok=True)
        workspace = Path(run["spec"]["workspace"])
        try:
            if run["spec"].get("resume"):
                base = git_evidence.read_worktree(run_dir)
                if base is None or not Path(base["worktree"]).is_dir():
                    raise CollabError("RESUME_UNAVAILABLE", "paused worktree is unavailable")
            else:
                if workspace != self.workspace:
                    git_evidence.cleanup_orphan_worktrees(self.root, workspace)
                base = git_evidence.prepare_worktree(
                    Path(run["spec"].get("repository_root", run["spec"]["root"])),
                    run_dir, run["id"], workspace,
                    run["spec"].get("task_root_relative", "."))
            execution_spec = dict(run["spec"])
            task_relative = base.get("task_root_relative", ".")
            execution_root = (Path(base["worktree"]) if task_relative == "."
                              else Path(base["worktree"]) / task_relative)
            if not execution_root.is_dir():
                raise CollabError("GIT_TASK_ROOT_UNAVAILABLE",
                                  "task root is not present in the repository HEAD")
            execution_spec["root"] = str(execution_root)
            execution_spec["worktree"] = base["worktree"]
            write_json(run_dir / "execution-spec.json", execution_spec)
            write_json(run_dir / "evidence-mode.json", {"mode": "worktree"})
        except Exception as error:
            git_evidence.cleanup_worktree(run_dir)
            self._fail_before_start(run, getattr(error, "code", "GIT_WORKTREE_FAILED"))
            return False
        return self._start_job(run, run_dir)

    def _start_job(self, run, run_dir):
        entry = [str(self.driver_path)] if self.driver_path else ["-m", "collab.driver"]
        argv = [sys.executable, "-B", *entry,
                "--state-root", str(self.root), "--run-id", run["id"],
                "--workspace", str(run["spec"]["workspace"])]
        job = NativeJob()
        try:
            process = job.start(argv, Path(__file__).resolve().parents[1], os.environ.copy())
            resume = run["spec"].get("resume")
            suffix = f"-resume-{resume.get('continuation_count', 1)}" if resume else ""
            stdout = (run_dir / "logs" / f"driver{suffix}.stdout.log").open("w", encoding="utf-8")
            stderr = (run_dir / "logs" / f"driver{suffix}.stderr.log").open("w", encoding="utf-8")
            threads = [
                threading.Thread(target=self._drain, args=(process.stdout, stdout), daemon=True),
                threading.Thread(target=self._drain, args=(process.stderr, stderr), daemon=True),
            ]
            for thread in threads:
                thread.start()
            self.active[run["id"]] = {
                "run": run, "job": job, "process": process,
                "deadline": time.monotonic() + run["spec"]["timeout_seconds"] + 120,
                "checked": 0.0, "stdout": stdout, "stderr": stderr, "threads": threads,
            }
            return True
        except BaseException:
            try:
                job.close()
            except BaseException:
                pass
            self._complete(run, {"status": "FAILED", "error": "DRIVER_START_FAILED",
                                 "outcome_reason": "DRIVER_START_FAILED"})
            return False

    def _fail_before_start(self, run, code):
        try:
            self.store.finish(run["id"], {"status": "FAILED", "error": code,
                                          "outcome_reason": code},
                              {"changes": [], "out_of_scope": [],
                               "before_digest": None, "after_digest": None})
            self.notify()
        except BaseException:
            pass

    def _complete(self, run, delivery):
        run_dir = self.root / "runs" / run["id"]
        try:
            manifest = git_evidence.deliver_worktree(
                run_dir, run["spec"]["write_paths"],
                excludes=run["spec"].get("exclude_paths", []))
        except Exception as error:
            code = getattr(error, "code", "RESULT_MANIFEST_FAILED")
            manifest = getattr(error, "manifest", None) or {
                "changes": [], "out_of_scope": [], "before_digest": None,
                "after_digest": None, "error": code}
            delivery = {**delivery, "status": "FAILED", "error": code,
                        "outcome_reason": "RESULT_MANIFEST_FAILED"}
        finished = self.store.finish(run["id"], delivery, manifest)
        if (run_dir / "worktree-base.json").is_file() and finished.get("state") not in ("DELIVERED", "PAUSED"):
            git_evidence.cleanup_worktree(run_dir)
        self.notify()

    def _finish_active(self, run_id):
        active = self.active.pop(run_id, None)
        if active is None:
            return
        process, job, run = active["process"], active["job"], active["run"]
        cleanup_error = None
        try:
            process.wait(timeout=5)
            for thread in active["threads"]:
                thread.join(timeout=2)
            active["stdout"].close()
            active["stderr"].close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            job.close()
        except BaseException:
            cleanup_error = "DRIVER_CLEANUP_FAILED"
            try:
                job.terminate()
            except BaseException:
                pass
        result_path = self.root / "runs" / run["id"] / "delivery.json"
        try:
            delivery = read_json(result_path)
            if not isinstance(delivery, dict) or not isinstance(delivery.get("status"), str):
                raise CollabError("DRIVER_RESULT_INVALID", "driver result is invalid")
        except (CollabError, OSError, ValueError):
            delivery = {"status": "FAILED", "error": "DRIVER_RESULT_MISSING",
                        "outcome_reason": "DRIVER_RESULT_MISSING"}
        if cleanup_error:
            delivery = {**delivery, "status": "FAILED", "error": cleanup_error,
                        "outcome_reason": cleanup_error}
        self._complete(run, delivery)

    def _loop(self):
        while True:
            try:
                if not self._tick():
                    return
            except BaseException:
                self._abort_active()
                if self.closing:
                    return
                time.sleep(0.5)

    def _tick(self):
        now = time.monotonic()
        for run_id, active in list(self.active.items()):
            process = active["process"]
            if process.poll() is not None:
                self._finish_active(run_id)
                continue
            if now >= active["deadline"]:
                active["job"].terminate()
                try:
                    process.wait(timeout=10)
                except BaseException:
                    pass
                continue
            if self.closing:
                active["job"].terminate()
                continue
            if now - active["checked"] >= 2.0:
                active["checked"] = now
                try:
                    active["job"].active()
                except BaseException:
                    active["job"].terminate()

        if self.closing:
            if not self.active:
                return False
            time.sleep(0.05)
            return True

        while len(self.active) < self.max_concurrency:
            run = self.store.claim_next()
            if run is None:
                break
            self._start(run)

        if self.active:
            time.sleep(0.1)
        else:
            self.wake.wait(0.5)
            self.wake.clear()
        return True

    def _abort_active(self):
        for run_id, active in list(self.active.items()):
            self.active.pop(run_id, None)
            try:
                active["job"].terminate()
            except BaseException:
                pass
            try:
                self._complete(active["run"], {"status": "FAILED", "error": "SCHEDULER_FAILURE",
                                               "outcome_reason": "SCHEDULER_FAILURE"})
            except BaseException:
                pass

    def close(self):
        self.closing = True
        self.wake.set()
        for active in list(self.active.values()):
            try:
                active["job"].terminate()
            except BaseException:
                pass
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise CollabError("SERVICE_STOP_FAILED", "scheduler did not stop")
