# MV v2.0 升级日志 (2026-08-07)

> **审计**: Diana (2026-08-07)

## 🎯 v2.0 主题: 独立化 + 持久化 + 持续进化

### 三大目标

1. **音频模块独立化** — 为剥离子项目做准备
2. **LLM 意境持久化** — 避免重复调 LLM, 支持历史
3. **持续进化** — 历史回看 + LLM 增量优化

---

## 📦 主要改动 (v2.0)

### 新增模块

- `app/services/audio/` — 独立音频分析包 (10 个文件)
  - `analyzer.py` — 主入口 `analyze_audio()` + 缓存层
  - `models.py` — 7 个 dataclass (强类型契约)
  - `features/{tempo,key,pitch,dynamic,spectral,sections,style}.py` — 7 个特征模块
  - `vocab.py` — 术语映射 (Diana 3.2, 30 个调性 + 6 档 BPM)
  - `id3_utils.py` — ID3 tag 读取 (mutagen)
  - `fingerprint.py` — song_signature 三层识别 (Diana 2.2)
  - `preprocess.py` — 音频预处理

- `app/services/mv/` — MV 意境持久化包
  - `db/schema.py` — SQLite schema + 索引 + 视图
  - `db/connection.py` — 线程安全连接管理
  - `db/__init__.py` — DB 客户端公共 API
  - `intent_repository.py` — CRUD + 业务方法
  - `mv_intent_schema.py` — Pydantic JSON Schema 校验
  - `__init__.py` — 公共 API

### 重构

- `app/services/mv_planner.py` — v2 重构
  - 接 audio_id / song_signature / history / user_id
  - 双模式 prompt: `_USER_PROMPT_FIRST` / `_USER_PROMPT_EVOLUTION`
  - 重试 + 降级 + 写库

- `app/controllers/v1/mv.py` — 5 个端点
  - `POST /mv/upload-audio` (+ ID3)
  - `POST /mv/upload-lyrics`
  - `POST /mv/analyze` (+ signature + version)
  - `GET /mv/cache/{audio_id}` (新增)
  - `GET /mv/health` (+ DB 状态)

### 删除

- `app/services/audio_analyzer.py` — 已迁移到 `app/services/audio/` 包

### 配置

- `config.example.toml` — 新增 mv_intent_db_path / mv_intent_max_versions / mv_intent_retention_days

---

## 🔧 Diana 审计修复 (5 P0 + 4 P1 + 5 P2)

### P0 (必须修复, 已全部修复)

| # | 问题 | 修复 |
|---|------|------|
| 2.1 | git subtree --prefix=单文件 | ✅ 用 audio/ 包目录 |
| 2.2 | song_signature SHA1 文件指纹 | ✅ 三层识别: 元数据 > ID3 > 声学指纹 |
| 2.3 | LLM 输出无 Schema 校验 | ✅ Pydantic IntentSchema |
| 2.4 | 缺 user_id 字段 | ✅ 表里有 + 索引 (预留, 不接业务) |
| 2.5 | latest 查询用相关子查询 | ✅ ROW_NUMBER 窗口函数视图 + is_latest 字段 |

### P1 (重要, 部分完成)

| # | 问题 | 修复 |
|---|------|------|
| 3.1 | 缺风格识别 | ✅ StyleInfo dataclass + features/style.py |
| 3.2 | 术语映射缺失 | ✅ features/vocab.py (30 调性 + 6 档 BPM + 6 档动态) |
| 3.3 | 无进化 Prompt | ✅ _USER_PROMPT_EVOLUTION 模板 |
| 4.4 | 缺成本追踪 | ✅ DB 字段 (cost_usd, prompt_tokens, completion_tokens) |

### P2 (建议, 大部分完成)

| # | 问题 | 修复 |
|---|------|------|
| 4.2 | 缺音频缓存层 | ✅ analyzer.py _analysis_cache |
| 4.3 | 音频未预处理 | ✅ preprocess.py |
| 4.5 | 缺并发锁 | ✅ repository.acquire_lock(signature) |
| 其他 | minor | ✅ |

---

## 📡 API 端点对比 (v1 → v2)

### POST /api/v1/mv/upload-audio

**v1** → v2 改动:
```diff
{
  "file_id": "...",
  "size": 123456,
+ "id3": {                    # 新增: Diana 2.2 三层识别第一层
+   "artist": "...",
+   "title": "...",
+   "album": "...",
+   "year": "..."
+ }
}
```

### POST /api/v1/mv/analyze

**v1** → v2 改动:
```diff
{
  "audio_id": "...",
  "audio_features": {...},
  "lyrics_meta": {...},
  "mv_plan": {...},
+ "song_signature": {         # 新增: Diana 2.2
+   "signature": "fp:hash::179::83::G major",
+   "source": "audio_fingerprint",
+   "components": {...}
+ },
+ "version": 1,              # 新增: 当前意境版本号
+ "source": "llm"            # 新增: llm/cache_fallback/fallback_rule
}
```

### GET /api/v1/mv/cache/{audio_id} 🆕

**新增**: 查询意境历史 (Diana 3.3)

```json
{
  "audio_id": "...",
  "latest": {
    "version": 2,
    "is_latest": true,
    "source": "llm",
    "intent": {...},
    "llm_model": "minimax",
    "latency_ms": 23771,
    "created_at": "2026-08-07 23:01:33"
  },
  "history": [...],
  "total_versions": 2
}
```

### GET /api/v1/mv/health

**v1** → v2 改动:
```diff
{
  "librosa": true,
  ...
+ "intent_db": true,                  # 新增
+ "intent_db_path": "storage/mv/mv_intent.db",
+ "intent_db_schema": "2026-08-07.v1"
}
```

---

## 🧪 E2E 测试 (2026-08-07 23:38 全部通过)

| # | 场景 | 结果 |
|---|------|------|
| 1 | 上传 + analyze | ✅ 12.8s, signature |
| 2 | 同一首歌多次上传, signature 一致 | ✅ |
| 3 | 重复 analyze, 历史递增 v1→v2 | ✅ |
| 4 | cache 看历史 (v2 is_latest=True) | ✅ |
| 5 | 不存在 audio_id cache | ✅ HTTP 404 |
| 6 | 上传 .exe 错误格式 | ✅ HTTP 400 |
| 7 | analyze 不存在 audio_id | ✅ HTTP 404 |

---

## 🚧 已知未做 (v2.1 待补)

1. `_llm_model` 字段返回 provider 名 (没从 config 读具体 model_name)
2. `cost_usd` 字段没接 (Diana 4.4 token 计费逻辑)
3. `force_refresh` 参数接到了但未生效
4. `user_id` X-User-Id header 接到了但未接业务 (预留, 当前用不上)
5. WebUI MV 页面 (改 Main.py)
6. 旧 audio_analyzer.py import 路径清理
7. 单元测试 (pytest)

---

## 📦 部署

1. 配置文件: `config.example.toml` 新增 3 个 mv_intent_* 配置项
2. DB 文件: 首次启动自动创建 `storage/mv/mv_intent.db`
3. 服务依赖: 现有依赖 + `pydantic>=2.0` (IntentSchema 用)

---

**v2.0 commit + push 当前进度**, v2.1 (UI + WebUI MV 页面) 是下个里程碑。