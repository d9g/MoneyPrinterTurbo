"""
audio.mureka_prompts — AI 歌曲提示词生成器 (Mureka 适配)

- 方案 A: 在 MPT audio 服务下加新模块
- 字段动态生成 (按 features 选词条)
- 中英双版, 精简 + 详细切换
- 英文版意译 (Chinese folk singer style / raw intimate vocal quality)

输入: AudioFeatures (analyze_audio 输出)
输出: 4 个提示词字符串
  - zh_short / zh_long / en_short / en_long

核心思路 (戴拿 3.1 风格):
1. 每个维度 (人声/流派/乐器/节奏/调性/情绪/动态/演唱质感) 拆独立选词条函数
2. 字符串拼接时按 [人声]、[流派]、[乐器]、[节奏]、[调性]、[情绪]、[演唱质感] 顺序
3. 精简版 = 6 个核心维度 (人声/流派/乐器/节奏/调性/情绪)
4. 详细版 = 8 个维度 (含演唱质感 + 副歌特征 + 动态)
"""
from typing import Dict, List, Optional

from .models import AudioFeatures


# ================ 词库 ================

# 人声类型 (基于 PitchRange.mid_midi + 音域跨度)
VOCAL_CN = {
    "male/baritone":   "温暖男声",
    "male/tenor":      "高亢男声",
    "female/mezzo":    "温暖女声",
    "female/soprano":  "清亮女声",
    "instrumental":    "纯器乐无人声",
    "mixed":           "多人合唱",
}

VOCAL_EN = {
    "male/baritone":   "warm male baritone vocal",
    "male/tenor":      "bright male tenor vocal",
    "female/mezzo":    "warm female mezzo vocal",
    "female/soprano":  "bright female soprano vocal",
    "instrumental":    "instrumental, no vocals",
    "mixed":           "mixed ensemble vocals",
}

# 流派 (基于 genre + acousticness + valence)
GENRE_CN = {
    ("Ballad", "high"):      "慢板抒情",
    ("Ballad", "low"):       "深沉抒情",
    ("Pop", "high"):         "流行明快",
    ("Pop", "low"):          "流行忧郁",
    ("Electronic", "high"): "电子舞曲",
    ("Electronic", "low"):  "电子氛围",
}

GENRE_EN = {
    ("Ballad", "high"):      "slow ballad",
    ("Ballad", "low"):       "moody ballad",
    ("Pop", "high"):         "upbeat pop",
    ("Pop", "low"):          "melancholic pop",
    ("Electronic", "high"): "electronic dance",
    ("Electronic", "low"):  "electronic ambient",
}

# 乐器 (基于 dominant_instruments list)
INSTRUMENT_CN = {
    "acoustic guitar":  "木吉他分解和弦",
    "electric guitar":  "电吉他riff",
    "soft piano":       "钢琴流动",
    "bright piano":     "钢琴颗粒",
    "strings":          "弦乐铺底",
    "soft synth":       "合成器柔和",
    "bright synth":     "合成器高频",
    "drum machine":     "鼓机节拍",
    "light percussion": "轻打击乐",
    "acoustic bass":    "原声贝斯",
    "electric bass":    "电贝斯",
    "harmonica":        "口琴间奏",
}

INSTRUMENT_EN = {
    "acoustic guitar":  "acoustic guitar arpeggios",
    "electric guitar":  "electric guitar riff",
    "soft piano":       "flowing piano",
    "bright piano":     "bright piano notes",
    "strings":          "string pads",
    "soft synth":       "soft synthesizer",
    "bright synth":     "bright synth leads",
    "drum machine":     "drum machine beats",
    "light percussion": "light percussion",
    "acoustic bass":    "acoustic bass",
    "electric bass":    "electric bass",
    "harmonica":        "harmonica interlude",
}

# 节奏 (基于 BPM)
TEMPO_CN = {
    "Largo":       "庄严舒缓",
    "Adagio":      "从容柔和",
    "Andante":     "步行中板",
    "Moderato":    "适中节奏",
    "Allegro":     "活泼快板",
    "Presto":      "热烈急板",
    "Prestissimo": "极致爆裂",
}

