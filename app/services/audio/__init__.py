"""
Standalone Audio Style Analyzer
===============================
老杨 2026-08-07 21:14 v2-1 重构 (Diana 审计 2.1)

独立使用:
    pip install librosa numpy  # mutagen (可选, ID3 读取)
    
    from audio import analyze_audio
    features = analyze_audio("song.mp3")
    print(features.tempo.bpm, features.key_info.key)

剥离子项目:
    git subtree split --prefix=app/services/audio -b audio-standalone

公共 API (剥离子项目时只需这些):
- analyze_audio() 主入口
- 所有 dataclass 类型
"""
import logging

logger = logging.getLogger(__name__)

logger.info(
    "\n%s\n"
    "⚠️  音频版权与使用声明\n"
    "%s\n"
    "本工具仅分析用户自行上传的明文音频，不提供、不存储、不传播任何受版权保护内容。\n"
    "请确保您对上传的音频享有合法使用权（CC0 / 自制 / 已授权）。\n"
    "VIP / 加密音乐请使用 UnlockMusic (https://git.unlock-music.dev/) 本地解密后重新上传。\n"
    "严禁将分析结果用于侵犯第三方版权的内容生成。\n"
    "%s",
    "=" * 60,
    "=" * 60,
    "=" * 60,
)
from .analyzer import analyze_audio, clear_cache
from .features.vocab import (
    TEMPO_VOCAB, KEY_VOCAB, DYNAMIC_VOCAB,
    get_tempo_vocab, get_key_vocab, get_dynamic_vocab,
)
from .fingerprint import compute_audio_fingerprint, compute_song_signature
from .id3_utils import ID3Metadata, extract_id3_metadata, read_id3_tags
from .models import (
    AudioAnalyzerError,
    AudioFeatures,
    DynamicInfo,
    KeyInfo,
    PitchRange,
    SectionInfo,
    SpectralInfo,
    StyleInfo,
    TempoInfo,
)
from .preprocess import preprocess_audio

__all__ = [
    # 主入口
    "analyze_audio",
    "clear_cache",
    # Dataclass (强类型契约)
    "AudioFeatures",
    "TempoInfo",
    "KeyInfo",
    "PitchRange",
    "DynamicInfo",
    "SpectralInfo",
    "SectionInfo",
    "StyleInfo",
    "ID3Metadata",
    # 辅助
    "AudioAnalyzerError",
    "preprocess_audio",
    "compute_audio_fingerprint",
    "compute_song_signature",
    "read_id3_tags",
    "extract_id3_metadata",
    # 术语映射
    "TEMPO_VOCAB",
    "KEY_VOCAB",
    "DYNAMIC_VOCAB",
    "get_tempo_vocab",
    "get_key_vocab",
    "get_dynamic_vocab",
]