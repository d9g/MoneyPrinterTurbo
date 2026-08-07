"""
audio.features.tempo — BPM 节奏分析
"""
import librosa

from ..models import TempoInfo
from .vocab import get_tempo_vocab


def get_tempo(y, sr) -> TempoInfo:
    """BPM 检测 + 术语映射 (Diana 3.2)"""
    bpm_raw, _ = librosa.beat.beat_track(y=y, sr=sr)
    # librosa 0.11+ 返回 ndarray shape=(1,), 用 .item() 转标量
    bpm_value = float(bpm_raw.item() if hasattr(bpm_raw, 'item') else bpm_raw)
    vocab = get_tempo_vocab(bpm_value)
    return TempoInfo(
        bpm=round(bpm_value, 1),
        tempo_class=vocab["tempo_class"],
        tempo_italian=vocab["tempo_italian"],
        tempo_description=vocab["tempo_description"],
    )