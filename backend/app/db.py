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