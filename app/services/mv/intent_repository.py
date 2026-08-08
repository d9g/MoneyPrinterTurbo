"""
mv.intent_repository — 意境历史 CRUD + 业务逻辑
老杨 2026-08-07 22:18 v2-4 (Diana 4.5 并发锁)

主要功能:
1. CRUD: insert / get_latest / get_all_versions
2. 业务: get_latest_by_signature, list_by_user
3. 并发: per-signature 锁 (Diana 4.5)
4. 维护: mark_latest, cleanup_old_versions (Diana 4.4 retention)
"""
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger

from .db import IntentDB, get_db


# ================ DATA CLASSES ================

@dataclass
class IntentRecord:
    """意境历史记录 (从 DB 行转换)"""
    id: int
    user_id: Optional[str]
    audio_id: str
    song_signature: str
    artist: Optional[str]
    title: Optional[str]
    duration_seconds: float
    version: int
    is_latest: bool
    intent_json: str
    source: str
    llm_error: Optional[str]
    prompt_history_json: Optional[str]
    llm_model: Optional[str]
    llm_latency_ms: Optional[int]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cost_usd: Optional[float]
    created_at: str

    @property
    def intent(self) -> dict:
        """intent_json 反序列化为 dict"""
        return json.loads(self.intent_json)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "IntentRecord":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            audio_id=row["audio_id"],
            song_signature=row["song_signature"],
            artist=row["artist"],
            title=row["title"],
            duration_seconds=row["duration_seconds"],
            version=row["version"],
            is_latest=bool(row["is_latest"]),
            intent_json=row["intent_json"],
            source=row["source"],
            llm_error=row["llm_error"],
            prompt_history_json=row["prompt_history_json"],
            llm_model=row["llm_model"],
            llm_latency_ms=row["llm_latency_ms"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cost_usd=row["cost_usd"],
            created_at=row["created_at"],
        )


# ================ REPOSITORY ================

