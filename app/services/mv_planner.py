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


# 老杨 2026-08-15 拍板: 兜底规则也走曲风 / 场景 一致性
_FALLBACK_GENRE_RULES = [
    # (检测函数, 曲风名, 中文关键词, 英文 Pexels 关键词)
    (
        lambda af, lyr: _detect_chinese_style(af, lyr),
        "国风 / 古风",
        ["古建筑", "水墨山水", "竹林", "古道", "纸灯"],
        ["chinese architecture", "ink painting", "bamboo forest", "river landscape", "ancient temple"],
        ["水墨黑", "淡青", "宣纸白", "琥珀金", "青瓦灰"],
    ),
    (
        lambda af, lyr: _detect_chinese_pop(af, lyr),
        "中式流行",
        ["中国城市", "灯笼街", "老上海", "天际线"],
        ["chinese city night", "lantern street", "oriental skyline", "modern oriental"],
        ["灯笼红", "暖橙", "霓虹暖黄", "都市灰蓝", "金"],
    ),
    (
        lambda af, lyr: _detect_electronic(af),
        "电子",
        ["赛博", "夜店", "舞台", "光影"],
        ["cyberpunk", "dj stage", "laser show", "futuristic neon"],
        ["赛博青", "霓虹粉", "深空黑", "电流紫", "锐红"],
    ),
    (
        lambda af, lyr: _detect_ballad(af),
        "抒情",
        ["雨天", "窗台", "离别", "孤灯"],
        ["rain window", "lonely street", "single lamp", "autumn leaves"],
        ["雨天冷蓝", "柔灰", "橙黄暖灯", "夜青", "古铜"],
    ),
    (
        lambda af, lyr: _detect_folk(af),
        "民谣",
        ["田园", "乡间", "草原", "原野"],
        ["countryside", "folk guitar", "meadow", "mountain village"],
        ["草原暖绿", "麦黄", "夕照橙", "土褐", "天青"],
    ),
    (
        lambda af, lyr: _detect_rock(af),
        "摇滚",
        ["现场", "舞台", "烟雾"],
        ["rock concert", "stage lights", "smoke machine"],
        ["烟雾白", "舞台紫", "锐灯金", "深黑", "泫红"],
    ),
]


def _detect_chinese_style(af: dict, lyrics: str) -> bool:
    """国风/古风检测: 五声音阶 + 中频主导 + 古典歌词关键词"""
    brightness = (af.get("spectral") or {}).get("brightness_hz", 0)
    lyrics_lower = (lyrics or "").lower()
    has_chinese_lyric_keywords = any(
        kw in lyrics for kw in ["古", "风", "山水", "江河", "万里", "长安", "千年", "江湖",
                                 "剑", "琴", "酒", "月", "青", "红", "梦", "红尘"]
    )
    # 五声音阶提示: brightness 较低 (<1500Hz) + 中文歌词有古典意象
    if brightness > 0 and brightness < 1500 and has_chinese_lyric_keywords:
        return True
    return False


def _detect_chinese_pop(af: dict, lyrics: str) -> bool:
    """中式流行检测: 中速 + 中文歌词 + 现代城市意象"""
    tempo_obj = af.get("tempo") or {}
    bpm = tempo_obj.get("bpm", 0)
    lyrics_has_urban = any(
        kw in (lyrics or "") for kw in ["城市", "街", "夜", "霓虹", "梦", "心", "爱"]
    )
    if 80 <= bpm <= 130 and lyrics_has_urban:
        return True
    return False


def _detect_electronic(af: dict) -> bool:
    """电子检测: 高 BPM (>120)"""
    tempo_obj = af.get("tempo") or {}
    return tempo_obj.get("bpm", 0) > 120


def _detect_ballad(af: dict) -> bool:
    """抒情检测: 慢 BPM (<80)"""
    tempo_obj = af.get("tempo") or {}
    return 0 < tempo_obj.get("bpm", 999) < 80


def _detect_folk(af: dict) -> bool:
    """民谣检测: 中低速 + 较高动态范围"""
    tempo_obj = af.get("tempo") or {}
    bpm = tempo_obj.get("bpm", 0)
    dynamic_obj = af.get("dynamic") or {}
    dynamic_db = dynamic_obj.get("dynamic_range_db", 0)
    return 60 <= bpm <= 110 and dynamic_db > 15


def _detect_rock(af: dict) -> bool:
    """摇滚检测: 中高速 (110-160 BPM)"""
    tempo_obj = af.get("tempo") or {}
    bpm = tempo_obj.get("bpm", 0)
    return 110 < bpm < 160


