"""
MV 意境综合判断器 - LLM 进化版
老杨 2026-08-07 22:18 v2-5 重构 (Diana 3.3 + 2.5)

接收音频特征 + 歌词 + 历史, 输出 MV 制作方案.

v2 升级:
1. Diana 3.3: 进化 Prompt (带历史, 保留上次 + 增量优化)
2. Diana 2.3: Pydantic JSON Schema 校验
3. Diana 4.5: 重试 + 降级到最新历史
4. Diana 2.4: cost tracking (写库)
"""
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

from app.services import llm as llm_service
from app.services.mv import (
    IntentRecord,
    IntentRepository,
    IntentSchema,
    get_intent_repository,
    validate_intent,
)
from app.services.mv.db import get_db


class MvPlannerError(Exception):
    """MV 规划失败 (LLM 错误 / Schema 校验失败 / 输入数据缺失)"""


@dataclass
class LLMCallResult:
    """LLM 调用结果 (含元数据)"""
    raw_text: str
    parsed: dict
    schema: Optional[IntentSchema]
    model: Optional[str]
    latency_ms: int
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


# ================ FALLBACK (规则降级) ================

_FALLBACK_TEMPO_MOOD = {
    "极慢板": ["庄严", "冥想", "悠远"],
    "慢板":   ["温柔", "内省", "诗意"],
    "中板":   ["流动", "温暖", "自然"],
    "小快板": ["明快", "活泼", "轻盈"],
    "快板":   ["热烈", "奔放", "活力"],
    "急板":   ["急速", "爆裂", "冲击"],
    "狂板":   ["极快", "混沌", "高潮"],
}


_FALLBACK_KEYWORD_EN_MAP = {
    "庄严": "solemn", "冥想": "meditation", "悠远": "ethereal",
    "温柔": "gentle", "内省": "introspective", "诗意": "poetic",
    "流动": "flowing", "温暖": "warm", "自然": "natural",
    "明快": "bright", "活泼": "lively", "轻盈": "light",
    "热烈": "passionate", "奔放": "exuberant", "活力": "energetic",
    "急速": "rapid", "爆裂": "explosive", "冲击": "impact",
    "极快": "extreme", "混沌": "chaotic", "高潮": "climax",
    "节奏": "rhythm", "歌词": "lyrics", "音乐": "music",
}


def _translate_keywords_to_en(cn_keywords: list[str]) -> list[str]:
    """中文关键词 → 英文 (规则映射, LLM 不可用时兑底)"""
    out = []
    for kw in cn_keywords:
        translated = _FALLBACK_KEYWORD_EN_MAP.get(kw)
        if translated:
            out.append(translated)
        else:
            # 兑底: 保留中文 + 拼一个通用词
            out.append(f"{kw} scene")
    return out


def _fallback_plan(audio_features: dict, lyrics: str) -> dict:
    """规则版降级方案 (LLM 失败 + 无缓存时使用)"""
    # 审计 P2-5: 任何字段缺失/异常都兑底, 不抛 KeyError
    tempo_obj = audio_features.get("tempo") or {}
    tempo_class_raw = tempo_obj.get("tempo_class", "") if isinstance(tempo_obj, dict) else ""
    tempo_class = tempo_class_raw.split(" ")[0] if tempo_class_raw else "节奏"
    key_obj = audio_features.get("key_info") or {}
    key = key_obj.get("key", "C") if isinstance(key_obj, dict) else "C"
    keywords = _FALLBACK_TEMPO_MOOD.get(tempo_class, ["节奏"])
    sections = audio_features.get("sections", [])
    duration = float(audio_features.get("duration") or 0)
    lyrics_lines = len([l for l in (lyrics or "").splitlines() if l.strip()])

    # 老杨 8/9 08:19 拍板: 动态段数, 不硬凑 6 段
    # 规则: 音频段落存在用其长度, 否则按 ~30 秒/段估算, 同时考虑歌词行数 (每 6-8 行一段)
    if sections:
        n = len(sections)
    elif duration > 0:
        # ~30 秒一段 (4 分钟歌 ~8 段), 最少 2 段, 最多 10 段
        n = max(2, min(10, round(duration / 30)))
    elif lyrics_lines > 0:
        # 每 6-8 行歌词一段
        n = max(2, min(8, round(lyrics_lines / 7)))
    else:
        n = 4  # 兑底

    return {
        "mood_summary": (
            f"基于 {key} 调性 + {tempo_class} 节奏, 整体氛围以 {', '.join(keywords[:3])} 为主."
            + (f" 歌词提示: {lyrics[:50]}" if lyrics else "")
        ),
        "theme_keywords_cn": keywords + (["歌词"] if lyrics else ["音乐"]),
        "theme_keywords_en": _translate_keywords_to_en(keywords) + (["lyrics"] if lyrics else ["music"]),
        "color_palette": ["暖金琥珀", "暮色蓝灰", "柔光奶白"],
        "video_prompts": [
            {
                "section_index": i,
                "label": sections[i].get("intensity", "medium") if i < len(sections) else f"段落{i+1}",
                "prompt": f"{keywords[0]} {tempo_class} cinematic shot",
                "style": f"{keywords[0]} cinematic, soft natural light",
            }
            for i in range(n)
        ],
        "transition_style": "fade",
        "subtitle_style": "bottom",
        "_source": "fallback_rule",
        "_fallback_reason": "LLM 调用失败且无缓存",
    }


