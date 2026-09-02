# MV 自动化功能改造计划

## 🎯 目标

把 MoneyPrinterTurbo 从「主题词 → 视频」扩展为支持「音乐 → MV」流程：

```
上传音乐 mp3
  ↓
[1] 音频特征分析 (librosa)
  ↓
[2] 歌词识别 (可选, Whisper / LRClib)
  ↓
[3] LLM 综合判断: 曲调 + 歌词 → 意境 + 主题 + 视频搜索 prompts
  ↓
[4] Pexels/Pixabay/Coverr 多源搜索素材
  ↓
[5] MV 风格合成: 跟着节拍切镜头 + 转场
  ↓
输出 .mp4 MV 成片
```

## 📐 改造原则

1. **保守扩展**: 不动老流程, 新增 MV 平行分支
2. **LLM 一步到位**: 曲调特征 + 歌词 → 意境 + 主题 + 搜索 prompts, 不做多阶段规则
3. **歌词可附加**: 上传 mp3 时可选附 lyrics.txt, LLM 一起消化
4. **回复简洁**: 回复信息过长常被 truncated — 计划只写关键决策

---

## 🏗️ 架构 (3 层 + 3 步)

### 3 个新增 service 模块

```
app/services/
├── audio_analyzer.py    # [新] 音频特征提取
├── mv_planner.py        # [新] LLM 综合: 曲调+歌词 → 主题/prompts/段落
└── mv_builder.py        # [新] MV 风格合成 (复用 video.py + 新增节拍切镜头)
```

### 1 个新路由 + 1 个新 Streamlit tab

```
app/controllers/v1/
└── mv.py                # [新] POST /mv/analyze, POST /mv/build

webui/Main.py
└── [新增 tab] "🎵 MV 模式"
```

---

## 🔧 模块 1: audio_analyzer.py

**职责**: 上传 mp3 → 提取曲调要素

**输入**: 音频文件路径
**输出**: JSON dict
```json
{
  "duration_seconds": 234.5,
  "tempo_bpm": 78.4,
  "tempo_class": "慢板",
  "key": "A minor",
  "key_confidence": 0.83,
  "pitch_range": {"low": "A3", "high": "D5", "range_semitones": 28},
  "sections": [
    {"start": 0, "end": 18, "intensity": "low", "label": "前奏"},
    {"start": 18, "end": 75, "intensity": "medium", "label": "主歌"},
    {"start": 75, "end": 130, "intensity": "high", "label": "副歌"}
  ],
  "dynamic_db": 14.5,
  "spectral_brightness_hz": 2100.0
}
```

**实现要点**:
- `librosa.load(sr=22050, mono=True)`
- BPM: `librosa.beat.beat_track`
- 调性: Krumhansl-Schmuckler 算法 (chroma_cqt + 12 大调 + 12 小调 profile 相关)
- 段落: `librosa.segment.agglomerative(chroma, 6)` + RMS 能量辅助标记强度
- 音域: `librosa.piptrack` → midi 转 note 名

**测试**: 10 首不同风格音乐 (民谣/古风/流行/摇滚) 验证

---

## 🔧 模块 2: mv_planner.py (LLM 一步到位)

**职责**: 接收 audio_features + lyrics → 生成完整 MV 制作方案

**输入**:
```json
{
  "audio_features": { /* 模块1输出 */ },
  "lyrics": "又见炊烟升起..."  // 可选
}
```

**输出** (LLM 返回, JSON schema):
```json
{
  "mood_summary": "一首带有淡淡忧伤的民谣, 思乡与亲情交织",
  "theme_keywords": ["故乡", "炊烟", "母亲", "黄昏", "离别"],
  "color_palette": ["暖黄", "暮色", "灰蓝"],
  "video_prompts": [
    {"section_index": 0, "label": "前奏", "prompt": "sunset village smoke chimney rural slow motion", "style": "warm cinematic"},
    {"section_index": 1, "label": "主歌", "prompt": "old man walking countryside autumn golden hour", "style": "nostalgic warm"},
    {"section_index": 2, "label": "副歌", "prompt": "mother figure silhouette window warm light", "style": "emotional close-up"}
  ],
  "transition_style": "fade",
  "subtitle_style": "意境词浮现在画面中央, 跟随节拍淡入淡出"
}
```

