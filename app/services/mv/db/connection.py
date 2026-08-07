"""
mv.db.connection — 线程安全的 DB 连接管理
老杨 2026-08-07 22:18 拍板

策略:
- 用 thread.local() 给每个线程一个连接
- 短事务 (单查询/单写入) 自动 commit
- 长事务用 context manager (`with db.transaction():`)
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from loguru import logger


_thread_local = threading.local()


class IntentDB:
    """MV 意境历史 DB 连接管理器

    Usage:
        db = IntentDB("/path/to/mv_intent.db")
        with db.transaction() as conn:
            conn.execute(...)
        row = db.fetch_one("SELECT ...", (param,))
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # 确保 DB 已初始化 (创建表)
        from .schema import init_db
        init_db(db_path)
        logger.info(f"IntentDB: ready at {db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接 (没则创建)"""
        if not hasattr(_thread_local, "connections"):
            _thread_local.connections = {}

        if self.db_path not in _thread_local.connections:
            conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row  # 默认返回 dict-like
            # PRAGMA: 开启 WAL 模式 (读写并发 + 性能)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            _thread_local.connections[self.db_path] = conn

        return _thread_local.connections[self.db_path]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行单条 SQL, 返回 lastrowid"""
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid or 0

    def executemany(self, sql: str, params_list: list) -> int:
        """批量执行"""
        with self.transaction() as conn:
            cursor = conn.executemany(sql, params_list)
            return cursor.rowcount

    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """查询单条"""
        conn = self._get_conn()
        cursor = conn.execute(sql, params)
        return cursor.fetchone()

    def fetch_all(self, sql: str, params: tuple = ()) -> list:
        """查询多条"""
        conn = self._get_conn()
        cursor = conn.execute(sql, params)
        return cursor.fetchall()

    @contextmanager
    def transaction(self):
        """事务上下文管理器 (自动 commit/rollback)"""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self):
        """关闭当前线程的连接"""
        if hasattr(_thread_local, "connections"):
            conn = _thread_local.connections.pop(self.db_path, None)
            if conn:
                conn.close()


# 全局单例 (懒加载)
_db_instance: Optional[IntentDB] = None
_db_lock = threading.Lock()


def get_db(db_path: Optional[str] = None) -> IntentDB:
    """获取全局 IntentDB 实例 (懒加载)

    Args:
        db_path: DB 路径, 首次调用必须传
    """
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                if db_path is None:
                    raise ValueError("db_path required on first call to get_db()")
                _db_instance = IntentDB(db_path)
    return _db_instance


def reset_db_instance() -> None:
    """重置全局实例 (用于测试)"""
    global _db_instance
    if _db_instance:
        _db_instance.close()
    _db_instance = None