# ================ PROMPTS ================

_SYSTEM_PROMPT = """你是 MV 意境综合分析师.
你的任务: 把音频特征 + 歌词翻译成具体的 MV 拍摄意境方案, 让摄影师/剪辑师能直接照做.

# 严格输出要求 (按这个 JSON Schema)

```json
{
  "mood_summary": "100-300 字中文, 诗化但具体, 包含意像+情绪+节奏感",
  "theme_keywords_cn": ["中文关键词1", "中文关键词2", "中文关键词3", "中文关键词4", "中文关键词5"],
  "theme_keywords_en": ["English Keyword 1", "English Keyword 2", "English Keyword 3", "English Keyword 4", "English Keyword 5"],
  "color_palette": ["颜色名1", "颜色名2", "颜色名3", "颜色名4", "颜色名5"],
  "video_prompts": [
    {"section_index": 0, "label": "前奏", "prompt": "english keyword for pexels", "style": "中文风格描述"},
    {"section_index": 1, "label": "主歌A", "prompt": "english keyword", "style": "中文风格描述"}
  ],
  "transition_style": "具体方案描述 (30字内)",
  "subtitle_style": "字幕样式描述 (50字内)"
}
```

# 重要约束
- 所有字段必须存在, 不能遗漏
- color_palette 是字符串数组 (中文颜色名), 不是对象
- video_prompts 是数组, 每个元素必须含 section_index/label/prompt/style 四个字段
- **video_prompts 段数: 根据歌词情节自然划分 (老杨 8/9 08:19 拍板).**
  - **不要硬凑 6 段**. 歌曲有几句歌词、几个情绪转折, 就出几段.
  - 建议参考: 叙事/抒情 歌曲 3-5 段, 完整故事歌曲 6-10 段, 短歌/Intro-Outro 1-3 段.
  - 数组长度最少 1 段, 最多 12 段 (超过会被裁剪).
  - 每段对应歌词的一个情节/情绪/场景, 不重复不重叠.
  - section_index 从 0 连续递增到 N-1, 不能跳过.
- **theme_keywords_cn 是中文关键词数组，5-10 个, 每个 2-4 字中文**
- **theme_keywords_en 是对应英文 Pexels 搜索关键词，5-10 个, 每个 1-3 词英文（用于素材库检索, 必须返回字符串数组）**
- 不要包含 hex 颜色代码, 只给中文颜色名
- 不要输出 Markdown 代码块包裹, 直接输出 JSON

只用 ```json 包裹, 不要其他 Markdown 装饰."""


_USER_PROMPT_FIRST = """# 首次分析 (无历史)

## 歌曲音频特征
- 时长: {duration_seconds}s
- 节奏: {bpm} BPM ({tempo_class})
- 调性: {key} (置信度 {key_confidence})
- 音域: {low_note} - {high_note} (跨 {range_semitones} 个半音)
- 动态: {dynamic_db} dB
- 频谱亮度: {brightness_hz} Hz
- 段落数: {section_count}

## 段落详情
{section_table}

## 歌词
{lyrics_block}

请输出完整 MV 意境方案 (JSON).
"""