class IntentRepository:
    """意境历史 Repository

    Usage:
        repo = IntentRepository(db)
        record = repo.get_latest("audio_001")
        repo.insert_history(...)
    """

    def __init__(self, db: Optional[IntentDB] = None):
        self.db = db or get_db()

    # ---------- CRUD ----------

    def insert_history(
        self,
        audio_id: str,
        song_signature: str,
        duration_seconds: float,
        version: int,
        intent_json: str,
        source: str,
        user_id: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        llm_error: Optional[str] = None,
        prompt_history_json: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_latency_ms: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        set_latest: bool = True,
    ) -> int:
        """插入一条意境历史

        Args:
            set_latest: 是否标记为最新版本 (会同时把旧版本 is_latest=0)
        """
        with self.db.transaction() as conn:
            if set_latest:
                # 把旧版本标记为非最新
                conn.execute(
                    "UPDATE mv_intent_history SET is_latest = 0 WHERE audio_id = ?",
                    (audio_id,),
                )
            cursor = conn.execute(
                """
                INSERT INTO mv_intent_history
                (user_id, audio_id, song_signature, artist, title, duration_seconds,
                 version, is_latest, intent_json, source, llm_error,
                 prompt_history_json, llm_model, llm_latency_ms,
                 prompt_tokens, completion_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, audio_id, song_signature, artist, title, duration_seconds,
                    version, 1 if set_latest else 0, intent_json, source, llm_error,
                    prompt_history_json, llm_model, llm_latency_ms,
                    prompt_tokens, completion_tokens, cost_usd,
                ),
            )
            return cursor.lastrowid or 0

    def get_latest(self, audio_id: str) -> Optional[IntentRecord]:
        """获取指定 audio_id 的最新版本"""
        row = self.db.fetch_one(
            "SELECT * FROM mv_intent_latest WHERE audio_id = ?",
            (audio_id,),
        )
        return IntentRecord.from_row(row) if row else None

    def get_all_versions(self, audio_id: str, limit: int = 20) -> List[IntentRecord]:
        """获取指定 audio_id 的所有版本 (按 version desc)"""
        rows = self.db.fetch_all(
            "SELECT * FROM mv_intent_history WHERE audio_id = ? ORDER BY version DESC LIMIT ?",
            (audio_id, limit),
        )
        return [IntentRecord.from_row(r) for r in rows]

    def get_recent_for_evolution(self, audio_id: str, n: int = 3) -> List[IntentRecord]:
        """Diana 3.3: 拿最近 N 条历史, 用于 LLM 进化 prompt"""
        return self.get_all_versions(audio_id, limit=n)

    def get_latest_by_signature(self, song_signature: str, user_id: Optional[str] = None) -> Optional[IntentRecord]:
        """通过 song_signature 查找最新意境

        Diana 4.5: 用于并发锁内的 double-check
        老杨 8/8 14:00 bug fix: webui 多次上传同歌生成不同 audio_id (uuid),
        mv_intent_latest view 只按 audio_id PARTITION, 跨 audio_id 取最新失效。
        改为直接查 mv_intent_history 主表 + ORDER BY version DESC, 跨 audio_id 正确。
        """
        if user_id:
            sql = """
                SELECT * FROM mv_intent_history
                WHERE song_signature = ? AND user_id = ?
                ORDER BY version DESC, created_at DESC LIMIT 1
            """
            params = (song_signature, user_id)
        else:
            sql = """
                SELECT * FROM mv_intent_history
                WHERE song_signature = ?
                ORDER BY version DESC, created_at DESC LIMIT 1
            """
            params = (song_signature,)
        row = self.db.fetch_one(sql, params)
        return IntentRecord.from_row(row) if row else None

    def count_versions(self, audio_id: str) -> int:
        """统计历史版本数"""
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM mv_intent_history WHERE audio_id = ?",
            (audio_id,),
        )
        return row["cnt"] if row else 0

    def count_versions_by_signature(self, song_signature: str) -> int:
        """老杨 8/8 14:00 bug fix: 跨 audio_id 重跑计数 (同歌多次分析都计入)

        webui 每次生成新 uuid file_id, count_versions(audio_id) 只数 1。
        按 song_signature 数才是'同歌被分析过几次'。
        """
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM mv_intent_history WHERE song_signature = ?",
            (song_signature,),
        )
        return row["cnt"] if row else 0

    # ---------- 维护 ----------

    def delete_by_signature(self, song_signature: str) -> int:
        """老杨 8/8 17:34: 按 song_signature 删除该歌所有历史记录 (调试用)

        返回删除的条数. 调试按钮 '🗑 Clear MV Cache' 调用此函数.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM mv_intent_history WHERE song_signature = ?",
                (song_signature,),
            )
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"intent_repo: deleted {deleted} records by signature {song_signature[:20]}")
            return deleted

    def delete_all(self) -> int:
        """老杨 8/8 17:40: 清空所有 MV 缓存 (Settings 页面 '清所有' 按钮调用)

        返回删除的条数.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM mv_intent_history")
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"intent_repo: delete_all - {deleted} records")
            return deleted

    def get_stats(self) -> dict:
        """老杨 8/8 17:40: MV 缓存统计 (Settings 页面展示)

        Returns:
            dict: {"total": int, "unique_signatures": int, "latest_run_at": str}
        """
        total_row = self.db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM mv_intent_history"
        )
        total = total_row["cnt"] if total_row else 0
        sig_row = self.db.fetch_one(
            "SELECT COUNT(DISTINCT song_signature) AS cnt FROM mv_intent_history"
        )
        unique_signatures = sig_row["cnt"] if sig_row else 0
        latest_row = self.db.fetch_one(
            "SELECT MAX(created_at) AS ts FROM mv_intent_history"
        )
        latest_run_at = latest_row["ts"] if latest_row else None
        return {
            "total": total,
            "unique_signatures": unique_signatures,
            "latest_run_at": latest_run_at,
        }

    def cleanup_old_versions(self, audio_id: str, keep: int = 10) -> int:
        """Diana 4.4: 保留最近 N 个版本, 删除旧的"""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM mv_intent_history
                WHERE audio_id = ? AND id NOT IN (
                    SELECT id FROM mv_intent_history
                    WHERE audio_id = ?
                    ORDER BY version DESC LIMIT ?
                )
                """,
                (audio_id, audio_id, keep),
            )
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"intent_repo: cleaned {deleted} old versions for {audio_id}")
            return deleted

    def cleanup_expired(self, retention_days: int = 90) -> int:
        """按时间清理过期记录 (Diana 4.4)"""
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM mv_intent_history WHERE created_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"intent_repo: cleaned {deleted} expired records (>{retention_days} days)")
            return deleted

    # ---------- 并发 (Diana 4.5) ----------

    def acquire_lock(self, signature: str) -> threading.Lock:
        """获取/创建 per-signature 锁 (Diana 4.5)

        两用户同时上传同一首歌时, 避免重复调 LLM
        """
        if not hasattr(_intent_locks, "locks"):
            _intent_locks.locks = {}
        if signature not in _intent_locks.locks:
            _intent_locks.locks[signature] = threading.Lock()
        return _intent_locks.locks[signature]


_intent_locks = threading.local()  # 进程内 per-thread 的锁字典


def get_intent_repository(db_path: Optional[str] = None) -> IntentRepository:
    """获取全局 repository 单例

    Args:
        db_path: 首次调用必须传, 后续可不传
    """
    return IntentRepository(get_db(db_path))