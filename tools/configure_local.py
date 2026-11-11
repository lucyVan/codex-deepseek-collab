"""Generate platform-specific local MCP and marketplace configuration; never start a service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

TOOLS = [
    "collab_status", "collab_submit", "collab_get",
    "collab_wait", "collab_cancel", "collab_review",
]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configuration(workspace_root: Path, platform: str, execution_workspace: Path | None = None):
    if platform not in ("darwin", "win32"):
        raise ValueError("supported configuration platforms are darwin and win32")
    workspace_root = workspace_root.resolve()
    execution_workspace = (execution_workspace or workspace_root).resolve()
    windows = platform == "win32"
    python = PROJECT_ROOT / (".venv/Scripts/python.exe" if windows else ".venv/bin/python")
    state_name = "windows" if windows else "macos"
    state = workspace_root / "runtime" / state_name / "state"
    connection = {
        "command": str(python),
        "args": ["-I", "-B", str(PROJECT_ROOT / "tools/mcp_collab.py"),
                 "--state-root", str(state), "--execution-workspace", str(execution_workspace)],
        "cwd": str(execution_workspace),
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 120,
        "enabled_tools": TOOLS,
    }
    mcp = {"mcpServers": {"codex_deepseek_collab": connection}}
    marketplace = {
        "name": "collab-local",
        "plugins": [{
            "name": "codex-deepseek-collab",
            "source": {"source": "local", "path": "./plugin"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
            "category": "Developer Tools",
            "interface": {"displayName": "本地协作插件"},
        }],
    }
    return mcp, marketplace


def main(expected_platform: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=PROJECT_ROOT.parent)
    parser.add_argument("--execution-workspace", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    platform = expected_platform or sys.platform
    if sys.platform != platform or platform not in ("darwin", "win32"):
        parser.error("this configuration entry must run on its target macOS or Windows host")
    workspace_root = args.workspace_root.resolve()
    if not workspace_root.is_dir():
        parser.error("Workspace root is not an existing directory")
    execution_workspace = (args.execution_workspace or workspace_root).resolve()
    if not execution_workspace.is_dir():
        parser.error("Execution workspace is not an existing directory")
    mcp, marketplace = configuration(workspace_root, platform, execution_workspace)
    targets = ((PROJECT_ROOT / "plugin/.mcp.json", mcp),
               (PROJECT_ROOT / ".agents/plugins/marketplace.json", marketplace))
    changed = []
    for path, expected in targets:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        changed.append({"path": str(path), "changed": current != expected})
        if not args.check and current != expected:
            atomic_json(path, expected)
    if args.check and any(row["changed"] for row in changed):
        print(json.dumps({"ok": False, "files": changed}, ensure_ascii=False))
        return 1
    python = PROJECT_ROOT / (".venv/Scripts/python.exe" if platform == "win32" else ".venv/bin/python")
    if not python.is_file():
        parser.error("create the Python 3.12 .venv before configuring the plugin")
    print(json.dumps({"ok": True, "platform": platform, "workspace_root": str(workspace_root),
                      "execution_workspace": str(execution_workspace),
                      "files": changed, "tool_count": len(TOOLS)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