_USER_PROMPT_EVOLUTION = """# 进化模式 (Diana 3.3)

## 歌曲音频特征
- 时长: {duration_seconds}s
- 节奏: {bpm} BPM ({tempo_class})
- 调性: {key} (置信度 {key_confidence})
- 音域: {low_note} - {high_note} (跨 {range_semitones} 个半音)
- 动态: {dynamic_db} dB
- 频谱亮度: {brightness_hz} Hz

## 歌词
{lyrics_block}

## 你之前对这个歌曲的 {history_count} 次分析
{history_table}

## 你的任务 (进化, 不是重做)
1. 保留上次分析中好的部分 (不要推翻重来)
2. 针对以下方面做增量优化:
   - 更精准的意境表达 (结合历史脉络)
   - 更丰富的关键词
   - 更贴合歌词的段落切分
   - 更符合视频拍摄建议的 prompts
3. 如果你认为上次已经足够好, 可以只做微调
4. 输出必须包含字段: mood_summary, theme_keywords_cn, theme_keywords_en, color_palette, video_prompts, transition_style, subtitle_style

请输出最新版本 (JSON).
"""


# ================ MV PLANNER ================

class MvPlanner:
    """MV 意境综合判断器 (v2 重构版)

    Usage:
        planner = MvPlanner()
        plan = planner.build(
            audio_features=features_dict,
            lyrics="...",
            audio_id="audio_001",
            song_signature="meta:老杨::测试歌::180.0",
            duration_seconds=180.0,
        )
    """

    def __init__(
        self,
        app_config: Optional[dict] = None,
        repo: Optional[IntentRepository] = None,
        db_path: Optional[str] = None,
        max_retries: int = 1,            # Diana 4.5: 重试次数
        retry_delay_seconds: float = 3.0,
    ):
        self.app_config = app_config
        self.repo = repo or get_intent_repository(db_path)
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    # ---------- 主入口 ----------

    def build(
        self,
        audio_features: dict,
        lyrics: str = "",
        audio_id: Optional[str] = None,
        song_signature: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        history: Optional[List[IntentRecord]] = None,
        user_id: Optional[str] = None,    # Diana 2.4 预留字段, 当前不接业务
    ) -> dict:
        """主入口

        Args:
            audio_features: AudioFeatures.to_dict() 输出
            lyrics: 歌词文本
            audio_id, song_signature, duration_seconds: 写库标识 (None 时不写库)
            artist, title: 歌曲元数据
            history: 历史记录 (None 时自动查库取最近 3 条)
            user_id: 预留 (当前不用)

        Returns:
            plan dict (含 _source, _version, _latency_ms 等元数据)
        """
        # 1. 准备 prompt
        if history is None and audio_id:
            history = self.repo.get_recent_for_evolution(audio_id, n=3)

        prompt = self._build_prompt(audio_features, lyrics, history)

        # 2. 调用 LLM (含重试 + 降级)
        history_json = json.dumps(
            [self._history_to_dict(h) for h in (history or [])],
            ensure_ascii=False,
        )

        try:
            llm_result = self._call_llm_with_retry(prompt)
            plan = llm_result.parsed
            plan["_source"] = "llm"
            plan["_latency_ms"] = llm_result.latency_ms
            plan["_llm_model"] = llm_result.model
        except Exception as exc:
            logger.warning(f"mv_planner: LLM 调用全部失败: {exc}")
            return self._fallback_with_cache(
                audio_features, lyrics, song_signature, exc, history,
            )

        # 3. 写库 (Diana 2.5: is_latest 维护)
        if audio_id and song_signature is not None and duration_seconds is not None:
            self._save_intent(
                audio_id=audio_id,
                song_signature=song_signature,
                duration_seconds=duration_seconds,
                intent=plan,
                source="llm",
                user_id=user_id,
                artist=artist,
                title=title,
                prompt_history_json=history_json,
                llm_result=llm_result,
            )
            plan["_version"] = self.repo.count_versions(audio_id)
        else:
            plan["_version"] = 0  # 没接 audio_id 时不写库

        return plan

    # ---------- LLM 调用 (重试 + Schema 校验) ----------

    def _call_llm_with_retry(self, prompt: str) -> LLMCallResult:
        """Diana 4.5: 重试 + Schema 校验

        Raises:
            MvPlannerError: 所有重试都失败
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                raw = llm_service._generate_response(prompt, app_config=self.app_config)
                latency_ms = int((time.time() - start) * 1000)

                # 解析 + Schema 校验
                parsed = self._parse_json_response(raw)
                schema = validate_intent(json.dumps(parsed, ensure_ascii=False))

                if schema is None:
                    raise MvPlannerError(f"Schema 校验失败 (attempt {attempt+1})")

                model = self._detect_model()
                return LLMCallResult(
                    raw_text=raw,
                    parsed=parsed,
                    schema=schema,
                    model=model,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(f"mv_planner: LLM attempt {attempt+1} failed: {exc}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds)

        raise MvPlannerError(f"LLM 调用失败 {self.max_retries+1} 次: {last_error}")

    def _detect_model(self) -> Optional[str]:
        """检测当前 LLM 模型"""
        try:
            from app.config import config
            provider = str(self.app_config.get("llm_provider", "minimax") if self.app_config else config.app.get("llm_provider", "minimax"))
            model_keys = {
                "openai": "openai_model_name",
                "moonshot": "moonshot_model_name",
                "gemini": "gemini_model_name",
                "deepseek": "deepseek_model_name",
            }
            model_key = model_keys.get(provider, f"{provider}_model_name")
            cfg = self.app_config or config.app
            model = cfg.get(model_key) or cfg.get(f"{provider}_model")
            return str(model) if model else provider
        except Exception:
            return None

    # ---------- 降级 ----------

    def _fallback_with_cache(
        self,
        audio_features: dict,
        lyrics: str,
        song_signature: Optional[str],
        llm_error: Exception,
        history: Optional[List[IntentRecord]],
    ) -> dict:
        """降级策略 (Diana 4.5)

        1. 有 history → 用最新一条 (标记 cache_fallback)
        2. 无 history → 用规则版 fallback
        """
        # 优先用 history 第一条
        if history:
            latest = history[0]
            plan = dict(latest.intent)
            plan["_source"] = "cache_fallback"
            plan["_llm_error"] = str(llm_error)
            plan["_latency_ms"] = 0
            plan["_llm_model"] = None
            plan["_version"] = latest.version
            logger.info(f"mv_planner: 降级到 cache (v{latest.version})")
            return plan

        # 完全无缓存 → 规则 fallback
        plan = _fallback_plan(audio_features, lyrics)
        plan["_llm_error"] = str(llm_error)
        logger.info("mv_planner: 降级到规则 fallback (无缓存)")
        return plan

    # ---------- 写库 ----------

    def _save_intent(
        self,
        audio_id: str,
        song_signature: str,
        duration_seconds: float,
        intent: dict,
        source: str,
        llm_result: LLMCallResult,
        user_id: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        prompt_history_json: Optional[str] = None,
        llm_error: Optional[str] = None,
    ) -> int:
        """写库 (Diana 2.5 is_latest 维护)"""
        # 计算下一个 version 号
        next_version = self.repo.count_versions(audio_id) + 1

        return self.repo.insert_history(
            audio_id=audio_id,
            song_signature=song_signature,
            duration_seconds=duration_seconds,
            version=next_version,
            intent_json=json.dumps(intent, ensure_ascii=False),
            source=source,
            user_id=user_id,
            artist=artist,
            title=title,
            llm_error=llm_error,
            prompt_history_json=prompt_history_json,
            llm_model=llm_result.model if llm_result else None,
            llm_latency_ms=llm_result.latency_ms if llm_result else None,
            prompt_tokens=llm_result.prompt_tokens if llm_result else None,
            completion_tokens=llm_result.completion_tokens if llm_result else None,
            cost_usd=llm_result.cost_usd if llm_result else None,
        )

    # ---------- Prompt 构建 ----------

    def _build_prompt(
        self,
        audio_features: dict,
        lyrics: str,
        history: Optional[List[IntentRecord]],
    ) -> str:
        """根据是否有 history 选择 first/evolution prompt"""
        sections = audio_features.get("sections", [])
        section_rows = []
        for i, sec in enumerate(sections):
            section_rows.append(
                f"  - 段 {i}: {sec['start']:.1f}-{sec['end']:.1f} 秒 "
                f"(时长 {sec['duration']:.1f}s, 强度={sec['intensity']})"
            )
        section_table = "\n".join(section_rows) if section_rows else "  (无段落信息)"
        lyrics_block = lyrics.strip() if lyrics and lyrics.strip() else "  (用户未提供歌词, 全部依赖曲调特征)"

        if not history:
            user_prompt = _USER_PROMPT_FIRST.format(
                duration_seconds=audio_features["duration_seconds"],
                bpm=audio_features["tempo"]["bpm"],
                tempo_class=audio_features["tempo"]["tempo_class"],
                key=audio_features["key_info"]["key"],
                key_confidence=audio_features["key_info"]["confidence"],
                low_note=audio_features["pitch_range"]["low_note"],
                high_note=audio_features["pitch_range"]["high_note"],
                range_semitones=audio_features["pitch_range"]["range_semitones"],
                dynamic_db=audio_features["dynamic"]["dynamic_range_db"],
                brightness_hz=audio_features["spectral"]["brightness_hz"],
                section_count=len(sections),
                section_table=section_table,
                lyrics_block=lyrics_block,
            )
        else:
            history_table = "\n\n".join([
                f"[version {h.version}] {h.created_at}\n"
                f"意境: {h.intent.get('mood_summary', '')}\n"
                f"中文关键词: {h.intent.get('theme_keywords_cn', [])}\n"
                f"英文关键词: {h.intent.get('theme_keywords_en', [])}\n"
                f"配色: {h.intent.get('color_palette', [])}"
                for h in history[:3]
            ])
            user_prompt = _USER_PROMPT_EVOLUTION.format(
                duration_seconds=audio_features["duration_seconds"],
                bpm=audio_features["tempo"]["bpm"],
                tempo_class=audio_features["tempo"]["tempo_class"],
                key=audio_features["key_info"]["key"],
                key_confidence=audio_features["key_info"]["confidence"],
                low_note=audio_features["pitch_range"]["low_note"],
                high_note=audio_features["pitch_range"]["high_note"],
                range_semitones=audio_features["pitch_range"]["range_semitones"],
                dynamic_db=audio_features["dynamic"]["dynamic_range_db"],
                brightness_hz=audio_features["spectral"]["brightness_hz"],
                lyrics_block=lyrics_block,
                history_count=len(history),
                history_table=history_table,
            )

        return user_prompt + "\n\n" + _SYSTEM_PROMPT

    @staticmethod
    def _history_to_dict(h: IntentRecord) -> dict:
        """history record → dict (用于 JSON 序列化 audit)"""
        return {
            "version": h.version,
            "created_at": h.created_at,
            "mood_summary": h.intent.get("mood_summary"),
            "theme_keywords_cn": h.intent.get("theme_keywords_cn"),
            "theme_keywords_en": h.intent.get("theme_keywords_en"),
            "source": h.source,
        }

    # ---------- 解析 ----------

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """LLM 输出可能带 Markdown 包裹, 尝试剥离后 parse"""
        if not raw:
            raise MvPlannerError("LLM 返回空")
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if match:
            text = match.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MvPlannerError(f"LLM 输出非 JSON: {exc}; raw={text[:200]}") from exc
        required = {"mood_summary", "theme_keywords_cn", "theme_keywords_en", "video_prompts", "transition_style", "subtitle_style"}
        missing = required - set(data.keys())
        if missing:
            raise MvPlannerError(f"LLM 输出缺字段: {missing}")
        # 老杨 8/9 08:19 拍板: 动态段数 - 限制范围 [1, 12], 超过裁剪
        prompts = data.get("video_prompts") or []
        if not isinstance(prompts, list):
            raise MvPlannerError(f"video_prompts 不是数组: {type(prompts)}")
        if len(prompts) == 0:
            raise MvPlannerError("video_prompts 为空")
        if len(prompts) > 12:
            logger.warning(
                f"video_prompts {len(prompts)} 段超 12 上限, 裁剪到 12"
            )
            prompts = prompts[:12]
        # 重排 section_index 确保连续 0..N-1
        for i, p in enumerate(prompts):
            if isinstance(p, dict):
                p["section_index"] = i
        data["video_prompts"] = prompts
        return data


# ================ CLI 测试 ================

def _cli():
    """用法: python -m app.services.mv_planner <audio_features.json> [lyrics.txt]"""
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m app.services.mv_planner <audio_features.json> [lyrics.txt]")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        audio_features = json.load(f)
    lyrics = ""
    if len(sys.argv) >= 3 and Path(sys.argv[2]).exists():
        with open(sys.argv[2]) as f:
            lyrics = f.read()
    planner = MvPlanner()
    plan = planner.build(audio_features, lyrics)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()