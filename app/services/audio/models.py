"""
audio.models — 所有 dataclass 类型定义
老杨 2026-08-07 21:14 v2-1 重构 (Diana 审计 2.3 修复)

这些 dataclass 是 audio 包对外的契约:
- 输入: analyze_audio(path) -> AudioFeatures
- 输出: 强类型结果 (to_dict() 转 JSON 给 API)
- 特征向量: feature_vector() 给相似度计算

剥离子项目时, models.py 是最重要的文件 — 包含完整数据契约.
"""
from dataclasses import asdict, dataclass, field
from typing import List, Optional


class AudioAnalyzerError(Exception):
    """音频分析失败 (文件损坏/格式不支持/采样失败)"""


# ================ 物理特征 ================

@dataclass
class TempoInfo:
    """节奏信息"""
    bpm: float
    tempo_class: str         # 中文术语: 急板 / 中板 / 慢板
    tempo_italian: str       # 意大利语术语: Presto / Andante / Largo (Diana 3.2)
    tempo_description: str   # 风格描述: "急速, 热烈"


@dataclass
class KeyInfo:
    """调性信息"""
    key: str                 # "G major"
    key_chinese: str         # "G大调" (Diana 3.2)
    key_description: str      # "开朗、田园、质朴"
    confidence: float


@dataclass
class PitchRange:
    """音域信息"""
    low_midi: int
    high_midi: int
    low_note: str
    high_note: str
    range_semitones: int


@dataclass
class DynamicInfo:
    """动态信息 (音量)"""
    rms_db: float
    dynamic_range_db: float
    dynamic_class: str       # Diana 3.2: 强动态/中动态/弱动态
    dynamic_mark: str        # ff / mf / p (力度记号)


@dataclass
class SpectralInfo:
    """频谱信息"""
    brightness_hz: float
    spectral_centroid_mean: float


@dataclass
class SectionInfo:
    """段落信息 (前奏/主歌/副歌)"""
    index: int
    start: float
    end: float
    duration: float
    intensity: str           # low / medium / high


@dataclass
class ChorusSegment:
    """高潮段信息 (Diana 8/8 高潮检测)

    多维特征识别: RMS能量 + spectral centroid (高频亮度) + onset strength (节拍密度)
    """
    index: int               # 精选1/2/3
    start: float             # 起始时间 (秒)
    end: float               # 结束时间 (秒)
    duration: float          # 时长
    confidence: float        # 置信度 0-1
    chorus_type: str         # main_chorus / post_chorus / pre_chorus / breakdown
    label: str               # 人类可读: "主歌A 高潮区" / "副歌 后半段"

@dataclass
class StyleInfo:
    """风格识别 — 把物理特征翻译成专业描述
    Diana 审计 3.1 新增的核心维度
    """
    genre: str                                       # "Synth-Pop"
    genre_confidence: float                          # 0-1
    mood: str                                        # "忧郁/梦幻"
    mood_valence: float                              # 0=消极 1=积极
    mood_energy: float                               # 0=平静 1=激昂
    acousticness: float                              # 0=电子 1=原声
    vocal_type: str                                  # "female/mezzo" / "instrumental"
    dominant_instruments: List[str] = field(default_factory=list)  # ["synth", "guitar"]


# ================ 总输出 ================

@dataclass
class AudioFeatures:
    """音频特征总输出 - audio 包对外的核心数据结构"""
    duration_seconds: float
    tempo: TempoInfo
    key_info: KeyInfo
    pitch_range: PitchRange
    dynamic: DynamicInfo
    spectral: SpectralInfo
    sections: List[SectionInfo]
    chorus_segments: List["ChorusSegment"] = field(default_factory=list)  # Diana 8/8: 高潮段检测 (精选1/2/3)
    style: Optional[StyleInfo] = None               # Diana 3.1: 风格识别可后续填入
    id3_metadata: Optional["ID3Metadata"] = None   # Diana 2.2: 歌曲指纹第一层 (v2-7 新增)

    def to_dict(self) -> dict:
        """转 dict 给 JSON 序列化 (API 响应)"""
        d = asdict(self)
        # id3_metadata 也转 dict (如有)
        if self.id3_metadata is not None:
            d["id3_metadata"] = self.id3_metadata.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AudioFeatures":
        """从 dict 还原 AudioFeatures (老杨 8/17 21:23 修复)
        背景: WebUI session_state 里 features 是 JSON 序列化后的 dict, 直接传给 mureka_prompts 会报 'dict has no attribute vocal_type'.
        每个嵌套 dataclass 都递归还原, 嵌套列表也还原.

        Args:
            d: dict (to_dict 输出来的格式)

        Returns:
            AudioFeatures dataclass (嵌套字段全转 dataclass)
        """
        if not d:
            # 兑底: 构造最小可用 AudioFeatures
            return cls(
                duration_seconds=0.0,
                tempo=TempoInfo(bpm=0, tempo_class="—", tempo_italian="—", tempo_description="—"),
                key_info=KeyInfo(key="—", key_chinese="—", key_description="—", confidence=0),
                pitch_range=PitchRange(low_midi=0, high_midi=0, low_note="—", high_note="—", range_semitones=0),
                dynamic=DynamicInfo(rms_db=-60, dynamic_range_db=0, dynamic_class="—", dynamic_mark="—"),
                spectral=SpectralInfo(brightness_hz=0, spectral_centroid_mean=0),
                sections=[],
            )
        return cls(
            duration_seconds=d.get("duration_seconds", 0),
            tempo=TempoInfo(**d["tempo"]) if isinstance(d.get("tempo"), dict) else d.get("tempo"),
            key_info=KeyInfo(**d["key_info"]) if isinstance(d.get("key_info"), dict) else d.get("key_info"),
            pitch_range=PitchRange(**d["pitch_range"]) if isinstance(d.get("pitch_range"), dict) else d.get("pitch_range"),
            dynamic=DynamicInfo(**d["dynamic"]) if isinstance(d.get("dynamic"), dict) else d.get("dynamic"),
            spectral=SpectralInfo(**d["spectral"]) if isinstance(d.get("spectral"), dict) else d.get("spectral"),
            sections=[SectionInfo(**s) if isinstance(s, dict) else s for s in (d.get("sections") or [])],
            chorus_segments=[ChorusSegment(**c) if isinstance(c, dict) else c for c in (d.get("chorus_segments") or [])],
            style=StyleInfo(**d["style"]) if isinstance(d.get("style"), dict) else d.get("style"),
        )

    def feature_vector(self) -> List[float]:
        """Diana 3.4: 用于相似度计算的特征向量 (6 维)
        Returns:
            [bpm_norm, valence, energy, acousticness, dynamic_norm, brightness_norm]
        """
        return [
            self.tempo.bpm / 200.0,
            self.style.mood_valence if self.style else 0.5,
            self.style.mood_energy if self.style else 0.5,
            self.style.acousticness if self.style else 0.5,
            self.dynamic.dynamic_range_db / 60.0,
            self.spectral.brightness_hz / 8000.0,
        ]