"""Service-owned DeepSeek desktop pairing and read-only session observation.

Launch URLs and cookies stay inside the service process or its owner-private
directory. Public tool results contain only a stable pairing identity, process
identity, loopback origin and bounded session metadata. This module never owns
the desktop lifecycle and never terminates the desktop application.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import installation as runtime
from .common import CollabError, read_json, write_json
from .desktop_connection import DesktopConnection, DesktopConnectionError, DesktopRPCError
from .process_identity import ProcessWatch, birth
from .service_state import private_directory


DESKTOP_PROCESS = "DeepSeek Harness.exe"
MAX_LAUNCH_WAIT = 45
DESKTOP_CONNECTION_TIMEOUT = 45
_PAIRING_URL = re.compile(r"dsh web: (https?://[^\s\x1b]+)")
_POPEN = subprocess.Popen


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LaunchResult:
    pid: int
    process_birth: str
    launch_url: str
    process: object | None = None


class DesktopPairing:
    """A service-scoped connection, not a scheduler or desktop process owner."""

    def __init__(self, root, *, app_path=None, running_pids=None, launcher=None,
                 alive=None, connection_factory=DesktopConnection, launch_wait=MAX_LAUNCH_WAIT):
        self.root = Path(root).resolve()
        local = os.environ.get("LOCALAPPDATA")
        if app_path:
            self._app_path = Path(app_path)
        elif sys.platform == "darwin":
            self._app_path = Path("/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness")
        else:
            self._app_path = (Path(local) / "Programs/DeepSeek Harness/DeepSeek Harness.exe"
                if local else Path.home() / "AppData/Local/Programs/DeepSeek Harness/DeepSeek Harness.exe")
        if self._app_path.parent.name == "MacOS" and self._app_path.parent.parent.name == "Contents":
            self._archive_path = self._app_path.parent.parent / "Resources/app.asar"
        else:
            self._archive_path = self._app_path.parent / "resources/app.asar"
        self._running_pids = running_pids or self._desktop_pids
        self._launcher = launcher or self._launch_desktop
        self._alive = alive or self._process_alive
        self._connection_factory = connection_factory
        self._launch_wait = launch_wait
        self._connection = None
        self._process = None
        self._pair = None
        self._lock = threading.RLock()

    @property
    def private_path(self):
        return self.root / "private/desktop-pairing.json"

    @property
    def launch_log_path(self):
        return self.root / "private/desktop-launch.log"

    def _desktop_pids(self):
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/ps", "-axo", "pid=,comm="], capture_output=True,
                text=True, errors="replace", timeout=15, check=True,
                stdin=subprocess.DEVNULL,
            )
            pids = []
            for line in result.stdout.splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) == 2 and Path(fields[1]).name == "DeepSeek Harness":
                    try:
                        pids.append(int(fields[0]))
                    except ValueError:
                        continue
            return pids
        tasklist = Path(os.environ["SYSTEMROOT"]) / "System32/tasklist.exe"
        result = subprocess.run(
            [str(tasklist), "/FI", "IMAGENAME eq " + DESKTOP_PROCESS, "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=True,
        )
        pids = []
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row) >= 2 and row[0].casefold() == DESKTOP_PROCESS.casefold():
                try:
                    pids.append(int(row[1]))
                except ValueError:
                    continue
        return pids


    @staticmethod
    def _process_alive(pid, process_birth):
        try:
            with ProcessWatch(pid, process_birth) as watch:
                return not watch.exited(0)
        except Exception:
            return False

    def _installed_version(self):
        archive = self._archive_path
        try:
            value = json.loads(runtime._asar(archive, "package.json"))
        except Exception:
            raise CollabError("DESKTOP_INSTALLATION_UNAVAILABLE", "desktop package metadata is unavailable") from None
        version = value.get("version")
        if not isinstance(version, str) or not version:
            raise CollabError("DESKTOP_INSTALLATION_UNAVAILABLE", "desktop package version is unavailable")
        return version

    def _launch_desktop(self):
        if not self._app_path.is_file():
            raise CollabError("DESKTOP_NOT_INSTALLED", "DeepSeek Harness executable is missing")
        env = dict(os.environ)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        private_directory(self.root)
        log_path = self.launch_log_path
        try:
            output = log_path.open("wb")
        except OSError:
            raise CollabError("DESKTOP_LAUNCH_LOG_UNAVAILABLE", "desktop launch log cannot be opened") from None
        try:
            process = _POPEN(
                [str(self._app_path)],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=os.name != "nt",
            )
        finally:
            output.close()
        launch_url = self._wait_for_pairing_url(process, log_path)
        try:
            process_birth = birth(process.pid)
        except Exception:
            raise CollabError("DESKTOP_PROCESS_IDENTITY_UNKNOWN", "desktop process identity is unavailable") from None
        return LaunchResult(process.pid, process_birth, launch_url, process)

    def _wait_for_pairing_url(self, process, log_path):
        deadline = time.monotonic() + self._launch_wait
        try:
            source = log_path.open("rb")
        except OSError:
            raise CollabError("DESKTOP_LAUNCH_LOG_UNAVAILABLE", "desktop launch log cannot be read") from None
        with source:
            tail = ""
            while True:
                chunk = source.read(65536)
                if chunk:
                    tail = (tail + chunk.decode("utf-8", errors="replace"))[-65536:]
                    match = _PAIRING_URL.search(tail)
                    if match and "token=" in match.group(1):
                        return match.group(1)
                if process.poll() is not None:
                    raise CollabError(
                        "DESKTOP_EXITED_WITHOUT_PAIRING_URL",
                        "desktop exited before publishing its pairing URL",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollabError(
                        "DESKTOP_STARTUP_URL_NOT_OBSERVED",
                        "desktop started but no official pairing URL was observed",
                    )
                time.sleep(min(0.05, remaining))

    def _load_private(self):
        path = self.private_path
        if not path.is_file():
            return None
        try:
            value = read_json(path)
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != 1
                or not isinstance(value.get("pairing_id"), str)
                or not isinstance(value.get("origin"), str)
                or not isinstance(value.get("cookie"), str)
                or not isinstance(value.get("created_at"), str)
                or not isinstance(value.get("runtime_version"), str)
                or not isinstance(value.get("process"), dict)
                or type(value["process"].get("pid")) is not int
                or not isinstance(value["process"].get("process_birth"), str)
            ):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError, KeyError, CollabError):
            path.unlink(missing_ok=True)
            return None

    def _save_private(self, auth, pid, process_birth, runtime_version):
        value = {
            "schema_version": 1,
            "pairing_id": _digest(f'{auth["origin"]}|{pid}|{process_birth}')[:32],
            "origin": auth["origin"],
            "cookie": auth["cookie"],
            "process": {"pid": pid, "process_birth": process_birth},
            "runtime_version": runtime_version,
            "created_at": _stamp(),
        }
        private_directory(self.root)
        write_json(self.private_path, value)
        if os.name != "nt":
            self.private_path.chmod(0o600)
        self._pair = value
        return value

    def _forget_local(self):
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        self._pair = None
        self.private_path.unlink(missing_ok=True)

    def _restore(self):
        if self._connection is not None and self._pair is not None:
            process = self._pair["process"]
            if self._alive(process["pid"], process["process_birth"]):
                return self._pair
            self._forget_local()
            raise CollabError("DESKTOP_PAIRING_STALE", "the paired desktop process exited or was replaced")
        pair = self._load_private()
        if pair is None:
            return None
        process = pair["process"]
        if not self._alive(process["pid"], process["process_birth"]):
            self._forget_local()
            raise CollabError("DESKTOP_PAIRING_STALE", "the paired desktop process exited or was replaced")
        try:
            connection = self._connection_factory.restore(
                pair["origin"],
                pair["cookie"],
                timeout=DESKTOP_CONNECTION_TIMEOUT,
            )
        except DesktopConnectionError:
            self._forget_local()
            raise CollabError("DESKTOP_PAIRING_INVALID", "stored desktop pairing material is invalid") from None
        self._connection, self._pair = connection, pair
        return pair

    def _public(self, state, pair=None, *, session_count=None):
        value = {
            "state": state,
            "credentials_persisted_in_private_state": pair is not None,
        }
        if pair is not None:
            value.update(
                pairing_id=pair["pairing_id"],
                origin=pair["origin"],
                runtime_version=pair["runtime_version"],
                tested_runtime_version=runtime.DSH_VERSION,
                compatibility="TESTED" if pair["runtime_version"] == runtime.DSH_VERSION else "UNTESTED",
                process={
                    "pid": pair["process"]["pid"],
                    "process_birth": pair["process"]["process_birth"],
                },
                created_at=pair["created_at"],
            )
            if session_count is not None:
                value["session_count"] = session_count
        return value

    def pair(self):
        with self._lock:
            return self._pair_impl()


    def _pair_impl(self):
        existing = self._load_private()
        if existing is not None:
            try:
                status = self.status()
            except CollabError:
                status = None
            if status is not None and status.get("state") == "CONNECTED":
                return {**status, "pairing": "ALREADY_PAIRED"}
        if self._running_pids():
            return {
                "state": "MANUAL_RESTART_REQUIRED",
                "pairing": "NOT_STARTED",
                "reason": "DESKTOP_PROCESS_ALREADY_RUNNING_WITHOUT_PAIRING",
                "instruction": "Quit the desktop through its normal application entry, then retry pairing.",
            }
        version = self._installed_version()
        launch = self._launcher()
        if isinstance(launch, LaunchResult) is False:
            raise CollabError("DESKTOP_PAIRING_FAILED", "desktop launcher returned no bounded launch result")
        self._process = launch.process
        if not self._alive(launch.pid, launch.process_birth):
            raise CollabError("DESKTOP_PROCESS_IDENTITY_UNKNOWN", "desktop process exited before pairing")
        connection = None
        try:
            connection = self._connection_factory(
                launch.launch_url,
                enabled=True,
                timeout=DESKTOP_CONNECTION_TIMEOUT,
            )
            connection.connect()
            sessions = connection.list_sessions()
            items = sessions.get("items") if isinstance(sessions, dict) else None
            if not isinstance(items, list):
                raise DesktopConnectionError("INVALID_SESSION_LIST")
            if not self._alive(launch.pid, launch.process_birth):
                raise DesktopConnectionError("DESKTOP_PROCESS_REPLACED")
            pair = self._save_private(
                connection.private_auth(),
                launch.pid,
                launch.process_birth,
                version,
            )
            self._connection = connection
            connection = None
            return {**self._public("CONNECTED", pair, session_count=len(items)), "pairing": "PAIRED"}
        except (DesktopConnectionError, DesktopRPCError):
            raise CollabError("DESKTOP_PAIRING_UNCONFIRMED", "desktop pairing could not be confirmed") from None
        finally:
            if connection is not None:
                connection.close()

    def status(self):
        with self._lock:
            return self._status()

    def _status(self):
        try:
            pair = self._restore()
        except CollabError as error:
            if error.code == "DESKTOP_PAIRING_STALE":
                return self._public("STALE_APP_EXITED")
            return self._public("INVALID_PAIRING")
        if pair is None:
            return self._public("UNPAIRED")
        try:
            sessions = self._connection.list_sessions()
        except DesktopRPCError:
            return self._public("UNREACHABLE", pair)
        except DesktopConnectionError:
            return self._public("UNREACHABLE", pair)
        items = sessions.get("items") if isinstance(sessions, dict) else None
        if not isinstance(items, list):
            return self._public("UNREACHABLE", pair)
        return self._public("CONNECTED", pair, session_count=len(items))
    def execution_configuration(self, agent_preset="standard"):
        """Confirm preset registration only; never infer live tool capabilities."""
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "preset inspection requires pairing")
            document = self._connection.rpc("agentPresets/read", {"agentPreset": agent_preset})
            content = document.get("content") if isinstance(document, dict) else None
            if not isinstance(content, str) or not content.strip() or document.get("agentPreset") != agent_preset:
                raise CollabError("AGENT_PRESET_UNAVAILABLE", f"Agent preset is unavailable: {agent_preset}")
            return {
                "agent_preset": agent_preset,
                "preset_sha256": _digest(content),
                "scope": "Official preset registration only; live capabilities require execution evidence",
            }

    def _session_values(self, session_id):
        sessions = self._connection.list_sessions()
        items = sessions.get("items") if isinstance(sessions, dict) else None
        if not isinstance(items, list):
            raise CollabError("INVALID_DESKTOP_RESPONSE", "desktop Session list is malformed")
        for item in items:
            if isinstance(item, dict) and item.get("sessionId") == session_id:
                projections = item.get("projections") if isinstance(item.get("projections"), dict) else {}
                values = projections.get("values") if isinstance(projections.get("values"), dict) else {}
                return values
        raise CollabError("INVALID_DESKTOP_RESPONSE", "created Session is absent from the official list")

    def create_task_session(self, cwd, title, agent_preset="standard", preset_sha256=None):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop Session creation requires pairing")
            resolved = self._connection.resolve_workspace(cwd)
            workspace = resolved.get("workspace") if isinstance(resolved, dict) else None
            workspace_id = workspace.get("workspaceId") if isinstance(workspace, dict) else None
            workspace_path = workspace.get("path") if isinstance(workspace, dict) else None
            if not isinstance(workspace_id, str) or not workspace_id:
                raise CollabError("INVALID_DESKTOP_RESPONSE", "desktop Workspace response lacks workspaceId")
            if not isinstance(workspace_path, str) or os.path.normcase(os.path.abspath(workspace_path)) != os.path.normcase(os.path.abspath(cwd)):
                raise CollabError("DESKTOP_WORKSPACE_MISMATCH", "desktop Workspace path differs from the execution workspace")
            # A bare cwd sets execution location but leaves the App session ungrouped.
            if preset_sha256 is None:
                configuration = self.execution_configuration(agent_preset=agent_preset)
            else:
                if not isinstance(preset_sha256, str) or not preset_sha256:
                    raise CollabError("INVALID_AGENT_PRESET", "frozen preset digest is invalid")
                configuration = {
                    "agent_preset": agent_preset,
                    "preset_sha256": preset_sha256,
                    "scope": "Frozen at submit; live capabilities require execution evidence",
                }
            created = self._connection.create(workspace_id=workspace_id, agent_preset=configuration["agent_preset"])
            session_id = created.get("sessionId") if isinstance(created, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise CollabError("INVALID_DESKTOP_RESPONSE", "desktop create response lacks sessionId")
            values = self._session_values(session_id)
            observed_cwd = values.get("workingDirectory")
            if not isinstance(observed_cwd, str) or os.path.normcase(os.path.abspath(observed_cwd)) != os.path.normcase(os.path.abspath(cwd)):
                raise CollabError("DESKTOP_WORKSPACE_MISMATCH", "desktop Session working directory differs from the frozen workspace")
            if created.get("agentPreset") != configuration["agent_preset"]:
                raise CollabError("DESKTOP_EXECUTION_MISMATCH", "new Session preset differs from the requested preset")
            permission = values.get("permissions", {}).get("currentValue")
            if not isinstance(permission, str) or not permission:
                raise CollabError("DESKTOP_EXECUTION_MISMATCH", "new Session permission is unavailable")
            return {
                "session_id": session_id,
                "title": values.get("title") if isinstance(values.get("title"), str) else title,
                "working_directory": observed_cwd,
                "workspace_id": workspace_id,
                "execution_configuration": {**configuration, "permission_preset": permission},
            }

    def prompt_task(self, session_id, text, request_id):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop pairing is unavailable")
            return self._connection.prompt(session_id, text, request_id=request_id)

    def task_state(self, session_id):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop pairing is unavailable")
            return self._connection.state(session_id)

    def wait_task(self, session_id, *, timeout_ms=8000, after_seq=-1):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop pairing is unavailable")
            return self._connection.wait(session_id, timeout_ms=timeout_ms, after_seq=after_seq)

    def task_events(self, session_id, *, through_seq=None):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop pairing is unavailable")
            return self._connection.events(session_id, through_seq=through_seq, max_messages=1000,
                                           min_messages=1, min_turns=1)

    def task_metadata(self, session_id):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop Session metadata is unavailable")
            return self._session_values(session_id)

    def cancel_task(self, session_id):
        with self._lock:
            if self._restore() is None:
                raise CollabError("DESKTOP_NOT_PAIRED", "desktop pairing is unavailable")
            return self._connection.cancel(session_id)
    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
