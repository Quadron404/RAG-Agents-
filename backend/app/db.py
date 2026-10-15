from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .auth.schema import AUTH_SCHEMA, CONTROL_PLANE_SCHEMA

#: Owner identifiers the pre-authentication app wrote for every client.  Any
#: thread stored under one of these belongs to nobody we can verify, so it is
#: quarantined rather than exposed to the first person who signs in.
LEGACY_OWNER_IDS = ("me", "default", "", "user", "local")


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Add a column to an existing table if it is not already there.

        ``CREATE TABLE IF NOT EXISTS`` cannot add a column to a database that
        already has the table, so an existing checkout would silently keep the
        old shape.  This keeps the schema self-upgrading without a separate
        migration step, using the same idempotent, safe-to-rerun approach as the
        table definitions.
        """
        try:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        except Exception:
            return
        names = {r["name"] for r in rows}
        if column not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    agent TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS memory (
                    user_id TEXT,
                    key TEXT,
                    value TEXT,
                    updated_at REAL,
                    PRIMARY KEY (user_id, key)
                );
                -- Long-term memory with provenance.  Kept separate from the
                -- legacy `memory` table (key/value only) because a fact's
                -- trustworthiness matters: something the user stated is not the
                -- same as something the model inferred, and only the confirmed
                -- facts may be treated as instructions.  Scoped by user_id so a
                -- fact can never leak across accounts.
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'inferred',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at REAL,
                    updated_at REAL,
                    UNIQUE (user_id, key)
                );
                -- One row per computer-control run, written when the run
                -- finishes.  Only the summary and the per-turn event log: no
                -- screenshot bytes, so a long run costs a few kilobytes rather
                -- than tens of megabytes, and nothing here can rebuild a picture
                -- of the user's screen.
                CREATE TABLE IF NOT EXISTS computer_runs (
                    task_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    status TEXT,
                    steps INTEGER,
                    summary TEXT,
                    events TEXT,
                    created_at REAL
                );
                """
            )
            # The identity, control-plane and email schema.  Runs on every
            # startup so an existing database gains the new tables without a
            # separate migration step, and a fresh database is created complete.
            self.conn.executescript(AUTH_SCHEMA)
            self.conn.executescript(CONTROL_PLANE_SCHEMA)
            # Columns added to an existing email_sends table by later phases.
            self._ensure_column("email_sends", "recipient_email", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("email_sends", "sender_email", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("email_sends", "provider_message_id", "TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
            self._quarantine_legacy_threads()

    def _quarantine_legacy_threads(self) -> None:
        """Move pre-auth threads out of any real user's reach.

        The application used to serve every conversation to whoever claimed the
        id ``"me"``.  Those rows cannot be attributed to a verified Google
        identity, so assigning them to the first person who logs in would hand
        one user another's history.  They are moved to a sentinel owner and
        recorded in ``legacy_quarantine`` so they remain recoverable by an
        operator, but no authenticated session can ever list or read them.
        """
        placeholders = ",".join("?" for _ in LEGACY_OWNER_IDS)
        rows = self.conn.execute(
            f"SELECT id, user_id FROM threads WHERE user_id IN ({placeholders})",
            LEGACY_OWNER_IDS,
        ).fetchall()
        if not rows:
            return
        now = time.time()
        for row in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO legacy_quarantine "
                "(thread_id, original_user_id, reason, quarantined_at) VALUES (?, ?, ?, ?)",
                (
                    row["id"],
                    row["user_id"],
                    "pre-authentication conversation with no verified owner",
                    now,
                ),
            )
        self.conn.execute(
            f"UPDATE threads SET user_id = ? WHERE user_id IN ({placeholders})",
            ("__legacy__", *LEGACY_OWNER_IDS),
        )
        self.conn.commit()

    def create_thread(self, user_id: str, title: str = "New conversation") -> str:
        thread_id = uuid.uuid4().hex
        with self.lock:
            self.conn.execute(
                "INSERT INTO threads (id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
                (thread_id, user_id, title, time.time()),
            )
            self.conn.commit()
        return thread_id

    def get_thread(self, thread_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def list_threads(self, user_id: str) -> List[Dict[str, object]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM threads WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def add_message(
        self, thread_id: str, role: str, content: str, agent: str = "", data: Optional[Dict[str, object]] = None
    ) -> int:
        """Append one canonical transcript row and return its id.

        The id is returned rather than swallowed because it is the transcript's
        high-water mark: callers use it to record conversation activity and to
        exclude the turn they are currently producing from the context they pass
        to the model, so that turn is not counted twice.
        """
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages (thread_id, role, agent, content, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, role, agent, content, json.dumps(data or {}), time.time()),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def list_messages(self, thread_id: str, tail: int = 60) -> List[Dict[str, object]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?", (thread_id, tail)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_message_id(self, thread_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT MAX(id) AS max_id FROM messages WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return int((row and row["max_id"]) or 0)

    def list_messages_since(self, thread_id: str, after_id: int, limit: int = 200) -> List[Dict[str, object]]:
        """Canonical transcript rows strictly after ``after_id``, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (thread_id, int(after_id or 0), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def messages_between(self, thread_id: str, after_id: int, through_id: int) -> List[Dict[str, object]]:
        """Rows in ``(after_id, through_id]`` -- the exact span a summary covers."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND id > ? AND id <= ? ORDER BY id ASC",
                (thread_id, int(after_id or 0), int(through_id or 0)),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_computer_run(
        self,
        thread_id: str,
        task_id: str,
        summary: Dict[str, object],
        events: Optional[List[Dict[str, object]]] = None,
    ) -> None:
        """Record a finished computer-control run.

        The per-turn events keep the command, the result, any error and the
        screenshot's dimensions and hash -- enough to answer "where did that
        click go" from the log, without storing the image itself.
        """
        with self.lock:
            self.conn.execute(
                "INSERT INTO computer_runs (task_id, thread_id, status, steps, summary, events, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET status = excluded.status, "
                "steps = excluded.steps, summary = excluded.summary, events = excluded.events",
                (
                    task_id,
                    thread_id,
                    str(summary.get("status", "")),
                    int(summary.get("steps", 0) or 0),
                    json.dumps(summary),
                    json.dumps(events or []),
                    time.time(),
                ),
            )
            self.conn.commit()

    def get_computer_run(self, task_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM computer_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        for field in ("summary", "events"):
            try:
                record[field] = json.loads(record.get(field) or "{}")
            except Exception:
                record[field] = {}
        return record

    def list_computer_runs(self, thread_id: str, tail: int = 20) -> List[Dict[str, object]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM computer_runs WHERE thread_id = ? ORDER BY created_at DESC LIMIT ?",
                (thread_id, tail),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["summary"] = json.loads(record.get("summary") or "{}")
            except Exception:
                record["summary"] = {}
            out.append(record)
        return out

    def remember(self, user_id: str, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO memory (user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (user_id, key, value, time.time()),
            )
            self.conn.commit()

    def recall(self, user_id: str) -> Dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM memory WHERE user_id = ?", (user_id,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- long-term memory with provenance ---------------------------------

    def remember_fact(
        self,
        user_id: str,
        key: str,
        value: str,
        source: str = "inferred",
        confidence: float = 0.5,
    ) -> None:
        """Upsert one remembered fact, keeping its provenance.

        ``source`` is ``confirmed`` for something the user stated and
        ``inferred`` for something the model deduced.  The distinction is what
        lets the context builder treat user assertions as instructions and model
        guesses as suggestions -- a memory with no provenance cannot make that
        call, so it is not stored without one.
        """
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO memory_facts (user_id, key, value, source, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, "
                "source = excluded.source, confidence = excluded.confidence, updated_at = excluded.updated_at",
                (user_id, key, value, source, float(confidence), now, now),
            )
            self.conn.commit()

    def recall_facts(self, user_id: str) -> List[Dict[str, object]]:
        if not user_id:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, value, source, confidence, updated_at FROM memory_facts "
                "WHERE user_id = ? ORDER BY source = 'confirmed' DESC, updated_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def forget_fact(self, user_id: str, key: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM memory_facts WHERE user_id = ? AND key = ?", (user_id, key)
            )
            self.conn.commit()
        return cur.rowcount > 0

    # --- conversation activity (passive summary bookkeeping) --------------

    def touch_conversation_activity(
        self, conversation_id: str, user_id: str, last_event_id: int, idle_seconds: int
    ) -> None:
        """Record that a conversation advanced to ``last_event_id``.

        Sets the idle deadline at which a summary becomes due.  Every new turn
        re-arms it, so a conversation that is actively being used is never
        summarised mid-thought -- the summary only becomes due after the
        configured quiet period.  ``summarized_through`` is left where it was:
        the still-unsummarised tail is exactly ``(summarized_through,
        last_event_id]``.
        """
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO conversation_activity "
                "(conversation_id, user_id, last_event_id, last_activity_at, summarized_through, "
                "summary_status, summary_due_at) VALUES (?, ?, ?, ?, 0, 'pending', ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET last_event_id = excluded.last_event_id, "
                "last_activity_at = excluded.last_activity_at, summary_status = 'pending', "
                "summary_due_at = excluded.summary_due_at",
                (conversation_id, user_id, int(last_event_id), now, now + idle_seconds),
            )
            self.conn.commit()

    def get_conversation_activity(self, conversation_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM conversation_activity WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def due_conversation_summaries(self, now: Optional[float] = None) -> List[Dict[str, object]]:
        """Conversations whose quiet period has elapsed with unsummarised turns."""
        moment = time.time() if now is None else float(now)
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM conversation_activity "
                "WHERE summary_status = 'pending' AND summary_due_at IS NOT NULL "
                "AND summary_due_at <= ? AND last_event_id > summarized_through "
                "ORDER BY summary_due_at ASC",
                (moment,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_summary_status(
        self,
        conversation_id: str,
        status: str,
        *,
        summarized_through: Optional[int] = None,
        summary_due_at: Optional[float] = None,
    ) -> None:
        """Update a conversation's summary state, advancing the checkpoint.

        ``summarized_through`` is only ever moved forward, and only by a caller
        that has already persisted the summary it describes -- the checkpoint
        must never run ahead of what has actually been written down.  Passing
        ``summary_due_at`` re-arms the idle deadline, which is how a conversation
        with more unsummarised turns left over is made due again immediately.
        """
        sets = ["summary_status = ?"]
        args: List[object] = [status]
        if summarized_through is not None:
            sets.append("summarized_through = MAX(summarized_through, ?)")
            args.append(int(summarized_through))
        if summary_due_at is not None:
            sets.append("summary_due_at = ?")
            args.append(float(summary_due_at))
        args.append(conversation_id)
        with self.lock:
            self.conn.execute(
                f"UPDATE conversation_activity SET {', '.join(sets)} WHERE conversation_id = ?",
                tuple(args),
            )
            self.conn.commit()

    def latest_summary(self, conversation_id: str, up_to: Optional[int] = None) -> Optional[Dict[str, object]]:
        """The newest persisted summary, optionally one covering a given point.

        ``up_to`` returns the latest summary whose covered range includes that
        event, which is what context assembly asks for: the summary that stands
        in for the turns before the verbatim window.
        """
        with self.lock:
            if up_to is None:
                row = self.conn.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ? AND covered_to >= ? "
                    "ORDER BY version DESC LIMIT 1",
                    (conversation_id, int(up_to)),
                ).fetchone()
        return dict(row) if row else None

    def save_summary(
        self,
        conversation_id: str,
        user_id: str,
        covered_from: int,
        covered_to: int,
        activity_version: int,
        content: str,
    ) -> int:
        """Persist a new summary version and return it.

        A new version is written rather than overwriting the previous one so a
        stale summary can never silently replace a newer one: the version chain
        keeps every generation, and ``save_summary`` only ever appends.
        """
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                "SELECT MAX(version) AS v FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            version = int((row and row["v"]) or 0) + 1
            self.conn.execute(
                "INSERT INTO conversation_summaries "
                "(conversation_id, user_id, version, covered_from, covered_to, activity_version, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, user_id, version, int(covered_from), int(covered_to), int(activity_version), content, now),
            )
            self.conn.commit()
        return version

    # --- durable jobs (external scheduler interface) ----------------------

    def enqueue_job(
        self,
        kind: str,
        user_id: str,
        payload: Optional[Dict[str, object]] = None,
        *,
        job_id: str = "",
        available_at: Optional[float] = None,
    ) -> str:
        """Enqueue one unit of durable work, idempotently by ``job_id``.

        An empty ``job_id`` is generated.  Passing one makes the call safe to
        repeat: the external scheduler may retry its own dispatch without
        creating a second copy of the same job.
        """
        jid = job_id or uuid.uuid4().hex
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO durable_jobs "
                "(job_id, kind, user_id, payload, status, attempts, available_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (jid, kind, user_id, json.dumps(payload or {}), now if available_at is None else float(available_at), now, now),
            )
            self.conn.commit()
        return jid

    def claim_jobs(self, kind: str, limit: int = 5, lease_seconds: float = 300.0) -> List[Dict[str, object]]:
        """Atomically lease up to ``limit`` due jobs of ``kind``.

        The status change from ``pending`` to ``running`` happens under the same
        lock as the read, so two schedulers racing for the same row cannot both
        win it.  A lease that expires makes the job due again, which is what lets
        a crashed worker's job be retried without a second queue.
        """
        now = time.time()
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM durable_jobs WHERE kind = ? AND status = 'pending' "
                "AND (available_at IS NULL OR available_at <= ?) ORDER BY created_at ASC LIMIT ?",
                (kind, now, int(limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                self.conn.execute(
                    "UPDATE durable_jobs SET status = 'running', lease_until = ?, attempts = attempts + 1, "
                    "updated_at = ? WHERE job_id = ?",
                    (now + float(lease_seconds), now, row["job_id"]),
                )
                claimed.append(dict(row))
            self.conn.commit()
        for record in claimed:
            try:
                record["payload"] = json.loads(record.get("payload") or "{}")
            except Exception:
                record["payload"] = {}
        return claimed

    def complete_job(self, job_id: str, status: str = "done", result: str = "", error_code: str = "") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE durable_jobs SET status = ?, result = ?, error_code = ?, lease_until = NULL, updated_at = ? "
                "WHERE job_id = ?",
                (status, result, error_code, time.time(), job_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def start_job(self, job_id: str, lease_seconds: float = 300.0) -> bool:
        """Lease a specific job for execution, reentrant only after its lease.

        Returns True when this caller won the job.  A job already ``running``
        under a live lease is not won, which is what makes a duplicated dispatch
        of the same routine run a no-op instead of a second execution.  An
        expired lease is reclaimable, so a backend that died mid-run does not
        wedge the job forever.
        """
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "UPDATE durable_jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, "
                "updated_at = ? WHERE job_id = ? AND (status = 'pending' "
                "OR (status = 'running' AND (lease_until IS NULL OR lease_until <= ?)))",
                (now + float(lease_seconds), now, job_id, now),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def requeue_job(self, job_id: str, delay_seconds: float = 60.0, error_code: str = "") -> bool:
        """Return a claimed job to the queue for a later attempt.

        Used when a failure is the provider's "later, please" rather than the
        work's fault: the job is retained and re-armed instead of being failed,
        so an exhausted model quota delays and retries the work rather than
        discarding it.  ``attempts`` is left as-is so a caller can still bound
        how many times it is willing to wait.
        """
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "UPDATE durable_jobs SET status = 'pending', lease_until = NULL, available_at = ?, "
                "error_code = ?, updated_at = ? WHERE job_id = ?",
                (now + float(delay_seconds), error_code, now, job_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def get_job(self, job_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        try:
            record["payload"] = json.loads(record.get("payload") or "{}")
        except Exception:
            record["payload"] = {}
        return record

    # --- identity ---------------------------------------------------------

    def upsert_user(
        self,
        google_sub: str,
        email: str = "",
        email_verified: bool = False,
        name: str = "",
        picture: str = "",
    ) -> Dict[str, object]:
        """Find or create the internal user for a verified Google subject.

        The Google subject is the identity; the email is a display attribute
        that Google may change, so it is updated on every login but never used
        to look a user up.
        """
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE google_sub = ?", (google_sub,)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE users SET email = ?, email_verified = ?, name = ?, picture = ?, updated_at = ? "
                    "WHERE id = ?",
                    (email, 1 if email_verified else 0, name, picture, now, row["id"]),
                )
                user_id = row["id"]
            else:
                user_id = uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO users (id, google_sub, email, email_verified, name, picture, timezone, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        google_sub,
                        email,
                        1 if email_verified else 0,
                        name,
                        picture,
                        "UTC",
                        now,
                        now,
                    ),
                )
            self.conn.commit()
            created = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(created)

    def get_user(self, user_id: str) -> Optional[Dict[str, object]]:
        if not user_id:
            return None
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_sub(self, google_sub: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        return dict(row) if row else None

    def set_user_timezone(self, user_id: str, timezone: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?",
                (timezone, time.time(), user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    # --- sessions ---------------------------------------------------------

    def create_session(
        self,
        session_hash: str,
        csrf: str,
        user_id: str,
        ttl_seconds: int,
        rotated_from: Optional[str] = None,
    ) -> Dict[str, object]:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (session_hash, user_id, csrf, created_at, expires_at, revoked, rotated_from) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (session_hash, user_id, csrf, now, now + ttl_seconds, rotated_from or ""),
            )
            if rotated_from:
                # Rotation is a replacement, not an addition: the session it
                # was derived from is revoked in the same transaction.
                self.conn.execute("UPDATE sessions SET revoked = 1 WHERE session_hash = ?", (rotated_from,))
            self.conn.commit()
        return {"session_hash": session_hash, "csrf": csrf, "user_id": user_id, "expires_at": now + ttl_seconds}

    def get_valid_session(self, session_hash: str) -> Optional[Dict[str, object]]:
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE session_hash = ? AND revoked = 0 AND expires_at > ?",
                (session_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, session_hash: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE session_hash = ?", (session_hash,)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def revoke_sessions_for_user(self, user_id: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)
            )
            self.conn.commit()
        return cur.rowcount

    # --- oauth state / nonce / pkce --------------------------------------

    def create_oauth_state(
        self,
        state: str,
        mode: str,
        user_id: str,
        code_verifier: str,
        nonce: str,
        redirect_uri: str,
        ttl_seconds: int = 600,
    ) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO oauth_states (state, mode, user_id, code_verifier, nonce, redirect_uri, "
                "created_at, expires_at, consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (state, mode, user_id, code_verifier, nonce, redirect_uri, now, now + ttl_seconds),
            )
            self.conn.commit()

    def consume_oauth_state(self, state: str) -> Optional[Dict[str, object]]:
        """Atomically read and invalidate an authorization state.

        A single UPDATE...RETURNING would be ideal, but SQLite's support varies,
        so the row is selected and marked consumed inside one lock, and the
        caller only proceeds when the exact state was unconsumed and unexpired.
        """
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM oauth_states WHERE state = ? AND consumed = 0 AND expires_at > ?",
                (state, now),
            ).fetchone()
            if not row:
                return None
            self.conn.execute("UPDATE oauth_states SET consumed = 1 WHERE state = ?", (state,))
            self.conn.commit()
        return dict(row)

    def prune_expired_states(self) -> int:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM oauth_states WHERE expires_at < ? OR consumed = 1", (time.time() - 3600,)
            )
            self.conn.commit()
        return cur.rowcount

    # --- ownership --------------------------------------------------------

    def thread_owned_by(self, thread_id: str, user_id: str) -> bool:
        if not thread_id or not user_id:
            return False
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM threads WHERE id = ? AND user_id = ?", (thread_id, user_id)
            ).fetchone()
        return row is not None

    def is_quarantined(self, thread_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM legacy_quarantine WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None

    def audit_login(self, google_sub: str, user_id: str, outcome: str, reason: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO login_audit (google_sub, user_id, outcome, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (google_sub, user_id, outcome, reason, time.time()),
            )
            self.conn.commit()

    # --- Gmail connections ------------------------------------------------

    def save_gmail_connection(
        self,
        *,
        user_id: str,
        google_sub: str,
        email: str,
        scope: str,
        refresh_token_enc: str,
        access_token_enc: str,
        token_expiry: float,
    ) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO gmail_connections (user_id, google_sub, email, scope, refresh_token_enc, "
                "access_token_enc, token_expiry, status, connected_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'connected', ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET google_sub = excluded.google_sub, "
                "email = excluded.email, scope = excluded.scope, refresh_token_enc = excluded.refresh_token_enc, "
                "access_token_enc = excluded.access_token_enc, token_expiry = excluded.token_expiry, "
                "status = 'connected', last_error = '', updated_at = excluded.updated_at",
                (
                    user_id,
                    google_sub,
                    email,
                    scope,
                    refresh_token_enc,
                    access_token_enc,
                    token_expiry,
                    now,
                    now,
                ),
            )
            self.conn.commit()

    def get_gmail_connection(self, user_id: str) -> Optional[Dict[str, object]]:
        if not user_id:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM gmail_connections WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_gmail_connection(self, user_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM gmail_connections WHERE user_id = ?", (user_id,)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def set_gmail_status(self, user_id: str, status: str, error: str = "") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE gmail_connections SET status = ?, last_error = ?, updated_at = ? WHERE user_id = ?",
                (status, error, time.time(), user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def update_gmail_tokens(
        self,
        *,
        user_id: str,
        access_token_enc: str,
        token_expiry: float,
        status: str = "connected",
        error: str = "",
    ) -> bool:
        """Replace the stored access token after a successful refresh.

        Only the access token and its expiry move here; the refresh token -- the
        long-lived credential -- is left exactly as it was, and the status is set
        back to ``connected`` so a previous transient error clears itself.
        """
        with self.lock:
            cur = self.conn.execute(
                "UPDATE gmail_connections SET access_token_enc = ?, token_expiry = ?, "
                "status = ?, last_error = ?, updated_at = ? WHERE user_id = ?",
                (access_token_enc, token_expiry, status, error, time.time(), user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    # --- email notifications (owner-only) ---------------------------------

    def create_email_draft(
        self, *, user_id: str, agent_id: str, conversation_id: str, subject: str, body: str
    ) -> str:
        """Create an owner-notification draft owned by exactly one user.

        The recipient is deliberately absent: it is resolved at send time from
        the authenticated owner's verified identity, never chosen by the model
        or stored from a tool call.
        """
        draft_id = uuid.uuid4().hex
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO email_drafts (draft_id, user_id, agent_id, conversation_id, subject, body, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                (draft_id, user_id, agent_id, conversation_id or "", subject or "", body or "", now, now),
            )
            self.conn.commit()
        return draft_id

    def get_email_draft(self, draft_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM email_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def update_email_draft(self, *, draft_id: str, user_id: str, subject: str, body: str) -> bool:
        """Update a draft's content, but only when ``user_id`` owns it."""
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "UPDATE email_drafts SET subject = ?, body = ?, updated_at = ? WHERE draft_id = ? AND user_id = ?",
                (subject or "", body or "", now, draft_id, user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def mark_email_draft_sent(self, draft_id: str) -> bool:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "UPDATE email_drafts SET status = 'sent', updated_at = ? WHERE draft_id = ?",
                (now, draft_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def claim_email_draft_for_send(self, *, draft_id: str, user_id: str) -> bool:
        """Atomically move an owned draft ``draft -> sending``.

        This is the duplicate-send gate: the status transition happens in a
        single conditional UPDATE, so of two concurrent send attempts only one
        can win the draft and reach the provider.  The loser sees ``False`` and
        reports "already being sent" / "already sent" instead of sending twice.
        """
        with self.lock:
            cur = self.conn.execute(
                "UPDATE email_drafts SET status = 'sending', updated_at = ? "
                "WHERE draft_id = ? AND user_id = ? AND status = 'draft'",
                (time.time(), draft_id, user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def set_email_draft_status(self, *, draft_id: str, user_id: str, status: str) -> bool:
        """Set a draft's status for an owner -- used to release a failed send.

        A provider failure returns the draft to ``draft`` so the owner's next
        turn can retry it, rather than leaving it stuck in ``sending``.
        """
        with self.lock:
            cur = self.conn.execute(
                "UPDATE email_drafts SET status = ?, updated_at = ? WHERE draft_id = ? AND user_id = ?",
                (status, time.time(), draft_id, user_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def create_email_send(
        self,
        *,
        user_id: str,
        agent_id: str,
        draft_id: str,
        subject: str,
        recipient_email: str,
        sender_email: str,
        provider_message_id: str = "",
        gmail_message_id: str = "",
        status: str = "sent",
        error_code: str = "",
        idempotency_key: str = "",
    ) -> str:
        """Record one send.

        ``idempotency_key`` (defaulting to one per draft) is UNIQUE, so a second
        attempt to send the same draft cannot create a second row -- and cannot
        cause a second provider delivery once the first has been recorded.
        """
        email_id = uuid.uuid4().hex
        now = time.time()
        if not idempotency_key:
            idempotency_key = f"owner-notification:{draft_id}"
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO email_sends (email_id, user_id, agent_id, draft_id, subject, to_alias, recipient_email, sender_email, provider_message_id, gmail_message_id, gmail_thread_id, status, idempotency_key, error_code, created_at, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
                    (
                        email_id,
                        user_id,
                        agent_id,
                        draft_id,
                        subject or "",
                        recipient_email or "",
                        recipient_email or "",
                        sender_email or "",
                        provider_message_id or "",
                        gmail_message_id or provider_message_id or "",
                        status,
                        idempotency_key,
                        error_code or "",
                        now,
                        now if status == "sent" else None,
                    ),
                )
                self.conn.commit()
                return email_id
            except sqlite3.IntegrityError:
                # Already recorded under this key; return the existing row.
                row = self.conn.execute(
                    "SELECT email_id FROM email_sends WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                self.conn.commit()
                return str(row["email_id"]) if row else email_id

    def get_email_send_by_idem(self, idempotency_key: str) -> Optional[Dict[str, object]]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM email_sends WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return dict(row) if row else None

    def list_email_sends(self, user_id: str, tail: int = 50) -> List[Dict[str, object]]:
        """A user's own send records, newest first.  Never crosses accounts."""
        if not user_id:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM email_sends WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, int(tail)),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_email_audit(
        self,
        *,
        user_id: str,
        agent_id: str,
        draft_id: str,
        recipient_email: str,
        sender_email: str,
        provider: str,
        status: str,
        provider_message_id: str = "",
        error_code: str = "",
    ) -> None:
        """Record one notification attempt for accountability.

        Only the accountability facts are stored: owner, draft, the resolved
        recipient and sender, the provider and the outcome.  The subject and
        body are deliberately not kept here, and no token or header is ever
        written to this table.
        """
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO email_audit (user_id, agent_id, draft_id, recipient_email, sender_email, provider, status, provider_message_id, error_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, draft_id, recipient_email, sender_email, provider, status, provider_message_id, error_code, now),
            )
            self.conn.commit()

    def list_email_audit(self, user_id: str, tail: int = 50) -> List[Dict[str, object]]:
        """A user's own notification audit trail.  Owner-isolated by user_id."""
        if not user_id:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM email_audit WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, int(tail)),
            ).fetchall()
        return [dict(r) for r in rows]
