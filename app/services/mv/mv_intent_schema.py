"""
mv.mv_intent_schema — LLM 输出 JSON Schema 校验
Diana 审计 2.3 新增 (P0)

Pydantic 模型定义 + validate_intent() 函数
LLM 输出不符合 schema 时返回 None, 触发降级
"""
from typing import List, Optional

from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class VideoPromptSchema(BaseModel):
    """单段视频 prompt"""
    section_index: int = Field(..., ge=0)
    label: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    style: str = Field(..., min_length=1)


class IntentSchema(BaseModel):
    """MV 意境方案完整 schema"""
    mood_summary: str = Field(..., min_length=10, max_length=500)
    theme_keywords: List[str] = Field(..., min_length=3, max_length=15)
    color_palette: List[str] = Field(..., min_length=3, max_length=8)
    video_prompts: List[VideoPromptSchema] = Field(..., min_length=1)
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