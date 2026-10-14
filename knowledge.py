"""设定库数据层：SQLite + FTS5(trigram) 全文检索。纯逻辑，无 GUI 依赖。

结构化词条（名称/类别/正文/标签/别名/来源/日期），支持关键字检索、
类别与日期筛选、精确检索、去重查找。标准库 sqlite3，零依赖，打包进 exe。
FTS5 或 trigram 不可用时自动降级为 LIKE 检索。
"""

import os
import sqlite3
import threading
from datetime import datetime

DEFAULT_DB_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "DiceBot", "knowledge.db"
)

FIELDS = ("id", "title", "category", "content", "tags", "aliases",
          "source", "created_at", "updated_at")

RECAP_CATEGORY = "前情提要"   # 特殊词条：始终自动注入对话，不参与普通勾选/关键词匹配
OVERVIEW_CATEGORY = "设定总览"  # 特殊词条：整体讨论时注入的世界观归纳
SPECIAL_CATEGORIES = (RECAP_CATEGORY, OVERVIEW_CATEGORY)


def _norm_list(value) -> str:
    """把标签/别名规整成逗号分隔字符串（接受 list 或 str）。"""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [s.strip() for s in str(value or "").replace("，", ",").split(",")]
    return ", ".join(i for i in items if i)


def split_list(value: str) -> list:
    return [s.strip() for s in str(value or "").replace("，", ",").split(",") if s.strip()]