TEMPO_EN = {
    "Largo":       "solemn and slow",
    "Adagio":      "gentle and calm",
    "Andante":     "moderate walking pace",
    "Moderato":    "moderate tempo",
    "Allegro":     "lively and fast",
    "Presto":      "fast and fiery",
    "Prestissimo": "extremely fast and intense",
}

# 调性 (基于 key_info.key + key_description)
KEY_MOOD_CN = {
    "major": "明快",
    "minor": "忧郁",
}

KEY_MOOD_EN = {
    "major": "bright",
    "minor": "melancholic",
}

# 情绪 (基于 mood_valence + mood_energy)
MOOD_CN = [
    # (min_valence, max_valence, min_energy, max_energy, cn_phrase)
    (0.7,  1.1,  0.7, 1.1, "活力四射"),
    (0.7,  1.1,  0.3, 0.7, "温暖治愈"),
    (0.7,  1.1, -0.1, 0.3, "安静平和"),
    (0.4,  0.7,  0.7, 1.1, "激情澎湃"),
    (0.4,  0.7,  0.3, 0.7, "怀旧追忆"),
    (0.4,  0.7, -0.1, 0.3, "忧伤沉思"),
    (0.0,  0.4,  0.7, 1.1, "激烈躁动"),
    (0.0,  0.4,  0.3, 0.7, "深沉忧郁"),
    (-0.1, 0.0, -0.1, 0.3, "绝望孤寂"),
]

MOOD_EN = [
    (0.7,  1.1,  0.7, 1.1, "energetic and uplifting"),
    (0.7,  1.1,  0.3, 0.7, "warm and healing"),
    (0.7,  1.1, -0.1, 0.3, "calm and peaceful"),
    (0.4,  0.7,  0.7, 1.1, "passionate and intense"),
    (0.4,  0.7,  0.3, 0.7, "nostalgic"),
    (0.4,  0.7, -0.1, 0.3, "contemplative and sad"),
    (0.0,  0.4,  0.7, 1.1, "turbulent and agitated"),
    (0.0,  0.4,  0.3, 0.7, "deeply melancholic"),
    (-0.1, 0.0, -0.1, 0.3, "desolate and lonely"),
]

# 演唱质感 (基于 acousticness + vocal_type)
ARTICULATION_CN = [
    # (min_acousticness, max_acousticness, vocal_type_pattern, cn_phrase)
    (0.6, 1.1, "*", "咬字自然不机械"),
    (0.0, 0.6, "male/*", "磁性沙哑"),
    (0.0, 0.6, "female/*", "电音修饰"),
    (0.0, 0.4, "*", "合成感强"),
    (0.4, 1.1, "male/*", "类似赵雷毛不易中国民谣风"),
    (0.4, 1.1, "female/*", "类似陈粒邵夷贝中国民谣风"),
]

ARTICULATION_EN = [
    (0.6, 1.1, "*", "natural articulation, not robotic"),
    (0.0, 0.6, "male/*", "raspy and magnetic timbre"),
    (0.0, 0.6, "female/*", "vocoder-processed vocal"),
    (0.0, 0.4, "*", "heavily synthesized vocal"),
    (0.4, 1.1, "male/*", "Chinese folk singer style, raw and intimate vocal quality"),
    (0.4, 1.1, "female/*", "Chinese folk singer style, raw and intimate vocal quality"),
]

# 副歌特征 (基于 chorus_segments + sections)
CHORUS_CN = {
    "dynamic": "副歌旋律上口",
    "calm":    "副歌层次递进",
    "absent":  "无明显副歌",
}

CHORUS_EN = {
    "dynamic": "memorable catchy chorus melody",
    "calm":    "layered progressive chorus",
    "absent":  "no distinct chorus structure",
}

# 动态 (基于 dynamic_range_db)
DYNAMIC_CN = {
    "p":  "细腻内敛",
    "mf": "张弛有度",
    "ff": "强动态爆发",
}

