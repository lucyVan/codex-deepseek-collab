"""Six-tool MCP schema and direct Controller dispatch."""
from __future__ import annotations

from jsonschema import Draft202012Validator, ValidationError

from .common import CollabError


MAX_OUTPUT = 48 * 1024
ID = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"^[A-Za-z0-9_-]+$"}
REQUEST_ID = {**ID, "description": "Caller-stable idempotency key; reuse only for identical input."}
TEXT = {"type": "string", "minLength": 1, "maxLength": 16384, "pattern": r"\S"}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 100}
PATH = {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": r"\S"}
PATHS = {"type": "array", "items": PATH, "maxItems": 100, "uniqueItems": True}
EXCLUDES = {"type": "array", "items": PATH, "maxItems": 100, "uniqueItems": True}
VERSION = {"type": "integer", "minimum": 1}


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


SCHEMAS = {
    "collab_status": object_schema({}),
    "collab_submit": object_schema({
        "request_id": REQUEST_ID, "root": PATH, "goal": TEXT,
        "execution_workspace": {"type": "string", "minLength": 1},
        "read_inputs": PATHS, "write_paths": PATHS, "exclude_paths": EXCLUDES,
        "constraints": STRINGS, "acceptance": {**STRINGS, "minItems": 1},
        "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
        "dry_run": {"type": "boolean", "default": False},
    }, ("request_id", "goal", "acceptance")),
    "collab_get": object_schema({
        "id": ID, "view": {"enum": ["result", "summary", "diff", "history"]},
        "after_row": {"type": "integer", "minimum": 0},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "current": {"type": "boolean", "default": False},
    }),
    "collab_wait": object_schema({
        "id": ID, "after_version": {"type": "integer", "minimum": 0},
        "max_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
    }, ("id", "after_version")),
    "collab_resume": object_schema({
        "request_id": REQUEST_ID, "id": ID, "expected_version": VERSION,
        "instructions": TEXT,
    }, ("request_id", "id", "expected_version")),
    "collab_cancel": object_schema({
        "request_id": REQUEST_ID, "id": ID, "expected_version": VERSION,
    }, ("request_id", "id", "expected_version")),
    "collab_review": object_schema({
        "request_id": REQUEST_ID, "id": ID, "expected_version": VERSION,
        "conclusion": {"enum": ["ACCEPTED", "REJECTED"]},
        "summary": TEXT, "checks": {**STRINGS, "minItems": 1},
    }, ("request_id", "id", "expected_version", "conclusion", "summary", "checks")),
}

DESCRIPTIONS = {
    "collab_status": "Read current service and DeepSeek pairing. No model request or lifecycle action.",
    "collab_submit": "Validate and submit one DeepSeek desktop task. Codex supplies the current execution_workspace when available; otherwise the service default is used. Task root can be an existing git root or a directory that is safely initialized with a baseline commit. dry_run only reports initialization and never writes .git. Execution uses an isolated worktree and dry_run returns the full spec. DELIVERED is not Codex acceptance. Requires a stable request_id.",
    "collab_get": "List runs or read one run: summary (small, default for checks), bounded diff, history, or full result. Does not execute or accept work.",
    "collab_wait": "Wait up to 60 seconds for run.version to exceed after_version. Returns a small run head (id/version/state/summary); use collab_get for details. A timeout only means no newer version; keep waiting and never resubmit.",
    "collab_resume": "Resume a PAUSED max-tokens run using its original DeepSeek Session and worktree. Uses the original total deadline, a new prompt request ID, and events after the previous cursor. Does not create a new task or accept partial work.",
    "collab_cancel": "Request cancellation of a queued, running, or paused task using a stable request_id and expected run version. ACK is not a terminal state.",
    "collab_review": "Codex independently accepts a DELIVERED run or rejects a DELIVERED/PAUSED run. Returns a small run head plus the review record. Partial paused work cannot be accepted.",
}
READ_ONLY = {"collab_status", "collab_get", "collab_wait"}
VALIDATORS = {name: Draft202012Validator(schema) for name, schema in SCHEMAS.items()}


class Gateway:
    def __init__(self, controller):
        self.controller = controller

    def handle(self, name, arguments):
        if name not in SCHEMAS:
            return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "request_id": None}}
        arguments = arguments or {}
        request_id = arguments.get("request_id") if isinstance(arguments, dict) else None
        try:
            VALIDATORS[name].validate(arguments)
            if name == "collab_status":
                result = self.controller.status()
            elif name == "collab_submit":
                result = self.controller.submit(arguments)
            elif name == "collab_get":
                result = self.controller.get(arguments)
            elif name == "collab_wait":
                result = self.controller.wait(arguments)
            elif name == "collab_resume":
                result = self.controller.resume(arguments)
            elif name == "collab_cancel":
                result = self.controller.cancel(arguments)
            else:
                result = self.controller.review(arguments)
            return {"ok": True, "result": result, "request_id": request_id}
        except CollabError as error:
            return {"ok": False, "error": {"code": error.code, "request_id": request_id}}
        except ValidationError as error:
            path = ".".join(str(part) for part in error.absolute_path)
            return {"ok": False, "error": {"code": "INVALID_INPUT", "request_id": request_id,
                                           "field": path or None,
                                           "message": error.message[:300]}}
        except (ValueError, TypeError, KeyError):
            return {"ok": False, "error": {"code": "INVALID_INPUT", "request_id": request_id}}
        except Exception:
            code = "SERVICE_UNAVAILABLE" if name in READ_ONLY else "REQUEST_OUTCOME_UNKNOWN"
            return {"ok": False, "error": {"code": code, "request_id": request_id}}
