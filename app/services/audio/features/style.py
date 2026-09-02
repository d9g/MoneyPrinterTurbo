"""
audio.features.style — 风格识别 ()

v2.0 占位实现 (heuristic-only)
v2.1 升级 ():
  - vocal_type: 用 librosa pyin 音高分布 (中位数 + 跨度) 判定男/女/无
  - dominant_instruments: 用频段能量分布 + 频谱质心
  - genre_confidence: 用流派-特征匹配度计算 (0-1)

设计原则 (戴拿 3.1):
- 启发式优先, 不依赖 LLM (BPM 92 → 0.5s 内出结果)
- 阈值基于中位歌手音域 (男 48-65, 女 60-77, MIDI)
- 频段能量比基于流行音乐平均频谱
"""
from typing import Dict, List, Optional

import numpy as np

try:
    import librosa
except ImportError:
    librosa = None  # 允许 import 阶段不挂

from ..models import StyleInfo


# ================ v2.1 常量 ================

# MIDI 音域分界 (基于普通歌手音域)
_MIDI_MALE_LOW = 48   # ~C3
_MIDI_MALE_HIGH = 65  # ~E4
_MIDI_FEMALE_LOW = 60  # ~C4
_MIDI_FEMALE_HIGH = 77  # ~F5

# 频段 (Hz) 划分 (基于流行音乐平均频谱)
_LOW_BAND = (60, 250)       # bass / kick / low tom
_MID_BAND = (250, 2000)     # vocal / guitar / piano
_HIGH_BAND = (2000, 6000)   # cymbals / hi-hat / 弦乐泛音
_AIR_BAND = (6000, 22050)   # sibilance / air

# 频段能量占比阈值 (基于统计)
_LOW_THRESHOLD = 0.35   # 35%+
_MID_THRESHOLD = 0.50
_HIGH_THRESHOLD = 0.25
_AIR_THRESHOLD = 0.05


def _voiced_ratio(y: np.ndarray, sr: int) -> float:
    """有声帧占比 (0-1), 反映人声存在度"""
    if librosa is None:
        return 0.0
    try:
        # 用 librosa.effects.split 检测有声段 (比 pyin 快 10x)
        intervals = librosa.effects.split(y, top_db=30)
        if len(intervals) == 0:
            return 0.0
        voiced_samples = sum(end - start for start, end in intervals)
        return voiced_samples / len(y)
    except Exception:
        return 0.0


def _vocal_pitch_midi(y: np.ndarray, sr: int) -> Optional[float]:
    """有声段音高 (MIDI), 反映人声音域"""
    if librosa is None or len(y) < sr:  # < 1s 跳过
        return None
    try:
        # pyin 只对有声段计算 (先 split)
        intervals = librosa.effects.split(y, top_db=30)
        if len(intervals) == 0:
            return None
        # 拼接有声段
        voiced = np.concatenate([y[start:end] for start, end in intervals])
        if len(voiced) < sr // 2:  # < 0.5s
            return None
        # 降采样到 8000 Hz 加速 pyin
        y_mono = librosa.to_mono(voiced) if voiced.ndim > 1 else voiced
        y_8k = librosa.resample(y_mono, orig_sr=sr, target_sr=8000)
        f0, voiced_flag, _ = librosa.pyin(
            y_8k,
            fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C6'),
            sr=8000,
        )
        voiced_f0 = f0[voiced_flag] if voiced_flag is not None else f0[~np.isnan(f0)] if f0 is not None else None
        if voiced_f0 is None or len(voiced_f0) == 0:
            return None
        voiced_f0 = voiced_f0[voiced_f0 > 0]
        if len(voiced_f0) == 0:
            return None
        median_hz = float(np.median(voiced_f0))
        return float(librosa.hz_to_midi(median_hz))
    except Exception:
        return None


