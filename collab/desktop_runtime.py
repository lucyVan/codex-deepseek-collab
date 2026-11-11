"""R3 service-owned desktop Session executor.

The desktop Host is operator-owned. Completion is based on the target Session
becoming idle plus a correlated turn/end event, never process exit or Job
cleanup belonging to another runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .common import CollabError
from .desktop_connection import DesktopConnectionError, DesktopRPCError
from .desktop_pairing import DesktopPairing
from .limits import MAX_PROMPT_UTF8_BYTES


WAIT_CHUNK_MS = 30000
WAIT_TRANSPORT_RETRY_SECONDS = 0.25


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def _terminal(events):
    if not isinstance(events, dict) or not isinstance(events.get("records"), list):
        return None
    for record in reversed(events["records"]):
        event = record.get("event") if isinstance(record, dict) else None
        if not isinstance(event, dict) or event.get("type") != "turn/end":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        kind = reason.get("kind")
        if isinstance(kind, str):
            return {
                "kind": kind,
                "seq": event.get("seq") if type(event.get("seq")) is int else None,
                "error_code": reason.get("error", {}).get("code")
                if isinstance(reason.get("error"), dict) and isinstance(reason["error"].get("code"), str) else None,
            }
    return None


def _outcome(terminal, wait_reason, cancelled):
    if terminal is not None:
        kind = terminal["kind"]
        if kind == "completed":
            return "EXECUTION_COMPLETED", "COMPLETED", None
        if kind in ("interrupted", "cancelled", "aborted"):
            return "CANCELLED", "CANCELLED", None
        if kind in ("failed", "error"):
            return "FAILED", "UPSTREAM_FAILED", None
        if kind == "max-tokens":
            return "OUTCOME_UNKNOWN", "MAX_OUTPUT_TOKENS", True
        return "OUTCOME_UNKNOWN", "UNRECOGNIZED_TERMINAL", None
    if cancelled:
        return "OUTCOME_UNKNOWN", "CANCEL_UNCONFIRMED", None
    if wait_reason == "input-required":
        return "FAILED", "INPUT_REQUIRED", None
    return "OUTCOME_UNKNOWN", "MISSING_TERMINAL", None


def _message_summary(data):
    content = data.get("content") if isinstance(data, dict) else None
    texts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
    text = "\n".join(texts)
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _token_total(usage):
    if not isinstance(usage, dict):
        return None
    values = [usage.get(name) for name in (
        "uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")]
    if not all(type(value) is int and value >= 0 for value in values):
        return None
    return sum(values)


def analyze_session_events(events, request_id=None):
    """Return prompt/turn evidence without retaining user message content."""
    analysis = {
        "last_seq": None,
        "controller_prompt_seqs": [],
        "manual_prompt_seqs": [],
        "manual_prompts": [],
        "turn_starts": [],
        "turn_ends": [],
        "latest_terminal": None,
    }
    if not isinstance(events, dict) or not isinstance(events.get("records"), list):
        return analysis
    current_turn = None
    for record in events["records"]:
        event = record.get("event") if isinstance(record, dict) else None
        if not isinstance(event, dict):
            continue
        seq = event.get("seq") if type(event.get("seq")) is int else None
        if seq is not None:
            analysis["last_seq"] = seq if analysis["last_seq"] is None else max(analysis["last_seq"], seq)
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "turn/start":
            current_turn = data.get("turn") if type(data.get("turn")) is int else current_turn
            analysis["turn_starts"].append({"seq": seq, "turn": current_turn})
        elif kind == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            terminal = {
                "seq": seq,
                "turn": data.get("turn") if type(data.get("turn")) is int else current_turn,
                "kind": reason.get("kind") if isinstance(reason.get("kind"), str) else None,
                "error_code": reason.get("error", {}).get("code")
                if isinstance(reason.get("error"), dict) and isinstance(reason["error"].get("code"), str) else None,
            }
            analysis["turn_ends"].append(terminal)
            analysis["latest_terminal"] = terminal
        elif kind == "user/message":
            source = data.get("source") if isinstance(data.get("source"), dict) else {}
            if source.get("kind") != "user":
                continue
            rpc_id = source.get("rpcId") if isinstance(source.get("rpcId"), str) else None
            if request_id is not None and rpc_id == request_id:
                analysis["controller_prompt_seqs"].append(seq)
                continue
            summary = _message_summary(data)
            analysis["manual_prompt_seqs"].append(seq)
            analysis["manual_prompts"].append({
                "seq": seq,
                "turn": current_turn,
                "rpc_id": rpc_id,
                **summary,
            })
    return analysis


def session_stopped(state):
    """Completion invariant: idle plus empty queue and pending-input evidence."""
    if not isinstance(state, dict) or state.get("running") is not False:
        return False
    queue, pending = state.get("queue"), state.get("pendingInput")
    return isinstance(queue, list) and not queue and isinstance(pending, list) and not pending


def _state_summary(state):
    if not isinstance(state, dict):
        return None
    return {
        "running": state.get("running") is True,
        "queue_len": len(state.get("queue") or []),
        "pending_len": len(state.get("pendingInput") or []),
        "cursor": state.get("cursor") if type(state.get("cursor")) is int else None,
    }


def _events_window(events):
    records = events.get("records") if isinstance(events, dict) else None
    seqs = []
    if isinstance(records, list):
        for record in records:
            event = record.get("event") if isinstance(record, dict) else None
            seq = event.get("seq") if isinstance(event, dict) else None
            if type(seq) is int:
                seqs.append(seq)
    return {
        "cursor": events.get("cursor") if isinstance(events, dict) and type(events.get("cursor")) is int else None,
        "first_seq": min(seqs) if seqs else None,
        "last_seq": max(seqs) if seqs else None,
        "has_more": events.get("hasMore") if isinstance(events, dict) and isinstance(events.get("hasMore"), bool) else None,
        "record_count": len(records) if isinstance(records, list) else None,
    }


def _best_effort_unknown(desktop, result, submission):
    """After a sent-but-unconfirmed prompt, try to observe/cancel the known session."""
    session_id = result.get("session_id")
    if submission == "NOT_SENT" or not session_id:
        return
    try:
        ack = desktop.cancel_task(session_id)
        result["cancel_acknowledged"] = isinstance(ack, dict) and ack.get("accepted") is True
    except Exception:
        pass
    try:
        result["post_unknown_state"] = _state_summary(desktop.task_state(session_id))
        terminal = _terminal(desktop.task_events(session_id))
        result["post_unknown_terminal"] = terminal["kind"] if terminal else None
    except Exception:
        pass


def run_desktop(desktop: DesktopPairing, run: dict, prompt: str, logdir: Path,
                cancel, emit) -> dict:
    """Run one official Session and return only execution-relevant evidence."""
    started = time.monotonic()
    result = {
        "status": "FAILED",
        "session_id": None,
        "session_title": None,
        "agent_preset": run.get("agent_preset", "standard"),
        "preset_sha256": None,
        "permission_preset": None,
        "prompt_request_id": None,
        "terminal_kind": None,
        "terminal_seq": None,
        "session_idle": False,
        "manual_prompt_count": 0,
        "controller_prompt_seen": False,
        "cancel_requested": False,
        "cancel_acknowledged": False,
        "token_total": None,
        "started_at": _stamp(),
        "ended_at": None,
        "duration_seconds": None,
        "error": None,
        "outcome_reason": None,
        "partial": None,
        "prompt_sha256": None,
        "prompt_bytes": None,
        "completion_verified": False,
        "final_state": None,
        "events_window": None,
        "post_unknown_state": None,
        "post_unknown_terminal": None,
    }
    root = Path(run["root"]).resolve()
    logdir = Path(logdir).resolve()
    workspace = Path(run["workspace"]).resolve()
    deadline = started + run.get("timeout_seconds", 1800)
    submission = "NOT_SENT"
    claimed = False
    cancel_sent = False
    terminal = None
    wait_reason = None
    state = None
    cursor = -1
    transport_retries = 0
    try:
        if not root.is_dir() or not workspace.is_dir() or not logdir.is_dir():
            raise CollabError("RUN_DIRECTORY_MISSING", "desktop run directories are unavailable")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_UTF8_BYTES:
            raise CollabError("INVALID_PROMPT", "desktop prompt is empty or exceeds the controller byte limit")
        _save(logdir / "native-attempt.json", {
            "run_id": run["id"], "started_at": result["started_at"],
        })
        prompt_bytes = prompt.encode("utf-8")
        result["prompt_bytes"] = len(prompt_bytes)
        result["prompt_sha256"] = hashlib.sha256(prompt_bytes).hexdigest()
        claimed = True
        title = f'collab {run["task_id"][:8]} / {run["id"][:8]}'
        created = desktop.create_task_session(
            workspace, title, agent_preset=result["agent_preset"],
            preset_sha256=run.get("preset_sha256"))
        configuration = created.get("execution_configuration", {})
        result.update(
            session_id=created["session_id"], session_title=created.get("title"),
            preset_sha256=configuration.get("preset_sha256"),
            permission_preset=configuration.get("permission_preset"),
        )
        session_id = created["session_id"]
        emit("desktop.session.created", {"session_id": session_id, "title": created.get("title")})
        state = desktop.task_state(session_id)
        if not isinstance(state, dict) or state.get("running") is not False:
            raise CollabError("DESKTOP_SESSION_BUSY", "new desktop Session is not idle before prompt")
        cursor = state.get("cursor") if type(state.get("cursor")) is int else -1
        if cancel.is_set():
            result.update(status="CANCELLED", outcome_reason="CANCEL_REQUESTED",
                          error="CANCEL_REQUESTED", session_idle=True)
            return result
        request_id = "desktop-" + run["id"]
        result["prompt_request_id"] = request_id
        submission = "SENT_RECEIPT_PENDING"
        emit("prompt.sending", {"request_id": request_id})
        receipt = desktop.prompt_task(session_id, prompt, request_id)
        if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
            raise CollabError("DESKTOP_PROMPT_REJECTED", "desktop prompt was not acknowledged")
        submission = "ACKED"
        emit("prompt.accepted", {"request_id": request_id})

        while terminal is None:
            remaining = deadline - time.monotonic()
            timed_out = remaining <= 0
            if (cancel.is_set() or timed_out) and not cancel_sent:
                try:
                    ack = desktop.cancel_task(session_id)
                    result["cancel_acknowledged"] = isinstance(ack, dict) and ack.get("accepted") is True
                except (DesktopConnectionError, DesktopRPCError):
                    pass
                result["cancel_requested"] = True
                cancel_sent = True
                emit("desktop.cancel.requested", {"accepted": result["cancel_acknowledged"]})
            if timed_out:
                if cancel.is_set():
                    result.update(status="OUTCOME_UNKNOWN", outcome_reason="CANCEL_UNCONFIRMED",
                                  error="CANCEL_UNCONFIRMED")
                else:
                    result.update(status="FAILED", outcome_reason="TIME_BUDGET_EXCEEDED",
                                  error="TIME_BUDGET_EXCEEDED", partial=True)
                break
            timeout_ms = max(1, min(WAIT_CHUNK_MS, math.ceil(remaining * 1000)))
            try:
                observed = desktop.wait_task(session_id, timeout_ms=timeout_ms, after_seq=cursor)
            except DesktopRPCError as error:
                if error.remote_code == "session/wait-timeout":
                    continue
                raise
            except DesktopConnectionError as error:
                if error.code == "TRANSPORT_ERROR":
                    transport_retries += 1
                    emit("runtime.transport_retry", {"method": "desktop.wait", "retry": transport_retries})
                    time.sleep(min(WAIT_TRANSPORT_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
                    continue
                raise
            if not isinstance(observed, dict) or not isinstance(observed.get("state"), dict):
                raise CollabError("INVALID_DESKTOP_RESPONSE", "desktop wait response lacks state")
            state = observed["state"]
            wait_reason = observed.get("reason")
            result["final_state"] = _state_summary(state)
            result["session_idle"] = state.get("running") is False
            if type(state.get("cursor")) is int:
                cursor = state["cursor"]
            if state.get("running") is False:
                events = desktop.task_events(session_id, through_seq=state.get("cursor"))
                result["events_window"] = _events_window(events)
                analysis = analyze_session_events(events, request_id)
                result["manual_prompt_count"] = len(analysis["manual_prompt_seqs"])
                result["controller_prompt_seen"] = bool(analysis["controller_prompt_seqs"])
                terminal = _terminal(events)
                if terminal is not None:
                    result["terminal_kind"] = terminal["kind"]
                    result["terminal_seq"] = terminal["seq"]
                    result["completion_verified"] = session_stopped(state)
                    if not result["completion_verified"]:
                        result.update(status="OUTCOME_UNKNOWN", partial=None,
                                      outcome_reason="SESSION_NOT_STOPPED_AT_TERMINAL",
                                      error="DESKTOP_SESSION_NOT_STOPPED")
                    break
                if wait_reason in ("idle", "input-required"):
                    break

        if result["outcome_reason"] is None:
            status, reason, partial = _outcome(terminal, wait_reason, cancel_sent)
            if result["manual_prompt_count"]:
                status, reason, partial = "OUTCOME_UNKNOWN", "MANUAL_TAKEOVER_OBSERVED", None
            result.update(status=status, outcome_reason=reason, partial=partial)
            if status == "FAILED" and reason == "INPUT_REQUIRED":
                result["error"] = "DESKTOP_INPUT_REQUIRED"
            elif status == "OUTCOME_UNKNOWN" and reason == "MISSING_TERMINAL":
                result["error"] = "DESKTOP_TERMINAL_MISSING"
            elif status == "OUTCOME_UNKNOWN" and reason == "MANUAL_TAKEOVER_OBSERVED":
                result["error"] = "DESKTOP_MANUAL_TAKEOVER"
    except CollabError as error:
        result["error"] = error.code
        result["outcome_reason"] = error.code
        result["status"] = "FAILED" if submission == "NOT_SENT" else "OUTCOME_UNKNOWN"

        _best_effort_unknown(desktop, result, submission)
    except (DesktopConnectionError, DesktopRPCError) as error:
        result["error"] = getattr(error, "remote_code", None) or getattr(error, "code", None) or "DESKTOP_TRANSPORT_ERROR"
        result["outcome_reason"] = result["error"]
        result["status"] = "FAILED" if submission == "NOT_SENT" else "OUTCOME_UNKNOWN"

        _best_effort_unknown(desktop, result, submission)
    except Exception:
        result["error"] = "DESKTOP_ADAPTER_FAILURE"
        result["outcome_reason"] = "DESKTOP_ADAPTER_FAILURE"
        result["status"] = "FAILED" if submission == "NOT_SENT" else "OUTCOME_UNKNOWN"

        _best_effort_unknown(desktop, result, submission)
    finally:
        if result["session_id"]:
            try:
                metadata = desktop.task_metadata(result["session_id"])
                result["token_total"] = _token_total(metadata.get("tokenUsage"))
            except Exception:
                result["token_total"] = None
        result["ended_at"] = _stamp()
        result["duration_seconds"] = round(max(0.0, time.monotonic() - started), 3)
        if claimed:
            try:
                emit("runtime.finished", {
                    "status": result["status"], "terminal_kind": result["terminal_kind"],
                    "session_idle": result["session_idle"],
                })
                _save(logdir / "desktop-result.json", result)
            except Exception:
                result.update(status="OUTCOME_UNKNOWN", error="RESULT_PERSIST_FAILED",
                              outcome_reason="RESULT_PERSIST_FAILED")
    return result
