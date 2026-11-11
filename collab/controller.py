"""Application controller for lightweight DeepSeek task distribution."""
from __future__ import annotations

from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

from .common import CollabError, write_json
from .desktop_pairing import DesktopPairing
from .desktop_runtime import _outcome, _terminal, session_stopped
from .driver import prompt_for
from .files import clean_rel, safe_path, secret_path
from . import git_evidence
from .limits import MAX_PROMPT_UTF8_BYTES
from .scheduler import Scheduler
from .store import Store


WORKER_PRESET = "collab-worker"


class Controller:
    def __init__(self, state_root, execution_workspace, registration, *, native_enabled=False,
                 desktop=None, scheduler_factory=Scheduler):
        self.root = Path(state_root).resolve()
        self.workspace = Path(execution_workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise CollabError("INVALID_WORKSPACE", "execution workspace must be an existing directory")
        self.registration = registration
        self.store = Store(self.root)
        self.desktop = desktop or DesktopPairing(self.root)
        self.native_enabled = native_enabled
        self.stopping = False
        self.condition = threading.Condition(threading.RLock())
        self.review_lock = threading.Lock()
        self.recovered_interrupted = self._recover_running_runs()
        self.scheduler = scheduler_factory(
            self.root, self.workspace, self.store, self._changed,
        ) if native_enabled else None

    def _session_id(self, run):
        run_dir = self.root / "runs" / run["id"]
        try:
            value = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
            session_id = value.get("session_id") if isinstance(value, dict) else None
            if isinstance(session_id, str) and session_id:
                return session_id
        except (OSError, ValueError):
            pass
        try:
            tail = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace")[-65536:]
            for line in reversed(tail.splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") == "desktop.session.created":
                    value = event.get("value") or {}
                    session_id = value.get("session_id") if isinstance(value, dict) else None
                    if isinstance(session_id, str) and session_id:
                        return session_id
        except OSError:
            pass
        return None

    def _recovered_delivery(self, run):
        session_id = self._session_id(run)
        if session_id and hasattr(self.desktop, "task_state"):
            try:
                state = self.desktop.task_state(session_id)
                terminal = _terminal(self.desktop.task_events(session_id))
                if terminal is not None and session_stopped(state):
                    status, reason, partial = _outcome(terminal, None, False)
                    return {"status": status,
                            "error": None if status == "EXECUTION_COMPLETED" else reason,
                            "outcome_reason": reason, "partial": partial,
                            "terminal_kind": terminal["kind"], "terminal_seq": terminal["seq"],
                            "completion_verified": True, "session_id": session_id,
                            "recovery_reason": "SESSION_TERMINAL"}
                try:
                    self.desktop.cancel_task(session_id)
                except Exception:
                    pass
                return {"status": "OUTCOME_UNKNOWN", "error": "RECONCILE_SESSION_ACTIVE",
                        "outcome_reason": "RECONCILE_SESSION_ACTIVE", "outcome_unknown": True,
                        "session_id": session_id, "recovery_reason": "SESSION_ACTIVE_CANCELLED"}
            except Exception:
                pass
        return {"status": "OUTCOME_UNKNOWN", "error": "CONTROLLER_RESTARTED",
                "outcome_reason": "CONTROLLER_RESTARTED", "outcome_unknown": True,
                "recovery_reason": "SESSION_UNOBSERVABLE"}

    def _recovered_manifest(self, run):
        run_dir = self.root / "runs" / run["id"]
        if not (run_dir / "worktree-base.json").is_file():
            raise CollabError("GIT_WORKTREE_FAILED", "worktree baseline is unavailable during recovery")
        return git_evidence.deliver_worktree(
            run_dir, run["spec"]["write_paths"],
            excludes=run["spec"].get("exclude_paths", []))

    def _recover_running_runs(self):
        recovered = []
        for run in self.store.running_runs():
            recovered.append(run["id"])
            delivery = self._recovered_delivery(run)
            try:
                manifest = self._recovered_manifest(run)
            except Exception as error:
                code = getattr(error, "code", "RESULT_MANIFEST_FAILED")
                manifest = {"changes": [], "out_of_scope": [], "before_digest": None,
                            "after_digest": None, "error": code}
                if delivery.get("status") == "EXECUTION_COMPLETED":
                    delivery = {**delivery, "status": "FAILED", "error": code,
                                "outcome_reason": "RESULT_MANIFEST_FAILED"}
            try:
                self.store.finish(run["id"], delivery, manifest)
            except Exception:
                continue
        for run_id in self.store.recover_interrupted():
            if run_id not in recovered:
                recovered.append(run_id)
        return recovered

    def _changed(self):
        with self.condition:
            self.condition.notify_all()

    @staticmethod
    def _run_head(run, *, include_review=False):
        """Small status view used by wait/submit/cancel/review to save caller tokens."""
        summary = (run.get("result") or {}).get("summary") or {}
        value = {
            "id": run["id"], "version": run["version"], "state": run["state"],
            "summary": {key: summary[key] for key in (
                "native_status", "error", "outcome_reason", "outcome_unknown",
                "changed_files", "out_of_scope_files", "main_changed", "main_changes",
            ) if key in summary},
        }
        if include_review and run.get("review") is not None:
            value["review"] = run["review"]
        return value

    @staticmethod
    def _run_summary(run):
        result = run.get("result") or {}
        manifest = result.get("manifest") or {}
        summary = result.get("summary") or {}
        spec = run.get("spec") or {}
        review = run.get("review") or {}
        return {
            "id": run["id"], "version": run["version"], "state": run["state"],
            "outcome_unknown": bool(summary.get("outcome_unknown")),
            "spec_summary": {key: spec[key] for key in (
                "root_relative", "workspace", "agent_preset", "read_inputs", "write_paths",
                "exclude_paths", "timeout_seconds",
            ) if key in spec},
            "manifest_summary": {
                "root": manifest.get("root"),
                "changed_files": len(manifest.get("changes") or []),
                "out_of_scope_files": len(manifest.get("out_of_scope") or []),
                "main_changed": bool(manifest.get("main_changed")),
                "main_changes": list(manifest.get("main_changes") or [])[:20],
                "excluded": list(manifest.get("excluded") or [])[:20],
            },
            "review": {"conclusion": review.get("conclusion"), "summary": review.get("summary")}
                      if review else None,
            "has_result": bool(result),
        }

    def health(self):
        return {
            "state": "STOPPING" if self.stopping else "RUNNING",
            **self.registration,
            "model_execution_enabled": self.scheduler is not None,
            "run_counts": self.store.counts(),
            "recovered_interrupted": self.recovered_interrupted,
        }

    def close(self):
        if self.scheduler is not None:
            self.scheduler.close()
        self.desktop.close()

    def stop(self):
        counts = self.store.counts()
        if counts.get("RUNNING"):
            return {"stopped": False, "blockers": [{"kind": "run", "state": "RUNNING",
                                                    "count": counts["RUNNING"]}]}
        self.stopping = True
        if self.scheduler is not None:
            self.scheduler.closing = True
        return {"stopped": False, "stop_requested": True, "blockers": []}

    def status(self):
        try:
            desktop = self.desktop.status()
        except CollabError as error:
            desktop = {"state": "UNAVAILABLE", "error": error.code}
        if desktop.get("state") != "CONNECTED":
            desktop["repair"] = {
                "required": True,
                "steps": [
                    "quit the DSH Desktop App through its normal application entry",
                    "run the desktop pair command below",
                    "start the native service (or submit and let the bridge auto-start it)",
                ],
                "command": self._repair_command(),
            }
        return {"service": self.health(), "desktop": desktop}

    def _repair_command(self):
        project = Path(__file__).resolve().parents[1]
        argv = [sys.executable, "-B", "-m", "collab", "--state-root", str(self.root),
                "--execution-workspace", str(self.workspace), "desktop", "pair"]
        join = subprocess.list2cmdline if os.name == "nt" else shlex.join
        return f"cd {join([str(project)])} && {join(argv)}"

    def _configuration(self):
        configuration = self.desktop.execution_configuration(WORKER_PRESET)
        if (configuration.get("agent_preset") != WORKER_PRESET
                or not isinstance(configuration.get("preset_sha256"), str)):
            raise CollabError("AGENT_PRESET_UNAVAILABLE", "worker preset is not registered")
        return configuration

    def _spec(self, arguments):
        workspace_value = arguments.get("execution_workspace")
        try:
            workspace = (Path(workspace_value).resolve(strict=True) if workspace_value is not None
                         else self.workspace)
        except OSError as error:
            raise CollabError("INVALID_WORKSPACE", "execution workspace is unavailable") from error
        if (not workspace.is_dir() or workspace == self.root or workspace in self.root.parents
                or workspace == Path.home() or workspace == Path(workspace.anchor)):
            raise CollabError("INVALID_WORKSPACE",
                              "execution workspace must be a specific directory outside service state")
        root_value = arguments.get("root", ".")
        root_rel = "." if root_value == "." else clean_rel(root_value)
        root = workspace if root_rel == "." else safe_path(workspace, root_rel)
        if not git_evidence.available():
            raise CollabError("GIT_REQUIRED", "git is required for worktree execution")
        if git_evidence.repo_root(Path(root)) != Path(root).resolve():
            raise CollabError("GIT_EVIDENCE_REQUIRED", "task root must be a git repository root")
        root = root.resolve()
        if not root.is_dir() or (root != workspace and workspace not in root.parents):
            raise CollabError("INVALID_PATH", "task root must stay inside the execution workspace")

        def paths(name):
            values = arguments.get(name, [])
            if not isinstance(values, list) or len(values) > 100:
                raise CollabError("INVALID_INPUT", f"{name} must be a bounded path list")
            result = []
            for value in values:
                rel = clean_rel(value)
                if secret_path(rel):
                    raise CollabError("INVALID_PATH", f"credential-like path is not allowed: {rel}")
                result.append(rel)
            return sorted(set(result))

        read_inputs = paths("read_inputs")
        write_paths = paths("write_paths")
        exclude_values = arguments.get("exclude_paths", [])
        if not isinstance(exclude_values, list) or len(exclude_values) > 100:
            raise CollabError("INVALID_INPUT", "exclude_paths must be a bounded path list")
        excludes = []
        for value in exclude_values:
            rel = clean_rel(value)
            if secret_path(rel):
                raise CollabError("INVALID_PATH", f"credential-like path is not allowed: {rel}")
            excludes.append(rel)
        excludes = sorted(set(excludes))
        for rel in read_inputs:
            if not safe_path(root, rel).exists():
                raise CollabError("MISSING_INPUT", f"read input does not exist: {rel}")
        for rel in write_paths:
            safe_path(root, rel)
        timeout = arguments.get("timeout_seconds", 1800)
        if type(timeout) is not int or not 60 <= timeout <= 86400:
            raise CollabError("INVALID_INPUT", "timeout_seconds must be between 60 and 86400")
        return {
            "goal": arguments["goal"].strip(),
            "root": str(root), "root_relative": root_rel,
            "workspace": str(workspace),
            "read_inputs": read_inputs, "write_paths": write_paths,
            "exclude_paths": excludes or (["runtime"] if root_rel == "." else []),
            "constraints": list(arguments.get("constraints", [])),
            "acceptance": list(arguments["acceptance"]),
            "agent_preset": WORKER_PRESET,
            "timeout_seconds": timeout,
        }

    def submit(self, arguments):
        dry_run = arguments.get("dry_run", False)
        identity = {key: value for key, value in arguments.items() if key not in ("request_id", "dry_run")}
        if not dry_run:
            replay = self.store.replay(arguments["request_id"], "submit", identity)
            if replay is not None:
                if isinstance(replay.get("run"), dict):
                    replay["run"] = self._run_head(replay["run"])
                return replay
        spec = self._spec(arguments)
        if len(prompt_for(spec).encode("utf-8")) > MAX_PROMPT_UTF8_BYTES:
            raise CollabError("INVALID_INPUT", "assembled prompt exceeds the controller byte limit")
        configuration = self._configuration()
        spec["preset_sha256"] = configuration["preset_sha256"]
        if dry_run:
            return {"ready": True, "spec": spec, "execution_configuration": configuration}
        for active in self.store.blocking_specs():
            if active.get("root") == spec["root"]:
                raise CollabError("ROOT_BUSY",
                                  "a previous run on this task root is still queued/running/awaiting review")
        try:
            desktop = self.desktop.status()
        except CollabError:
            desktop = {"state": "UNAVAILABLE"}
        if desktop.get("state") != "CONNECTED":
            raise CollabError(
                "DESKTOP_NOT_READY",
                "DSH Desktop pairing is not connected; run collab_status for the repair command",
            )
        if self.scheduler is None or self.scheduler.closing or self.stopping:
            raise CollabError("NATIVE_DISABLED", "start the service with --native before submitting work")
        created = None

        def prepare(run_id):
            nonlocal created
            created = run_id
            run_dir = self.root / "runs" / run_id
            run_dir.mkdir(parents=True)
            write_json(run_dir / "spec.json", spec)

        try:
            result = self.store.submit(spec, arguments["request_id"], prepare=prepare, identity=identity)
        except BaseException:
            if created is not None:
                shutil.rmtree(self.root / "runs" / created, ignore_errors=True)
            raise
        self.scheduler.kick()
        self._changed()
        result["run"] = self._run_head(result["run"])
        return result

    def get(self, arguments):
        run_id = arguments.get("id")
        if run_id is None:
            return self.store.list(after_row=arguments.get("after_row", 0), limit=arguments.get("limit", 20))
        run = self.store.get(run_id)
        view = arguments.get("view", "result")
        if view == "summary":
            return self._run_summary(run)
        if view == "history":
            return {"id": run_id, "history": self.store.history(run_id)}
        if view == "diff" and arguments.get("current"):
            raise CollabError("INVALID_INPUT", "view=diff does not support current=true; read view=result with current=true separately")
        if view == "diff":
            result = run.get("result") or {}
            manifest = result.get("manifest") or {}
            changes = manifest.get("changes", [])
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
            page = changes[offset:offset + limit]
            root, run_dir = Path(run["spec"]["root"]), self.root / "runs" / run_id
            if manifest.get("evidence_mode") != "worktree":
                return {"id": run_id, "version": run["version"], "changes": [],
                        "out_of_scope": [], "offset": offset, "next_offset": None,
                        "has_more": False, "deprecated": "LEGACY_EVIDENCE_MODE"}
            root = Path(manifest.get("worktree", root))
            render = git_evidence.worktree_change_diff
            out_of_scope = manifest.get("out_of_scope", [])
            return {"id": run_id, "version": run["version"],
                    "changes": [render(root, run_dir, item) for item in page],
                    "out_of_scope": out_of_scope[:20],
                    "out_of_scope_total": len(out_of_scope),
                    "out_of_scope_truncated": len(out_of_scope) > 20,
                    "offset": offset, "next_offset": offset + len(page) if offset + len(page) < len(changes) else None,
                    "has_more": offset + len(page) < len(changes)}
        if arguments.get("current"):
            manifest = (run.get("result") or {}).get("manifest") or {}
            if manifest.get("evidence_mode") == "worktree":
                run["current_changes"] = git_evidence.worktree_current_changes(
                    self.root / "runs" / run_id, run["spec"]["write_paths"],
                    excludes=run["spec"].get("exclude_paths", []))
            else:
                run["current_changes"] = {"available": False, "changes": [], "out_of_scope": [],
                                          "current_digest": None,
                                          "reason": "LEGACY_EVIDENCE_MODE"}
        summary = (run.get("result") or {}).get("summary") or {}
        run["outcome_unknown"] = bool(summary.get("outcome_unknown"))
        return run

    def wait(self, arguments):
        run_id, after = arguments["id"], arguments["after_version"]
        seconds = arguments.get("max_seconds", 60)
        deadline = time.monotonic() + seconds
        with self.condition:
            while True:
                run = self.store.get(run_id)
                if run["version"] > after:
                    return {"run": self._run_head(run), "timed_out": False}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"run": self._run_head(run), "timed_out": True}
                self.condition.wait(min(remaining, 1.0))

    def cancel(self, arguments):
        result = self.store.cancel(arguments["id"], arguments["expected_version"], arguments["request_id"])
        run = result["run"]
        if run["cancel_requested"] and run["state"] == "RUNNING":
            (self.root / "runs" / run["id"] / "cancel.requested").touch()
        self._changed()
        result["run"] = self._run_head(result["run"])
        return result

    def review(self, arguments):
        with self.review_lock:
            return self._review_locked(arguments)

    def _review_locked(self, arguments):
        identity = {key: value for key, value in arguments.items() if key != "request_id"}
        replay = self.store.replay(arguments["request_id"], "review", identity)
        if replay is not None:
            if isinstance(replay.get("run"), dict):
                replay["run"] = self._run_head(replay["run"], include_review=True)
            return replay
        run = self.store.get(arguments["id"])
        root = run["spec"]["root"]
        for active in self.store.running_specs():
            if active.get("root") == root:
                raise CollabError("RUN_BUSY", "a run is still active on this task root; wait for its terminal state")
        changes = {"available": False, "reason": "NOT_COMPUTED_ON_REVIEW",
                   "use": "collab_get(current=true)"}
        manifest = (run.get("result") or {}).get("manifest") or {}
        run_dir = self.root / "runs" / arguments["id"]
        worktree_mode = manifest.get("evidence_mode") == "worktree"
        if worktree_mode and arguments["conclusion"] == "ACCEPTED":
            if run.get("state") != "DELIVERED" or run.get("version") != arguments["expected_version"]:
                raise CollabError("VERSION_CONFLICT", "run changed before worktree apply")
            git_evidence.apply_worktree(run_dir, cleanup=False)
        result = self.store.review(
            arguments["id"], arguments["expected_version"], arguments["conclusion"],
            arguments["summary"].strip(), list(arguments["checks"]), changes, arguments["request_id"],
        )
        if worktree_mode:
            git_evidence.cleanup_worktree(run_dir)
        self._changed()
        result["run"] = self._run_head(result["run"], include_review=True)
        return result
