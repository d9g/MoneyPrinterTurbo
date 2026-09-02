"""
mv.db.schema — MV 意境历史数据库 schema
独立 SQLite (option A)
审计项 2.4 + 2.5 + 4.4 全部吸收

包含 1 张表 + 1 个视图:
- mv_intent_history: 意境历史 (每首歌多次解析)
- mv_intent_latest: 最新版本视图 (ROW_NUMBER 窗口函数, SQLite 3.25+)

字段包括:
- 基础: audio_id, song_signature, user_id, artist, title, duration_seconds
- 版本: version, is_latest (维护标记, 避免相关子查询)
- LLM 输出: intent_json (完整 JSON), source, llm_error
- 审计: prompt_history_json, llm_model, llm_latency_ms
- 成本: prompt_tokens, completion_tokens, cost_usd ()
- 时间: created_at (CST, +8 hours)
"""
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = "2026-08-09.v2"  # 2026-08-09 加 task_id 字段 (P1-4)


# ================ CREATE TABLE ================

CREATE_INTENT_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS mv_intent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 2026-08-09 P1-4: 关联 video task (mv_intent_history ↔ state.tasks)
    task_id TEXT,                    -- FastAPI 生成的 task_id, 可空 (老数据)

    -- 用户标识
    user_id TEXT,

    -- 音频标识
    audio_id TEXT NOT NULL,
    song_signature TEXT NOT NULL,    -- 来自 audio.fingerprint.compute_song_signature
    artist TEXT,
    title TEXT,
    duration_seconds REAL NOT NULL,

    -- 版本控制 (is_latest 字段)
    version INTEGER NOT NULL,
    is_latest INTEGER DEFAULT 0,

    -- LLM 输出
    intent_json TEXT NOT NULL,      -- 完整 JSON
    source TEXT NOT NULL,           -- 'llm' / 'cache_fallback' / 'user_edit'
    llm_error TEXT,                 -- 降级时的错误信息

    -- Prompt 审计
    prompt_history_json TEXT,       -- 上送 LLM 的 history (用于审计/debug)

    -- LLM 元数据 (token 成本)
    llm_model TEXT,
    llm_latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL,

    -- 时间 (东八区)
    created_at DATETIME DEFAULT (datetime('now', '+8 hours')),

    UNIQUE(audio_id, version)
);
"""


# ================ CREATE INDEX ================

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_intent_audio ON mv_intent_history(audio_id);",
    "CREATE INDEX IF NOT EXISTS idx_intent_signature ON mv_intent_history(song_signature);",
    # 用户索引
    "CREATE INDEX IF NOT EXISTS idx_intent_user ON mv_intent_history(user_id, song_signature);",
    # 加快最新版本查询
    "CREATE INDEX IF NOT EXISTS idx_intent_latest ON mv_intent_history(audio_id, is_latest);",
    # 2026-08-09 P1-4: task_id 索引 (WebUI 弹窗查询)
    "CREATE INDEX IF NOT EXISTS idx_intent_task ON mv_intent_history(task_id);",
    # 时间索引 (用于 retention 清理)
    "CREATE INDEX IF NOT EXISTS idx_intent_created ON mv_intent_history(created_at);",
]


# ================ CREATE VIEW ================

CREATE_LATEST_VIEW = """
-- 用 ROW_NUMBER() 窗口函数, 避免相关子查询的性能问题
-- 需要 SQLite 3.25+, 项目要求 Python 3.11+ 自带 3.37+
CREATE VIEW IF NOT EXISTS mv_intent_latest AS
SELECT * FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY audio_id
            ORDER BY version DESC
        ) AS rn
    FROM mv_intent_history
) WHERE rn = 1;
"""


# ================ META TABLE (schema 版本) ================

CREATE_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS mv_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ================ INIT QUERIES ================

INIT_QUERIES = [
    CREATE_INTENT_HISTORY_TABLE,
    CREATE_SCHEMA_META,
    *CREATE_INDEXES,
    CREATE_LATEST_VIEW,
]


def init_db(db_path: str) -> None:
    """初始化 DB (创建表 + 索引 + 视图)

    Args:
        db_path: SQLite 文件路径 (会自动创建父目录)
    """
    import sqlite3
    from loguru import logger

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        # 2026-08-09 P1-4: 旧表 ADD COLUMN task_id (schema=v1 不存 task_id)
        # SQLite 不支持 IF NOT EXISTS for ADD COLUMN, 先检查列是否存在
        cur = conn.execute("PRAGMA table_info(mv_intent_history)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "task_id" not in existing_cols:
            conn.execute("ALTER TABLE mv_intent_history ADD COLUMN task_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_intent_task "
                "ON mv_intent_history(task_id)"
            )
            logger.info("migration: added task_id column to mv_intent_history")

        for query in INIT_QUERIES:
            conn.execute(query)
        # 记录 schema 版本
        conn.execute(
            "INSERT OR REPLACE INTO mv_schema_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()

    logger.info(f"mv_intent_db: initialized at {db_path} (schema={SCHEMA_VERSION})")


def get_db_path(config) -> str:
    """从 config 读取 DB 路径 (默认 storage/mv/mv_intent.db)"""
    return config.app.get("mv_intent_db_path", "storage/mv/mv_intent.db")


# 模块级 fixture (供测试用)
def create_test_db() -> str:
    """创建临时测试 DB (用 :memory: 或 tmpfile)"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    return path