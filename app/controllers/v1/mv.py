"""
MV 模式路由 v2 - MoneyPrinterTurbo MV 模式
v2-7 API 接 signature + history + cache

端点:
- POST /api/v1/mv/upload-audio    上传音乐, 返回 file_id
- POST /api/v1/mv/upload-lyrics   上传歌词文件 (.lrc/.qrc/.txt)
- POST /api/v1/mv/analyze         上传音频 + 歌词, 返回曲调特征 + LLM 意境方案
- GET /api/v1/mv/cache/{audio_id} 查询意境历史 ()
- GET  /api/v1/mv/health          健康检查
"""
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import File, Form, Header, Request, UploadFile
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import lyrics_parser, mv_planner
from app.services.audio import (
    AudioAnalyzerError,
    analyze_audio,
    compute_song_signature,
    extract_id3_metadata,
)
from app.services.mv import get_intent_repository
from app.services.mv.db import init_db as init_mv_db
from app.utils import utils

router = new_router()

# 白名单 (跟 BGM 上传一致)
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
_LYRICS_EXTENSIONS = {".lrc", ".qrc", ".txt"}

# 上传目录
_STORAGE_MV = Path(config.app.get("storage_mv_dir", "storage/mv"))
_STORAGE_MV.mkdir(parents=True, exist_ok=True)
_STORAGE_MV_RESOLVED = _STORAGE_MV.resolve()  # 审计 P0-1: 路径遍历校验基准


def _validate_storage_id(file_id: str, request_id: str) -> Path:
    """审计 P0-1: 校验 file_id 安全, 防止路径遍历 (audio_id / lyrics_file_id)

    返回安全的 Path (绝对路径, 在 _STORAGE_MV 之下)。
    校验: file_id 只含安全字符 + resolve() 在 _STORAGE_MV.resolve() 之下。
    """
    import re
    # 格式: mva- 或 mvl- 前缀 + uuid.hex[:12] + 文件后缀
    if not re.match(r'^mv[al]-[a-f0-9]{12}\.[a-z0-9]{1,5}$', file_id):
        raise HttpException(
            task_id=request_id, status_code=400,
            message=f"invalid file_id format: {file_id}",
        )
    target = (_STORAGE_MV / file_id).resolve()
    # 必须落在 _STORAGE_MV 之下 (防止 .. 绕过)
    try:
        target.relative_to(_STORAGE_MV_RESOLVED)
    except ValueError:
        raise HttpException(
            task_id=request_id, status_code=400,
            message=f"file_id escapes storage dir: {file_id}",
        )
    return target

# MV 意境历史 DB 路径 (独立 SQLite)
_MV_INTENT_DB = config.app.get("mv_intent_db_path", "storage/mv/mv_intent.db")
init_mv_db(_MV_INTENT_DB)


def _save_upload(file: UploadFile, target_dir: Path, allowed_ext: set, prefix: str) -> tuple[str, str]:
    """保存上传文件到目标目录, 返回 (file_id, absolute_path)"""
    request_id = base.get_task_id(file) if hasattr(base, 'get_task_id') else str(uuid.uuid4())[:8]
    safe_name = Path((file.filename or "").strip()).name
    if not safe_name:
        raise HttpException(task_id=request_id, status_code=400, message="filename is empty")
    ext = Path(safe_name).suffix.lower()
    if ext not in allowed_ext:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"unsupported extension: {ext} (allowed: {', '.join(allowed_ext)})",
        )
    file_id = f"{prefix}-{uuid.uuid4().hex[:12]}{ext}"
    target_dir.mkdir(parents=True, exist_ok=True)
    abs_path = target_dir / file_id
    with abs_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return file_id, str(abs_path)


# ================ UPLOAD AUDIO ================

@router.post("/mv/upload-audio", summary="Upload audio for MV analysis")
async def upload_mv_audio(request: Request, file: UploadFile = File(...)):
    """上传音乐文件, 返回 file_id + 解析后的 ID3 metadata (三层识别第一层)"""
    request_id = base.get_task_id(request)
    file_id, abs_path = _save_upload(file, _STORAGE_MV, _AUDIO_EXTENSIONS, "mva")

    # 尝试读 ID3 (失败不影响上传)
    id3_meta = extract_id3_metadata(abs_path)

    return utils.get_response(200, {
        "file_id": file_id,
        "size": os.path.getsize(abs_path),
        "id3": {
            "artist": id3_meta.artist,
            "title": id3_meta.title,
            "album": id3_meta.album,
            "year": id3_meta.year,
        } if id3_meta else None,
    })


