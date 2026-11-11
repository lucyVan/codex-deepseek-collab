"""Operator lifecycle CLI. Task submission and review are exposed through MCP."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .common import CollabError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = (PROJECT_ROOT.parent / "runtime" /
                 ("windows" if os.name == "nt" else "macos") / "state")
DEFAULT_WORKSPACE = PROJECT_ROOT.parent


def parser():
    p = argparse.ArgumentParser(description="Lightweight Codex × DeepSeek MCP collaboration service")
    p.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
    p.add_argument("--execution-workspace", type=Path, default=DEFAULT_WORKSPACE)
    commands = p.add_subparsers(dest="command", required=True)
    service = commands.add_parser("service").add_subparsers(dest="action", required=True)
    service.add_parser("start").add_argument("--native", action="store_true")
    for action in ("status", "stop"):
        service.add_parser(action)
    internal = service.add_parser("_serve", help=argparse.SUPPRESS)
    internal.add_argument("--instance", required=True)
    internal.add_argument("--native", action="store_true")
    desktop = commands.add_parser("desktop").add_subparsers(dest="action", required=True)
    desktop.add_parser("status")
    desktop.add_parser("pair")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root, workspace = args.state_root.resolve(), args.execution_workspace.resolve()
        if args.command == "service":
            from . import service
            if args.action == "_serve":
                service.serve(root, workspace, args.instance, native=args.native)
                return 0
            if args.action == "start":
                result = service.start(root, workspace, native=args.native)
            elif args.action == "status":
                from .client import status
                result = status(root)
            else:
                result = service.stop(root)
        else:
            from .desktop_pairing import DesktopPairing
            manager = DesktopPairing(root)
            try:
                result = manager.status() if args.action == "status" else manager.pair()
            finally:
                manager.close()
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 1 if result.get("stopped") is False or result.get("state") == "UNAVAILABLE" else 0
    except CollabError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