class KnowledgeBase:
    """封装 SQLite 设定库；接口化便于将来替换后端。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.fts = False
        self.init_db()

    # ------------------------------------------------------------ 建表

    def init_db(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT DEFAULT '',
                content TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                aliases TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()
        self._try_init_fts()

    def _try_init_fts(self):
        """建 FTS5(trigram) 虚拟表与同步触发器；不支持则降级 LIKE。"""
        try:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                    title, content, tags, aliases,
                    content='entries', content_rowid='id', tokenize='trigram'
                )
            """)
            cur.executescript("""
                CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
                  INSERT INTO entries_fts(rowid, title, content, tags, aliases)
                  VALUES (new.id, new.title, new.content, new.tags, new.aliases);
                END;
                CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
                  INSERT INTO entries_fts(entries_fts, rowid, title, content, tags, aliases)
                  VALUES('delete', old.id, old.title, old.content, old.tags, old.aliases);
                END;
                CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
                  INSERT INTO entries_fts(entries_fts, rowid, title, content, tags, aliases)
                  VALUES('delete', old.id, old.title, old.content, old.tags, old.aliases);
                  INSERT INTO entries_fts(rowid, title, content, tags, aliases)
                  VALUES (new.id, new.title, new.content, new.tags, new.aliases);
                END;
            """)
            self.conn.commit()
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    # ------------------------------------------------------------ 写

    def add(self, title, category="", content="", tags="", aliases="", source=""):
        title = str(title).strip()
        if not title:
            raise ValueError("词条名称不能为空")
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO entries (title, category, content, tags, aliases, source,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (title, category.strip(), content.strip(), _norm_list(tags),
                 _norm_list(aliases), source.strip(), now, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def update(self, entry_id, **fields):
        allowed = {"title", "category", "content", "tags", "aliases", "source"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("tags", "aliases"):
                v = _norm_list(v)
            elif k == "title":
                v = str(v).strip()
                if not v:
                    raise ValueError("词条名称不能为空")
            else:
                v = str(v).strip()
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        vals.append(datetime.now().isoformat(timespec="seconds"))
        vals.append(entry_id)
        with self._lock:
            self.conn.execute(f"UPDATE entries SET {', '.join(sets)} WHERE id = ?", vals)
            self.conn.commit()

    def delete(self, entry_id):
        with self._lock:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self.conn.commit()

    # ------------------------------------------------------------ 读

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def get(self, entry_id):
        with self._lock:
            return self._row(
                self.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            )

    def list_entries(self, category=None, order="updated_at DESC", exclude_category=None):
        sql = "SELECT * FROM entries"
        where, params = [], []
        if category:
            where.append("category = ?")
            params.append(category)
        if exclude_category:
            where.append("category != ?")
            params.append(exclude_category)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order}"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_recap(self):
        """取当前的前情提要词条（无则返回 None）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE category = ? ORDER BY updated_at DESC LIMIT 1",
                (RECAP_CATEGORY,),
            ).fetchone()
        return dict(row) if row else None

    def set_recap(self, content, title="前情提要", source=""):
        """新建或更新前情提要（全库只保留一条），返回其 id。"""
        existing = self.get_recap()
        if existing:
            self.update(existing["id"], title=title, content=content, source=source)
            return existing["id"]
        return self.add(title, category=RECAP_CATEGORY, content=content, source=source)

    def get_overview(self):
        """取当前的设定总览词条（无则返回 None）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE category = ? ORDER BY updated_at DESC LIMIT 1",
                (OVERVIEW_CATEGORY,),
            ).fetchone()
        return dict(row) if row else None

    def set_overview(self, content, fingerprint="", title="设定总览"):
        """新建或更新设定总览（全库只保留一条），fingerprint 存进 source 供过期比对。"""
        existing = self.get_overview()
        if existing:
            self.update(existing["id"], title=title, content=content, source=fingerprint)
            return existing["id"]
        return self.add(title, category=OVERVIEW_CATEGORY, content=content,
                        source=fingerprint)

    def overview_fingerprint(self):
        """普通词条的指纹（条数 + 最近更新时间）；与总览 source 不一致即为已过期。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m"
                " FROM entries WHERE category NOT IN (?, ?)",
                SPECIAL_CATEGORIES,
            ).fetchone()
        return f"{row['c']}|{row['m']}"

    def overview_stale(self):
        """总览是否已过期（不存在也算需要生成）。"""
        ov = self.get_overview()
        if not ov:
            return True
        return (ov.get("source") or "") != self.overview_fingerprint()

    def normal_entries(self):
        """所有普通词条（排除前情提要与设定总览）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM entries WHERE category NOT IN (?, ?)"
                " ORDER BY category, title", SPECIAL_CATEGORIES,
            ).fetchall()
        return [dict(r) for r in rows]

    def categories(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT category FROM entries WHERE category != '' ORDER BY category"
            ).fetchall()
        return [r["category"] for r in rows]

    def count(self):
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]

    def search(self, query="", category=None, date_from=None, date_to=None, exact=False):
        """关键字/类别/日期检索。

        exact=True：词条名称或别名与 query 完全一致（不区分大小写）。
        否则：query≥3 字且 FTS 可用时走 FTS5，否则 LIKE 子串匹配。
        """
        query = (query or "").strip()
        where, params = [], []

        if query and exact:
            where.append("(LOWER(title) = LOWER(?) OR (',' || LOWER(REPLACE(aliases,' ','')) || ',')"
                         " LIKE '%,' || LOWER(?) || ',%')")
            params += [query, query]
            use_fts = False
        elif query and self.fts and len(query) >= 3:
            use_fts = True
        else:
            use_fts = False
            if query:
                like = f"%{query}%"
                where.append("(title LIKE ? OR content LIKE ? OR tags LIKE ? OR aliases LIKE ?)")
                params += [like, like, like, like]

        if category:
            where.append("category = ?")
            params.append(category)
        if date_from:
            where.append("date(created_at) >= date(?)")
            params.append(date_from)
        if date_to:
            where.append("date(created_at) <= date(?)")
            params.append(date_to)

        if use_fts:
            sql = ("SELECT e.* FROM entries e JOIN entries_fts f ON e.id = f.rowid"
                   " WHERE entries_fts MATCH ?")
            fts_params = ['"' + query.replace('"', '""') + '"']
            if where:
                sql += " AND " + " AND ".join(where)
                fts_params += params
            sql += " ORDER BY rank"
            try:
                with self._lock:
                    return [dict(r) for r in self.conn.execute(sql, fts_params).fetchall()]
            except sqlite3.OperationalError:
                # trigram 对某些 query 报错时降级 LIKE
                pass
            like = f"%{query}%"
            where.insert(0, "(title LIKE ? OR content LIKE ? OR tags LIKE ? OR aliases LIKE ?)")
            params = [like, like, like, like] + params

        sql = "SELECT * FROM entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def find_similar(self, title, aliases=""):
        """去重查找：名称或别名相互命中的既有词条（不区分大小写）。"""
        title = str(title).strip()
        alias_set = {a.lower() for a in split_list(aliases)}
        matches = {}
        with self._lock:
            rows = self.conn.execute("SELECT * FROM entries").fetchall()
        for row in rows:
            e = dict(row)
            e_title = e["title"].strip().lower()
            e_aliases = {a.lower() for a in split_list(e["aliases"])}
            hit = (
                e_title == title.lower()
                or e_title in alias_set
                or title.lower() in e_aliases
                or bool(alias_set & e_aliases)
            )
            if hit:
                matches[e["id"]] = e
        return list(matches.values())

    def close(self):
        self.conn.close()