def _fallback_plan(audio_features: dict, lyrics: str) -> dict:
    """规则版降级方案 (LLM 失败 + 无缓存时使用)

    老杨 2026-08-15 拍板: 兜底也按曲风锁定场景, 不再给国风输出霓虹/城市词
    """
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

    # 老杨 2026-08-15: 按曲风锁定视觉符号 (不依赖 LLM)
    matched_genre = None
    cn_scene = []
    en_scene = []
    palette = ["暖金琥珀", "暮色蓝灰", "柔光奶白"]
    for detect_fn, genre_name, cn_kw, en_kw, pl in _FALLBACK_GENRE_RULES:
        if detect_fn(audio_features, lyrics or ""):
            matched_genre = genre_name
            cn_scene = cn_kw
            en_scene = en_kw
            palette = pl
            break

    # 未匹配曲风时, 兑底为流行 (最安全)
    if matched_genre is None:
        matched_genre = "流行"
        cn_scene = ["都市", "生活", "年轻"]
        en_scene = ["urban lifestyle", "young people", "city street"]
        palette = ["暖金琥珀", "暮色蓝灰", "柔光奶白"]

    # 老杨 8/9 08:19 拍板: 动态段数, 不硬凑 6 段
    if sections:
        n = len(sections)
    elif duration > 0:
        n = max(2, min(10, round(duration / 30)))
    elif lyrics_lines > 0:
        n = max(2, min(8, round(lyrics_lines / 7)))
    else:
        n = 4

    return {
        "mood_summary": (
            f"【{matched_genre}】基于 {key} 调性 + {tempo_class} 节奏, "
            f"整体氛围以 {', '.join(keywords[:3])} 为主, 视觉锁定 {', '.join(cn_scene[:3])}."
            + (f" 歌词提示: {lyrics[:50]}" if lyrics else "")
        ),
        "theme_keywords_cn": cn_scene,
        "theme_keywords_en": en_scene,
        "color_palette": palette,
        "transition_style": "fade",
        "subtitle_style": "bottom",
        "_source": "fallback_rule",
        "_fallback_reason": "LLM 调用失败且无缓存",
        "_detected_genre": matched_genre,
    }


# ================ PROMPTS ================

