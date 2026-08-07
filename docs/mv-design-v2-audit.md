# MV v2 设计文档审计报告

> 审计时间: 2026-08-07 | 审计对象: mv-design-v2.md | 审计人: Diana

---

## 一、总体评价

文档结构清晰，三大目标（模块独立化、意境持久化、持续进化）方向正确。技术选型合理（librosa + dataclass + SQLite + LLM），5 天工作量估算有参考价值。

**但存在 3 个层面的问题**：
1. **工程层面**：有几个技术方案写法有 bug，直接照做会踩坑
2. **产品层面**：核心价值——"把好听翻译成专业词语"——覆盖不足，缺少风格识别的关键维度
3. **独立化层面**：为"未来独立成产品"做的准备停留在代码标记层面，缺少真正的解耦设计

下文按优先级分 4 级：🔴 必须修、🟡 应该改、🟢 建议加、⚪ 可选优化

---

## 二、🔴 工程级问题（必须修复，否则会踩坑）

### 2.1 `git subtree split` 命令对单文件无效

**文档原文**：
```bash
git subtree split --prefix=app/services/audio_analyzer.py -b audio-standalone
```

**问题**：`git subtree split --prefix` 只支持**目录**，不支持单个文件。执行会报错。

**修复方案**：先把 audio_analyzer 重构为一个**独立包目录**，再做 subtree split：
```
app/services/audio/
├── __init__.py          # 导出公共 API
├── analyzer.py          # analyze_audio() 主入口
├── features/
│   ├── tempo.py         # get_tempo()
│   ├── key.py           # get_key()
│   ├── pitch.py         # get_pitch_range()
│   ├── dynamic.py       # get_dynamic()
│   ├── spectral.py      # get_spectral()
│   └── sections.py      # detect_sections()
├── models.py            # 所有 dataclass 定义
├── fingerprint.py       # 歌曲指纹计算
└── id3_utils.py         # ID3 tag 读取
```

这样 `git subtree split --prefix=app/services/audio -b audio-standalone` 才能正常工作，且剥离子项目后目录结构天然完整。

### 2.2 song_signature 的 SHA1-first-1MB 太脆弱

**文档原文**：
```python
audio_hash = _sha1_first_1mb(audio_path)
return f"fp:{audio_hash[:12]}::{int(duration)}::{int(bpm)}::{key}"
```

**问题**：
- 同一首歌不同编码（128kbps vs 320kbps vs FLAC）→ 前 1MB 完全不同 → 签名不匹配
- 前 1MB 可能是静音/前奏，信息量低
- 真正的音频指纹应该基于**声学特征**，不是文件字节

**修复方案**：用 **Chromaprint/AcoustID**（librosa 生态有 chromaprint-python）：
```python
import chromaprint

def compute_audio_fingerprint(audio_path: str) -> str:
    """基于声学特征计算指纹, 同一首歌不同编码也能匹配"""
    fingerprint, duration = chromaprint.generate(audio_path)
    return f"fp:{fingerprint[:32]}"
```

如果不想引入新依赖，至少用 librosa 的 chroma 特征做 hash：
```python
def compute_chroma_hash(y, sr) -> str:
    chroma = librosa.feature.chroma_stft(y=y, sr=sr)
    # 降采样后取 hash, 对编码差异鲁棒
    downsampled = chroma[:, ::50].flatten()
    return hashlib.sha1(downsampled.tobytes()).hexdigest()[:16]
```

### 2.3 LLM 输出无 JSON Schema 校验

**问题**：文档中 LLM 输出直接写库，没有校验 JSON 是否符合预期结构。LLM 返回格式错误（多/少字段、类型错误）会导致下游崩溃。

**修复方案**：加一层 Pydantic 校验：
```python
from pydantic import BaseModel, ValidationError

class IntentSchema(BaseModel):
    mood_summary: str
    theme_keywords: list[str]
    color_palette: list[str]  # ["#FF5733", ...]
    video_prompts: list[dict]
    transition_style: str
    subtitle_style: str

def validate_intent(raw_json: str) -> IntentSchema | None:
    try:
        return IntentSchema.model_validate_json(raw_json)
    except ValidationError as e:
        logger.warning(f"LLM output schema error: {e}")
        return None  # 触发降级
```

### 2.4 数据库缺少 user_id 字段

**问题**：`mv_intent_history` 表没有 `user_id`。如果产品有多用户场景（视频平台天然多用户），不同用户的意境偏好会串。

**修复方案**：
```sql
CREATE TABLE mv_intent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,                    -- 新增: 用户标识
    audio_id TEXT NOT NULL,
    -- ... 其他字段不变
);
CREATE INDEX idx_intent_user ON mv_intent_history(user_id, song_signature);
```

