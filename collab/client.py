"""Private health/stop client. Task operations use the MCP service directly."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .common import CollabError, canonical, file_lock
from .service_state import load_registration, marker_path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(root, path, payload=None, *, timeout=10):
    if path not in ("/v1/health", "/v1/stop"):
        raise CollabError("INVALID_INPUT", "only private health/stop endpoints are available")
    try:
        registration, token = load_registration(root)
    except (OSError, ValueError, KeyError, CollabError) as exc:
        raise CollabError("SERVICE_UNAVAILABLE", "MCP service registration unavailable") from exc
    req = Request(registration["admin_url"] + path,
                  data=None if payload is None else canonical(payload),
                  headers={"Authorization": "Bearer " + token,
                           "X-Collab-Instance": registration["instance_id"],
                           "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as response:
            value = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise CollabError("SERVICE_UNAVAILABLE", "MCP service response unavailable") from None
    if not isinstance(value, dict) or "ok" not in value:
        raise CollabError("SERVICE_UNAVAILABLE", "invalid service response")
    return value


def status(root):
    root = Path(root).resolve()
    if not marker_path(root).exists():
        if (root / "locks/service.lock").is_file():
            try:
                with file_lock(root / "locks/service.lock", timeout=0):
                    pass
            except CollabError:
                return {"state": "STARTING", "state_root": str(root), "model_execution_enabled": False}
        return {"state": "STOPPED", "state_root": str(root), "model_execution_enabled": False}
    try:
        response = request(root, "/v1/health", timeout=3)
        if not response["ok"]:
            raise CollabError("SERVICE_UNAVAILABLE", "health check failed")
        return response["result"]
    except CollabError as exc:
        return {"state": "UNAVAILABLE", "state_root": str(root),
                "model_execution_enabled": False, "error_code": exc.code}
