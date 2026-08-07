"""
mv.db — MV 意境历史 SQLite 客户端
老杨 2026-08-07 22:18 拍板: 独立 SQLite (option A)

使用示例:
    from app.services.mv.db import get_db, init_db
    from app.services.mv.db.schema import init_db as init_schema
    
    # 1. 初始化 DB (创建表 + 索引 + 视图)
    init_schema("storage/mv/mv_intent.db")
    
    # 2. 获取 DB 实例
    db = get_db("storage/mv/mv_intent.db")
    
    # 3. 查询
    row = db.fetch_one("SELECT * FROM mv_intent_latest WHERE audio_id = ?", ("abc123",))
    
    # 4. 事务
    with db.transaction() as conn:
        conn.execute("INSERT INTO mv_intent_history ...")
"""
from .connection import IntentDB, get_db, reset_db_instance
from .schema import SCHEMA_VERSION, init_db

__all__ = [
    "IntentDB",
    "get_db",
    "reset_db_instance",
    "init_db",
    "SCHEMA_VERSION",
]