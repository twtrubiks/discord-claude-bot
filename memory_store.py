"""SQLite + FTS5 對話永久封存層（含中文 trigram 搜尋）。

- messages：永久保存每一則對話原文 + metadata（append-only）
- messages_fts：unicode61 分詞，給英文/數字關鍵字
- messages_fts_trigram：trigram 分詞，給中文等無空格語言做子字串比對

設計參考 Hermes hermes_state.py。本檔不依賴 discord，純函式/類別，方便單測。
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("sessions.db")

# messages 是「唯一真相」(canonical)；兩個 FTS 表都用 external content
# (content='messages')，只存索引、不存原文 —— 省空間，且壞掉時能用 FTS5
# 'rebuild' 從 messages 就地重建。
# append-only：只需 AFTER INSERT trigger，不需 update/delete trigger。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    ts         TEXT    NOT NULL,     -- ISO 字串，給人看
    created_at REAL    NOT NULL      -- unix epoch，給排序/區間查
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# trigram 表單獨建：有些 SQLite build 沒編 trigram tokenizer，失敗時要能單獨
# 降級，不能拖垮主表。同樣 external content + 自己的 trigger。
_SCHEMA_TRIGRAM = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
    USING fts5(content, content='messages', content_rowid='id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS messages_ai_trigram AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
"""


def _contains_cjk(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK 統一表意
            or 0x3400 <= cp <= 0x4DBF  # 擴展 A
            or 0xF900 <= cp <= 0xFAFF
        ):  # 相容表意
            return True
    return False


def _count_cjk(text: str) -> int:
    return sum(
        1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF or 0x3400 <= ord(ch) <= 0x4DBF
    )


def _fts_phrase(token: str) -> str:
    """把 token 包成 FTS5 phrase（雙引號 + 內部 " 跳脫）。

    不這樣做，query 內的 FTS5 特殊字元（" : ( ) * ^ 或 AND/OR/NOT/NEAR）
    會被當成查詢語法 → OperationalError → 被當「找不到」靜默吃掉。包成
    phrase 後一律當字面字串，tokenizer 再正常切詞。
    """
    return '"' + token.replace('"', '""') + '"'


class SessionStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._trigram_ok = False
        # 單一連線跨執行緒共用（append 在 event loop、/recall 在 executor），
        # 用一把鎖序列化所有讀寫。__init__ 在 bot 啟動單執行緒跑、尚未對外發布，
        # 故建表/探測不必上鎖。
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")  # 多寫者併發
        self._conn.execute("PRAGMA busy_timeout=3000;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        try:
            self._conn.executescript(_SCHEMA_TRIGRAM)
            self._trigram_ok = True
        except sqlite3.OperationalError as e:
            # "no such tokenizer: trigram" → 降級用 LIKE，仍可運作
            logger.warning("trigram 不可用，中文搜尋將降級為 LIKE：%s", e)
        self._conn.commit()
        # FTS 索引可能「讀得到、寫不進」—— base table 讀正常、integrity_check 也過，
        # 但 INSERT 一穿過 FTS 觸發器就整批失敗，封存從此靜默停擺。啟動時主動探測，
        # 壞了就地 rebuild。
        if not self._write_health_ok():
            logger.warning("偵測到 FTS 寫入損毀，嘗試就地 rebuild…")
            self.rebuild_fts()

    def _write_health_ok(self) -> bool:
        """用一筆『一定 rollback 的 insert』穿過 FTS 觸發器，專抓寫入型損毀。

        唯讀探測（SELECT / PRAGMA integrity_check）抓不到這一類，必須真的試寫一次。
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO messages(user_id, role, content, ts, created_at) "
                "VALUES (?,?,?,?,?)",
                ("_health_probe", "user", "_fts_health_probe", "_", 0.0),
            )
            self._conn.execute("ROLLBACK")  # 永不留下探測列
            return True
        except sqlite3.DatabaseError as e:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            logger.warning("FTS 寫入健康探測失敗：%s", e)
            return False

    def rebuild_fts(self) -> bool:
        """就地重建 FTS 索引（external content → 從 messages 重讀，不刪資料）。

        messages 才是唯一真相；FTS 只是它的索引，重建零資料損失。
        """
        try:
            self._conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
            )
            if self._trigram_ok:
                self._conn.execute(
                    "INSERT INTO messages_fts_trigram(messages_fts_trigram) "
                    "VALUES('rebuild')"
                )
            self._conn.commit()
            logger.info("FTS 索引已就地 rebuild")
            return True
        except sqlite3.Error as e:
            logger.error("FTS rebuild 失敗，請檢查磁碟或改用備份：%s", e)
            return False

    # ---- 寫入（append-only；trigger 自動同步兩個 FTS 表）----
    def append(self, user_id: int, role: str, content: str) -> None:
        if not content:
            return
        now = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        try:
            with self._lock, self._conn:  # 序列化 + 自動 commit/rollback
                self._conn.execute(
                    "INSERT INTO messages(user_id, role, content, ts, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (str(user_id), role, content, ts, now),
                )
            # FTS 由 messages_ai / messages_ai_trigram 觸發器自動寫入。
        except sqlite3.Error as e:
            # FTS 索引若損毀，這裡會收到 'database disk image is malformed'。
            # 封存失敗只記 log，絕不影響 bot 回訊息（熱狀態在 JSON）。
            logger.error("封存訊息失敗（不影響對話）：%s", e)

    @staticmethod
    def _trigrams(text: str) -> list[str]:
        """產生 query 的 3 字滑動視窗（去重、略過含空白者），對齊 trigram tokenizer。

        用於模糊召回：把整句拆成 trigram 用 OR 比對，讓自然語言整句也能撈回
        共享 ≥3 字片段的舊訊息（如句中含「興富發」「停損」等主題詞）。
        """
        seen: list[str] = []
        s: set[str] = set()
        for i in range(len(text) - 2):
            g = text[i : i + 3]
            if g in s or any(c.isspace() for c in g):
                continue
            s.add(g)
            seen.append(g)
            if len(seen) >= 60:  # 上限：避免超長訊息產生過長 MATCH 字串
                break
        return seen

    # ---- 搜尋（中文路由）----
    # fuzzy=False（預設，/recall）：精準子字串 / phrase 比對
    # fuzzy=True（auto-recall）：拆 token / trigram 用 OR，任一片段命中即召回
    def search(
        self,
        query: str,
        user_id: Optional[int] = None,
        limit: int = 8,
        fuzzy: bool = False,
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        where = []
        params: list[Any] = []
        if user_id is not None:
            where.append("m.user_id = ?")
            params.append(str(user_id))

        # 整段查詢握同一把鎖：序列化跨執行緒對單一連線的存取（如 /recall 與 append）
        with self._lock:
            if fuzzy:
                return self._fuzzy_search(query, where, params, limit)

            if not _contains_cjk(query):
                # 英文/數字：先走標準 FTS5（以詞為單位、bm25 排序）；token 各自包
                # phrase 跳脫特殊字元。FTS 0 筆時降級 LIKE 子字串，與中文短詞路徑
                # 一致 —— 讓 /recall 對英數也是真子字串（如 'ell' 命中 'hello'）。
                en_tokens = [t for t in query.split() if t]
                if not en_tokens:
                    return []
                match = " ".join(_fts_phrase(t) for t in en_tokens)
                rows = self._fts_search("messages_fts", match, where, params, limit)
                if rows:
                    return rows
                return self._like_search(en_tokens, where, params, limit)

            # 中文：依「每 token 是否 ≥3 個 CJK 字」決定 trigram 或 LIKE
            tokens = [t for t in query.split() if t.upper() not in {"AND", "OR", "NOT"}]
            any_short = any(_count_cjk(t) < 3 for t in tokens) or not tokens
            if self._trigram_ok and not any_short:
                q = " ".join(_fts_phrase(t) for t in tokens)
                rows = self._fts_search("messages_fts_trigram", q, where, params, limit)
                if rows:
                    return rows
            # 短中文 / trigram 不可用 / trigram 0 筆 → LIKE 子字串
            return self._like_search(tokens or [query], where, params, limit)

    def _fuzzy_search(self, query, where, params, limit):
        """召回導向：OR 比對，任一片段命中即可（bm25 自動排序相關度）。"""
        if _contains_cjk(query) and self._trigram_ok:
            grams = self._trigrams(query)
            if grams:
                match = " OR ".join(_fts_phrase(g) for g in grams)
                rows = self._fts_search(
                    "messages_fts_trigram", match, where, params, limit
                )
                if rows:
                    return rows
            # trigram 撈不到（如查詢僅含 2 字 CJK，無共同 3-gram）→ 降級 LIKE
            tokens = [t for t in query.split() if t.upper() not in {"AND", "OR", "NOT"}]
            return self._like_search(tokens or [query], where, params, limit)
        # 英文/數字：OR 各詞，任一命中即可（各詞包 phrase，跳脫特殊字元）
        tokens = [
            t for t in query.split() if t and t.upper() not in {"AND", "OR", "NOT"}
        ]
        if not tokens:
            return []
        match = " OR ".join(_fts_phrase(t) for t in tokens)
        return self._fts_search("messages_fts", match, where, params, limit)

    def _fts_search(self, table, match_query, where, params, limit):
        sql = f"""
            SELECT m.id, m.user_id, m.role, m.content, m.ts,
                   snippet({table}, 0, '»', '«', '…', 12) AS snippet
            FROM {table} f JOIN messages m ON m.id = f.rowid
            WHERE {table} MATCH ?
            {("AND " + " AND ".join(where)) if where else ""}
            ORDER BY rank LIMIT ?
        """
        try:
            cur = self._conn.execute(sql, [match_query, *params, limit])
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError as e:
            logger.warning("FTS 查詢失敗，回空：%s", e)
            return []

    def _like_search(self, tokens, where, params, limit):
        clauses, like_params = [], []
        for tok in tokens:
            esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("m.content LIKE ? ESCAPE '\\'")
            like_params.append(f"%{esc}%")
        sql = f"""
            SELECT m.id, m.user_id, m.role, m.content, m.ts,
                   substr(m.content, 1, 80) AS snippet
            FROM messages m
            WHERE ({" OR ".join(clauses)})
            {("AND " + " AND ".join(where)) if where else ""}
            ORDER BY m.id DESC LIMIT ?
        """
        cur = self._conn.execute(sql, [*like_params, *params, limit])
        return [dict(r) for r in cur.fetchall()]


# 全域單例（與 bot 共用一個連線）
_store: Optional[SessionStore] = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
