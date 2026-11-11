"""Service-owned desktop pairing tests with a local HTTP fixture only."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collab.desktop_connection import DesktopConnectionError
from collab.desktop_pairing import DesktopPairing, LaunchResult
from tests.support import assert_collab_code, fixture


SESSIONS = [
    {
        "sessionId": "session-visible",
        "updatedAt": 123456,
        "running": False,
        "blank": False,
        "formatStatus": "current",
        "projections": {
            "asOfSeq": 9,
            "values": {
                "title": "Read-only desktop session",
                "workingDirectory": "C:/Users/fixture/project",
                "sessionStats": {"turns": 2, "steps": 7},
                "modelSelection": {
                    "lastUsed": {
                        "provider": "opencode-go",
                        "model": "deepseek-v4.1-flash",
                    }
                },
                "turnOutline": [
                    {
                        "prompt": "PRIVATE PROMPT MUST NOT ESCAPE",
                        "response": "PRIVATE RESPONSE MUST NOT ESCAPE",
                    }
                ],
            },
        },
    }
]


class DesktopPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir()
        self.alive = True

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, launch, *, pids=()):
        value = DesktopPairing(
            self.root,
            app_path=Path(self.temp.name) / "DeepSeek Harness.exe",
            running_pids=lambda: list(pids),
            launcher=lambda: launch,
            alive=lambda _pid, _birth: self.alive,
            launch_wait=1,
        )
        value._installed_version = lambda: "0.2.2-rc.1"
        return value

    def test_launcher_log_and_early_exit(self):
        app_path = Path(self.temp.name) / "DeepSeek Harness.exe"
        app_path.write_bytes(b"fixture")
        launch_url = "http://127.0.0.1:1924/?token=PRIVATE"
        calls = []

        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            kwargs["stdout"].write(f"startup\ndsh web: {launch_url}\n".encode("utf-8"))
            kwargs["stdout"].flush()
            return SimpleNamespace(pid=4321, poll=lambda: None)

        manager = DesktopPairing(self.root, app_path=app_path, running_pids=lambda: [],
                                 alive=lambda _pid, _birth: True, launch_wait=1)
        with self.subTest(kind="live-log"), patch(
            "collab.desktop_pairing._POPEN", side_effect=spawn
        ), patch("collab.desktop_pairing.birth", return_value="birth-1"):
            launch = manager._launch_desktop()
            self.assertEqual(launch.launch_url, launch_url)
            self.assertNotEqual(calls[0][1]["stdout"], subprocess.PIPE)
            self.assertEqual(calls[0][1]["stderr"], subprocess.STDOUT)
            self.assertEqual(manager.launch_log_path,
                             self.root.resolve() / "private" / "desktop-launch.log")
            self.assertTrue(manager.launch_log_path.is_file())
            self.assertIn(launch_url, manager.launch_log_path.read_text(encoding="utf-8"))

        with self.subTest(kind="early-exit"), patch(
            "collab.desktop_pairing._POPEN",
            return_value=SimpleNamespace(pid=4321, poll=lambda: 1),
        ):
            error = assert_collab_code(self, "DESKTOP_EXITED_WITHOUT_PAIRING_URL",
                                       manager._launch_desktop)
            self.assertIsNotNone(error)

    def test_pairing_reader_uses_live_file_redirect(self):
        manager = self.manager(LaunchResult(1, "birth-1", "http://127.0.0.1:1/?token=x"))
        launch_url = "http://127.0.0.1:1924/?token=LIVE"
        log_path = self.root / "live-launch.log"
        script = (
            "import sys,time;"
            f"sys.stdout.write('dsh web: {launch_url}\\n');"
            "sys.stdout.flush();"
            "time.sleep(0.2)"
        )
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            self.assertEqual(manager._wait_for_pairing_url(process, log_path), launch_url)
        finally:
            process.wait(timeout=5)

    def test_pair_restore_metadata_and_preset_registration(self):
        with fixture(sessions=SESSIONS) as (url, _records):
            launch = LaunchResult(1234, "birth-1", url)
            manager = self.manager(launch)
            paired = manager.pair()
            self.assertEqual(paired["state"], "CONNECTED")
            self.assertEqual(paired["pairing"], "PAIRED")
            self.assertEqual(paired["session_count"], 1)
            self.assertEqual(paired["compatibility"], "TESTED")
            self.assertNotIn("cookie", json.dumps(paired).lower())
            self.assertTrue(manager.private_path.is_file())

            restored = self.manager(launch)
            status = restored.status()
            self.assertEqual(status["state"], "CONNECTED")
            self.assertEqual(status["pairing_id"], paired["pairing_id"])
            restored.close()

            manager._connection.rpc = lambda method, args: {
                "agentPreset": args["agentPreset"],
                "content": "- id: persona\n  name: '@deepseek-ai/dsh-persona'\n",
            }
            configuration = manager.execution_configuration("collab-worker")
            self.assertEqual(configuration["agent_preset"], "collab-worker")
            self.assertEqual(len(configuration["preset_sha256"]), 64)
            self.assertNotIn("capabilities", configuration)
            manager._pair["runtime_version"] = "different-version"
            self.assertEqual(manager._public("CONNECTED", manager._pair)["compatibility"], "UNTESTED")

    def test_existing_unpaired_desktop_returns_manual_action_without_launch(self):
        manager = self.manager(
            LaunchResult(1, "birth-1", "http://127.0.0.1:1/?token=never-used"), pids=(99,))
        manager._launcher = lambda: (_ for _ in ()).throw(AssertionError("launcher must not run"))
        result = manager.pair()
        self.assertEqual(result["state"], "MANUAL_RESTART_REQUIRED")
        self.assertNotIn("desktop_termination_requested", result)
        self.assertFalse(manager.private_path.exists())

    def test_pairing_invalidation_cases(self):
        def fresh_root(name):
            self.root = Path(self.temp.name) / name
            self.root.mkdir(parents=True, exist_ok=True)
            self.alive = True

        with self.subTest(kind="stale-app-exit"):
            fresh_root("stale-exit")
            with fixture(sessions=SESSIONS) as (url, _records):
                manager = self.manager(LaunchResult(1234, "birth-1", url))
                self.assertEqual(manager.pair()["state"], "CONNECTED")
                self.assertTrue(manager.private_path.exists())
                self.alive = False
                self.assertEqual(manager.status()["state"], "STALE_APP_EXITED")
                self.assertFalse(manager.private_path.exists())

        with self.subTest(kind="stale-after-restart"):
            fresh_root("stale-restart")
            with fixture(sessions=SESSIONS) as (url, _records):
                launch = LaunchResult(1234, "birth-1", url)
                self.alive = True
                manager = self.manager(launch)
                self.assertEqual(manager.pair()["state"], "CONNECTED")
                restarted_service = self.manager(launch)
                self.alive = False
                self.assertEqual(restarted_service.status()["state"], "STALE_APP_EXITED")
                self.assertFalse(restarted_service.private_path.exists())

        with self.subTest(kind="corrupt-private-state"):
            fresh_root("corrupt")
            self.manager(LaunchResult(1, "birth-1", "http://127.0.0.1:1/?token=x"))
            self.root.joinpath("private").mkdir()
            self.root.joinpath("private/desktop-pairing.json").write_text(json.dumps({
                "schema_version": 1,
                "pairing_id": "x" * 32,
                "origin": "http://example.org",
                "cookie": "fixture=x",
                "process": {"pid": 1234, "process_birth": "birth-1"},
                "runtime_version": "0.2.2-rc.1",
                "created_at": "2026-11-07T00:00:00+00:00",
            }), encoding="utf-8")
            manager = self.manager(LaunchResult(1234, "birth-1", "http://127.0.0.1:1/?token=x"))
            self.assertEqual(manager.status()["state"], "INVALID_PAIRING")
            self.assertFalse(manager.private_path.exists())

        with self.subTest(kind="unreachable-without-replay"):
            fresh_root("unreachable")
            with fixture(sessions=SESSIONS) as (url, records):
                manager = self.manager(LaunchResult(1234, "birth-1", url))
                self.assertEqual(manager.pair()["state"], "CONNECTED")
                manager._connection._exchange = lambda *args, **kwargs: (_ for _ in ()).throw(
                    DesktopConnectionError("TRANSPORT_ERROR", outcome="UNKNOWN"))
                self.assertEqual(manager.status()["state"], "UNREACHABLE")
                self.assertEqual(len(records), 2)  # connect + initial list; no replay

    def test_task_session_control_wrappers_use_connected_pairing(self):
        with fixture(sessions=SESSIONS) as (url, _records):
            manager = self.manager(LaunchResult(1234, "birth-1", url))
            self.assertEqual(manager.pair()["state"], "CONNECTED")
            manager._connection.resolve_workspace = lambda cwd: {
                "workspace": {"workspaceId": "workspace-task", "path": str(cwd)}, "created": False}
            created_with = []
            configuration = {"agent_preset": "standard", "preset_sha256": "fixture-preset"}
            manager.execution_configuration = lambda *, agent_preset="standard": {
                **configuration, "agent_preset": agent_preset}
            manager._connection.create = lambda **kwargs: (
                created_with.append(kwargs)
                or {"sessionId": "session-task", "agentPreset": kwargs["agent_preset"]}
            )
            manager._session_values = lambda session_id: {
                "workingDirectory": str(self.root / "workspace"),
                "permissions": {"currentValue": "workspace-write"},
            }
            workspace = self.root / "workspace"
            workspace.mkdir()
            created = manager.create_task_session(
                workspace, "collab fixture", agent_preset="collab-worker")
            self.assertEqual(created["session_id"], "session-task")
            self.assertEqual(created["workspace_id"], "workspace-task")
            self.assertEqual(created_with, [{
                "workspace_id": "workspace-task",
                "agent_preset": "collab-worker",
            }])
            self.assertEqual(created["execution_configuration"], {
                **configuration, "agent_preset": "collab-worker",
                "permission_preset": "workspace-write"})
            manager._connection.prompt = lambda session_id, text, request_id: {
                "accepted": True, "requestId": request_id}
            self.assertTrue(manager.prompt_task("session-task", "fixture", "request-1")["accepted"])
            manager._connection.state = lambda session_id: {
                "cursor": 5, "active": True, "running": True, "queue": [], "pendingInput": []}
            self.assertTrue(manager.task_state("session-task")["running"])
            manager._connection.wait = lambda session_id, **kwargs: {
                "reason": "idle",
                "state": {"cursor": 9, "active": True, "running": False, "queue": [], "pendingInput": []},
                "output": {},
            }
            self.assertEqual(manager.wait_task("session-task")["reason"], "idle")
            manager._connection.events = lambda session_id, **kwargs: {
                "cursor": 9, "records": [], "hasMore": False}
            self.assertEqual(manager.task_events("session-task")["cursor"], 9)
            manager._connection.cancel = lambda session_id: {"accepted": True}
            self.assertTrue(manager.cancel_task("session-task")["accepted"])

            manager._session_values = lambda session_id: {
                "workingDirectory": "C:/different",
                "permissions": {"currentValue": "workspace-write"},
            }
            assert_collab_code(self, "DESKTOP_WORKSPACE_MISMATCH",
                               manager.create_task_session, workspace, "wrong cwd")
            manager._connection.resolve_workspace = lambda cwd: {
                "workspace": {"workspaceId": "workspace-other", "path": "C:/different"}}
            before = len(created_with)
            assert_collab_code(self, "DESKTOP_WORKSPACE_MISMATCH",
                               manager.create_task_session, workspace, "wrong group")
            self.assertEqual(len(created_with), before)

    def test_create_task_session_uses_frozen_preset_digest_without_preset_rpc(self):
        with fixture(sessions=SESSIONS) as (url, _records):
            manager = self.manager(LaunchResult(1234, "birth-1", url))
            self.assertEqual(manager.pair()["state"], "CONNECTED")
            workspace = self.root / "workspace"
            workspace.mkdir()
            manager._connection.resolve_workspace = lambda cwd: {
                "workspace": {"workspaceId": "workspace-task", "path": str(cwd)}}
            manager._connection.create = lambda **kwargs: {
                "sessionId": "session-task", "agentPreset": kwargs["agent_preset"]}
            manager._session_values = lambda session_id: {
                "workingDirectory": str(workspace),
                "permissions": {"currentValue": "workspace-write"},
            }
            manager.execution_configuration = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("agentPresets/read must not be called when the digest is frozen"))
            created = manager.create_task_session(
                workspace, "frozen preset", agent_preset="collab-worker",
                preset_sha256="a" * 64)
            self.assertEqual(created["execution_configuration"]["preset_sha256"], "a" * 64)

    def test_task_session_requires_connected_pairing(self):
        manager = self.manager(LaunchResult(1, "birth-1", "http://127.0.0.1:1/?token=x"))
        assert_collab_code(self, "DESKTOP_NOT_PAIRED",
                           manager.create_task_session, self.root, "fixture")


if __name__ == "__main__":
    unittest.main()
