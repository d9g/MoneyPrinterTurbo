"""
audio.fingerprint — 歌曲指纹计算
审计项 2.2 修复版

替代 SHA1-first-1MB (同一首歌不同编码会失败)
用 librosa chroma 特征做 hash, 对编码差异鲁棒

输出:
- signature: 唯一识别字符串 (用于 DB lookup)
- metadata: {artist, title, duration, bpm, key}
"""
import hashlib
from typing import Optional, Tuple

import librosa
import numpy as np


def compute_audio_fingerprint(
    y: np.ndarray,
    sr: int,
    duration: float,
    bpm: float,
    key: str,
) -> str:
    """基于 chroma 特征的音频指纹

    Args:
        y: 音频数组 (预处理后)
        sr: 采样率
        duration: 时长 (秒)
        bpm: 节奏
        key: 调性 ("G major")

    Returns:
        形如 "fp:abc123def456::180::72::G major"
    """
    # chroma 特征 = 12 维音调能量分布
    chroma = librosa.feature.chroma_stft(y=y, sr=sr)

    # 降采样 (每 50 帧取 1 帧), 保留音调主成分
    downsampled = chroma[:, ::50].flatten()

    # 转 bytes 做 SHA1 hash
    chroma_hash = hashlib.sha1(downsampled.tobytes()).hexdigest()[:16]

    return f"fp:{chroma_hash}::{int(duration)}::{int(bpm)}::{key}"


def compute_song_signature(
    audio_path: str,
    y: np.ndarray,
    sr: int,
    duration: float,
    bpm: float,
    key: str,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    id3_artist: Optional[str] = None,
    id3_title: Optional[str] = None,
) -> Tuple[str, dict]:
    """三层识别优先级: 元数据 > ID3 > 声学指纹

    Returns: (signature, metadata)
    """
    # 优先级: 用户元数据 > ID3 tag
    final_artist = artist or id3_artist or ""
    final_title = title or id3_title or ""

    metadata = {
        "artist": final_artist,
        "title": final_title,
        "duration": round(duration, 1),
        "bpm": round(bpm, 1),
        "key": key,
    }

    # 1. 元数据完整 → 用元数据签名
    if final_artist and final_title:
        sig = f"meta:{final_artist}::{final_title}::{duration:.1f}"
        return sig, metadata

    # 2. 兜底 → 声学指纹
    fingerprint = compute_audio_fingerprint(y, sr, duration, bpm, key)
    metadata["signature_source"] = "audio_fingerprint"
    return fingerprint, metadata