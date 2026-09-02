"""
mv — MV 模式核心服务包

简化为基础 video 生成
- 删 VideoPromptSchema (不再分段时间轴)
- 保留: IntentSchema + IntentRepository + validate_intent

子模块:
- db: SQLite 客户端 (2.5 + 4.4 全部吸收)
- intent_repository: 意境历史 CRUD (并发锁)
- mv_intent_schema: Pydantic JSON Schema 校验 ()
"""
from .intent_repository import IntentRecord, IntentRepository, get_intent_repository
from .mv_intent_schema import (
    IntentSchema,
    validate_intent,
    validate_intent_dict,
)

__all__ = [
    "IntentRecord",
    "IntentRepository",
    "get_intent_repository",
    "IntentSchema",
    "validate_intent",
    "validate_intent_dict",
]