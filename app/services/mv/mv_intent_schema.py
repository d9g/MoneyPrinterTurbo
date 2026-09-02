"""
mv.mv_intent_schema — LLM 输出 JSON Schema 校验
MV 模式回退到基础 video 生成
LLM 只输出意境 + 中英关键词 + 调色, 不再要求 video_prompts 分段

Pydantic 模型定义 + validate_intent() 函数
LLM 输出不符合 schema 时返回 None, 触发降级
"""
from typing import List, Optional

from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class IntentSchema(BaseModel):
    """MV 意境方案完整 schema (简化版)

    校验规则:
    - 必须存在 theme_keywords_cn + theme_keywords_en
    - video_prompts 不再必填 (不要分段设计)
    - color_palette + transition_style + subtitle_style 保留 (UI 显示用)
    """
    model_config = {"populate_by_name": True}

    mood_summary: str = Field(..., min_length=10, max_length=500)
    theme_keywords_cn: List[str] = Field(..., min_length=3, max_length=15)
    theme_keywords_en: List[str] = Field(..., min_length=3, max_length=15)
    # 旧字段仅兑底 (旧任务中可能的旧主题关键词, 留给老数据兼容)
    theme_keywords: Optional[List[str]] = Field(default=None, description="Legacy field, kept for backward compatibility")
    color_palette: List[str] = Field(..., min_length=3, max_length=8)
    transition_style: str = Field(..., min_length=1)
    subtitle_style: str = Field(..., min_length=1)


def validate_intent(raw_json: str) -> Optional[IntentSchema]:
    """校验 LLM 输出的 JSON

    Args:
        raw_json: LLM 返回的 JSON 字符串

    Returns:
        IntentSchema 实例 (校验通过) 或 None (校验失败触发降级)
    """
    try:
        return IntentSchema.model_validate_json(raw_json)
    except ValidationError as e:
        logger.warning(f"intent_schema: LLM output validation failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"intent_schema: unexpected error: {e}")
        return None


def validate_intent_dict(raw: dict) -> Optional[IntentSchema]:
    """校验 dict (LLM 返回已 parse_json 的情况)"""
    try:
        return IntentSchema.model_validate(raw)
    except ValidationError as e:
        logger.warning(f"intent_schema: validation failed: {e}")
        return None