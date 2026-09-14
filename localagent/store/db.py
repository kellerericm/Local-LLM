"""SQLite persistence for projects, chats, messages, task lists, and approvals.

One connection shared across threads, serialized with a lock. Rows come back as plain dicts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    env_path TEXT,
    toolsets TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats(
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    gen_overrides TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'normal',
    content TEXT,
    reasoning TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    ok INTEGER,
    usage TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS tasks(
    chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    items TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
    id TEXT PRIMARY KEY,
    chat_id TEXT,
    scope TEXT NOT NULL,
    keys TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS approval_rules(
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(scope, key)
);
"""

DEFAULT_TOOLSETS = ["files", "shell", "python"]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            # Approvals left pending by a previous run can never be answered.
            self._conn.execute(
                "UPDATE approvals SET status='expired' WHERE status='pending'")
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        added = {"chats": [("gen_overrides", "TEXT")], "messages": [("usage", "TEXT")]}
        for table, columns in added.items():
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers -----------------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # -- projects ----------------------------------------------------------
    @staticmethod
    def _project_out(row: dict | None) -> dict | None:
        if row is not None:
            row["toolsets"] = json.loads(row["toolsets"])
        return row

    def create_project(self, name: str, workspace_path: str, env_path: str | None = None,
                       toolsets: list[str] | None = None, description: str = "") -> dict:
        pid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO projects(id,name,workspace_path,env_path,toolsets,description,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (pid, name, str(workspace_path), env_path, json.dumps(toolsets or DEFAULT_TOOLSETS),
             description, time.time()))
        return self.get_project(pid)

    def get_project(self, project_id: str | None) -> dict | None:
        if not project_id:
            return None
        return self._project_out(self._one("SELECT * FROM projects WHERE id=?", (project_id,)))

    def list_projects(self) -> list[dict]:
        return [self._project_out(r) for r in self._all("SELECT * FROM projects ORDER BY name COLLATE NOCASE")]

    def update_project(self, project_id: str, **fields) -> dict | None:
        allowed = {"name", "workspace_path", "env_path", "toolsets", "description"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(json.dumps(v) if k == "toolsets" else v)
        if sets:
            self._exec(f"UPDATE projects SET {','.join(sets)} WHERE id=?", (*params, project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        self._exec("DELETE FROM projects WHERE id=?", (project_id,))

    # -- chats -------------------------------------------------------------
    def create_chat(self, project_id: str | None = None, title: str = "New chat") -> dict:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec("INSERT INTO chats(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                   (cid, project_id, title, now, now))
        return self.get_chat(cid)

    @staticmethod
    def _chat_out(row: dict | None) -> dict | None:
        if row is not None:
            row["gen_overrides"] = json.loads(row["gen_overrides"]) if row.get("gen_overrides") else {}
        return row

    def get_chat(self, chat_id: str) -> dict | None:
        return self._chat_out(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)))

    def list_chats(self, status: str = "active") -> list[dict]:
        return [self._chat_out(r) for r in
                self._all("SELECT * FROM chats WHERE status=? ORDER BY updated_at DESC", (status,))]

    def update_chat(self, chat_id: str, **fields) -> dict | None:
        allowed = {"title", "project_id", "status", "gen_overrides"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append((json.dumps(v) if v else None) if k == "gen_overrides" else v)
        sets.append("updated_at=?")
        params.append(time.time())
        self._exec(f"UPDATE chats SET {','.join(sets)} WHERE id=?", (*params, chat_id))
        return self.get_chat(chat_id)

    def touch_chat(self, chat_id: str) -> None:
        self._exec("UPDATE chats SET updated_at=? WHERE id=?", (time.time(), chat_id))

    def delete_chat(self, chat_id: str) -> None:
        self._exec("DELETE FROM chats WHERE id=?", (chat_id,))

    # -- messages ----------------------------------------------------------
    @staticmethod
    def _message_out(row: dict) -> dict:
        row["tool_calls"] = json.loads(row["tool_calls"]) if row["tool_calls"] else None
        row["usage"] = json.loads(row["usage"]) if row.get("usage") else None
        if row["ok"] is not None:
            row["ok"] = bool(row["ok"])
        return row

    def add_message(self, chat_id: str, role: str, content: str | None = None, *, kind: str = "normal",
                    reasoning: str | None = None, tool_calls: list[dict] | None = None,
                    tool_call_id: str | None = None, name: str | None = None,
                    ok: bool | None = None, usage: dict | None = None) -> dict:
        cur = self._exec(
            "INSERT INTO messages(chat_id,role,kind,content,reasoning,tool_calls,tool_call_id,name,ok,usage,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, role, kind, content, reasoning, json.dumps(tool_calls) if tool_calls else None,
             tool_call_id, name, None if ok is None else int(ok), json.dumps(usage) if usage else None, time.time()))
        self.touch_chat(chat_id)
        return self._message_out(self._one("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)))

    def list_messages(self, chat_id: str) -> list[dict]:
        return [self._message_out(r) for r in
                self._all("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]

    # -- tasks -------------------------------------------------------------
    def set_tasks(self, chat_id: str, items: list[dict]) -> None:
        self._exec("INSERT INTO tasks(chat_id,items,updated_at) VALUES(?,?,?)"
                   " ON CONFLICT(chat_id) DO UPDATE SET items=excluded.items, updated_at=excluded.updated_at",
                   (chat_id, json.dumps(items), time.time()))

    def get_tasks(self, chat_id: str) -> list[dict]:
        row = self._one("SELECT items FROM tasks WHERE chat_id=?", (chat_id,))
        return json.loads(row["items"]) if row else []

    # -- approvals ---------------------------------------------------------
    def create_approval(self, chat_id: str | None, scope: str, keys: list[str], summary: str, detail: str) -> dict:
        aid = uuid.uuid4().hex[:12]
        self._exec("INSERT INTO approvals(id,chat_id,scope,keys,summary,detail,status,created_at)"
                   " VALUES(?,?,?,?,?,?,'pending',?)",
                   (aid, chat_id, scope, json.dumps(keys), summary, detail, time.time()))
        return self.get_approval(aid)

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row:
            row["keys"] = json.loads(row["keys"])
        return row

    def pending_approvals(self) -> list[dict]:
        rows = self._all("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at")
        for r in rows:
            r["keys"] = json.loads(r["keys"])
        return rows

    def decide_approval(self, approval_id: str, decision: str) -> None:
        self._exec("UPDATE approvals SET status='decided', decision=?, decided_at=? WHERE id=?",
                   (decision, time.time(), approval_id))

    def has_rule(self, scope: str, key: str) -> bool:
        return self._one("SELECT 1 AS x FROM approval_rules WHERE scope=? AND key=?", (scope, key)) is not None

    def add_rules(self, scope: str, keys: list[str]) -> None:
        for k in keys:
            self._exec("INSERT OR IGNORE INTO approval_rules(scope,key,created_at) VALUES(?,?,?)",
                       (scope, k, time.time()))

    def list_rules(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            return self._all("SELECT * FROM approval_rules ORDER BY scope, key")
        return self._all("SELECT * FROM approval_rules WHERE scope=? ORDER BY key", (scope,))

    def delete_rule(self, scope: str, key: str) -> None:
        self._exec("DELETE FROM approval_rules WHERE scope=? AND key=?", (scope, key))