# ================ UPLOAD LYRICS ================

@router.post("/mv/upload-lyrics", summary="Upload lyrics file (.lrc/.qrc/.txt)")
async def upload_mv_lyrics(request: Request, file: UploadFile = File(...)):
    """上传歌词文件, 返回解析后的歌词结构"""
    request_id = base.get_task_id(request)
    file_id, abs_path = _save_upload(file, _STORAGE_MV, _LYRICS_EXTENSIONS, "mvl")
    try:
        parsed = lyrics_parser.parse_lyrics_file(abs_path)
    except lyrics_parser.LyricsParseError as exc:
        raise HttpException(task_id=request_id, status_code=400, message=str(exc))
    return utils.get_response(200, {
        "file_id": file_id,
        "line_count": len(parsed),
        "lyrics": parsed,
        "llm_friendly_text": lyrics_parser.format_for_planner(parsed),
    })


# ================ ANALYZE (v2 主端点) ================

@router.post("/mv/analyze", summary="Analyze audio + lyrics, return features + intent (v2 with history + cache)")
async def mv_analyze(
    request: Request,
    audio_id: str = Form(..., description="from /mv/upload-audio"),
    lyrics_text: Optional[str] = Form(None, description="手动粘贴的歌词文本"),
    lyrics_file_id: Optional[str] = Form(None, description="from /mv/upload-lyrics"),
    force_refresh: bool = Form(False, description="强制重新 LLM, 忽略缓存 ()"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id", description="预留 (当前不接业务)"),
):
    """MV 主入口 v2

    v2 新增:
    - song_signature 三层识别 (ID3 > metadata > audio fingerprint)
    - 意境历史查询 ()
    - cache fallback ()
    - cost tracking ()
    """
    request_id = base.get_task_id(request)

    # 1. 找音频文件 (审计 P0-1: 路径遍历校验)
    audio_path = _validate_storage_id(audio_id, request_id)
    if not audio_path.exists():
        raise HttpException(task_id=request_id, status_code=404, message=f"audio_id not found: {audio_id}")

    # 2. 决定歌词来源
    lyrics_str = ""
    lyrics_meta = {"source": "none"}
    if lyrics_text and lyrics_text.strip():
        lyrics_str = lyrics_text.strip()
        lyrics_meta = {"source": "manual_paste", "char_count": len(lyrics_str)}
    elif lyrics_file_id:
        # 审计 P0-1: 路径遍历校验
        lyrics_path = _validate_storage_id(lyrics_file_id, request_id)
        if not lyrics_path.exists():
            raise HttpException(task_id=request_id, status_code=404, message=f"lyrics_file_id not found: {lyrics_file_id}")
        try:
            parsed = lyrics_parser.parse_lyrics_file(str(lyrics_path))
            lyrics_str = lyrics_parser.format_for_planner(parsed)
            lyrics_meta = {
                "source": f"file:{lyrics_file_id}",
                "line_count": len(parsed),
                "char_count": len(lyrics_str),
            }
        except lyrics_parser.LyricsParseError as exc:
            logger.warning(f"歌词文件解析失败: {exc}")
            lyrics_meta = {"source": f"file:{lyrics_file_id}", "parse_error": str(exc)}

    # 3. 音频分析 (三层识别) — 审计 P0-3: 复用 y, sr, 不重复 preprocess_audio
    logger.info(f"mv_analyze: 开始音频分析 {audio_path}")
    try:
        from app.services.audio.analyzer import analyze_audio_with_audio
        from app.services.audio.preprocess import preprocess_audio
        features_obj, y, sr = analyze_audio_with_audio(str(audio_path))
        if y is None or sr is None:
            # 缓存命中: features_obj 已存在, y, sr 需要重新加载 (compute_song_signature 需要)
            y, sr = preprocess_audio(str(audio_path))
        features = features_obj.to_dict()
        id3_meta = features_obj.id3_metadata

        # song_signature (三层识别)
        signature_str, signature_meta = compute_song_signature(
            audio_path=str(audio_path),
            y=y,
            sr=sr,
            duration=features["duration_seconds"],
            bpm=features["tempo"]["bpm"],
            key=features["key_info"]["key"],
            id3_artist=id3_meta.artist if id3_meta else None,
            id3_title=id3_meta.title if id3_meta else None,
        )
    except AudioAnalyzerError as exc:
        raise HttpException(task_id=request_id, status_code=400, message=f"audio analyze failed: {exc}")

    # 4. LLM 综合判断 (4.5)
    logger.info(
        f"mv_analyze: 开始 LLM 规划 "
        f"(signature={signature_str[:60]}..., "
        f"signature_source={signature_meta.get('signature_source', '?')}, "
        f"force_refresh={force_refresh})"
    )
    planner = mv_planner.MvPlanner(db_path=_MV_INTENT_DB)
    plan = planner.build(
        audio_features=features,
        lyrics=lyrics_str,
        audio_id=audio_id,
        song_signature=signature_str,
        duration_seconds=features["duration_seconds"],
        artist=id3_meta.artist if id3_meta else None,
        title=id3_meta.title if id3_meta else None,
        user_id=x_user_id,  # 预留, 当前不接业务
    )

    return utils.get_response(200, {
        "audio_id": audio_id,
        "audio_features": features,
        "lyrics_meta": lyrics_meta,
        "mv_plan": plan,
        # v2 新增字段
        "song_signature": {
            "signature": signature_str,
            "source": signature_meta.get("signature_source", "user_metadata"),
            "components": signature_meta,
        },
        "version": plan.get("_version", 0),
        "source": plan.get("_source", "unknown"),
    })


# ================ CACHE QUERY () ================

@router.get("/mv/cache/{audio_id}", summary="Get intent history for audio_id ()")
async def mv_cache(
    request: Request,
    audio_id: str,
    limit: int = 5,
):
    """查询意境历史 (用于 LLM 进化 prompt)

    Args:
        audio_id: from /mv/upload-audio
        limit: 返回历史条数 (默认 5, 最大 50)
    """
    request_id = base.get_task_id(request)
    # 审计 P0-1: 路径遍历校验 (GET 路由同样需要)
    _validate_storage_id(audio_id, request_id)
    repo = get_intent_repository(_MV_INTENT_DB)
    latest = repo.get_latest(audio_id)
    if not latest:
        raise HttpException(task_id=request_id, status_code=404, message=f"no history for audio_id: {audio_id}")

    all_versions = repo.get_all_versions(audio_id, limit=min(limit, 50))

    return utils.get_response(200, {
        "audio_id": audio_id,
        "latest": {
            "version": latest.version,
            "is_latest": latest.is_latest,
            "source": latest.source,
            "intent": latest.intent,
            "llm_model": latest.llm_model,
            "latency_ms": latest.llm_latency_ms,
            "cost_usd": latest.cost_usd,
            "created_at": latest.created_at,
        },
        "history": [
            {
                "version": v.version,
                "is_latest": v.is_latest,
                "source": v.source,
                "llm_error": v.llm_error,
                "llm_model": v.llm_model,
                "latency_ms": v.llm_latency_ms,
                "cost_usd": v.cost_usd,
                "created_at": v.created_at,
            }
            for v in all_versions
        ],
        "total_versions": repo.count_versions(audio_id),
    })


# ================ HEALTH ================

@router.get("/mv/health", summary="MV module health check")
async def mv_health():
    """检查 mv 模块依赖 (librosa / qrcd / LLM / DB)"""
    health = {
        "librosa": False,
        "qrc_decoder": config.app.get("mv_qrc_decoder", "qrcd"),
        "lyrics_extensions": sorted(_LYRICS_EXTENSIONS),
        "audio_extensions": sorted(_AUDIO_EXTENSIONS),
        "intent_db": False,
        "intent_db_path": _MV_INTENT_DB,
    }
    try:
        import librosa
        health["librosa"] = True
        health["librosa_version"] = librosa.__version__
    except ImportError:
        health["librosa_error"] = "librosa not installed"

    try:
        from app.services.mv.db import SCHEMA_VERSION
        health["intent_db"] = True
        health["intent_db_schema"] = SCHEMA_VERSION
    except Exception as exc:
        health["intent_db_error"] = str(exc)

    return utils.get_response(200, health)