**LLM Prompt 模板** (核心):
```
你是 MV 导演. 请根据歌曲曲调特征和歌词, 输出一份 MV 拍摄方案 JSON.

# 曲调特征
- BPM: 78.4 (慢板 - 抒情)
- 调性: A 小调 (忧伤深沉)
- 段落: 前奏 18s / 主歌 57s / 副歌 55s
- 动态: 14.5 dB (适中, 有明显高潮段)

# 歌词 (可选)
[歌词文本]

# 输出要求 (严格 JSON)
{
  "mood_summary": "100字以内的意境总结",
  "theme_keywords": ["5-8个核心意象词"],
  "color_palette": ["3-5个色调关键词"],
  "video_prompts": [
    {"section_index": 0, "label": "前奏", "prompt": "英文 Pexels 搜索词 (3-5词)", "style": "风格描述"},
    ...
  ],
  "transition_style": "fade | cut | dissolve | zoom",
  "subtitle_style": "字幕风格描述"
}
```

**实现要点**:
- 复用现有 `app/services/llm.py` 的 OpenAI/Azure/GLM client
- LLM 调用失败时降级到**纯规则版** MoodMapper (调性 + BPM 关键词表)
- LLM 输出严格 JSON, 用 `json.loads` + `try/except` 兜底

**测试**: 5 首有 LLM 服务的歌曲, 验证 JSON 格式稳定 + 意境合理

---

## 🔧 模块 3: mv_builder.py

**职责**: 按 planner 输出 + 搜索回来的素材 → 合成 MV

**输入**:
```json
{
  "audio_id": "uuid-xxx",
  "video_prompts": [{"section_index": 0, "prompt": "...", "duration": 18}, ...],
  "downloaded_videos": ["/storage/materials/pexels_001.mp4", ...]
}
```

**输出**: `/storage/tasks/{task_id}/mv_final.mp4`

**实现要点**:
- 复用 `app/services/video.py` 的合成逻辑
- **新增**: 按段落时长切分视频片段 (不平均切)
- **新增**: 跟着 BPM 的转场节拍 (副歌段加快切换, 主歌段慢切)
- **新增**: 意境词字幕 (用现有 `app/services/subtitle.py` 的 ASS 生成能力)
- moviepy 2.x + FFmpeg 软编码

**测试**: 1 首完整流程跑通

---

## 🌐 API 端点

### POST /api/v1/mv/analyze
```json
// 请求: multipart/form-data
{ "file": <mp3 binary>, "lyrics": "可选歌词文本" }

// 响应:
{
  "code": 200,
  "data": {
    "audio_id": "uuid-xxx",
    "audio_features": { ... },
    "lyrics_detected": false,
    "planner_result": { /* mood_summary + theme_keywords + video_prompts */ }
  }
}
```

### POST /api/v1/mv/build
```json
// 请求: JSON
{
  "audio_id": "uuid-xxx",
  "video_prompts": [...],
  "video_material_choices": {
    "section_index_0": ["pexels:123456", "pixabay:789"],
    "section_index_1": ["pexels:234567"]
  }
}

// 响应: 标准 task response (异步任务, 返回 task_id)
```

---

## 🖥️ WebUI 新 Tab: "🎵 MV 模式"

**位置**: Streamlit `webui/Main.py` 加一个 `st.tabs(["📝 主题模式", "🎵 MV 模式"])`

**流程**:
```
[步骤 1] 上传音乐 + (可选) 歌词
   ↓
[步骤 2] 后台分析: 展示 BPM/调性/段落 + LLM 意境 + 主题关键词
   ↓
[步骤 3] 表格: 每个段落显示 prompt + 3 个候选视频 (用户可选)
   ↓
[步骤 4] 一键生成 MV
```

