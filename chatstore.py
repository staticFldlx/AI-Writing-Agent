"""对话存档与长期记忆的存储层：独立 SQLite（chat.db），与设定库分开。

纯逻辑，无 GUI、无 AI 依赖。设定库存"世界观"，这里存"对话与记忆"，
两者分开管理，避免对话内容污染设定库。
"""

import os
import sqlite3
import threading
from datetime import datetime

DEFAULT_CHAT_DB = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "DiceBot", "chat.db"
)


def _now():
    return datetime.now().isoformat(timespec="seconds")


class ChatStore:
    """会话、消息、长期记忆笔记的持久化。"""

    def __init__(self, db_path: str = DEFAULT_CHAT_DB):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    display TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    session_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
            """)
            self.conn.commit()

    # ------------------------------------------------------------ 会话

    def create_session(self, title=None):
        title = (title or "").strip() or datetime.now().strftime("会话 %m-%d %H:%M")
        now = _now()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO sessions (title, created_at, updated_at) VALUES (?,?,?)",
                (title, now, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def list_sessions(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id, title):
        title = (title or "").strip()
        if not title:
            raise ValueError("会话名不能为空")
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now(), session_id),
            )
            self.conn.commit()

    def delete_session(self, session_id):
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.conn.commit()

    def touch_session(self, session_id):
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
            self.conn.commit()

    # ------------------------------------------------------------ 消息

    def add_message(self, session_id, role, content, display=None):
        """content=发给模型的完整内容（含注入），display=界面展示的原文。"""
        display = content if display is None else display
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages (session_id, role, content, display, created_at)"
                " VALUES (?,?,?,?,?)",
                (session_id, role, content, display, _now()),
            )
            self.conn.commit()
            msg_id = cur.lastrowid
        self.touch_session(session_id)
        return msg_id

    def get_messages(self, session_id):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_messages(self, session_id):
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.conn.commit()

    def message_count(self, session_id):
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()["c"]

    # ------------------------------------------------------------ 长期记忆

    def add_memory(self, content, session_id=None):
        """写入一条记忆笔记；与已有条目完全重复则跳过，返回 id 或 None。"""
        content = (content or "").strip()
        if not content:
            return None
        existing = {m["content"] for m in self.list_memories()}
        if content in existing:
            return None
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO memories (content, session_id, created_at) VALUES (?,?,?)",
                (content, session_id, _now()),
            )
            self.conn.commit()
            return cur.lastrowid

    def list_memories(self, limit=None):
        sql = "SELECT * FROM memories ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def update_memory(self, memory_id, content):
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock:
            self.conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?", (content, memory_id))
            self.conn.commit()

    def delete_memory(self, memory_id):
        with self._lock:
            self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self.conn.commit()

    def memory_text(self, limit=40, char_limit=2000):
        """把记忆笔记拼成注入用的文本（新的在前，受长度上限约束）。"""
        parts, total = [], 0
        for m in self.list_memories(limit=limit):
            line = "- " + m["content"]
            if total + len(line) > char_limit:
                break
            parts.append(line)
            total += len(line)
        return "\n".join(parts)

    def close(self):
        self.conn.close()