def _pitch_range(y: np.ndarray, sr: int) -> int:
    """音域跨度 (半音数)"""
    if librosa is None or len(y) < sr:
        return 0
    try:
        y_mono = librosa.to_mono(y) if y.ndim > 1 else y
        y_8k = librosa.resample(y_mono, orig_sr=sr, target_sr=8000)
        f0, voiced_flag, _ = librosa.pyin(
            y_8k,
            fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C6'),
            sr=8000,
        )
        if f0 is None:
            return 0
        voiced_f0 = f0[voiced_flag] if voiced_flag is not None else f0[~np.isnan(f0)]
        voiced_f0 = voiced_f0[voiced_f0 > 0] if voiced_f0 is not None else []
        if len(voiced_f0) < 2:
            return 0
        midis = librosa.hz_to_midi(voiced_f0)
        return int(np.max(midis) - np.min(midis))
    except Exception:
        return 0


def _vocal_type(y: np.ndarray, sr: int) -> str:
    """判定人声类型

    Returns:
        - "male/baritone" / "male/tenor" (男中音 / 男高音)
        - "female/mezzo" / "female/soprano" (女中音 / 女高音)
        - "instrumental" (无明显人声)
        - "mixed" (多人或难判定)
    """
    voiced = _voiced_ratio(y, sr)
    # 阈值: 有声帧 < 5% 视为纯器乐
    if voiced < 0.05:
        return "instrumental"

    pitch = _vocal_pitch_midi(y, sr)
    if pitch is None:
        return "mixed"

    # 男音域: 48-65, 女音域: 60-77
    # 重叠区 60-65: 视为混合或男高音
    if pitch < _MIDI_MALE_LOW:
        return "male/baritone"  # 极低, 按男中音
    elif pitch < _MIDI_FEMALE_LOW:
        return "male/baritone"  # 男中音主力区
    elif pitch < _MIDI_MALE_HIGH:
        # 重叠区: 看音域跨度
        range_semi = _pitch_range(y, sr)
        if range_semi > 18:
            return "male/tenor"  # 跨度大→高音
        return "female/mezzo"  # 中位+跨度小→女中音
    elif pitch < _MIDI_FEMALE_HIGH:
        return "female/mezzo"
    else:
        return "female/soprano"


def _band_energy(y: np.ndarray, sr: int) -> Dict[str, float]:
    """频段能量占比 (0-1, 4 段归一化)"""
    if librosa is None:
        return {"low": 0.0, "mid": 0.0, "high": 0.0, "air": 0.0}
    try:
        # STFT
        stft = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        # 各频段 mask
        def band_mask(low_hz, high_hz):
            return (freqs >= low_hz) & (freqs < high_hz)
        # 每帧能量
        frame_energy = (stft ** 2).sum(axis=0)
        # 各频段总能量
        low_e = stft[band_mask(*_LOW_BAND)].sum()
        mid_e = stft[band_mask(*_MID_BAND)].sum()
        high_e = stft[band_mask(*_HIGH_BAND)].sum()
        air_e = stft[band_mask(*_AIR_BAND)].sum()
        total = low_e + mid_e + high_e + air_e
        if total == 0:
            return {"low": 0.0, "mid": 0.0, "high": 0.0, "air": 0.0}
        return {
            "low": float(low_e / total),
            "mid": float(mid_e / total),
            "high": float(high_e / total),
            "air": float(air_e / total),
        }
    except Exception:
        return {"low": 0.0, "mid": 0.0, "high": 0.0, "air": 0.0}