---

## 📅 实施步骤 (按风险递增)

| 步骤 | 工作量 | 依赖 | 可验证 |
|------|--------|------|--------|
| **Step 1: audio_analyzer.py** | 0.5 天 | librosa 已装 | 单元测试 10 首 |
| **Step 2: mv_planner.py (LLM)** | 1 天 | OpenAI/GLM key | 5 首风格各异 |
| **Step 3: 路由 /api/v1/mv/analyze** | 0.5 天 | Step 1+2 | curl 测试 |
| **Step 4: 素材搜索 + 用户选择 UI** | 1.5 天 | 复用 material.py | WebUI 实操 |
| **Step 5: mv_builder.py (合成)** | 2 天 | 复用 video.py | 完整 MV 出片 |
| **Step 6: WebUI "MV 模式" tab** | 1 天 | Step 3+5 | 端到端测试 |

**总计**: ~6.5 天 (一周)

---

## ⚠️ 5 个待决策项

1. **音频分析库**: librosa (Python, 稳但慢) / essentia (C++, 快) / audioread (简单)
2. **LLM 选择**: 复用现有 GLM-4 / OpenAI / 加 Claude? 国产推荐 GLM-4
3. **歌词来源**: 
   - **A**: 用户手动粘贴 (简单, 但用户操作多)
   - **B**: Whisper 自动识别 (mp3 上传后转, 慢但全自动)
   - **C**: LRClib 在线搜索 (海外曲友好, 国内曲不全)
   - **D**: A+B 组合 (用户可粘贴, 没粘贴用 Whisper)
4. **视频素材源**: 
   - Pexels/Pixabay/Coverr 已有
   - 是否加 Coverr / 国产摄图网 (pexels 国内有时慢)
5. **MV 字幕策略**: 
   - **A**: 意境词 + 关键歌词 (推荐)
   - **B**: 全歌词跟随
   - **C**: 仅意境词, 不显示歌词

---

## 📁 文件变更清单

### 新增文件 (5 个)
- `app/services/audio_analyzer.py`
- `app/services/mv_planner.py`
- `app/services/mv_builder.py`
- `app/controllers/v1/mv.py`
- `test/services/test_mv.py`

### 修改文件 (3 个)
- `app/router.py` (注册 mv router)
- `app/services/__init__.py` (导出新 service)
- `webui/Main.py` (新增 "MV 模式" tab)

### 配置变更 (1 个)
- `requirements.txt` (加 `librosa`)

---

## 🎬 完成标准

- [ ] 上传一首 mp3, 10 秒内返回曲调要素 + LLM 意境
- [ ] 至少 3 个视频源能搜到匹配素材
- [ ] 用户能选素材 → 一键生成 MV
- [ ] MV 跟着节拍切镜头, 有意境词字幕
- [ ] 全流程端到端跑通 1 首完整歌曲

---

**拍板后, 按 Step 1→6 顺序开干**

---

## 📍 v2 实施进度 (2026-08-07 23:38 完成)

v2 设计文档: [docs/mv-design-v2.md](./mv-design-v2.md)
Diana 审计报告: [docs/mv-design-v2-audit.md](./mv-design-v2-audit.md)
v2 升级日志: [CHANGELOG_MV_V2.md](../CHANGELOG_MV_V2.md)

**已交付** (v2-1 → v2-8):
- ✅ Step 1 音频特征分析 (用 v2 audio/ 包)
- ✅ Step 2 歌词识别 (lrc/qrc/txt)
- ✅ Step 3 LLM 综合判断 (带历史进化)
- ⏸ Step 4-6 (素材搜索 + 合成 + UI) — v2.1 待补

**核心能力已可用**, 下个里程碑 v2.1 (WebUI MV 页面 + Pexels 集成)。