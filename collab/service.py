"""Single-writer service lifecycle; business calls use MCP, not custom HTTP RPC."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4

from .common import CollabError, file_lock, now
from .controller import Controller
from .gateway import Gateway
from .mcp_server import make_http_app
from .process_identity import birth
from .service_state import PROTOCOL, marker_path, publish, remove_registration
from .store import ServiceOwnership, Store


_LAUNCHERS = {}


def _reap_launchers():
    for identity, process in list(_LAUNCHERS.items()):
        if process.poll() is not None:
            process.wait()
            _LAUNCHERS.pop(identity, None)


def serve(root, execution_workspace, instance, *, native=False):
    import uvicorn
    root = Path(root).resolve()
    workspace = Path(execution_workspace).resolve(strict=True)
    with ServiceOwnership(root) as owner:
        Store(root)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        registration = {
            "protocol": PROTOCOL, "instance_id": instance, "state_root": str(root),
            "pid": os.getpid(), "process_birth": birth(os.getpid()), "started_at": now(),
            "port": port, "url": origin + "/mcp", "admin_url": origin,
            "execution_workspace": str(workspace),
        }
        token = secrets.token_urlsafe(48)
        controller = Controller(root, workspace, registration, native_enabled=native)
        server = None
        try:
            app = make_http_app(Gateway(controller), controller, registration, token,
                                lambda: setattr(server, "should_exit", True))
            publish(root, registration, token)
            config = uvicorn.Config(app, access_log=False, log_level="warning", lifespan="on")
            server = uvicorn.Server(config)
            server.run(sockets=[sock])
        finally:
            sock.close()
            try:
                controller.close()
            except BaseException:
                os._exit(197)
            store = Store(root)
            if not store.counts().get("RUNNING"):
                remove_registration(root, instance)


def start(root, execution_workspace, *, native=False):
    root = Path(root).resolve()
    workspace = Path(execution_workspace).resolve(strict=True)
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "locks/launch.lock", timeout=25):
        _reap_launchers()
        return _start_locked(root, workspace, native=native)


def _start_locked(root, workspace, *, native=False):
    from .client import status
    current = status(root)
    if current["state"] == "RUNNING":
        if native and not current.get("model_execution_enabled"):
            raise CollabError("SERVICE_MODE_CONFLICT", "stop the read-only service before starting native mode")
        if current.get("execution_workspace") and Path(current["execution_workspace"]) != workspace:
            raise CollabError("SERVICE_MODE_CONFLICT", "running service uses a different execution workspace")
        return {**current, "already_running": True}
    if current["state"] == "STARTING":
        raise CollabError("SERVICE_START_PENDING", "service ownership is occupied")
    package = Path(__file__).resolve().parents[1]
    python = package / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise CollabError("MISSING_DEPENDENCY", "collaboration environment is unavailable")
    instance = str(uuid4())
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    argv = [str(python), "-B", "-m", "collab", "--state-root", str(root),
            "--execution-workspace", str(workspace), "service", "_serve", "--instance", instance]
    if native:
        argv.append("--native")
    with (logs / f"service-{instance}.log").open("ab") as out:
        process = subprocess.Popen(argv, cwd=package, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=out,
                                   creationflags=0x08000000 if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
    _LAUNCHERS[instance] = process
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = status(root)
        if found["state"] == "RUNNING":
            return {**found, "already_running": found["instance_id"] != instance}
        if process.poll() is not None:
            _reap_launchers()
            raise CollabError("SERVICE_START_FAILED", f"service did not start; inspect logs/service-{instance}.log")
        time.sleep(0.1)
    raise CollabError("SERVICE_START_PENDING", "service startup is unconfirmed; do not start another instance")


def stop(root):
    from .client import request, status
    from .process_identity import ProcessWatch
    current = status(root)
    if current["state"] == "STOPPED":
        return {"stopped": True, "already_stopped": True}
    with ProcessWatch(current.get("pid"), current.get("process_birth")) as process:
        response = request(root, "/v1/stop", {}, timeout=180)
        if not response["ok"]:
            raise CollabError(response["error"]["code"], "service stop failed")
        result = response["result"]
        if not result.get("stop_requested"):
            return result
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.exited(0) and not marker_path(root).exists():
                _reap_launchers()
                return {"stopped": True, "instance_id": current.get("instance_id"),
                        "process_exit_confirmed": True}
            time.sleep(0.05)
    return {"stopped": False, "stop_requested": True, "reason": "EXIT_NOT_CONFIRMED"}
