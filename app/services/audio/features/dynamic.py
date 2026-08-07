"""
audio.features.dynamic — 动态范围分析
"""
import numpy as np
import librosa

from ..models import DynamicInfo
from .vocab import get_dynamic_vocab


def get_dynamic(y, sr) -> DynamicInfo:
    """RMS 音量 + 动态范围"""
    rms = librosa.feature.rms(y=y)[0]
    rms_db = float(librosa.amplitude_to_db(rms).mean())

    # 动态范围: 最大 - 最小 RMS
    rms_db_array = librosa.amplitude_to_db(rms)
    dynamic_range_db = float(rms_db_array.max() - rms_db_array.min())

    vocab = get_dynamic_vocab(rms_db)
    return DynamicInfo(
        rms_db=round(rms_db, 1),
        dynamic_range_db=round(dynamic_range_db, 1),
        dynamic_class=vocab["dynamic_class"],
        dynamic_mark=vocab["dynamic_mark"],
    )