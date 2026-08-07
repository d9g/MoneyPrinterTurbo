"""
audio.features.spectral — 频谱特征 (明亮度)
"""
import librosa

from ..models import SpectralInfo


def get_spectral(y, sr) -> SpectralInfo:
    """频谱特征: 明亮度 / 频谱质心"""
    # 频谱质心 = "声音亮度"
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    brightness_hz = float(centroid.mean())

    return SpectralInfo(
        brightness_hz=round(brightness_hz, 1),
        spectral_centroid_mean=round(brightness_hz, 1),
    )