DYNAMIC_EN = {
    "p":  "delicate and restrained",
    "mf": "balanced dynamics",
    "ff": "powerful dynamic climax",
}


# ================ 选词函数 ================

def _vocal_pick(features: AudioFeatures) -> str:
    """选人声类型标签"""
    style = features.style
    if style is None:
        return "mixed"
    return style.vocal_type or "mixed"


def _genre_pick(features: AudioFeatures) -> str:
    """选流派标签"""
    style = features.style
    if not style or not style.genre:
        return "Pop"
    genre = style.genre
    valence = "high" if style.mood_valence >= 0.5 else "low"
    return GENRE_CN.get((genre, valence), "流行")


def _instrument_pick(features: AudioFeatures) -> List[str]:
    """选主乐器标签 (1-3 个)"""
    style = features.style
    if not style or not style.dominant_instruments:
        return []
    return [INSTRUMENT_CN.get(inst, inst) for inst in style.dominant_instruments[:3]]


def _tempo_pick(features: AudioFeatures) -> str:
    """选节奏描述 (中文)"""
    tempo = features.tempo
    return TEMPO_CN.get(tempo.tempo_italian, "中板")


def _tempo_en_pick(features: AudioFeatures) -> str:
    """选节奏描述 (英文)"""
    tempo = features.tempo
    return TEMPO_EN.get(tempo.tempo_italian, "moderate tempo")


def _key_mood_pick(features: AudioFeatures) -> str:
    """选调性情绪"""
    if not features.key_info or not features.key_info.key:
        return "明快"
    is_major = "major" in features.key_info.key
    return KEY_MOOD_CN["major" if is_major else "minor"]


def _mood_pick(features: AudioFeatures) -> str:
    """选情绪维度 (基于 valence + energy 双重判定)"""
    style = features.style
    if not style:
        return "温暖治愈"
    v = style.mood_valence
    e = style.mood_energy
    for min_v, max_v, min_e, max_e, phrase in MOOD_CN:
        if min_v <= v < max_v and min_e <= e < max_e:
            return phrase
    return "温暖治愈"


def _articulation_pick(features: AudioFeatures) -> str:
    """选演唱质感 (中文版可带具体人名, 英文版意译)

    优先级: 精确性别匹配 (male/*) > 通用匹配 (*)
    """
    style = features.style
    if not style:
        return "咬字自然不机械"
    ac = style.acousticness
    vt = style.vocal_type or "mixed"
    gender_part = vt.split("/", 1)[0] if "/" in vt else "*"
    pattern = f"{gender_part}/*"

    # 1. 精确匹配 (gender-specific)
    for min_ac, max_ac, vt_pattern, phrase in ARTICULATION_CN:
        if min_ac <= ac < max_ac and vt_pattern == pattern:
            return phrase
    # 2. 通用匹配 (通配 *)
    for min_ac, max_ac, vt_pattern, phrase in ARTICULATION_CN:
        if min_ac <= ac < max_ac and vt_pattern == "*":
            return phrase
    return "咬字自然不机械"


def _chorus_pick(features: AudioFeatures) -> str:
    """选副歌特征"""
    style = features.style
    if not style or not features.chorus_segments:
        return CHORUS_CN["absent"]
    # 有高潮段 + 强动态 → dynamic; 有高潮段 + 弱动态 → calm
    if features.dynamic and features.dynamic.dynamic_range_db > 20:
        return CHORUS_CN["dynamic"]
    return CHORUS_CN["calm"]


def _dynamic_pick(features: AudioFeatures) -> str:
    """选动态强度"""
    if not features.dynamic:
        return DYNAMIC_CN["mf"]
    return DYNAMIC_CN.get(features.dynamic.dynamic_mark, DYNAMIC_CN["mf"])


# ================ 提示词生成函数 ================

