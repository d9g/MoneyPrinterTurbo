"""
audio.analyzer — 主入口: analyze_audio(path) -> AudioFeatures
Diana 审计 4.2 加缓存层
"""
from pathlib import Path
from typing import Dict, Tuple

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

# 加密格式 (2026-08-10 老杨拍板: 不提供服务端解密, 友好报错引导本地解密)
ENCRYPTED_EXTENSIONS = {
    ".ncm",       # 网易云音乐
    ".qmc",       # QQ 音乐
    ".qmc0",      # QQ 音乐 (老版本)
    ".qmcflac",   # QQ 音乐 FLAC 加密
    ".qmc3",      # QQ 音乐 v3
    ".mflac",     # QQ 桌面客户端 FLAC 加密
    ".mgg",       # QQ 桌面客户端 v3 加密
    ".kgm",       # 酷狗音乐
    ".kwm",       # 酷我音乐
    ".xm",        # 虾米音乐
}

_UNLOCK_MUSIC_URL = "https://git.unlock-music.dev/"

# Diana 4.2: 缓存层 (文件 hash -> AudioFeatures)
_analysis_cache: Dict[str, AudioFeatures] = {}


def _file_hash(path: str) -> str:
    """缓存 key (审计 P0-2 修复: 避免大文件全量读 MD5 导致 OOM)

    方案 C 双保险: size + mtime_ns + name
    - size: 文件大小 (字节)
    - mtime_ns: 修改时间 (纳秒精度, 上传后修改必然变化)
    - name: 文件名 (防 path 不同但 size+mtime 一致的边缘 case)

    对 50-100MB 音频文件: 原方案 read(8192) 全量读 2 次 (hash + preprocess),
    新方案只需 os.stat, 耗时从 100ms+ 降到 1ms 以内。
    """
    p = Path(path)
    stat = p.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}:{p.name}"


def _validate_path(path: str) -> None:
    """验证文件存在 + 格式支持

    2026-08-10 老杨拍板:
      - 加密格式 (.ncm/.qmc/.mgg 等) 友好报错, 引导用户本地解密后重新上传
      - 本工具不提供、不参与任何平台加密机制的绕过
    """
    p = Path(path)
    if not p.exists():
        raise AudioAnalyzerError(f"文件不存在: {path}")
    if p.suffix.lower() in ENCRYPTED_EXTENSIONS:
        raise AudioAnalyzerError(
            f"检测到加密音频格式 {p.suffix}。本工具不提供服务端解密功能（技术不可行 + 法律高风险）。"
            f"请使用开源项目 UnlockMusic ({_UNLOCK_MUSIC_URL}) 在本地解密后重新上传明文音频。"
            f"详见项目根目录 README.md 的「音频版权免责声明」。"
        )
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


def analyze_audio_with_audio(path: str, use_cache: bool = True) -> Tuple[AudioFeatures, "object", int]:
    """analyze_audio + 复用 y, sr, 避免重复调用 preprocess_audio (审计 P0-3)

    返回 (features, y, sr):
      - features: AudioFeatures
      - y, sr: librosa 加载后的音频数据 + 采样率

    缓存层沿用 _analysis_cache (Diana 4.2)。
    缓存命中分支直接复用 features, 不重新加载 y, sr (调用方需要再处理)。
    """
    _validate_path(path)

    # Diana 4.2: 缓存命中
    cache_key = _file_hash(path) if use_cache else None
    if cache_key and cache_key in _analysis_cache:
        logger.info(f"audio_analyzer: cache hit for {path}")
        # 缓存命中: features 复用, 不再算 y, sr (调用方按需 preprocess)
        y, sr = None, None  # type: ignore
        return _analysis_cache[cache_key], y, sr

    # 1. 预处理 (Diana 4.3)
    y, sr = preprocess_audio(path)

    # 1.5 ID3 tag
    from .id3_utils import read_id3_tags
    id3_metadata = read_id3_tags(path)

    # 2. 计算各特征
    tempo_info = get_tempo(y, sr)
    key_info = get_key(y, sr)
    pitch_info = get_pitch_range(y, sr)
    dynamic_info = get_dynamic(y, sr)
    spectral_info = get_spectral(y, sr)
    sections = detect_sections(y, sr)
    chorus_segments = detect_chorus_segments(y, sr, top_k=6)

    # 3. 风格识别
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
        chorus_segments=chorus_segments,
        style=style_info,
        id3_metadata=id3_metadata,
    )

    if cache_key:
        _analysis_cache[cache_key] = features

    logger.info(
        f"audio_analyzer: {Path(path).name} "
        f"BPM={features.tempo.bpm}({features.tempo.tempo_class}) "
        f"key={features.key_info.key} "
        f"duration={features.duration_seconds}s "
        f"sections={len(features.sections)}"
    )

    return features, y, sr


def clear_cache() -> int:
    """清空分析缓存, 返回清空条目数"""
    global _analysis_cache
    count = len(_analysis_cache)
    _analysis_cache.clear()
    logger.info(f"audio_analyzer: cache cleared ({count} entries)")
    return count