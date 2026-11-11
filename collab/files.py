"""Portable relative-path validation and link/hardlink rejection."""
from __future__ import annotations

import functools
import os
from pathlib import Path
import re
import stat

from .common import CollabError


_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
_DEVICES.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")


def _fail(code, message):
    raise CollabError(code, message)


def _io_errors(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OSError as exc:
            raise CollabError("IO_ERROR", f"{function.__name__}: {exc}") from exc
    return wrapped


def validate_rel(value: str) -> str:
    """Normalize './' and repeated '/', rejecting traversal and Windows aliases."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        _fail("INVALID_PATH", "Expected a nonempty relative path")
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail("INVALID_PATH", f"Backslash/control character in path: {value!r}")
    parts = []
    for part in value.split("/"):
        if part == "..":
            _fail("INVALID_PATH", "Parent traversal is forbidden")
        if part in ("", "."):
            continue
        if part.endswith((".", " ")) or any(c in '<>:"|?*' for c in part):
            _fail("INVALID_PATH", f"Nonportable path component: {part!r}")
        if part.split(".", 1)[0].rstrip(" .").upper() in _DEVICES:
            _fail("INVALID_PATH", f"Windows device name: {part!r}")
        parts.append(part)
    if not parts:
        _fail("INVALID_PATH", "A file or subdirectory path is required")
    return "/".join(parts)


def _check_node(path, info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
        _fail("UNSAFE_PATH", f"Link/reparse point: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        _fail("UNSAFE_PATH", f"Not an ordinary file/directory: {path}")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        _fail("UNSAFE_PATH", f"Hard-linked file: {path}")


def _root(root):
    root = Path(root)
    info = root.lstat()
    _check_node(root, info)
    if not stat.S_ISDIR(info.st_mode):
        _fail("INVALID_PATH", f"Root is not a directory: {root}")
    return root


@_io_errors
def safe_path(root: Path, rel: str) -> Path:
    """Validate each existing component, including exact directory spelling."""
    parts = validate_rel(rel).split("/")
    current = _root(root)
    for index, part in enumerate(parts):
        with os.scandir(current) as entries:
            matches = [entry.name for entry in entries if entry.name.casefold() == part.casefold()]
        if not matches:
            try:
                (current / part).lstat()
            except FileNotFoundError:
                pass
            else:
                _fail("UNSAFE_PATH", f"Filesystem alias: {current / part}")
            return current.joinpath(*parts[index:])
        if matches != [part]:
            _fail("UNSAFE_PATH", f"Case alias/collision: {current / part}")
        current = current / part
        info = current.lstat()
        _check_node(current, info)
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _fail("UNSAFE_PATH", f"File used as a parent directory: {current}")
    return current


_SECRET = re.compile(r"(^|[._-])(secrets?|credentials?|tokens?|api[_-]?keys?|private[_-]?keys?|access[_-]?tokens?|refresh[_-]?tokens?)([._-]|$)", re.I)
_KEY_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}


def clean_rel(value):
    return validate_rel(value)


def secret_path(rel):
    parts = rel.lower().split("/")
    for part in parts:
        suffix = Path(part).suffix
        if (part == ".env" or part.startswith(".env.") or suffix in _KEY_SUFFIXES
                or part.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))
                or _SECRET.search(part)):
            return True
    return False
