"""
audio.features.key — 调性识别 (Krumhansl-Schmuckler 算法)
"""
import numpy as np
import librosa

from ..models import KeyInfo
from .vocab import get_key_vocab

_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def get_key(y, sr) -> KeyInfo:
    """调性识别 (Krumhansl-Schmuckler 相关性算法)"""
    # chroma 特征
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)

    # 12 个候选调性打分
    scores = []
    for i in range(12):
        # 循环移位对齐
        major_score = np.corrcoef(chroma_mean, np.roll(_MAJOR_PROFILE, i))[0, 1]
        minor_score = np.corrcoef(chroma_mean, np.roll(_MINOR_PROFILE, i))[0, 1]
        scores.append((major_score, "major", i))
        scores.append((minor_score, "minor", i))

    # 取最高分
    best = max(scores, key=lambda x: x[0] if not np.isnan(x[0]) else -1)
    confidence = float(best[0]) if not np.isnan(best[0]) else 0.0
    key_name = f"{_NOTE_NAMES[best[2]]} {best[1]}"

    vocab = get_key_vocab(key_name)
    return KeyInfo(
        key=key_name,
        key_chinese=vocab["key_chinese"],
        key_description=vocab["key_description"],
        confidence=round(confidence, 3),
    )