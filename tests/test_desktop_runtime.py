"""Desktop executor tests with a declared fake Host; no desktop or model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from collab.desktop_connection import DesktopConnectionError
from collab.desktop_pairing import DESKTOP_CONNECTION_TIMEOUT
from collab.desktop_runtime import (
    WAIT_CHUNK_MS,
    analyze_session_events,
    run_desktop,
)


class FakeDesktop:
    def __init__(self, waits, *, terminal="completed", prompt_error=None):
        self.waits = list(waits)
        self.terminal = terminal
        self.prompt_error = prompt_error
        self.prompts = []
        self.cancels = []
        self.created = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {"state": "CONNECTED"}

    def create_task_session(self, cwd, title, agent_preset="standard", preset_sha256=None):
        self.created.append((str(cwd), title))
        return {
            "session_id": "session-task",
            "title": title,
            "projection_seq": 1,
            "working_directory": str(cwd),
            "execution_configuration": {"agent_preset": agent_preset,
                                        "preset_sha256": preset_sha256},
        }

    def task_state(self, session_id):
        return {"cursor": 1, "active": True, "running": False, "queue": [], "pendingInput": []}

    def prompt_task(self, session_id, text, request_id):
        self.prompts.append((session_id, text, request_id))
        if self.prompt_error is not None:
            raise self.prompt_error
        return {"accepted": True, "requestId": request_id}

    def wait_task(self, session_id, **_kwargs):
        if len(self.waits) > 1:
            return self.waits.pop(0)
        return self.waits[0]

    def task_events(self, session_id, *, through_seq=None):
        return {
            "cursor": through_seq,
            "records": [{
                "type": "event",
                "event": {
                    "seq": through_seq,
                    "type": "turn/end",
                    "data": {"reason": {"kind": self.terminal}},
                },
            }],
            "hasMore": False,
        }

    def task_metadata(self, session_id):
        return {
            "tokenUsage": {"uncachedInputTokens": 10, "outputTokens": 5,
                           "cacheReadTokens": 100, "cacheWriteTokens": 0},
            "contextPressure": {"pressureTokens": 1000, "projectedTokens": 1200,
                                "contextWindow": 1000000},
            "contextBreakdown": {"systemTokens": 10, "toolsTokens": 20, "messageTokens": 30},
            "sessionStats": {"turns": 1, "steps": 1},
            "subagent": None,
            "subagentTiming": {"settledMs": 0},
        }

    def cancel_task(self, session_id):
        self.cancels.append(session_id)
        return {"accepted": True}


class ResumeDesktop(FakeDesktop):
    def __init__(self, workspace):
        super().__init__([idle(2)], terminal="max-tokens")
        self.workspace = str(workspace.resolve())
        self.records = []
        self.cursor = 1

    def create_task_session(self, cwd, title, agent_preset="standard", preset_sha256=None):
        value = super().create_task_session(cwd, title, agent_preset, preset_sha256)
        value["execution_configuration"]["permission_preset"] = "danger-full-access"
        return value

    def task_state(self, session_id):
        return {"cursor": self.cursor, "active": True, "running": False, "queue": [], "pendingInput": []}

    def prompt_task(self, session_id, text, request_id):
        super().prompt_task(session_id, text, request_id)
        turn = len(self.prompts)
        user_seq = 1 if turn == 1 else 3
        end_seq = 2 if turn == 1 else 4
        self.records.append({"event": {"seq": user_seq, "type": "user/message", "data": {
            "content": [{"type": "text", "text": text}],
            "source": {"kind": "user", "rpcId": request_id},
        }}})
        kind = "max-tokens" if turn == 1 else "completed"
        self.records.append({"event": {"seq": end_seq, "type": "turn/end",
                                       "data": {"reason": {"kind": kind}}}})
        self.cursor = end_seq
        self.waits[0] = idle(end_seq)
        return {"accepted": True, "requestId": request_id}

    def task_events(self, session_id, *, through_seq=None):
        records = [record for record in self.records
                   if through_seq is None or record["event"]["seq"] <= through_seq]
        return {"cursor": self.cursor if through_seq is None else through_seq,
                "records": records, "hasMore": False}

    def task_metadata(self, session_id):
        return {"workingDirectory": self.workspace, "title": "resumable task",
                "permissions": {"currentValue": "danger-full-access"},
                "tokenUsage": {"uncachedInputTokens": 10, "outputTokens": 5,
                               "cacheReadTokens": 100, "cacheWriteTokens": 0}}


def idle(cursor=9):
    return {
        "reason": "idle",
        "state": {"cursor": cursor, "active": True, "running": False, "queue": [], "pendingInput": []},
        "output": {},
    }


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "run"
        self.workspace = self.root / "workspace"
        self.logs = self.root / "logs/native"
        self.workspace.mkdir(parents=True)
        self.logs.mkdir(parents=True)
        self.run = {
            "id": "run-desktop-1",
            "task_id": "task-desktop-1",
            "root": str(self.root),
            "workspace": str(self.workspace),
            "timeout_seconds": 30,
        }
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def root_dir(self, name):
        root = Path(self.temp.name) / name
        (root / "workspace").mkdir(parents=True)
        (root / "logs/native").mkdir(parents=True)
        return root

    def invoke(self, desktop, cancel=None, root=None):
        root = root or self.root
        return run_desktop(
            desktop,
            {**self.run, "root": str(root), "workspace": str(root / "workspace")},
            "Frozen desktop task prompt",
            root / "logs/native",
            cancel or threading.Event(),
            lambda kind, data: self.events.append((kind, data)),
        )

    def test_event_analysis_distinguishes_controller_and_manual_prompts(self):
        events = {"records": [
            {"event": {"seq": 5, "type": "turn/start", "data": {"turn": 1}}},
            {"event": {"seq": 9, "type": "user/message", "data": {
                "source": {"kind": "user", "rpcId": "controller-request"},
                "content": [{"type": "text", "text": "PRIVATE CONTROLLER PROMPT"}],
            }}},
            {"event": {"seq": 10, "type": "user/message", "data": {
                "source": {"kind": "agent-instructions", "form": "instructions"},
                "content": [{"type": "text", "text": "PRIVATE PROJECT RULE"}],
            }}},
            {"event": {"seq": 20, "type": "turn/end", "data": {
                "turn": 1, "reason": {"kind": "completed"}}}},
            {"event": {"seq": 30, "type": "turn/start", "data": {"turn": 2}}},
            {"event": {"seq": 31, "type": "user/message", "data": {
                "source": {"kind": "user", "rpcId": "manual-request"},
                "content": [{"type": "text", "text": "PRIVATE USER FOLLOW-UP"}],
            }}},
        ]}
        analysis = analyze_session_events(events, "controller-request")
        self.assertEqual(analysis["controller_prompt_seqs"], [9])
        self.assertEqual(analysis["manual_prompt_seqs"], [31])
        self.assertEqual(analysis["manual_prompts"][0]["turn"], 2)
        self.assertNotIn("PRIVATE", json.dumps(analysis))
        self.assertEqual(analysis["latest_terminal"]["kind"], "completed")

    def test_terminal_without_stopped_session_is_outcome_unknown(self):
        busy_idle = {"reason": "idle",
                     "state": {"cursor": 9, "active": True, "running": False,
                               "queue": [{}], "pendingInput": []},
                     "output": {}}
        result = self.invoke(FakeDesktop([busy_idle], terminal="completed"))
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["outcome_reason"], "SESSION_NOT_STOPPED_AT_TERMINAL")
        self.assertFalse(result["completion_verified"])

    def test_completed_turn_uses_session_idle_and_turn_end_not_process_exit(self):
        desktop = FakeDesktop([idle()], terminal="completed")
        result = self.invoke(desktop)
        self.assertEqual(result["status"], "EXECUTION_COMPLETED")
        self.assertEqual(desktop.status_calls, 0)  # preset digest comes from the frozen spec

        self.assertTrue(result["completion_verified"])
        self.assertEqual(len(result["prompt_sha256"]), 64)
        self.assertGreater(result["prompt_bytes"], 0)
        self.assertIsNotNone(result["final_state"])
        self.assertIsNotNone(result["events_window"])
        self.assertEqual(result["prompt_request_id"], "desktop-run-desktop-1")
        self.assertEqual(result["terminal_kind"], "completed")
        self.assertTrue(result["session_idle"])
        self.assertNotIn("process_exit_confirmed", result)
        self.assertNotIn("job_active_final", result)
        self.assertEqual(desktop.created[0][0], str(self.workspace.resolve()))
        self.assertEqual(desktop.prompts[0][0], "session-task")
        saved = json.loads((self.logs / "desktop-result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "EXECUTION_COMPLETED")
        self.assertGreater(DESKTOP_CONNECTION_TIMEOUT, WAIT_CHUNK_MS / 1000)


    def test_max_tokens_resumes_the_same_session_after_the_old_cursor(self):
        desktop = ResumeDesktop(self.workspace)
        first = self.invoke(desktop)
        self.assertEqual(first["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(first["outcome_reason"], "MAX_OUTPUT_TOKENS")
        resumed_logs = self.root / "logs/resumed"
        resumed_logs.mkdir(parents=True)
        second = run_desktop(desktop, {**self.run, "resume": {
            "session_id": first["session_id"],
            "cursor": first["final_state"]["cursor"],
            "prompt_request_id": first["prompt_request_id"],
            "permission_preset": first["permission_preset"],
            "continuation_count": 1,
        }}, "Continue the task", resumed_logs, threading.Event(),
            lambda kind, data: self.events.append((kind, data)))
        self.assertEqual(second["status"], "EXECUTION_COMPLETED")
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["continuation_count"], 1)
        self.assertTrue(second["controller_prompt_seen"])
        self.assertEqual(len(desktop.created), 1)
        self.assertEqual(desktop.prompts[-1][2], "desktop-run-desktop-1-resume-1")
        self.assertEqual(second["terminal_seq"], 4)
    def test_transport_error_during_wait_is_retried_without_losing_completion(self):
        desktop = FakeDesktop([idle()], terminal="completed")
        original_wait = desktop.wait_task
        calls = 0

        def flaky_wait(session_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DesktopConnectionError("TRANSPORT_ERROR", outcome="UNKNOWN")
            return original_wait(session_id, **kwargs)

        desktop.wait_task = flaky_wait
        result = self.invoke(desktop)
        self.assertEqual(result["status"], "EXECUTION_COMPLETED")
        self.assertEqual(calls, 2)
        self.assertIn("runtime.transport_retry", [kind for kind, _ in self.events])
        self.assertEqual(desktop.prompts[0][0], "session-task")

    def test_end_metadata_is_read_once_and_does_not_drive_budget(self):
        desktop = FakeDesktop([idle()], terminal="completed")
        calls = []
        original = desktop.task_metadata

        def metadata(session_id):
            calls.append(session_id)
            return original(session_id)

        desktop.task_metadata = metadata
        result = self.invoke(desktop)
        self.assertEqual(result["status"], "EXECUTION_COMPLETED")
        self.assertEqual(result["token_total"], 115)
        self.assertEqual(calls, ["session-task"])
        self.assertNotIn("desktop_context_pressure", result)
        self.assertNotIn("budget_hard_exceeded", result)
        self.assertFalse((self.root / "native-attempt.json").exists())
        self.assertTrue((self.logs / "native-attempt.json").is_file())

    def test_outcome_matrix(self):
        with self.subTest(case="failed-terminal"):
            result = self.invoke(FakeDesktop([idle()], terminal="error"))
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["outcome_reason"], "UPSTREAM_FAILED")

        with self.subTest(case="max-tokens"):
            result = self.invoke(FakeDesktop([idle()], terminal="max-tokens"),
                                 root=self.root_dir("truncated"))
            self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
            self.assertEqual(result["outcome_reason"], "MAX_OUTPUT_TOKENS")
            self.assertTrue(result["partial"])

        with self.subTest(case="missing-terminal"):
            desktop = FakeDesktop([idle()], terminal=None)
            desktop.task_events = lambda session_id, **kwargs: {
                "cursor": 9, "records": [], "hasMore": False}
            result = self.invoke(desktop, root=self.root_dir("missing"))
            self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
            self.assertEqual(result["outcome_reason"], "MISSING_TERMINAL")

        with self.subTest(case="lost-prompt-receipt"):
            desktop = FakeDesktop([idle()], prompt_error=DesktopConnectionError(
                "TRANSPORT_ERROR", outcome="UNKNOWN"))
            result = self.invoke(desktop, root=self.root_dir("lost"))
            self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
            self.assertEqual(result["outcome_reason"], "TRANSPORT_ERROR")

    def test_cancel_targets_only_the_created_session_and_requires_terminal(self):
        cancel = threading.Event()
        desktop = FakeDesktop([idle()], terminal="interrupted")
        original_prompt = desktop.prompt_task

        def prompt_then_cancel(session_id, text, request_id):
            receipt = original_prompt(session_id, text, request_id)
            cancel.set()
            return receipt

        desktop.prompt_task = prompt_then_cancel
        result = self.invoke(desktop, cancel)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(desktop.cancels, ["session-task"])
        self.assertTrue(result["cancel_requested"])
        self.assertTrue(result["cancel_acknowledged"])
        self.assertEqual(result["terminal_kind"], "interrupted")

    def test_manual_prompt_during_turn_prevents_controller_completion_claim(self):
        desktop = FakeDesktop([idle()], terminal="completed")
        desktop.task_events = lambda session_id, **kwargs: {
            "cursor": 9,
            "records": [
                {"event": {"seq": 5, "type": "turn/start", "data": {"turn": 1}}},
                {"event": {"seq": 6, "type": "user/message", "data": {
                    "source": {"kind": "user", "rpcId": "desktop-run-desktop-1"},
                    "content": [{"type": "text", "text": "controller"}],
                }}},
                {"event": {"seq": 7, "type": "user/message", "data": {
                    "source": {"kind": "user", "rpcId": "manual-user"},
                    "content": [{"type": "text", "text": "PRIVATE MANUAL"}],
                }}},
                {"event": {"seq": 9, "type": "turn/end", "data": {
                    "turn": 1, "reason": {"kind": "completed"}}}},
            ],
            "hasMore": False,
        }
        result = self.invoke(desktop)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["outcome_reason"], "MANUAL_TAKEOVER_OBSERVED")
        self.assertEqual(result["manual_prompt_count"], 1)
        self.assertNotIn("PRIVATE MANUAL", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