即使 v2 阶段是单用户，也先把字段留好，后面加比改表结构容易。

### 2.5 视图查询性能问题

**问题**：`mv_intent_latest` 视图用相关子查询找 `MAX(version)`，数据量大了会慢。

**修复方案**：在表上加 `is_latest` 标记，或用窗口函数：
```sql
-- 方案 A: 加 is_latest 字段（写入时维护）
ALTER TABLE mv_intent_history ADD COLUMN is_latest INTEGER DEFAULT 0;
-- 每次写入新 version 时, UPDATE 旧记录 is_latest=0, 新记录 is_latest=1

-- 方案 B: 用 ROW_NUMBER() 窗口函数（SQLite 3.25+）
CREATE VIEW mv_intent_latest AS
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY audio_id ORDER BY version DESC) AS rn
    FROM mv_intent_history
) WHERE rn = 1;
```

---

## 三、🟡 产品级问题（核心价值覆盖不足）

### 3.1 缺少"风格识别"的关键维度

用户痛点是"好听但说不出专业词语"。文档当前的音频特征只有 6 个维度（tempo/key/pitch/dynamic/spectral/sections），这些都是**物理参数**，缺少**音乐学/风格学维度**：

| 缺失维度 | 说明 | 实现方式 | 专业价值 |
|----------|------|----------|----------|
| **Genre 分类** | 流派: 流行/摇滚/电子/R&B/民谣/古典 | librosa 特征 + 预训练分类器 或 LLM 推断 | "这是一首 Synth-Pop" |
| **Mood/情绪** | 情绪: 欢快/忧郁/激昂/宁静/神秘 | valence + energy 特征 → 情绪象限 | "情绪偏暗色系, 带压抑感" |
| **乐器识别** | 主导乐器: 钢琴/吉他/合成器/鼓组 | 频谱模式匹配 或 LLM 推断 | "以合成器铺底, 木吉他主导旋律" |
| **声学/电子度** | acousticness: 原声 vs 电子 | spectral contrast + zero crossing rate | "电子感强, 非原声录音" |
| **人声特征** | 男/女声、音域、唱法 | pitch 统计 + formant 分析 | "女声, 音域跨 1.5 个八度, 气声唱法" |
| **节奏型态** | 4/4 拍、三连音、切分节奏 | onset detection + 节拍模式 | "4/4 拍, 带切分, 律动感强" |

**建议在 Dataclass 中增加**：
```python
@dataclass
class StyleInfo:
    genre: str              # "Synth-Pop"
    genre_confidence: float
    mood: str               # "忧郁/梦幻"
    mood_valence: float     # 0-1, 0=消极 1=积极
    mood_energy: float      # 0-1, 0=平静 1=激昂
    dominant_instruments: list[str]  # ["synth", "guitar"]
    acousticness: float     # 0-1
    vocal_type: str         # "female/mezzo" / "male/baritone" / "instrumental"

@dataclass
class AudioFeatures:
    # ... 现有字段
    style: StyleInfo        # 新增
```

### 3.2 缺少"专业术语翻译"层

用户的核心诉求是"把感觉翻译成专业词语"。当前设计直接从音频特征 → LLM 意境，中间缺少一个**术语映射层**：

```
当前: AudioFeatures → LLM → 意境描述
建议: AudioFeatures → 术语映射 → 专业描述 + LLM → 意境描述
```

**术语映射表示例**：
```python
TEMPO_VOCAB = {
    (0, 60):   ("Largo", "广板", "庄严、舒缓"),
    (60, 76):  ("Adagio", "柔板", "从容、柔和"),
    (76, 108): ("Andante", "行板", "步行速度, 流动"),
    (108, 120):("Moderato", "中板", "中等, 适中"),
    (120, 156):("Allegro", "快板", "快速, 活泼"),
    (156, 200):("Presto", "急板", "急速, 热烈"),
}

KEY_VOCAB = {
    "C major":  ("C大调", "纯粹、明亮、坦荡"),
    "A minor":  ("a小调", "忧郁、内省、温柔"),
    "G major":  ("G大调", "开朗、田园、质朴"),
    # ... 24 个调性
}

DYNAMIC_VOCAB = {
    (0, -20):   ("强动态", "ff", "饱满有力, 冲击感强"),
    (-20, -35): ("中动态", "mf", "适中, 有起伏"),
    (-35, -60): ("弱动态", "p",  "细腻, 内敛"),
}
```

这样 LLM 拿到的不只是 `bpm=72`，而是 `行板(Andante), 从容柔和`，生成质量会高很多。用户也能从这些映射中学到专业词汇。