def generate_mureka_prompts(features: AudioFeatures) -> Dict[str, str]:
    """生成 4 个提示词字符串 (zh_short / zh_long / en_short / en_long)

    Args:
        features: AudioFeatures dataclass (from analyze_audio)

    Returns:
        {
            "zh_short": "温暖男声, 流行明快, 木吉他分解和弦, BPM 92, 大调明快, 怀旧治愈",
            "zh_long":  "温暖男声, 流行明快, 木吉他分解和弦, 副歌旋律上口, BPM 92(中板), 大调明快, 怀旧治愈, 强动态爆发, 咬字自然不机械, 类似赵雷毛不易中国民谣风, 带轻微笑意演唱",
            "en_short": "warm male vocal, upbeat pop, acoustic guitar arpeggios, BPM 92, bright major key, nostalgic healing",
            "en_long":  "warm male vocal, upbeat pop, acoustic guitar arpeggios, memorable catchy chorus melody, BPM 92 (moderate tempo), bright major key, nostalgic healing, powerful dynamic climax, natural articulation, Chinese folk singer style raw and intimate vocal quality, singing with a gentle smile",
        }
    """
    # 1. 提取各维度
    style = features.style
    if not style:
        # 兜底
        return {
            "zh_short": "流行明快, 中板节奏, 温暖治愈",
            "zh_long":  "流行明快, 中板节奏, 钢琴流动, 温暖治愈, 咬字自然不机械",
            "en_short": "upbeat pop, moderate tempo, warm and healing",
            "en_long":  "upbeat pop, moderate piano, moderate tempo, warm and healing, natural articulation",
        }

    vocal = VOCAL_CN.get(style.vocal_type, "人声演唱")
    genre = _genre_pick(features)
    instruments = _instrument_pick(features)
    tempo_phrase = _tempo_pick(features)
    bpm_val = features.tempo.bpm
    key_mood = _key_mood_pick(features)
    mood = _mood_pick(features)
    articulation = _articulation_pick(features)
    chorus = _chorus_pick(features)
    dynamic = _dynamic_pick(features)
    key_name = features.key_info.key_chinese if features.key_info else "—"
    bpm_phrase = f"BPM {bpm_val:.0f}"

    # 2. 拼装 (精简版 6 维度)
    zh_short_parts = [vocal, genre]
    if instruments:
        zh_short_parts.append(instruments[0])
    zh_short_parts.extend([bpm_phrase, f"{key_name}{key_mood}", mood])

    # 3. 拼装 (详细版 8 维度)
    zh_long_parts = [vocal, genre]
    zh_long_parts.extend(instruments)
    zh_long_parts.append(chorus)
    zh_long_parts.append(f"{bpm_phrase} ({features.tempo.tempo_class})")
    zh_long_parts.append(f"{key_name}{key_mood}")
    zh_long_parts.append(mood)
    zh_long_parts.append(dynamic)
    zh_long_parts.append(articulation)

    return {
        "zh_short": ", ".join(zh_short_parts),
        "zh_long":  ", ".join(zh_long_parts),
        "en_short": _build_en_short(features, vocal, genre, instruments, bpm_phrase, key_name, key_mood, mood),
        "en_long":  _build_en_long(features, vocal, genre, instruments, chorus, bpm_phrase, _tempo_en_pick(features), key_name, key_mood, mood, dynamic, articulation),
    }


def _build_en_short(features: AudioFeatures, vocal: str, genre: str, instruments: List[str], bpm: str, key: str, key_mood: str, mood: str) -> str:
    """英文精简版"""
    en_vocal = VOCAL_EN.get(features.style.vocal_type if features.style else "", "vocal")
    en_genre = _genre_en_pick(features)
    # 关键修复: 英文版乐器用中文映射表 → 英文词条 (而不是中文词条直接进英文版)
    en_instrument = INSTRUMENT_EN.get(_instrument_to_key(instruments[0]), instruments[0]) if instruments else ""
    en_key_mood = KEY_MOOD_EN.get("major" if "major" in features.key_info.key else "minor", "")
    en_mood = _mood_en_pick(features)
    en_key = features.key_info.key if features.key_info else "—"
    parts = [en_vocal, en_genre]
    if en_instrument:
        parts.append(en_instrument)
    parts.extend([bpm, f"{en_key}, {en_key_mood} tonal quality", en_mood])
    return ", ".join(parts)


