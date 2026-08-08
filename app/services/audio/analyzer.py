"""
audio.analyzer — 主入口: analyze_audio(path) -> AudioFeatures
Diana 审计 4.2 加缓存层
"""
import hashlib
from pathlib import Path
from typing import Dict

from loguru import logger

from .features.dynamic import get_dynamic
from .features.key import get_key
from .features.pitch import get_pitch_range
from .features.sections import detect_chorus_segments, detect_sections
from .features.spectral import get_spectral
from .features.style import detect_style
from .features.tempo import get_tempo
from .models import AudioFeatures, AudioAnalyzerError
from .preprocess import preprocess_audio


# 支持的音频格式
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

# Diana 4.2: 缓存层 (文件 hash -> AudioFeatures)
_analysis_cache: Dict[str, AudioFeatures] = {}


def _file_hash(path: str) -> str:
    """文件内容 MD5 hash, 用于缓存 key (Diana 4.2)"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _validate_path(path: str) -> None:
    """验证文件存在 + 格式支持"""
    p = Path(path)
    if not p.exists():
        raise AudioAnalyzerError(f"文件不存在: {path}")
    if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise AudioAnalyzerError(
            f"格式不支持: {p.suffix} (支持: {', '.join(SUPPORTED_EXTENSIONS)})"
        )


def analyze_audio(path: str, use_cache: bool = True) -> AudioFeatures:
    """音频分析主入口 (v2 重构版)

    Args:
        path: 音频文件路径
        use_cache: 是否使用缓存 (Diana 4.2, 默认 True)

    Returns:
        AudioFeatures (dataclass, 含 .to_dict() 转 JSON)

    Raises:
        AudioAnalyzerError: 文件不存在或格式不支持
    """
    _validate_path(path)

    # Diana 4.2: 缓存命中
    cache_key = _file_hash(path) if use_cache else None
    if cache_key and cache_key in _analysis_cache:
        logger.info(f"audio_analyzer: cache hit for {path}")
        return _analysis_cache[cache_key]

    # 1. 预处理 (Diana 4.3)
    y, sr = preprocess_audio(path)

    # 1.5 ID3 tag (Diana 2.2 歌曲指纹第一层, v2-7 新增)
    from .id3_utils import read_id3_tags
    id3_metadata = read_id3_tags(path)

    # 2. 计算各特征
    tempo_info = get_tempo(y, sr)
    key_info = get_key(y, sr)
    pitch_info = get_pitch_range(y, sr)
    dynamic_info = get_dynamic(y, sr)
    spectral_info = get_spectral(y, sr)
    sections = detect_sections(y, sr)
    chorus_segments = detect_chorus_segments(y, sr, top_k=6)  # 老杨 8/8 17:34: detect 多个, UI 列表只显示前 3 个 (识别出几个就返回几个)

    # 3. 风格识别 (Diana 3.1, v2.0 占位)
    style_info = detect_style(
        y, sr,
        tempo_bpm=tempo_info.bpm,
        key=key_info.key,
        dynamic_range_db=dynamic_info.dynamic_range_db,
    )

    duration = len(y) / sr

    features = AudioFeatures(
        duration_seconds=round(duration, 1),
        tempo=tempo_info,
        key_info=key_info,
        pitch_range=pitch_info,
        dynamic=dynamic_info,
        spectral=spectral_info,
        sections=sections,
        chorus_segments=chorus_segments,  # Diana 8/8
        style=style_info,
        id3_metadata=id3_metadata,    # Diana 2.2 歌曲指纹第一层 (v2-7 新增)
    )

    # 4. 写缓存
    if cache_key:
        _analysis_cache[cache_key] = features

    logger.info(
        f"audio_analyzer: {Path(path).name} "
        f"BPM={features.tempo.bpm}({features.tempo.tempo_class}) "
        f"key={features.key_info.key} "
        f"duration={features.duration_seconds}s "
        f"sections={len(features.sections)}"
    )

    return features


def clear_cache() -> int:
    """清空分析缓存, 返回清空条目数"""
    global _analysis_cache
    count = len(_analysis_cache)
    _analysis_cache.clear()
    logger.info(f"audio_analyzer: cache cleared ({count} entries)")
    return count