### 3.3 LLM 进化 Prompt 太模糊

**文档原文**：
```
在之前分析的基础上, 结合新的理解/灵感, 输出优化后的最新版本.
可以是: 更精准的意境表达 / 更丰富的关键词 / 更贴合歌词的段落切分 / 更符合视频拍摄建议的 prompts
```

**问题**：太开放，LLM 不知道该改什么、保留什么，容易每次输出完全不同的东西（不是进化，是随机）。

**改进**：给 LLM 明确的进化指令：
```python
EVOLUTION_PROMPT = """
你之前对这个歌曲做过 {n} 次分析。以下是最近一次的结果:
{latest_version}

用户的反馈/调整:
{user_feedback}  # 如果用户编辑过, 这里带上用户的修改内容

你的任务:
1. 保留上次分析中好的部分（不要推翻重来）
2. 针对以下方面做增量优化:
   - {improvement_target}  # 具体改进方向
3. 如果你认为上次分析已经足够好, 可以只做微调或保持不变
4. 输出必须包含以下字段: {required_fields}

注意: 这是"进化"不是"重做"。如果你没有新的信息或灵感, 保持上次结果也是可以的。
"""
```

### 3.4 缺少歌曲相似度/对比功能

用户场景："这首歌好听" → 下一步自然会问"还有什么类似的？"

**建议**：基于音频特征向量计算相似度：
```python
def compute_feature_vector(features: AudioFeatures) -> np.ndarray:
    """提取特征向量, 用于相似度计算"""
    return np.array([
        features.tempo.bpm / 200,           # 归一化 BPM
        features.style.mood_valence,
        features.style.mood_energy,
        features.style.acousticness,
        features.dynamic.dynamic_range_db / 60,
        features.spectral.brightness_hz / 8000,
    ])

def find_similar_songs(target: AudioFeatures, db_records: list, top_k=5):
    """找最相似的 N 首歌"""
    target_vec = compute_feature_vector(target)
    # 余弦相似度
    ...
```

---

## 四、🟢 产品化/独立化准备

### 4.1 独立产品的接口边界未定义

`# __standalone__` 标记只是注释，不构成真正的解耦。如果未来要独立，需要定义**清晰的接口契约**：

```python
# app/services/audio/__init__.py
"""
Standalone Audio Style Analyzer
===============================
独立使用: pip install standalone-audio-analyzer
依赖: librosa, numpy, chromaprint (可选)

用法:
    from audio_analyzer import analyze_audio
    features = analyze_audio("song.mp3")
    print(features.style.genre)  # "Synth-Pop"
"""

# 公共 API（剥离子项目时只需这些）
__all__ = [
    "analyze_audio",
    "AudioFeatures",
    "StyleInfo",
    "TempoInfo",
    # ...
]
```

### 4.2 缺少缓存层

音频分析（librosa）耗时 10-30 秒，同一首歌重复分析是浪费。

```python
from functools import lru_cache
import hashlib

def _file_hash(path: str) -> str:
    """文件内容 hash, 用于缓存 key"""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

# 分析结果缓存（文件 hash → AudioFeatures）
_analysis_cache: dict[str, AudioFeatures] = {}

def analyze_audio(path: str, use_cache: bool = True) -> AudioFeatures:
    cache_key = _file_hash(path) if use_cache else None
    if cache_key and cache_key in _analysis_cache:
        return _analysis_cache[cache_key]
    
    features = _do_analysis(path)
    if cache_key:
        _analysis_cache[cache_key] = features
    return features
```

### 4.3 缺少音频预处理

上传的音频可能有问题（采样率不一、有静音、音量差异大），需要预处理：

```python
def preprocess_audio(path: str) -> tuple[np.ndarray, int]:
    """音频预处理: 统一采样率 + 去首尾静音 + 峰值归一化"""
    y, sr = librosa.load(path, sr=22050, mono=True)
    
    # 去首尾静音
    y, _ = librosa.effects.trim(y, top_db=30)
    
    # 峰值归一化（不是响度归一化, 但够用）
    y = librosa.util.normalize(y)
    
    return y, sr
```

### 4.4 缺少 LLM 成本/Token 管理

每次进化调用 LLM 都会消耗 token，随着 history 增加成本上升。

```python
@dataclass
class LLMCallMeta:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int

# 写入数据库, 方便追踪成本
ALTER TABLE mv_intent_history ADD COLUMN prompt_tokens INTEGER;
ALTER TABLE mv_intent_history ADD COLUMN completion_tokens INTEGER;
ALTER TABLE mv_intent_history ADD COLUMN cost_usd REAL;
```

### 4.5 缺少并发处理

