"""Shared DSH installation metadata and app.asar reading."""
from __future__ import annotations

import json
from pathlib import Path
import struct


DSH_VERSION = "0.2.2-rc.1"


class InstallationError(Exception):
    """Fixed public error for unavailable host installation metadata."""


def _asar(path: Path, member: str) -> bytes:
    with path.open("rb") as stream:
        _, header_size, _, size = struct.unpack("<4I", stream.read(16))
        if not 0 < size <= 32_000_000:
            raise InstallationError("DSH_ARCHIVE_INVALID")
        node = json.loads(stream.read(size))
        for part in member.split("/"):
            node = node["files"][part]
        if node.get("unpacked") or "link" in node or not 0 <= int(node["size"]) < 8_000_000:
            raise InstallationError("DSH_ARCHIVE_INVALID")
        stream.seek(8 + header_size + int(node["offset"]))
        data = stream.read(int(node["size"]))
        if len(data) != int(node["size"]):
            raise InstallationError("DSH_ARCHIVE_INVALID")
        return data