def _build_en_long(features, vocal, genre, instruments, chorus, bpm, tempo, key, key_mood, mood, dynamic, articulation) -> str:
    """英文详细版"""
    style = features.style
    en_vocal = VOCAL_EN.get(style.vocal_type if style else "", "vocal")
    en_genre = _genre_en_pick(features)
    # 关键修复: 中文词条 → 反查英文 key → 映射 (如 "木吉他分解和弦" → "acoustic guitar" → "acoustic guitar arpeggios")
    en_instruments = [INSTRUMENT_EN.get(_instrument_to_key(i), i) for i in instruments]
    en_chorus = chorus  # 中文副歌特征本身就是短语,英文版需意译
    # 副歌特征英文映射
    en_chorus_map = {
        "副歌旋律上口": "memorable catchy chorus melody",
        "副歌层次递进": "layered progressive chorus",
        "无明显副歌": "no distinct chorus structure",
    }
    en_chorus_phrase = en_chorus_map.get(en_chorus, en_chorus)
    en_key_mood = KEY_MOOD_EN.get("major" if "major" in features.key_info.key else "minor", "")
    en_mood = _mood_en_pick(features)
    en_dynamic = DYNAMIC_EN.get(features.dynamic.dynamic_mark if features.dynamic else "mf", "")
    en_key = features.key_info.key if features.key_info else "—"
    # articulation 英文版: 同样从 ARTICULATION_EN 选
    en_articulation = _articulation_en_pick(features)

    parts = [en_vocal, en_genre]
    parts.extend(en_instruments)
    parts.append(en_chorus_phrase)
    parts.append(f"{bpm} ({tempo})")
    parts.append(f"{en_key}, {en_key_mood} tonal quality")
    parts.append(en_mood)
    parts.append(en_dynamic)
    parts.append(en_articulation)
    return ", ".join(parts)


def _instrument_to_key(cn_phrase: str) -> str:
    """反查中文词条 → 英文在 INSTRUMENT_EN 里的 key
    例: "木吉他分解和弦" → "acoustic guitar"
    """
    for key, phrase in INSTRUMENT_CN.items():
        if phrase == cn_phrase:
            return key
    return cn_phrase  # 兜底: 原文返回


def _genre_en_pick(features: AudioFeatures) -> str:
    """英文流派选词"""
    style = features.style
    if not style or not style.genre:
        return "pop"
    genre = style.genre
    valence = "high" if style.mood_valence >= 0.5 else "low"
    return GENRE_EN.get((genre, valence), "pop")


def _mood_en_pick(features: AudioFeatures) -> str:
    """英文情绪选词"""
    style = features.style
    if not style:
        return "warm and healing"
    v = style.mood_valence
    e = style.mood_energy
    for min_v, max_v, min_e, max_e, phrase in MOOD_EN:
        if min_v <= v < max_v and min_e <= e < max_e:
            return phrase
    return "warm and healing"


def _articulation_en_pick(features: AudioFeatures) -> str:
    """英文演唱质感选词 (优先级: 精确 > 通配)"""
    style = features.style
    if not style:
        return "natural articulation, not robotic"
    ac = style.acousticness
    vt = style.vocal_type or "mixed"
    gender_part = vt.split("/", 1)[0] if "/" in vt else "*"
    pattern = f"{gender_part}/*"
    # 1. 精确匹配
    for min_ac, max_ac, vt_pattern, phrase in ARTICULATION_EN:
        if min_ac <= ac < max_ac and vt_pattern == pattern:
            return phrase
    # 2. 通用匹配
    for min_ac, max_ac, vt_pattern, phrase in ARTICULATION_EN:
        if min_ac <= ac < max_ac and vt_pattern == "*":
            return phrase
    return "natural articulation, not robotic"
