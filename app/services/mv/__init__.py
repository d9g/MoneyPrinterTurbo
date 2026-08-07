"""
mv — MV 模式核心服务包
老杨 2026-08-07 22:18 拍板

子模块:
- db: SQLite 客户端 (Diana 2.4 + 2.5 + 4.4 全部吸收)
- intent_repository: 意境历史 CRUD (Diana 4.5 并发锁)
- mv_intent_schema: Pydantic JSON Schema 校验 (Diana 2.3)
"""
from .intent_repository import IntentRecord, IntentRepository, get_intent_repository
from .mv_intent_schema import (
    IntentSchema,
    VideoPromptSchema,
    validate_intent,
    validate_intent_dict,
)

__all__ = [
    "IntentRecord",
    "IntentRepository",
    "get_intent_repository",
    "IntentSchema",
    "VideoPromptSchema",
    "validate_intent",
    "validate_intent_dict",
]