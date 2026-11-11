"""Service rendezvous; bearer material is kept in an owner-private directory."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .common import CollabError, read_json, write_json

PROTOCOL = 1


def marker_path(root):
    return Path(root)/"service.json"


def auth_path(root):
    return Path(root)/"private/service-auth.json"


def private_directory(root):
    directory = Path(root)/"private"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or getattr(directory.stat(), "st_file_attributes", 0) & 0x400:
        raise CollabError("INVALID_ROOT", "private directory cannot be a reparse point")
    if os.name == "nt":
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=10)
        if identity.returncode:
            raise CollabError("PRIVATE_CONFIG_FAILED", "cannot identify current user for private configuration")
        rows = list(csv.reader(io.StringIO(identity.stdout.decode("utf-8", errors="replace"))))
        sid = rows[-1][-1].strip() if rows else ""
        if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
            raise CollabError("PRIVATE_CONFIG_FAILED", "invalid current user SID")
        result = subprocess.run(["icacls.exe", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, timeout=10)
        if result.returncode:
            raise CollabError("PRIVATE_CONFIG_FAILED", "cannot restrict private configuration ACL")
    else:
        directory.chmod(0o700)
    return directory


def publish(root, registration, token):
    private_directory(root)
    write_json(auth_path(root), {"instance_id": registration["instance_id"], "token": token})
    if os.name != "nt":
        auth_path(root).chmod(0o600)
    write_json(marker_path(root), registration)


def load_registration(root):
    value = read_json(marker_path(root))
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise CollabError("SERVICE_UNAVAILABLE", "unrecognized service registration; restart the service")
    if not isinstance(value.get("url"), str) or not isinstance(value.get("state_root"), str) or not isinstance(value.get("instance_id"), str):
        raise CollabError("SERVICE_UNAVAILABLE", "invalid service registration fields")
    parts = urlsplit(value.get("url", ""))
    admin = urlsplit(value.get("admin_url", ""))
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.username or parts.password
            or parts.path != "/mcp" or parts.query or parts.fragment or not parts.port
            or admin.scheme != "http" or admin.hostname != "127.0.0.1" or admin.username or admin.password
            or admin.path or admin.query or admin.fragment or admin.port != parts.port):
        raise CollabError("SERVICE_UNAVAILABLE", "invalid loopback MCP service registration")
    if Path(value.get("state_root", "")).resolve() != Path(root).resolve():
        raise CollabError("SERVICE_UNAVAILABLE", "service state root differs")
    secret = read_json(auth_path(root))
    if not isinstance(secret, dict) or secret.get("instance_id") != value.get("instance_id") or not isinstance(secret.get("token"), str) or len(secret["token"]) < 32:
        raise CollabError("SERVICE_UNAVAILABLE", "service registration is incomplete; restart the service")
    return value, secret["token"]


def remove_registration(root, instance):
    # Only called while holding the service ownership gate; never remove a successor.
    path = marker_path(root)
    if path.exists() and read_json(path).get("instance_id") == instance:
        path.unlink()
        auth_path(root).unlink(missing_ok=True)