_SYSTEM_PROMPT = """你是 MV 意境综合分析师.
你的任务: 把音频特征 + 歌词翻译成具体的 MV 拍摄意境方案, 让摄影师/剪辑师能直接照做.

老杨 8/9 10:30 拍板: MV 模式简化为基础 video 生成
- 不再要求分段时间轴 (video_prompts 分段)
- LLM 只输出意境 + 关键词 + 调色, 关键词会被 Pexels/Pixabay/Coverr 用于搜索视频

老杨 2026-08-15 拍板: 曲风 / 场景 一致性
- 强制从「曲风候选词表」选一个作为意境主调, 不可绕过
- 视觉符号必须与曲风一致; 不许给国风歌曲出霓虹/城市/赛博场景
- theme_keywords_en 只返与曲风一致的视觉符号, Pexels/Pixabay 才搜得到

# 曲风候选词表 (必选 1 个主调, 可加 1 个修饰)

| 曲风 | 音频识别要点 | 中文视觉符号 | theme_keywords_en (Pexels 友好) |
|------|------------|------------|------------------------------|
| 国风 / 古风 | 五声音阶(do re mi sol la), 中频集中(<1500Hz), 戏腔/古筝/箫/二胡 | 古建筑/水墨/山水/竹林/古道/纸灯/汉服/青瓦 | chinese architecture, ink painting, bamboo forest, river landscape, ancient temple, hanfu, lantern, calligraphy |
| 中式 R&B / 中国风流行 | 大调+中国传统五声音阶, 现代节奏+民族音色 | 都市夜景/中国城市天际线/灯笼街/老上海 | chinese city night, lantern street, oriental skyline, modern oriental, asian metropolis |
| 流行 Pop | 标准大调, 高频亮, 鼓点突出 | 都市日常/年轻/城市/潮流 | pop city, young people, urban lifestyle, fashion runway, neon street |
| 电子 Electronic | 高频能量, 节奏快(>120BPM), 合成器特征 | 赛博/夜店/科技/光影 | cyberpunk, nightclub lights, dj stage, laser show, futuristic neon |
| 民谣 Folk | 中低速, 原声吉他/钢琴主导, 高动态 | 田园/乡间/原野/草原/老人 | countryside, folk guitar, field, meadow, mountain village, campfire |
| 抒情 Ballad | 慢速(<80BPM), 钢琴/弦乐, 大动态 | 雨天/窗台/离别/孤灯/落叶 | rain window, lonely street, autumn leaves, single lamp, parting moment |
| 摇滚 Rock | 中高速, 失真吉他, 强鼓点 | 现场/舞台/呐喊/烟雾 | rock concert, stage lights, smoke machine, guitar solo, crowd energy |
| 复古 Retro | 70/80s 音色, 模拟合成器, 律动明显 | 迪斯科/老电视/胶片/黄昏 | retro disco, vintage film, old tv, 80s neon, sunset car |
| 治愈 / 温柔 | 慢速, 弦乐+钢琴, 暖色频谱 | 阳光/咖啡/猫/花田/午后 | warm sunlight, cafe latte, cat sleeping, flower field, afternoon |
| 说唱 / 嘻哈 | 强节奏(80-120BPM), bass 重, 人声押韵 | 街头/涂鸦/潮牌/嘻哈文化 | street graffiti, hip hop culture, urban dance, lowrider, basketball court |

# theme_keywords_en 强约束 (Pexels 才能搜得到)
- 必须从对应曲风的 theme_keywords_en 列里选 5-10 个, 不许跨风格混
- 每个关键词必须是 Pexels 能搜出视频的具体名词, 不是抽象词
- 负向清单 (出现就重写):
  - ❌ "city night, neon lights, highway, running silhouette" 给国风/民谣/抒情 → 换成 chinese architecture / bamboo forest / rain window
  - ❌ "cyberpunk, dj stage, laser show" 给国风/民谣 → 换成中式符号
  - ❌ 抽象词 (emotion / feeling / vibe / atmosphere / mood) → Pexels 搜不出, 替换成具体景物
- 同义词扩展: 中式意境可用 "ancient chinese, oriental, eastern, zen, asian traditional" 增强搜索召回

# 严格输出要求 (按这个 JSON Schema)

```json
{
  "mood_summary": "100-300 字中文, 诗化但具体, 包含意像+情绪+节奏感",
  "theme_keywords_cn": ["中文关键词1", "中文关键词2", "中文关键词3", "中文关键词4", "中文关键词5"],
  "theme_keywords_en": ["English Keyword 1", "English Keyword 2", "English Keyword 3", "English Keyword 4", "English Keyword 5"],
  "color_palette": ["颜色名1", "颜色名2", "颜色名3", "颜色名4", "颜色名5"],
  "transition_style": "具体方案描述 (30字内)",
  "subtitle_style": "字幕样式描述 (50字内)"
}
```

# 重要约束
- 所有字段必须存在, 不能遗漏
- color_palette 是字符串数组 (中文颜色名), 不是对象
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

## 曲风识别 (必走流程, 不许跳)

**第一步: 判曲风** - 看音频特征 + 歌词双重验证
- 五声音阶(CDEGA) + 中频主导 + 古筝/箫/二胡音色 → **国风 / 古风**
- 大调 + 现代节奏 + 中国城市意象词 → **中式 R&B / 中国风流行**
- 标准大调 + 高频亮 + 鼓点强 + 都市歌词 → **流行 Pop**
- 高频亮 + >120BPM + 合成器 → **电子 Electronic**
- 中低速 + 原声吉他/钢琴 + 田园意象 → **民谣 Folk**
- <80BPM + 钢琴/弦乐 + 抒情意象 → **抒情 Ballad**
- 中高速 + 失真吉他 → **摇滚 Rock**
- 70/80s 音色 + 律动明显 → **复古 Retro**
- 慢速 + 暖色频谱 + 治愈意象 → **治愈**
- 强节奏 + bass 重 + 押韵 → **说唱**

**第二步: 锁视觉符号** - 按曲风候选词表的「theme_keywords_en」列选 5-10 个具体名词

**第三步: 自检** - 拿主题词反问: "给一个录影师, 他拿这些词能拍出匹配这首歌的镜头吗?"
- 国风 + "city night, neon lights" → 失败, 重写
- 民谣 + "dj stage, laser show" → 失败, 重写
- 抒情 + "rock concert, crowd energy" → 失败, 重写

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
3. 如果你认为上次已经足够好, 可以只做微调
4. 输出必须包含字段: mood_summary, theme_keywords_cn, theme_keywords_en, color_palette, transition_style, subtitle_style

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
        task_id: Optional[str] = None,    # 2026-08-09 P1-4: 关联 video task
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
                task_id=task_id,  # 2026-08-09 P1-4
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
        task_id: Optional[str] = None,  # 2026-08-09 P1-4
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
            task_id=task_id,  # 2026-08-09 P1-4: 关联 video task
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
        required = {"mood_summary", "theme_keywords_cn", "theme_keywords_en", "transition_style", "subtitle_style"}
        missing = required - set(data.keys())
        if missing:
            raise MvPlannerError(f"LLM 输出缺字段: {missing}")
        # 老杨 8/9 10:30 拍板: 不再要求分段时间轴 (video_prompts)
        # theme_keywords_en 是 Pexels 搜索关键词, theme_keywords_cn 是中文意境
        # 不需要再校验 video_prompts 数组
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