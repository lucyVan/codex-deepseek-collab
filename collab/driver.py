"""Isolated native driver entrypoint; only the service writes run state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading

from .common import CollabError, read_json, write_json
from .desktop_pairing import DesktopPairing
from .desktop_runtime import run_desktop
from .runtime_transport import scrub


def prompt_for(spec):
    def section(name, values):
        return name + ":\n" + ("\n".join(f"- {value}" for value in values) if values else "- (none)")
    return f"""You are the DeepSeek execution worker for one authorized task. Work independently: inspect, implement, run relevant checks, fix issues, and rerun them. Return a concise evidence-backed delivery, not an acceptance claim.

Task root: {spec['root']}
The App workspace may be a parent directory. Treat every listed path as relative to the task root. Read applicable AGENTS.md files.

Goal:
{spec['goal']}

{section('Authorized read inputs', spec['read_inputs'])}

{section('Authorized write paths', spec['write_paths'])}

{section('Constraints', spec['constraints'])}

{section('Acceptance criteria supplied by the controller', spec['acceptance'])}

Rules:
- Modify only the authorized write paths. Do not edit tests or configuration to hide failures.
- Do not create subagents; the collaboration worker preset has no delegation.
- Do not read credentials, private service state, cookies, launch URLs, or unrelated task history.
- Preserve actual command outcomes and distinguish implementation from verification.
- The controller independently computes file changes and Codex makes the final acceptance decision.
"""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.state_root.resolve()
    run_dir = root / "runs" / args.run_id
    execution_spec = run_dir / "execution-spec.json"
    spec = read_json(execution_spec if execution_spec.is_file() else run_dir / "spec.json")
    if Path(spec["workspace"]).resolve() != args.workspace.resolve():
        raise CollabError("WORKSPACE_MISMATCH", "driver workspace differs from the frozen spec")
    cancel = threading.Event()
    done = threading.Event()

    def watch_cancel():
        while not done.wait(0.2):
            if (run_dir / "cancel.requested").exists():
                cancel.set()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    event_lock = threading.Lock()

    def emit(kind, value):
        line = scrub(json.dumps({"kind": kind, "value": value}, ensure_ascii=False))
        with event_lock:
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        if kind == "desktop.session.created":
            write_json(run_dir / "session.json", {
                "session_id": value.get("session_id"), "title": value.get("title"),
            })

    run = {
        "id": args.run_id, "task_id": args.run_id,
        "root": spec["root"], "workspace": spec["workspace"],
        "agent_preset": spec["agent_preset"],
        "preset_sha256": spec.get("preset_sha256"),
        "timeout_seconds": spec["timeout_seconds"],
    }
    desktop = DesktopPairing(root)
    try:
        result = run_desktop(desktop, run, prompt_for(spec), run_dir / "logs", cancel, emit)
    except CollabError as error:
        result = {"status": "FAILED", "error": error.code, "outcome_reason": error.code}
    except Exception:
        result = {"status": "FAILED", "error": "DRIVER_FAILURE", "outcome_reason": "DRIVER_FAILURE"}
    finally:
        done.set()
        watcher.join(timeout=2)
        desktop.close()
    write_json(run_dir / "delivery.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
