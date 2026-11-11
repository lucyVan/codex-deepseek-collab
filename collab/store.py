"""Small persistent store for the single-writer MCP collaboration service."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4

from .common import CollabError, canonical, digest, file_lock, now


SCHEMA_VERSION = 1

class ServiceOwnership:
    """Cooperative single-service lock; it does not restrict arbitrary file writes."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = file_lock(self.root / "locks/service.lock", timeout=0)
        self._lock.__enter__()
        return self

    def __exit__(self, *args):
        self._lock.__exit__(*args)
        self._lock = None


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise CollabError("SCHEMA_VERSION", "unsupported lightweight state schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL,
                    spec TEXT NOT NULL, result TEXT, review TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, response TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_history(
                    run_id TEXT NOT NULL, version INTEGER NOT NULL,
                    state TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, version));
            """)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()

    @contextmanager
    def _db(self, *, immediate=False):
        db = sqlite3.connect(self.root / "state.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        if immediate:
            db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _run(row) -> dict:
        if row is None:
            raise CollabError("NOT_FOUND", "run not found")
        return {
            "id": row["id"], "version": row["version"], "state": row["state"],
            "spec": json.loads(row["spec"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "review": json.loads(row["review"]) if row["review"] else None,
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def _get_db(self, db, run_id):
        return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    @staticmethod
    def _history(db, run_id, version, state, note):
        db.execute("INSERT INTO run_history(run_id,version,state,note,created_at) VALUES(?,?,?,?,?)",
                   (run_id, version, state, note, now()))

    def get(self, run_id):
        with self._db() as db:
            return self._get_db(db, run_id)

    def list(self, *, after_row=0, limit=20):
        with self._db() as db:
            rows = db.execute("SELECT rowid,* FROM runs WHERE rowid>? ORDER BY rowid LIMIT ?",
                              (after_row, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                value = self._run(row)
                value.pop("spec", None)
                if value["result"] is not None:
                    value["result"] = value["result"].get("summary")
                items.append(value)
            return {"items": items, "next_after": rows[limit - 1]["rowid"] if len(rows) > limit and limit else
                    (rows[-1]["rowid"] if rows else after_row), "has_more": len(rows) > limit}

    def history(self, run_id, *, limit=100):
        with self._db() as db:
            self._get_db(db, run_id)
            rows = db.execute("SELECT version,state,note,created_at FROM run_history WHERE run_id=? ORDER BY version DESC LIMIT ?",
                              (run_id, limit)).fetchall()
            return [dict(row) for row in rows]

    def counts(self):
        with self._db() as db:
            rows = db.execute("SELECT state,COUNT(*) AS count FROM runs GROUP BY state").fetchall()
            return {row["state"]: row["count"] for row in rows}

    def replay(self, key, operation, arguments):
        fingerprint = digest({"operation": operation, "arguments": arguments})
        with self._db() as db:
            previous = db.execute("SELECT state,response,fingerprint FROM requests WHERE key=?", (key,)).fetchone()
            if previous is None:
                return None
            if previous["fingerprint"] != fingerprint:
                raise CollabError("IDEMPOTENCY_CONFLICT", "request_id was reused for different input")
            if previous["state"] != "DONE" or not previous["response"]:
                raise CollabError("REQUEST_OUTCOME_UNKNOWN", "previous request has no committed response")
            return {**json.loads(previous["response"]), "idempotent_replay": True}

    def mutate(self, key, operation, arguments, writer):
        fingerprint = digest({"operation": operation, "arguments": arguments})
        with self._db(immediate=True) as db:
            previous = db.execute("SELECT state,response,fingerprint FROM requests WHERE key=?", (key,)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise CollabError("IDEMPOTENCY_CONFLICT", "request_id was reused for different input")
                if previous["state"] != "DONE" or not previous["response"]:
                    raise CollabError("REQUEST_OUTCOME_UNKNOWN", "previous request has no committed response")
                return {**json.loads(previous["response"]), "idempotent_replay": True}
            stamp = now()
            db.execute("INSERT INTO requests(key,fingerprint,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (key, fingerprint, "STARTED", stamp, stamp))
            result = writer(db)
            db.execute("UPDATE requests SET state='DONE',response=?,updated_at=? WHERE key=?",
                       (canonical(result).decode("utf-8"), now(), key))
            db.commit()
            return {**result, "idempotent_replay": False}

    def submit(self, spec, request_id, prepare=None, identity=None):
        def writer(db):
            run_id = str(uuid4())
            if prepare is not None:
                prepare(run_id)
            stamp = now()
            db.execute("INSERT INTO runs(id,version,state,spec,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (run_id, 1, "QUEUED", canonical(spec).decode("utf-8"), stamp, stamp))
            self._history(db, run_id, 1, "QUEUED", "submitted")
            return {"run": self._get_db(db, run_id)}
        return self.mutate(request_id, "submit", identity or spec, writer)

    def claim_next(self):
        with self._db(immediate=True) as db:
            row = db.execute("SELECT id FROM runs WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                db.commit()
                return None
            current = self._get_db(db, row["id"])
            version = current["version"] + 1
            db.execute("UPDATE runs SET version=?,state='RUNNING',updated_at=? WHERE id=?",
                       (version, now(), row["id"]))
            self._history(db, row["id"], version, "RUNNING", "driver starting")
            db.commit()
            return self._get_db(db, row["id"])

    def finish(self, run_id, delivery, manifest):
        with self._db(immediate=True) as db:
            current = self._get_db(db, run_id)
            if current["state"] != "RUNNING":
                raise CollabError("INVALID_STATE", "only a running driver can finish")
            native_status = delivery.get("status")
            state = "DELIVERED" if native_status == "EXECUTION_COMPLETED" else (
                "CANCELLED" if native_status == "CANCELLED" or current["cancel_requested"] else "FAILED")
            result = {
                "summary": {
                    "native_status": native_status,
                    "error": delivery.get("error"),
                    "outcome_reason": delivery.get("outcome_reason"),
                    "outcome_unknown": native_status == "OUTCOME_UNKNOWN",
                    "changed_files": len(manifest["changes"]),
                    "out_of_scope_files": len(manifest["out_of_scope"]),
                    "main_changed": bool(manifest.get("main_changed")),
                    "main_changes": list(manifest.get("main_changes") or [])[:20],
                },
                "delivery": delivery,
                "manifest": manifest,
            }
            version = current["version"] + 1
            db.execute("UPDATE runs SET version=?,state=?,result=?,updated_at=? WHERE id=?",
                       (version, state, canonical(result).decode("utf-8"), now(), run_id))
            self._history(db, run_id, version, state, "driver finished")
            db.commit()
            return self._get_db(db, run_id)

    def cancel(self, run_id, expected_version, request_id):
        arguments = {"id": run_id, "expected_version": expected_version}
        def writer(db):
            current = self._get_db(db, run_id)
            if current["version"] != expected_version:
                raise CollabError("VERSION_CONFLICT", "run version differs")
            if current["state"] == "QUEUED":
                version = current["version"] + 1
                result = {"summary": {"native_status": "CANCELLED", "error": "CANCEL_REQUESTED"}}
                db.execute("UPDATE runs SET version=?,state='CANCELLED',result=?,updated_at=? WHERE id=?",
                           (version, canonical(result).decode("utf-8"), now(), run_id))
                self._history(db, run_id, version, "CANCELLED", "cancelled before start")
            elif current["state"] == "RUNNING":
                version = current["version"] + 1
                db.execute("UPDATE runs SET version=?,cancel_requested=1,updated_at=? WHERE id=?",
                           (version, now(), run_id))
                self._history(db, run_id, version, "RUNNING", "cancellation requested")
            else:
                raise CollabError("INVALID_STATE", "only queued or running work can be cancelled")
            return {"run": self._get_db(db, run_id)}
        return self.mutate(request_id, "cancel", arguments, writer)

    def review(self, run_id, expected_version, conclusion, summary, checks, current_changes, request_id):
        arguments = {"id": run_id, "expected_version": expected_version, "conclusion": conclusion,
                     "summary": summary, "checks": checks}
        def writer(db):
            current = self._get_db(db, run_id)
            if current["version"] != expected_version:
                raise CollabError("VERSION_CONFLICT", "run version differs")
            if current["state"] != "DELIVERED":
                raise CollabError("INVALID_STATE", "only a delivered run can be reviewed")
            review = {"reviewer": "codex", "conclusion": conclusion, "summary": summary,
                      "checks": checks, "current_changes": current_changes, "created_at": now()}
            version = current["version"] + 1
            db.execute("UPDATE runs SET version=?,state=?,review=?,updated_at=? WHERE id=?",
                       (version, conclusion, canonical(review).decode("utf-8"), now(), run_id))
            self._history(db, run_id, version, conclusion, "codex review")
            return {"run": self._get_db(db, run_id)}
        return self.mutate(request_id, "review", arguments, writer)

    def running_specs(self):
        with self._db() as db:
            rows = db.execute("SELECT spec FROM runs WHERE state='RUNNING'").fetchall()
            return [json.loads(row["spec"]) for row in rows]

    def blocking_specs(self):
        with self._db() as db:
            rows = db.execute("SELECT spec FROM runs WHERE state IN ('QUEUED','RUNNING','DELIVERED')").fetchall()
            return [json.loads(row["spec"]) for row in rows]

    def running_runs(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM runs WHERE state='RUNNING' ORDER BY rowid").fetchall()
            return [self._run(row) for row in rows]

    def attach_manifest(self, run_id, manifest):
        with self._db(immediate=True) as db:
            current = self._get_db(db, run_id)
            result = current.get("result") or {}
            result["manifest"] = manifest
            db.execute("UPDATE runs SET result=?,updated_at=? WHERE id=?",
                       (canonical(result).decode("utf-8"), now(), run_id))
            db.commit()
            return self._get_db(db, run_id)

    def recover_interrupted(self):
        with self._db(immediate=True) as db:
            rows = db.execute("SELECT id FROM runs WHERE state='RUNNING'").fetchall()
            recovered = []
            for row in rows:
                current = self._get_db(db, row["id"])
                version = current["version"] + 1
                result = {"summary": {"native_status": "FAILED", "error": "CONTROLLER_RESTARTED",
                                      "outcome_unknown": True}}
                db.execute("UPDATE runs SET version=?,state='FAILED',result=?,updated_at=? WHERE id=?",
                           (version, canonical(result).decode("utf-8"), now(), row["id"]))
                self._history(db, row["id"], version, "FAILED", "controller restarted; outcome unknown")
                recovered.append(row["id"])
            db.commit()
            return recovered