两个用户同时上传同一首歌 → 同时查库都 miss → 同时调 LLM → 写入两个 version=1。

```python
import threading
_analysis_locks: dict[str, threading.Lock] = {}

def analyze_with_lock(signature: str):
    """同一首歌的分析加锁, 避免重复调用"""
    if signature not in _analysis_locks:
        _analysis_locks[signature] = threading.Lock()
    
    with _analysis_locks[signature]:
        # double-check: 拿到锁后再查一次库
        existing = repo.get_latest(signature)
        if existing:
            return existing
        # 执行分析...
```

---

## 五、⚪ UX 与交互改进

### 5.1 分析过程缺少进度反馈

10-30 秒的等待需要进度提示：

```
[████████░░░░░░░░] 正在分析音频特征... (3/6)
✓ 节奏检测: 72 BPM (行板)
✓ 调性识别: G大调
✓ 音域分析: D3-D5 (2个八度)
⟳ 动态范围分析中...
○ 频谱特征
○ 段落切分
```

### 5.2 版本对比 UI 需要更细化

不只是"选 v2 还是 v3"，应该做**字段级 diff**：

```
┌─────────────┬──────────────────┬──────────────────┐
│ 字段         │ v2               │ v3 (new)         │
├─────────────┼──────────────────┼──────────────────┤
│ mood_summary│ "梦幻电子"        │ "迷幻合成器流行"   │ ← 变更
│ theme_keywords│ [梦幻, 电子, 夜] │ [迷幻, 合成器, 夜] │ ← 变更
│ color_palette│ [#4A90D9, ...]   │ [#4A90D9, ...]   │ ← 相同
│ video_prompts│ (3 段)           │ (4 段)           │ ← 新增段
└─────────────┴──────────────────┴──────────────────┘
```

### 5.3 缺少导出/分享功能

用户分析完一首歌，自然想分享"专业描述"：

- 导出为图片（色板 + 关键词 + 意境描述，适合社交媒体）
- 导出为 JSON（给其他系统消费）
- 分享链接（如果独立成产品，这是传播入口）

### 5.4 缺少用户学习/词汇积累

用户的核心诉求是"学会用专业词语描述音乐"。系统可以：
- 每次分析后展示用到的专业术语 + 解释
- 积累用户的"词汇库"（学过哪些词）
- 做一个"音乐风格知识库"模块

---

## 六、优先级排序

| 优先级 | 改进项 | 影响面 | 工作量 |
|--------|--------|--------|--------|
| P0 | 2.1 修复 git subtree split | 阻塞独立化 | 0.5 天 |
| P0 | 2.2 修复 song_signature | 影响核心功能正确性 | 0.5 天 |
| P0 | 2.3 加 JSON Schema 校验 | 影响系统稳定性 | 0.5 天 |
| P0 | 2.4 加 user_id 字段 | 影响多用户 | 0.1 天 |
| P1 | 3.1 增加风格识别维度 | 核心价值 | 2 天 |
| P1 | 3.2 术语映射层 | 核心价值 | 1 天 |
| P1 | 3.3 改进进化 Prompt | 影响输出质量 | 0.5 天 |
| P1 | 4.3 音频预处理 | 影响分析质量 | 0.5 天 |
| P2 | 4.2 缓存层 | 性能 | 0.5 天 |
| P2 | 4.5 并发处理 | 稳定性 | 0.5 天 |
| P2 | 5.1 进度反馈 | 用户体验 | 0.5 天 |
| P2 | 3.4 相似度功能 | 产品扩展 | 1 天 |
| P3 | 4.1 接口契约 | 独立化准备 | 0.5 天 |
| P3 | 4.4 Token 管理 | 成本控制 | 0.5 天 |
| P3 | 5.2-5.4 UX 增强 | 用户体验 | 1.5 天 |

**建议调整后的工作量**：原 5 天 → 建议拆为两期：
- **v2.0（核心可用）**: P0 + P1 + 4.3 = 约 5.5 天
- **v2.1（产品化）**: P2 + P3 = 约 4.5 天

---

## 七、总结

文档的**工程骨架是好的**（dataclass、纯函数、版本化存储、降级策略），但在以下三个方向有提升空间：

1. **技术细节有 4 个 bug 级问题**（git subtree、song_signature、JSON 校验、user_id），不修会踩坑
2. **核心价值覆盖不足**——用户要的是"专业词语"，但音频特征只有物理参数，缺少风格/情绪/乐器等音乐学维度，也缺少术语翻译层
3. **独立化准备停留在表面**——需要从接口契约、缓存、预处理等层面做真正的解耦

如果把 P0+P1 做完，这个模块的"好听→专业描述"能力会有质的提升，独立成产品的底气也更强。