def _dominant_instruments(y: np.ndarray, sr: int, acousticness: float) -> List[str]:
    """判定主乐器

    Returns:
        1-3 个乐器标签 (acoustic guitar / soft piano / drum machine / ...)
    """
    if librosa is None:
        return ["unknown"]

    band = _band_energy(y, sr)
    instruments = []

    # 频段能量解读
    if band["low"] > _LOW_THRESHOLD:
        # 低频能量大 → bass / kick
        if acousticness > 0.6:
            instruments.append("acoustic bass")
        else:
            instruments.append("electric bass")

    if band["mid"] > _MID_THRESHOLD:
        # 中频主导 → guitar / piano / vocal
        # 用频谱质心区分 guitar(高) vs piano(中) vs vocal(中)
        try:
            centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0].mean()
            if centroid > 2500:
                # 高质心 → 偏亮 → electric guitar
                if acousticness > 0.6:
                    instruments.append("acoustic guitar")
                else:
                    instruments.append("electric guitar")
            elif centroid > 1500:
                # 中质心 → piano / vocal
                if acousticness > 0.6:
                    instruments.append("soft piano")
                else:
                    instruments.append("soft synth")
            else:
                # 低质心 → 偏暗 → bass / kick (已在 low 处理)
                pass
        except Exception:
            if acousticness > 0.6:
                instruments.append("soft piano")
            else:
                instruments.append("soft synth")

    if band["high"] > _HIGH_THRESHOLD:
        # 高频能量大 → cymbals / hi-hat
        if acousticness > 0.6:
            instruments.append("light percussion")
        else:
            instruments.append("drum machine")

    # 兜底: 至少 1 个
    if not instruments:
        if acousticness > 0.6:
            instruments.append("soft piano")
        elif acousticness > 0.3:
            instruments.append("soft synth")
        else:
            instruments.append("drum machine")

    return instruments[:3]


def _genre_confidence(genre: str, tempo_bpm: float, is_major: bool, valence: float, acousticness: float) -> float:
    """流派置信度 (基于特征匹配度)

    各流派"典型特征"参考:
    - Ballad: BPM 60-90, 强动态, 高 acousticness
    - Pop: BPM 90-120, valence 中高, acousticness 中
    - Electronic: BPM 120+, 低 acousticness, 高 valence

    Returns:
        0-1, 越高越置信
    """
    score = 0.5  # 基础分
    if genre == "Ballad":
        if 60 <= tempo_bpm <= 90:
            score += 0.2
        if acousticness > 0.6:
            score += 0.15
        if not is_major:
            score += 0.15
    elif genre == "Pop":
        if 90 <= tempo_bpm <= 120:
            score += 0.2
        if 0.4 <= valence <= 0.8:
            score += 0.15
        if 0.3 <= acousticness <= 0.7:
            score += 0.15
    elif genre == "Electronic":
        if tempo_bpm > 120:
            score += 0.2
        if acousticness < 0.4:
            score += 0.2
        if valence > 0.5:
            score += 0.1
    return min(1.0, max(0.0, score))


def detect_style(y, sr, tempo_bpm: float, key: str, dynamic_range_db: float) -> StyleInfo:
    """风格识别 (v2.1)

    v2.0 实现: 用启发式规则粗略估算
    v2.1 升级: 用 librosa 真实计算 (vocal_type + dominant_instruments)

    Args:
        y: 音频数组
        sr: 采样率
        tempo_bpm: BPM (用于推断风格)
        key: 调性 ("G major")
        dynamic_range_db: 动态范围

    Returns:
        StyleInfo (vocal_type + dominant_instruments 真实填入)
    """
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

    # 3. 动态范围推断 acousticness
    if dynamic_range_db > 20:
        acousticness = 0.6
    else:
        acousticness = 0.3

    # 4. v2.1 真实计算: 主乐器
    try:
        dominant_instruments = _dominant_instruments(y, sr, acousticness)
    except Exception:
        dominant_instruments = ["unknown"]

    # 5. v2.1 真实计算: 人声类型
    try:
        vocal_type = _vocal_type(y, sr)
    except Exception:
        vocal_type = "unknown"

    # 6. v2.1 真实计算: 流派置信度
    confidence = _genre_confidence(genre_guess, tempo_bpm, is_major, valence, acousticness)

    return StyleInfo(
        genre=genre_guess,
        genre_confidence=round(confidence, 2),
        mood=mood_guess,
        mood_valence=valence,
        mood_energy=energy,
        acousticness=acousticness,
        vocal_type=vocal_type,
        dominant_instruments=dominant_instruments,
    )
