from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init()

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
    ) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO messages (thread_id, role, agent, content, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, role, agent, content, json.dumps(data or {}), time.time()),
            )
            self.conn.commit()

    def list_messages(self, thread_id: str, tail: int = 60) -> List[Dict[str, object]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?", (thread_id, tail)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

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