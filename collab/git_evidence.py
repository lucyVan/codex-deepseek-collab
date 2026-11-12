"""Worktree-based evidence for tasks rooted at a git repository root.

The main worktree is never modified during a task. A detached worktree at HEAD
is created under a fixed private cache directory and removed after review.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .common import CollabError, write_bytes, write_json
from .files import safe_path, secret_path


TIMEOUT = 30
MAX_DIFF_LINES = 400
MAX_BASELINE_FILE_BYTES = 100 * 1024 * 1024
_SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
              ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache"}


def skipped_dir(rel, excludes=()):
    if any(part.lower() in _SKIP_DIRS for part in rel.split("/")):
        return True
    for exclude in excludes:
        if rel == exclude or rel.startswith(exclude.rstrip("/") + "/"):
            return True
    return False


def _patch_pathspecs(excludes):
    """Use the same exclusions for the apply patch and the visible manifest."""
    specs = ["."]
    for name in sorted(_SKIP_DIRS):
        specs.extend((f":(exclude){name}/**", f":(exclude)**/{name}/**"))
    for exclude in excludes:
        normalized = exclude.rstrip("/")
        specs.extend((f":(exclude){normalized}", f":(exclude){normalized}/**"))
    return specs


def in_scope(rel, write_paths):
    return any(rel == owned or rel.startswith(owned.rstrip("/") + "/") for owned in write_paths)


def available():
    return shutil.which("git") is not None


def _git(root, *args, binary=False):
    try:
        options = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              timeout=TIMEOUT, stdin=subprocess.DEVNULL, **options)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollabError("GIT_UNAVAILABLE", "git command is unavailable") from error


def _git_checked(root, code, message, *args, binary=False):
    result = _git(root, *args, binary=binary)
    if result.returncode != 0:
        raise CollabError(code, message)
    return result


def _stage_all(worktree):
    _git_checked(worktree, "GIT_WORKTREE_FAILED", "cannot stage worktree changes", "add", "-A", ".")


def _render_patch(worktree, head, excludes):
    result = _git_checked(worktree, "GIT_WORKTREE_FAILED", "cannot render worktree patch",
                          "diff", "--cached", "--binary", head, "--",
                          *_patch_pathspecs(excludes), binary=True)
    return result.stdout


def _toplevel(root):
    if not available():
        return None
    result = _git(Path(root).resolve(), "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None


def repo_root(root):
    root = Path(root).resolve()
    top = _toplevel(root)
    return top if top == root else None


def initialize_repository(root, excludes=()):
    """Initialize a repository with a safe baseline so HEAD contains the current task files."""
    root = Path(root).resolve()
    if repo_root(root) == root:
        return {"git_initialized": False, "baseline_head": None,
                "baseline_files": 0, "baseline_excluded": []}
    if _toplevel(root) is not None:
        raise CollabError("GIT_EVIDENCE_REQUIRED",
                          "task root is inside an existing repository; use the repository root instead")
    files = []
    excluded = []
    for current, directories, names in os.walk(root, followlinks=False):
        current = Path(current)
        relative_dir = current.relative_to(root).as_posix()
        directories[:] = sorted(name for name in directories
                                if not skipped_dir(f"{relative_dir}/{name}" if relative_dir != "." else name,
                                                   excludes))
        for name in sorted(names):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if skipped_dir(rel, excludes):
                continue
            if secret_path(rel):
                excluded.append(rel)
                continue
            safe_path(root, rel)
            size = path.stat().st_size
            if size > MAX_BASELINE_FILE_BYTES:
                raise CollabError("GIT_BASELINE_TOO_LARGE",
                                  f"file exceeds the 100 MiB baseline limit: {rel}")
            files.append(rel)
    _git_checked(root, "GIT_BASELINE_FAILED", "cannot initialize the task repository", "init")
    for offset in range(0, len(files), 100):
        _git_checked(root, "GIT_BASELINE_FAILED", "cannot stage the baseline files",
                     "add", "--", *files[offset:offset + 100])
    _git_checked(root, "GIT_BASELINE_FAILED", "cannot create the baseline commit",
                 "-c", "user.name=codex-deepseek-collab",
                 "-c", "user.email=codex-deepseek-collab@local",
                 "commit", "--allow-empty", "-qm", "Baseline before DeepSeek collaboration")
    head = _git_checked(root, "GIT_BASELINE_FAILED", "cannot read the baseline HEAD",
                        "rev-parse", "HEAD").stdout.strip()
    return {"git_initialized": True, "baseline_head": head,
            "baseline_files": len(files), "baseline_excluded": excluded[:100]}


def ensure_worktree_ignored(workspace):
    """Ensure <workspace>/.worktrees is ignored by its containing git repository.

    The workspace is the DSH execution workspace, so it may be the repository
    root or a subdirectory inside one.  Only .git/info/exclude is touched.
    """
    workspace = Path(workspace).resolve()
    base = workspace / ".worktrees"
    top = _toplevel(workspace)
    if top is None:
        return False
    try:
        relative = base.relative_to(top)
    except ValueError:
        return False
    probe = relative.as_posix().rstrip("/") + "/"
    check = _git(top, "check-ignore", "-q", "--", probe)
    if check.returncode == 0:
        return True
    exclude = top / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        pattern = "/" + probe.rstrip("/") + "/"
        if pattern not in existing.splitlines():
            if existing and not existing.endswith("\n"):
                existing += "\n"
            exclude.write_text(existing + pattern + "\n", encoding="utf-8")
    except OSError as error:
        raise CollabError("WORKTREE_IGNORE_FAILED",
                          "cannot add .worktrees to .git/info/exclude") from error
    check = _git(top, "check-ignore", "-q", "--", probe)
    if check.returncode != 0:
        raise CollabError("WORKTREE_IGNORE_FAILED", ".worktrees is still not ignored")
    return True


def _hash(path):
    if not path.is_file():
        return None
    digest_value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest_value.update(block)
    return digest_value.hexdigest()


def _status_entries(root):
    result = _git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    if result.returncode != 0:
        raise CollabError("GIT_UNAVAILABLE", "git status failed")
    entries = []
    tokens = result.stdout.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        kind = token[0]
        if kind == "1":
            parts = token.split(" ", 8)
            entries.append((parts[8], parts[1]))
        elif kind == "2":
            parts = token.split(" ", 9)
            entries.append((parts[9], parts[1]))
            if index < len(tokens) and tokens[index]:
                index += 1  # original path of a rename
        elif kind == "?":
            entries.append((token[2:], "??"))
        elif kind == "!":
            entries.append((token[2:], "!!"))
    return entries


def _kind(entry):
    _path, xy = entry
    if xy == "??" or "A" in xy:
        return "create"
    if "D" in xy:
        return "delete"
    return "modify"


def worktree_path(run_id, workspace):
    """Resolve a run worktree strictly under <workspace>/.worktrees."""
    value = str(run_id)
    if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for char in value):
        raise CollabError("INVALID_WORKTREE_ID", "worktree id contains unsupported characters")
    workspace = Path(workspace).resolve()
    ensure_worktree_ignored(workspace)
    base = workspace / ".worktrees"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.parent != workspace:
        raise CollabError("INVALID_WORKTREE_PATH", "worktree base escapes the workspace")
    path = (base / value).resolve()
    if path.parent != base.resolve():
        raise CollabError("INVALID_WORKTREE_PATH", "worktree path escapes the workspace base")
    return path


def prepare_worktree(root, run_dir, run_id, workspace):
    """Create an isolated worktree at HEAD; return its base record."""
    root, run_dir = Path(root).resolve(), Path(run_dir).resolve()
    if repo_root(root) != root:
        raise CollabError("GIT_EVIDENCE_REQUIRED", "worktree evidence requires the repository root")
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CollabError("GIT_UNAVAILABLE", "git HEAD is unavailable")
    path = worktree_path(run_id, workspace)
    if path.exists():
        cleanup_path = path
        _git(root, "worktree", "remove", "--force", str(cleanup_path))
        if cleanup_path.exists():
            import shutil
            shutil.rmtree(cleanup_path, ignore_errors=True)
    result = _git(root, "worktree", "add", "--detach", str(path), "HEAD")
    if result.returncode != 0:
        raise CollabError("GIT_WORKTREE_FAILED", "cannot create git worktree")
    base = {"mode": "worktree", "main_root": str(root), "worktree": str(path),
            "worktree_base": str(path.parent), "head": head.stdout.strip(),
            "main_status": sorted([list(item) for item in _status_entries(root)])}
    write_json(run_dir / "worktree-base.json", base)
    return base


def read_worktree(run_dir):
    import json
    path = Path(run_dir) / "worktree-base.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def deliver_worktree(run_dir, write_paths, *, excludes=()):
    run_dir = Path(run_dir).resolve()
    base = read_worktree(run_dir)
    if base is None:
        raise CollabError("GIT_WORKTREE_FAILED", "worktree baseline is unavailable")
    worktree = Path(base["worktree"])
    _stage_all(worktree)
    entries = _status_entries(worktree)
    changes = []
    for entry in entries:
        path, xy = entry
        if skipped_dir(path, excludes):
            continue
        kind = _kind(entry)
        changes.append({"path": path, "kind": kind,
                        "in_scope": in_scope(path, write_paths),
                        "after": _hash(safe_path(worktree, path)), "git_status": xy})
    changes.sort(key=lambda item: item["path"])
    write_bytes(run_dir / "worktree.patch", _render_patch(worktree, "HEAD", excludes))
    main = Path(base["main_root"])
    main_head = _git_checked(main, "GIT_UNAVAILABLE", "main repository HEAD is unavailable",
                             "rev-parse", "HEAD").stdout.strip()
    main_entries = _status_entries(main)
    main_status = sorted([list(item) for item in main_entries])
    result = {
        "evidence_mode": "worktree", "root": base["main_root"],
        "main_root": base["main_root"], "worktree": base["worktree"],
        "head": base["head"], "before_digest": None, "after_digest": None,
        "patch_path": str(run_dir / "worktree.patch"),
        "exclude_paths": list(excludes),
        "excluded": sorted({entry[0] for entry in entries
                            if skipped_dir(entry[0], excludes)})[:20],
        "changes": changes,
        "out_of_scope": [item["path"] for item in changes if not item["in_scope"]],
        "main_head_changed": main_head != base["head"],
        "main_changed": (main_head != base["head"]
                         or main_status != base.get("main_status", [])),
        "main_changes": sorted({entry[0] for entry in main_entries})[:50],
    }
    write_json(run_dir / "delivery-manifest.json", result)
    return result


def worktree_change_diff(worktree, run_dir, change):
    worktree = Path(worktree).resolve()
    path = change["path"]
    if not worktree.is_dir():
        return {"path": path, "kind": change["kind"], "in_scope": change["in_scope"],
                "diff": None, "truncated": False, "reason": "WORKTREE_CLEANED_UP",
                "after_matches_current": None, "evidence_mode": "worktree"}
    base = read_worktree(run_dir) or {}
    base_head = base.get("head", "HEAD")
    result = _git(worktree, "diff", "--text", base_head, "--", path)
    if result.returncode not in (0, 1):
        raise CollabError("GIT_UNAVAILABLE", "git worktree diff failed")
    current = _hash(safe_path(worktree, path))
    lines = result.stdout.splitlines()
    return {
        "path": path, "kind": change["kind"], "in_scope": change["in_scope"],
        "diff": "\n".join(lines[:MAX_DIFF_LINES]) if lines else None,
        "truncated": len(lines) > MAX_DIFF_LINES,
        "reason": None if lines else "GIT_DIFF_EMPTY",
        "after_matches_current": current == change.get("after"),
        "evidence_mode": "worktree",
    }


def worktree_current_changes(run_dir, write_paths, *, excludes=()):
    import json
    run_dir = Path(run_dir).resolve()
    base = read_worktree(run_dir)
    if base is None:
        return {"available": False, "changes": [], "out_of_scope": [],
                "current_digest": None, "evidence_mode": "worktree"}
    worktree = Path(base["worktree"])
    if not worktree.is_dir():
        return {"available": False, "changes": [], "out_of_scope": [],
                "current_digest": None, "evidence_mode": "worktree"}
    try:
        delivery = json.loads((run_dir / "delivery-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "changes": [], "out_of_scope": [],
                "current_digest": None, "evidence_mode": "worktree"}
    _stage_all(worktree)
    old = {item["path"]: item for item in delivery.get("changes") or []}
    current = {}
    for entry in _status_entries(worktree):
        path = entry[0]
        if skipped_dir(path, excludes):
            continue
        current[path] = {"kind": _kind(entry), "after": _hash(safe_path(worktree, path))}
    changes = []
    for path in sorted(set(old) | set(current)):
        old_item, now = old.get(path), current.get(path)
        after = now["after"] if now else None
        if old_item is not None and old_item.get("after") == after:
            continue
        if old_item is not None:
            kind = "delete" if after is None else old_item.get("kind") or "modify"
        else:
            kind = now["kind"]
        changes.append({"path": path, "kind": kind, "after": after,
                        "in_scope": in_scope(path, write_paths)})
    return {"available": True, "changes": changes,
            "out_of_scope": [item["path"] for item in changes if not item["in_scope"]],
            "current_digest": None, "evidence_mode": "worktree"}


def _rebase_worktree(worktree, base, new_head, run_dir, excludes):
    """Integrate worktree changes onto new main HEAD, inside the worktree only."""
    worktree = Path(worktree)
    _stage_all(worktree)
    if _status_entries(worktree):
        commit = _git(worktree, "-c", "user.name=collab-worktree",
                      "-c", "user.email=collab-worktree@local",
                      "commit", "-qm", "collab worktree delivery")
        if commit.returncode != 0:
            raise CollabError("WORKTREE_REBASE_FAILED", "cannot snapshot worktree changes")
    result = _git(worktree, "rebase", new_head)
    if result.returncode != 0:
        conflict = _git(worktree, "diff", "--name-only", "--diff-filter=U")
        conflicts = [line for line in conflict.stdout.splitlines() if line]
        _git(worktree, "rebase", "--abort")
        write_json(Path(run_dir) / "worktree-conflicts.json",
                   {"head": new_head, "conflicts": conflicts})
        raise CollabError("WORKTREE_CONFLICT",
                          "worktree conflicts with the current main repository HEAD")
    write_bytes(Path(run_dir) / "worktree.patch", _render_patch(worktree, new_head, excludes))
    base["head"] = new_head
    base["main_status"] = sorted([list(item) for item in _status_entries(Path(base["main_root"]))])
    write_json(Path(run_dir) / "worktree-base.json", base)
    return base


def assert_worktree_unchanged(run_dir):
    """Fail closed if a delivered or paused worktree no longer matches its frozen patch."""
    import json
    run_dir = Path(run_dir).resolve()
    base = read_worktree(run_dir)
    if base is None:
        raise CollabError("GIT_WORKTREE_FAILED", "worktree baseline is unavailable")
    worktree = Path(base["worktree"])
    patch = run_dir / "worktree.patch"
    if not worktree.is_dir() or not patch.is_file():
        raise CollabError("GIT_WORKTREE_FAILED", "worktree or its patch is unavailable")
    try:
        delivery = json.loads((run_dir / "delivery-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        delivery = {}
    _stage_all(worktree)
    current_patch = _render_patch(worktree, base["head"], delivery.get("exclude_paths", []))
    if current_patch != patch.read_bytes():
        raise CollabError("WORKTREE_CHANGED_AFTER_DELIVERY",
                          "worktree changed after delivery; deliver and review it again")


def apply_worktree(run_dir, *, cleanup=True):
    """Validate and apply a worktree patch.

    If main HEAD advanced, rebase the worktree and refuse to apply until the
    caller re-reads the new diff and reviews it again (REVIEW_BASE_CHANGED).
    The operation is idempotent: a previously applied patch is detected by a
    reverse check, which lets a retried review complete after a failed store
    write.
    """
    import json
    run_dir = Path(run_dir).resolve()
    base = read_worktree(run_dir)
    if base is None:
        raise CollabError("GIT_WORKTREE_FAILED", "worktree baseline is unavailable")
    main = Path(base["main_root"])
    patch = run_dir / "worktree.patch"
    if not patch.is_file():
        raise CollabError("GIT_WORKTREE_FAILED", "worktree patch is unavailable")
    try:
        delivery = json.loads((run_dir / "delivery-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        delivery = {}
    excludes = delivery.get("exclude_paths", [])
    worktree = Path(base["worktree"])
    if worktree.is_dir():
        assert_worktree_unchanged(run_dir)
    head = _git(main, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise CollabError("GIT_UNAVAILABLE", "main repository HEAD is unavailable")
    current_head = head.stdout.strip()
    if current_head != base["head"]:
        old_head = base["head"]
        _rebase_worktree(worktree, base, current_head, run_dir, excludes)
        write_json(run_dir / "review-rebase.json", {
            "old_head": old_head, "new_head": current_head,
            "requires_review": True,
        })
        raise CollabError("REVIEW_BASE_CHANGED",
                          "main repository advanced; re-read the rebased diff and review again")
    changed_paths = {item["path"] for item in delivery.get("changes") or []}
    reverse = _git(main, "apply", "--reverse", "--check", "--binary", str(patch))
    if reverse.returncode == 0:
        if cleanup:
            cleanup_worktree(run_dir)
        return {"already_applied": True}
    if not worktree.is_dir():
        raise CollabError("GIT_WORKTREE_FAILED", "worktree is unavailable before apply")
    status_entries = _status_entries(main)
    dirty_paths = {item[0] for item in status_entries}
    overlap = sorted(changed_paths & dirty_paths)
    if overlap:
        write_json(run_dir / "worktree-main-conflicts.json",
                   {"changed_paths": sorted(changed_paths), "dirty_paths": sorted(dirty_paths),
                    "overlap": overlap})
        raise CollabError("WORKTREE_MAIN_DIRTY_CONFLICT",
                          "main repository has uncommitted changes in task paths")
    check = _git(main, "apply", "--check", "--binary", str(patch))
    if check.returncode != 0:
        raise CollabError("WORKTREE_APPLY_FAILED", "cannot apply worktree patch cleanly")
    result = _git(main, "apply", "--binary", str(patch))
    if result.returncode != 0:
        raise CollabError("WORKTREE_APPLY_FAILED", "cannot apply worktree patch")
    if cleanup:
        cleanup_worktree(run_dir)
    return {"already_applied": False}


def cleanup_orphan_worktrees(state_root, workspace):
    """Remove worktrees whose run is terminal or whose run directory no longer exists."""
    import shutil
    state_root, workspace = Path(state_root), Path(workspace).resolve()
    base_dir = workspace / ".worktrees"
    if not base_dir.is_dir():
        return 0
    live = {}
    terminal_states = {"ACCEPTED", "REJECTED", "FAILED", "CANCELLED"}
    for run_dir in sorted((state_root / "runs").glob("*")):
        base = read_worktree(run_dir)
        if base is not None:
            live[run_dir.name] = base
    removed = 0
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        owner = next((rid for rid, base in live.items()
                      if Path(base.get("worktree", "")).resolve() == child.resolve()), None)
        if owner is not None:
            run_dir = state_root / "runs" / owner
            base = live[owner]
            try:
                import sqlite3
                db = sqlite3.connect(state_root / "state.sqlite", timeout=2)
                row = db.execute("SELECT state FROM runs WHERE id=?", (owner,)).fetchone()
                db.close()
                state = row[0] if row else None
            except Exception:
                state = None
            if state not in terminal_states:
                continue
            cleanup_worktree(run_dir)
            removed += 1
            continue
        _git(workspace, "worktree", "remove", "--force", str(child))
        if child.exists():
            shutil.rmtree(child, ignore_errors=True)
        removed += 1
    try:
        _git(workspace, "worktree", "prune")
    except Exception:
        pass
    return removed


def cleanup_worktree(run_dir):
    base = read_worktree(Path(run_dir))
    if base is None:
        return
    path = Path(base.get("worktree", "")).resolve()
    base_dir = Path(base.get("worktree_base", "")).resolve()
    if path.parent != base_dir:
        return
    try:
        _git(Path(base["main_root"]), "worktree", "remove", "--force", str(path))
    except Exception:
        pass
    if path.exists():
        import shutil
        shutil.rmtree(path, ignore_errors=True)
