"""
audio.preprocess — 音频预处理
审计项 4.3 新增

标准化上传音频, 避免采样率/静音/音量差异影响分析
"""
import librosa
import numpy as np

# 统一采样率 (22050 Hz 单声道足够提取调性/BPM/段落)
DEFAULT_SR = 22050


def preprocess_audio(path: str, sr: int = DEFAULT_SR) -> tuple[np.ndarray, int]:
    """音频预处理: 统一采样率 + 去首尾静音 + 峰值归一化

    Args:
        path: 音频文件路径
        sr: 目标采样率 (默认 22050 Hz)

    Returns:
        (audio_array, sample_rate)
    """
    # 1. 加载并重采样 + 单声道
    y, loaded_sr = librosa.load(path, sr=sr, mono=True)

    # 2. 去首尾静音 (top_db=30 适合大多数流行音乐)
    y, _ = librosa.effects.trim(y, top_db=30)

    # 3. 峰值归一化 (避免音量差异影响 RMS 分析)
    y = librosa.util.normalize(y)

    return y, loaded_sr