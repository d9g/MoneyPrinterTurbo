"""
audio.features.vocab — 音乐术语映射表
Diana 审计 3.2 新增

把物理参数 (BPM/调性/动态) 翻译成专业术语, 让 LLM 和用户都能直接用

这些表是 v2 核心价值的"翻译层", 后续可以扩展 (风格 / 乐器 / 节奏型态)
"""

# ================ BPM 速度分级 (意大利语 + 中文 + 风格描述) ================

TEMPO_VOCAB = [
    {"min_bpm": 0,    "max_bpm": 60,  "italian": "Largo",       "chinese": "极慢板", "desc": "庄严、舒缓"},
    {"min_bpm": 60,   "max_bpm": 76,  "italian": "Adagio",      "chinese": "慢板",   "desc": "从容、柔和"},
    {"min_bpm": 76,   "max_bpm": 108, "italian": "Andante",     "chinese": "中板",   "desc": "步行速度, 流动"},
    {"min_bpm": 108,  "max_bpm": 120, "italian": "Moderato",    "chinese": "小快板", "desc": "中等, 适中"},
    {"min_bpm": 120,  "max_bpm": 156, "italian": "Allegro",     "chinese": "快板",   "desc": "快速, 活泼"},
    {"min_bpm": 156,  "max_bpm": 200, "italian": "Presto",      "chinese": "急板",   "desc": "急速, 热烈"},
    {"min_bpm": 200,  "max_bpm": 999, "italian": "Prestissimo", "chinese": "狂板",   "desc": "极快, 爆裂"},
]


def get_tempo_vocab(bpm: float) -> dict:
    """根据 BPM 返回术语"""
    for v in TEMPO_VOCAB:
        if v["min_bpm"] <= bpm < v["max_bpm"]:
            return {
                "tempo_italian": v["italian"],
                "tempo_class": v["chinese"],
                "tempo_description": v["desc"],
            }
    # 兜底
    return {"tempo_italian": "—", "tempo_class": "—", "tempo_description": "—"}


# ================ 调性映射 (24 个, 大调 + 小调) ================

KEY_VOCAB = {
    # 大调
    "C major":  {"chinese": "C大调",  "desc": "纯粹、明亮、坦荡"},
    "C# major": {"chinese": "升C大调", "desc": "神秘、华丽"},
    "D major":  {"chinese": "D大调",  "desc": "辉煌、胜利、阳光"},
    "D# major": {"chinese": "升D大调", "desc": "紧张、不安"},
    "E major":  {"chinese": "E大调",  "desc": "英雄、壮丽、宏大"},
    "F major":  {"chinese": "F大调",  "desc": "温暖、田园、柔和"},
    "F# major": {"chinese": "升F大调", "desc": "不安、戏剧"},
    "G major":  {"chinese": "G大调",  "desc": "开朗、田园、质朴"},
    "G# major": {"chinese": "升G大调", "desc": "紧张、怪诞"},
    "A major":  {"chinese": "A大调",  "desc": "自信、明朗、欢快"},
    "A# major": {"chinese": "升A大调", "desc": "阴郁、神秘"},
    "B major":  {"chinese": "B大调",  "desc": "辉煌、明亮、紧张"},
    # 小调
    "C minor":  {"chinese": "c小调",  "desc": "忧郁、内省、温柔"},
    "C# minor": {"chinese": "升c小调", "desc": "阴郁、戏剧性"},
    "D minor":  {"chinese": "d小调",  "desc": "深沉、庄严、悲怆"},
    "D# minor": {"chinese": "升d小调", "desc": "阴森、诡异"},
    "E minor":  {"chinese": "e小调",  "desc": "忧郁、内省、深沉"},
    "F minor":  {"chinese": "f小调",  "desc": "深沉、悲剧性"},
    "F# minor": {"chinese": "升f小调", "desc": "阴郁、神秘、激烈"},
    "G minor":  {"chinese": "g小调",  "desc": "不安、忧郁"},
    "G# minor": {"chinese": "升g小调", "desc": "阴森、紧张"},
    "A minor":  {"chinese": "a小调",  "desc": "忧郁、内省、温柔"},
    "A# minor": {"chinese": "升a小调", "desc": "阴郁、沉重"},
    "B minor":  {"chinese": "b小调",  "desc": "孤立、阴郁、激烈"},
}


def get_key_vocab(key: str) -> dict:
    """根据调性字符串返回中文+描述"""
    v = KEY_VOCAB.get(key)
    if v:
        return {"key_chinese": v["chinese"], "key_description": v["desc"]}
    return {"key_chinese": key, "key_description": "未知调性"}


# ================ 动态范围映射 (Diana 3.2) ================

DYNAMIC_VOCAB = [
    {"min_db": -60, "max_db": -35, "class": "弱动态", "mark": "p",  "desc": "细腻, 内敛"},
    {"min_db": -35, "max_db": -20, "class": "中动态", "mark": "mf", "desc": "适中, 有起伏"},
    {"min_db": -20, "max_db": 0,   "class": "强动态", "mark": "ff", "desc": "饱满有力, 冲击感强"},
]


def get_dynamic_vocab(db: float) -> dict:
    """根据 dB 返回力度记号"""
    for v in DYNAMIC_VOCAB:
        if v["min_db"] <= db < v["max_db"]:
            return {"dynamic_class": v["class"], "dynamic_mark": v["mark"]}
    return {"dynamic_class": "—", "dynamic_mark": "—"}