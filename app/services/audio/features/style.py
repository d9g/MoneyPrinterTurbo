"""
audio.features.style — 风格识别 (Diana 3.1)
v2.0 占位实现, v2.1 用 librosa 分类器/预训练模型做真实分类
"""
from ..models import StyleInfo


def detect_style(y, sr, tempo_bpm: float, key: str, dynamic_range_db: float) -> StyleInfo:
    """风格识别 (Diana 3.1)
    
    v2.0 实现: 用启发式规则粗略估算
    v2.1 升级: 用 librosa 预训练分类器 或 调用 LLM 做音乐学分类
    
    Args:
        y: 音频数组
        sr: 采样率
        tempo_bpm: BPM (用于推断风格)
        key: 调性
        dynamic_range_db: 动态范围
    
    Returns:
        StyleInfo (粗略估计, 后续会升级)
    """
    # 启发式规则 (v2.1 替换为分类器)
    # 1. BPM 推断 energy
    if tempo_bpm < 80:
        energy = 0.3
        genre_guess = "Ballad"
    elif tempo_bpm < 110:
        energy = 0.5
        genre_guess = "Pop"
    elif tempo_bpm < 140:
        energy = 0.7
        genre_guess = "Pop"
    else:
        energy = 0.85
        genre_guess = "Electronic"

    # 2. 调性推断 valence
    is_major = "major" in key
    if is_major:
        valence = 0.7
        mood_guess = "明快"
    else:
        valence = 0.4
        mood_guess = "忧郁"

    # 3. 动态范围推断情绪强度
    if dynamic_range_db > 20:
        acousticness = 0.6  # 高动态 → 偏向原声
    else:
        acousticness = 0.3  # 低动态 → 偏向电子

    # 4. 主导乐器 (v2.0 简单估算, v2.1 用频谱分析)
    dominant_instruments = ["unknown"]  # v2.0 占位

    # 5. 人声类型 (v2.0 简单判定, v2.1 用 pyin 音高分布)
    vocal_type = "unknown"  # v2.0 占位

    return StyleInfo(
        genre=genre_guess,
        genre_confidence=0.5,  # 占位
        mood=mood_guess,
        mood_valence=valence,
        mood_energy=energy,
        dominant_instruments=dominant_instruments,
        acousticness=acousticness,
        vocal_type=vocal_type,
    )