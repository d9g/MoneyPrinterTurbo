"""MV 音乐分析面板：音频分析、意境（intent）分析、高潮段选择与提示词生成。

这一整块原先内联在 webui/Main.py 里。Main.py 需要与上游
harry0703/MoneyPrinterTurbo 保持同步，把上千行的自定义代码放在其中，
每次同步都要在同一处反复解冲突。抽成独立模块后，Main.py 只剩几行调用点。

约定：
- 本模块不反向 import Main（会造成循环导入）。翻译函数走 webui.translation
  这个注册点：Main.py 定义完 tr 后先 set_translator(tr)，再导入本模块，
  这样顶层 @st.dialog(tr(...)) 装饰器在导入期也能拿到真正的 tr。
- 供 Main.py 调用的入口（无下划线前缀）：
    render_audio_analysis_panel / render_mv_analysis_dialog /
    render_mv_analysis_settings / apply_pending_mv_audio
"""
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from loguru import logger

from app.config import config
from app.utils import utils
from webui.translation import tr


# ================ 音频分析模块 () ================
# 上传音频后点"分析这首音乐"按钮 → 调用 mv.analyze 服务 →
# 回显曲调特征 + 意境总结 + 一键填充到 video_script / video_terms 两个输入框。
# 缓存策略: 同 song_signature 重跑 ≤ 3 次, 超过读缓存; 缓存超过 180 天才允许重跑 (Q5).

_MV_CACHE_REANALYZE_LIMIT = 5  # 同 signature 重跑上限 (默认5,调试时可调)
_MV_CACHE_TTL_DAYS = 180          # 超过 N 天才允许重新调 LLM (半年到 1 年阈值下限)
_MV_AUDIO_SESSION_KEY = "mv_audio_analysis_result"  # session_state 里存结果的 key
# 改为一次性信号. 原因: flag=True 会让后续所有 rerun 都弹, 主界面其他
# 选项改动都触发弹窗. 现在: 弹后立即 pop, 只有用户主动点击才产生新信号
_MV_DIALOG_REQUEST_KEY = "mv_audio_analysis_dialog_request"  # 一次性信号: True=下轮弹
_MV_DIALOG_FLAG_KEY = _MV_DIALOG_REQUEST_KEY  # 向后兼容别名 (遗留代码可能还用)
_MV_PENDING_APPLY_KEY = "mv_audio_pending_apply"  # dialog callback 写的待应用队列 (避免直接改 video_script / video_terms widget key)
_MV_FORCE_RERUN_KEY = "mv_force_rerun"  # 调试开关: True = 绕过缓存强制重跑 LLM


def _init_mv_runtime():
    """按需初始化 mv 模块 DB + IntentRepository (单例, 幂等)"""
    from app.config import config
    from app.services.mv.db import init_db as init_mv_db
    from app.services.mv import get_intent_repository

    db_path = config.app.get("mv_intent_db_path", "storage/mv/mv_intent.db")
    init_mv_db(db_path)
    # get_intent_repository 也是模块级单例, db_path 只首次需要
    repo = get_intent_repository(db_path)
    return db_path, repo


def _compute_song_signature_for_upload(audio_path: str, features: dict, id3_meta, y=None, sr=None) -> tuple[str, dict]:
    """跟 mv.py 路由里一致的三层签名计算 (ID3 > metadata > audio fingerprint)

    审计 P0-3: 如果上游调用方 (analyze_audio_with_audio) 已加载 y, sr, 复用它们,
    避免重复 preprocess_audio (librosa.load 耗时 2-5 秒)。
    """
    from app.services.audio import compute_song_signature
    from app.services.audio.preprocess import preprocess_audio

    if y is None or sr is None:
        y, sr = preprocess_audio(audio_path)
    signature_str, signature_meta = compute_song_signature(
        audio_path=audio_path,
        y=y,
        sr=sr,
        duration=features["duration_seconds"],
        bpm=features["tempo"]["bpm"],
        key=features["key_info"]["key"],
        id3_artist=id3_meta.artist if id3_meta else None,
        id3_title=id3_meta.title if id3_meta else None,
    )
    return signature_str, signature_meta


def _should_run_llm(repo, audio_id: str, song_signature: str) -> tuple[bool, dict, str]:
    """Q5 缓存策略判断

    bug: 同一个歌分析三次都 first_run
    根因: webui 每次生成新 uuid file_id (storage/mv/mva-aaa.mp3), DB 按 file_id 查
    永远查不到历史记录
    修法: 改为按 song_signature 查询 (相同歌永远同 signature, 跟上传次数无关)
    audio_id 参数保留以便调用者存历史

    Returns:
        (should_run, latest_record_or_None, reason)
        reason: 'first_run' / 'within_limit' / 'cache_fresh' / 'cache_expired' / 'force_rerun'
    """
    from datetime import datetime, timedelta

    # 调试开关, 勾选后绕过缓存强制重跑 LLM
    import streamlit as st
    if st.session_state.get(_MV_FORCE_RERUN_KEY, False):
        return True, None, "force_rerun (debug)"

    # bug fix: 按 song_signature 查 (跨 audio_id 重用)
    latest = repo.get_latest_by_signature(song_signature) if song_signature else None
    if not latest:
        return True, None, "first_run"

    # 同 signature 看重跑次数 (按 song_signature 数 history, 跨 audio_id 计入)
    version_count = repo.count_versions_by_signature(song_signature)
    if version_count < _MV_CACHE_REANALYZE_LIMIT:
        return True, latest, f"within_limit (v{version_count}/{_MV_CACHE_REANALYZE_LIMIT})"

    # version_count >= 3, 看时间间隔
    last_update = None
    parse_failed = False
    try:
        last_update = datetime.fromisoformat(latest.created_at.replace("Z", "+00:00")) if isinstance(latest.created_at, str) else latest.created_at
        if last_update is None:
            parse_failed = True
        elif last_update.tzinfo:
            from datetime import timezone
            last_update_naive = last_update.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            last_update_naive = last_update
    except Exception:
        # 审计 P2-7: 解析失败应允许重跑, 而非永远复用缓存
        parse_failed = True

    if parse_failed or last_update is None:
        return True, latest, "parse_failed (allow rerun)"

    if datetime.utcnow() - last_update_naive > timedelta(days=_MV_CACHE_TTL_DAYS):
        return True, latest, f"cache_expired (>{_MV_CACHE_TTL_DAYS} days)"

    return False, latest, "cache_fresh"


def _render_mureka_prompts_section(features: dict, version: int):
    """弹窗内增加 AI 歌曲提示词 (Mureka) 折叠段

    读取 features → 调用 generate_mureka_prompts() 生成 4 个提示词
    - zh_short / zh_long (中文精简 + 详细)
    - en_short / en_long (英文精简 + 详细, 意译)

    UI 布局 (st.expander, 默认折叠):
    - 详细程度 radio (精简 / 详细) - 动态切换同一特征下的两种粒度
    - 中文代码块 + ️一键复制 (st.code)
    - 英文代码块 + 一键复制
    - 应用到视频脚本按钮 (复用现有 _apply_keywords_callback)

    Args:
        features: AudioFeatures dict (从 session 里读)
        version: 源版本号 (写入 _MV_PENDING_APPLY_KEY 透传)
    """
    # 1. features -> AudioFeatures dataclass (mureka_prompts 需要 dataclass)
    try:
        from app.services.audio.mureka_prompts import generate_mureka_prompts
        from app.services.audio.models import AudioFeatures as AudioFeaturesDC
        # 修复: 用 from_dict() 递归还原嵌套 dataclass
        # 之前直接传 dict, style.vocal_type 等嵌套属性访问会报 'dict has no attribute'
        af = AudioFeaturesDC.from_dict(features) if isinstance(features, dict) else features
        prompts = generate_mureka_prompts(af)
    except Exception as exc:
        st.warning(f"无法生成 Mureka 提示词: {type(exc).__name__}: {exc}")
        return

    # 2. 详细程度 radio (每个 session 独立)
    detail_key = "mv_dlg_mureka_detail"
    if detail_key not in st.session_state:
        st.session_state[detail_key] = "short"
    detail = st.radio(
        "详细程度",
        options=["short", "long"],
        format_func=lambda x: "精简 ~6 词" if x == "short" else "详细 ~9 词",
        index=0 if st.session_state[detail_key] == "short" else 1,
        key=detail_key,
        horizontal=True,
        help="精简用于快速生成;详细用于精细控制 song structure",
    )

    # 3. 中文版 + 复制
    zh_text = prompts["zh_short"] if detail == "short" else prompts["zh_long"]
    st.markdown("**简体中文**")
    st.code(zh_text, language=None)

    # 4. 英文版 + 复制
    en_text = prompts["en_short"] if detail == "short" else prompts["en_long"]
    st.markdown("**English Version**")
    st.code(en_text, language=None)

    st.caption(
        "💡 中英双版可用于 Mureka / Suno / Udio 等 AI 歌曲生成。英文版为意译"
        "（warm/intimate/Chinese folk singer style raw vocal quality）"
    )

    # 5. 应用按钮 - 把简体中文版词条加入 video_terms
    def _apply_mureka_keywords_callback(zh_short: str, zh_long: str, ver: int):
        pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
        # 拆逗号 → 词条列表
        new_terms = [t.strip() for t in zh_short.split(",") if t.strip()]
        if not new_terms:
            return
        existing_terms = pending.get("video_terms_append", "")
        new_terms_str = ", ".join(new_terms)
        if existing_terms.strip():
            pending["video_terms_append"] = (
                existing_terms.rstrip().rstrip(",") + ", " + new_terms_str
            )
        else:
            pending["video_terms_append"] = new_terms_str
        pending["keyword_count"] = pending.get("keyword_count", 0) + len(new_terms)
        pending["source_version"] = pending.get("source_version", ver)
        st.session_state[_MV_PENDING_APPLY_KEY] = pending
        st.rerun(scope="app")

    st.button(
        "应用到视频脚本关键词",
        key="mv_dlg_apply_mureka",
        use_container_width=True,
        type="secondary",
        on_click=_apply_mureka_keywords_callback,
        args=(prompts["zh_short"], prompts["zh_long"], version),
    )


def _format_audio_features_for_humans(features: dict) -> str:
    """曲调特征 → 人类可读的中文摘要（含专业词汇）"""
    tempo = features.get("tempo", {})
    key_info = features.get("key_info", {})
    pitch = features.get("pitch_range", {})
    dynamic = features.get("dynamic", {})
    spectral = features.get("spectral", {})
    style = features.get("style") or {}
    id3_meta = features.get("id3_metadata") or {}

    lines = []
    if id3_meta.get("title") or id3_meta.get("artist"):
        title = id3_meta.get("title") or "(未知标题)"
        artist = id3_meta.get("artist") or "(未知歌手)"
        lines.append(f"🎼 歌曲: **{title}** — {artist}")
    else:
        lines.append(f"🎼 音频时长: **{features.get('duration_seconds', 0):.1f} 秒**")

    # 节奏
    bpm = tempo.get("bpm")
    tempo_class = tempo.get("tempo_class", "")
    tempo_italian = tempo.get("tempo_italian", "")
    tempo_desc = tempo.get("tempo_description", "")
    if bpm:
        lines.append(f"🥁 **节奏 (Tempo)**: {bpm:.0f} BPM → {tempo_class} ({tempo_italian}) — {tempo_desc}")

    # 调性
    key_name = key_info.get("key", "")
    key_cn = key_info.get("key_chinese", "")
    key_desc = key_info.get("key_description", "")
    confidence = key_info.get("confidence", 0)
    if key_name:
        lines.append(f"🎹 **调性 (Key)**: {key_name} ({key_cn}, 置信度 {confidence:.0%}) — {key_desc}")

    # 音域
    low = pitch.get("low_note", "")
    high = pitch.get("high_note", "")
    semitones = pitch.get("range_semitones", 0)
    if low and high:
        lines.append(f"🎤 **音域 (Pitch Range)**: {low} → {high}, 跨 {semitones} 个半音")

    # 动态
    dyn_db = dynamic.get("dynamic_range_db")
    dyn_class = dynamic.get("dynamic_class", "")
    dyn_mark = dynamic.get("dynamic_mark", "")
    if dyn_db is not None:
        lines.append(f"🔊 **动态 (Dynamics)**: {dyn_db:.1f} dB, {dyn_class} (力度记号 {dyn_mark})")

    # 频谱亮度
    bright = spectral.get("brightness_hz")
    if bright:
        lines.append(f"✨ **频谱亮度 (Brightness)**: {bright:.0f} Hz")

    # 段落数
    sections = features.get("sections", [])
    if sections:
        lines.append(f"📐 **段落 (Sections)**: {len(sections)} 段")

    # 风格识别 ()
    if style:
        genre = style.get("genre", "")
        genre_conf = style.get("genre_confidence", 0)
        mood = style.get("mood", "")
        valence = style.get("mood_valence")
        energy = style.get("mood_energy")
        acousticness = style.get("acousticness")
        vocal_type = style.get("vocal_type", "")
        instruments = style.get("dominant_instruments", [])
        if genre:
            lines.append(f"🎵 **风格 (Genre)**: {genre} (置信度 {genre_conf:.0%})")
        if mood:
            mood_bits = [f"情绪: {mood}"]
            if valence is not None:
                valence_word = "积极" if valence >= 0.6 else ("中性" if valence >= 0.4 else "消极")
                mood_bits.append(f"效价 {valence:.2f} ({valence_word})")
            if energy is not None:
                energy_word = "激昂" if energy >= 0.6 else ("中等" if energy >= 0.3 else "平静")
                mood_bits.append(f"能量 {energy:.2f} ({energy_word})")
            lines.append(f"💫 **情绪维度 (Mood)**: {' · '.join(mood_bits)}")
        if acousticness is not None:
            ac_word = "原声" if acousticness >= 0.6 else ("半原声" if acousticness >= 0.3 else "电子")
            lines.append(f"🎚️ **原声比重 (Acousticness)**: {acousticness:.2f} ({ac_word})")
        if vocal_type:
            lines.append(f"🗣️ **人声类型 (Vocal)**: {vocal_type}")
        if instruments:
            lines.append(f"🎹 **主乐器 (Instruments)**: {', '.join(instruments)}")

    return "\n\n".join(lines)


def _format_mv_plan_for_humans(plan: dict) -> str:
    """mv_plan → 人类可读摘要"""
    lines = []
    mood_summary = plan.get("mood_summary", "")
    if mood_summary:
        lines.append(f"## 🎨 意境总结 (Mood)\n\n{mood_summary}")

    keywords_cn = plan.get("theme_keywords_cn", [])
    keywords_en = plan.get("theme_keywords_en", [])
    if keywords_cn or keywords_en:
        lines.append("## 🏷️ 关键词 (Theme Keywords)")
        if keywords_cn:
            lines.append(f"**中文**: {' · '.join(keywords_cn)}")
        if keywords_en:
            lines.append(f"**英文 (用于素材检索)**: {', '.join(keywords_en)}")

    palette = plan.get("color_palette", [])
    if palette:
        lines.append(f"## 🎨 配色方案 (Color Palette)\n\n{' · '.join(palette)}")

    transitions = plan.get("transition_style", "")
    if transitions:
        lines.append(f"**转场风格**: {transitions}")

    subtitle_style = plan.get("subtitle_style", "")
    if subtitle_style:
        lines.append(f"**字幕样式**: {subtitle_style}")

    video_prompts = plan.get("video_prompts", [])
    if video_prompts:
        lines.append(f"\n## 🎬 段落拍摄提示 ({len(video_prompts)} 段)")
        for vp in video_prompts[:8]:  # 最多展示 8 段避免过长
            label = vp.get("label", f"段 {vp.get('section_index', '?')}")
            prompt = vp.get("prompt", "")
            style = vp.get("style", "")
            lines.append(f"- **{label}**: `{prompt}` — {style}")
        if len(video_prompts) > 8:
            lines.append(f"- ...(还有 {len(video_prompts) - 8} 段)")

    return "\n\n".join(lines)


def _find_matching_lrc_for_uploaded_audio(uploaded_audio_file) -> str:
    """WebUI 上传 mp3 后, 自动匹配 storage/lrc/ 下的 LRC.

    背景: LRC 上传时 (line 5006) 按 md5[:12] 命名, 但哈希的是 LRC 文件本身内容,
        不是对应的 mp3. 所以 mp3 跟 LRC md5 对不上.
    修法: 按 LRC 头 [ti:xxx] 标题 + 文件名双重匹配.

    匹配优先级:
      1. mp3 文件名 (去掉扩展名) 跟 LRC 文件名包含关系
      2. mp3 文件名 跟 LRC 头 [ti:xxx] 包含关系
      3. 找不到返空串, mv_planner 走纯音频路径.

    Args:
        uploaded_audio_file: streamlit UploadedFile (有 .getbuffer() / .name)

    Returns:
        解析后的歌词纯文本 (按时间戳一行), 找不到返空串.
    """
    try:
        from app.services.lyrics_parser import parse_lyrics_file, format_for_planner

        lrc_dir = Path(utils.root_dir()) / "storage" / "lrc"
        if not lrc_dir.exists():
            return ""

        # mp3 文件名去扩展名作为匹配 key (例 "长风渡.mp3" -> "长风渡")
        mp3_name = Path(uploaded_audio_file.name or "").stem
        if not mp3_name:
            return ""

        candidates = []
        for ext in (".lrc", ".qrc", ".txt"):
            candidates.extend(lrc_dir.glob(f"*{ext}"))
        if not candidates:
            return ""

        def _score(lrc_path: Path) -> int:
            """匹配分: 文件名包含 mp3 名 +5, LRC 头 ti 包含 mp3 名 +3"""
            score = 0
            # 去掉哈希前缀 ({12字符}_{原名}.lrc)
            stem = lrc_path.name
            if "_" in stem and len(stem.split("_", 1)[0]) == 12:
                stem = stem.split("_", 1)[1]
            stem_no_ext = Path(stem).stem
            if mp3_name in stem_no_ext or stem_no_ext in mp3_name:
                score += 5
            # 读 LRC 头 [ti:xxx]
            try:
                content = lrc_path.read_text(encoding="utf-8", errors="ignore")[:500]
                import re as _re
                ti_match = _re.search(r"\[ti:(.+?)\]", content)
                if ti_match and mp3_name in ti_match.group(1):
                    score += 3
            except Exception:
                pass
            return score

        scored = [(s, p) for s, p in ((_score(p), p) for p in candidates) if s > 0]
        if not scored:
            return ""
        scored.sort(key=lambda x: -x[0])
        best_lrc = scored[0][1]

        try:
            parsed = parse_lyrics_file(str(best_lrc))
            text = format_for_planner(parsed)
            if text:
                logger.info(
                    f"_find_matching_lrc: matched {best_lrc.name} "
                    f"(score={scored[0][0]}) for {uploaded_audio_file.name} "
                    f"({len(text)} chars)"
                )
                return text
        except Exception as parse_exc:
            logger.warning(f"_find_matching_lrc: parse failed for {best_lrc}: {parse_exc}")

        return ""
    except Exception as exc:
        logger.warning(f"_find_matching_lrc: unexpected error: {exc}")
        return ""


def _run_audio_mv_analysis(
    uploaded_audio_file,
    lyrics_text: str = "",
) -> dict:
    """的核心函数: 上传音频 → 调 mv.analyze 服务 → 返回 plan + features

    流程:
    1. 把 streamlit 上传文件存到 storage/mv/ (跟 FastAPI 路由一致)
    2. 计算 file_id (mva-xxxxxxxx.{ext})
    3. analyze_audio() 提取特征
    4. compute_song_signature() 三层签名
    5. Q5 缓存策略: 看是否要重跑 LLM
    6. MvPlanner.build() 出方案
    7. 返回 {plan, features, signature, audio_id, source}

    Raises:
        ValueError: 文件无效
        RuntimeError: LLM/分析失败
    """
    import time as _time
    from pathlib import Path

    from app.services.audio import analyze_audio, AudioAnalyzerError
    from app.services.mv_planner import MvPlanner
    from app.config import config

    if uploaded_audio_file is None:
        raise ValueError("uploaded_audio_file is None")

    db_path, repo = _init_mv_runtime()

    # 1. 保存文件到 storage/mv/ (跟 FastAPI 路由一致)
    storage_mv_dir = Path(config.app.get("storage_mv_dir", "storage/mv"))
    storage_mv_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(uploaded_audio_file.name or "audio.mp3").name
    ext = Path(safe_name).suffix.lower()
    if not ext:
        raise ValueError("uploaded audio has no extension")

    import uuid as _uuid
    file_id = f"mva-{_uuid.uuid4().hex[:12]}{ext}"
    abs_path = storage_mv_dir / file_id
    with abs_path.open("wb") as f:
        f.write(uploaded_audio_file.getbuffer())

    # 2. 音频分析 — 审计 P0-3: 复用 y, sr, 不重复 preprocess_audio
    try:
        from app.services.audio.analyzer import analyze_audio_with_audio
        from app.services.audio.preprocess import preprocess_audio
        features_obj, y, sr = analyze_audio_with_audio(str(abs_path))
        if y is None or sr is None:
            y, sr = preprocess_audio(str(abs_path))
    except AudioAnalyzerError as exc:
        raise RuntimeError(f"audio analyze failed: {exc}") from exc
    features = features_obj.to_dict()
    id3_meta = features_obj.id3_metadata

    # 3. song_signature (复用上面已加载的 y, sr)
    signature_str, signature_meta = _compute_song_signature_for_upload(
        str(abs_path), features, id3_meta, y=y, sr=sr
    )

    # 4. Q5 缓存策略
    should_run, latest, reason = _should_run_llm(repo, file_id, signature_str)
    logger.info(f"mv_audio_analysis: should_run={should_run} reason={reason}")
    # 记下当前 signature 供 clear_cache 用
    st.session_state["_mv_last_signature"] = signature_str

    # 5. 决定: 重跑 / 复用缓存
    if should_run:
        planner = MvPlanner(db_path=db_path)
        plan = planner.build(
            audio_features=features,
            lyrics=lyrics_text,
            audio_id=file_id,
            song_signature=signature_str,
            duration_seconds=features["duration_seconds"],
            artist=id3_meta.artist if id3_meta else None,
            title=id3_meta.title if id3_meta else None,
        )
        source = plan.get("_source", "unknown")
        version = plan.get("_version", 0)
    else:
        # 复用历史缓存
        plan = dict(latest.intent)
        plan["_source"] = "cache_reuse"
        plan["_version"] = latest.version
        plan["_cache_reason"] = reason
        source = "cache_reuse"
        version = latest.version

    return {
        "plan": plan,
        "features": features,
        "signature": signature_str,
        "signature_meta": signature_meta,
        "audio_id": file_id,
        "audio_path": str(abs_path),
        "source": source,
        "version": version,
        "cache_decision": reason,
        "latency_ms": plan.get("_latency_ms", 0),
    }


@st.dialog(tr("Audio Analysis Panel"), width="large", on_dismiss="rerun")
def _render_audio_analysis_dialog():
    """的弹窗: 曲调特征 + 意境 + 关键词 + 配色 + 段落拍摄 + 3 个应用按钮

    "折叠栏位太窄, 改成弹窗 (st.dialog) 更宽屏看"
    - width="large" 加大宽度
    - 弹窗内可调用 callback (在弹窗本身之前执行, 不会触发 StreamlitAPIException)
    - 弹窗内改 video_script / video_terms 的 session_state 在 dialog 关闭后生效
      (rerun 之后 video_script widget 重新实例化时看到新值)

    Trigger: _MV_DIALOG_FLAG_KEY = True → render_audio_analysis_panel() 会调它
    """
    result = st.session_state.get(_MV_AUDIO_SESSION_KEY)
    if not result:
        st.warning(tr("Audio Analysis No Audio"))
        if st.button(tr("Close"), key="mv_dialog_close_empty", use_container_width=True):
            st.session_state[_MV_DIALOG_FLAG_KEY] = False
        return

    plan = result["plan"]
    features = result["features"]
    source = result["source"]
    version = result["version"]
    cache_decision = result.get("cache_decision", "")
    mood_summary = plan.get("mood_summary", "")
    keywords_en = plan.get("theme_keywords_en", [])

    # 顶部 meta 行
    source_label = {
        "llm": tr("MV Plan Source LLM"),
        "cache_fallback": tr("MV Plan Source Cache"),
        "cache_reuse": tr("MV Plan Source Cache"),
        "fallback_rule": tr("MV Plan Source Fallback"),
    }.get(source, source)
    meta_cols = st.columns([3, 1])
    with meta_cols[0]:
        st.markdown(f"**{tr('MV Plan Source')}: {source_label}**")
        if cache_decision and source in ("cache_reuse", "llm"):
            st.caption(f"💡 {cache_decision}")
    with meta_cols[1]:
        latency_suffix = f" · {result['latency_ms']} ms" if result.get("latency_ms") else ""
        st.caption(f"v{version}{latency_suffix}")

    st.divider()

    # 双栏布局: 左边曲调特征, 右边意境
    body_cols = st.columns(2, gap="medium")
    with body_cols[0]:
        st.markdown(_format_audio_features_for_humans(features))
    with body_cols[1]:
        st.markdown(_format_mv_plan_for_humans(plan))

    st.divider()

    # 应用按钮 + 关闭按钮
    # + 13:58 bug 修复:
    # 1. dialog button click 触发的是完整 script run (不是 partial rerun)
    # 2. on_click callback 在 script run 之前执行, 它改 session_state 对后续
    #    widget render 可见 (这是 streamlit 设计)
    # 3. callback 里调 st.rerun() 是 no-op + warning, 跳过即可
    # 4. dialog 关闭机制: 设 _MV_DIALOG_FLAG_KEY=False, 下次 render_audio_analysis_panel
    #    看到 flag=False 不调 _render_audio_analysis_dialog(), dialog 自动隐藏
    # 5. 不能直接改 page widget session_state (video_script / video_terms),
    #    改用 _MV_PENDING_APPLY_KEY pending dict, _render_application() 顶部消费
    def _apply_mood_callback(ms: str, ver: int):
        pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
        pending["video_script_append"] = (pending.get("video_script_append") or "") + ("\n\n" if pending.get("video_script_append") else "") + ms
        pending["source_version"] = ver
        st.session_state[_MV_PENDING_APPLY_KEY] = pending
        # 不需手动设 flag=False. 一次性 signal 被 render_audio_analysis_panel 弹后 pop.
        # callback 直接 st.rerun(scope='app') 触发 page rerun → apply_pending_mv_audio 消费
        st.rerun(scope="app")

    def _apply_keywords_callback(kws: list):
        pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
        existing_terms = pending.get("video_terms_append", "")
        new_terms = ", ".join(kws)
        if existing_terms.strip():
            pending["video_terms_append"] = (
                existing_terms.rstrip().rstrip(",") + ", " + new_terms
            )
        else:
            pending["video_terms_append"] = new_terms
        pending["keyword_count"] = pending.get("keyword_count", 0) + len(kws)
        st.session_state[_MV_PENDING_APPLY_KEY] = pending
        st.rerun(scope="app")

    def _apply_both_callback(ms: str, kws: list):
        pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
        if ms:
            prev = pending.get("video_script_append", "")
            sep = "\n\n" if prev.strip() else ""
            pending["video_script_append"] = (prev.rstrip() + sep + ms).strip() if prev else ms
        if kws:
            existing_terms = pending.get("video_terms_append", "")
            new_terms = ", ".join(kws)
            if existing_terms.strip():
                pending["video_terms_append"] = (
                    existing_terms.rstrip().rstrip(",") + ", " + new_terms
                )
            else:
                pending["video_terms_append"] = new_terms
            pending["keyword_count"] = pending.get("keyword_count", 0) + len(kws)
        pending["source_version"] = pending.get("source_version", version)
        st.session_state[_MV_PENDING_APPLY_KEY] = pending
        st.rerun(scope="app")

    def _close_dialog():
        st.session_state[_MV_DIALOG_FLAG_KEY] = False
        st.rerun(scope="app")

    apply_cols = st.columns(4)
    with apply_cols[0]:
        st.button(
            tr("Apply Mood To Script Button"),
            key="mv_dlg_apply_mood",
            use_container_width=True,
            disabled=not mood_summary,
            on_click=_apply_mood_callback,
            args=(mood_summary, version),
        )
    with apply_cols[1]:
        st.button(
            tr("Apply English Keywords Button"),
            key="mv_dlg_apply_kws",
            use_container_width=True,
            disabled=not keywords_en,
            on_click=_apply_keywords_callback,
            args=(keywords_en,),
        )
    with apply_cols[2]:
        st.button(
            tr("Apply Both Button"),
            key="mv_dlg_apply_both",
            use_container_width=True,
            type="primary",
            disabled=not (mood_summary or keywords_en),
            on_click=_apply_both_callback,
            args=(mood_summary, keywords_en),
        )
    with apply_cols[3]:
        st.button(
            tr("Close"),
            key="mv_dlg_close",
            use_container_width=True,
            on_click=_close_dialog,
        )

    st.divider()

    # === AI 歌曲提示词 (Mureka) — ===
    # 折叠段默认收起, 不抩高弹窗. 详细程度 radio 动态切换精简/详细.
    with st.expander("🎵 AI 歌曲提示词 (Mureka)", expanded=False):
        _render_mureka_prompts_section(features, version)

    st.divider()

    # === 高潮段检测 ===
    # "有多少高潮就选几个, 识别不出来高潮就不选"
    # detect_chorus_segments 返回 1-N 个 (实际几个就几个)
    # UI 最多展示 3 个 -> 如果识别出 >3 个取 confidence 最高的 3 个, <3 个按数量显示
    chorus_segments = features.get("chorus_segments", []) if isinstance(features, dict) else []
    if chorus_segments:
        # 取前 3 个 (detect_chorus_segments 已经按 confidence 降序排过)
        displayed_segments = chorus_segments[:3]
        st.markdown(f"### {tr('MV Chorus Section Title')}")
        st.caption(tr("MV Chorus Section Description"))

        # 高潮段选项 radio (最多 3 个)
        chorus_options = []
        chorus_labels = []
        for cs in displayed_segments:
            idx = cs.get("index", 0) + 1
            start = cs.get("start", 0)
            end = cs.get("end", 0)
            duration = cs.get("duration", 0)
            chorus_type = cs.get("chorus_type", "main_chorus")
            confidence = cs.get("confidence", 0)
            type_label = {
                "main_chorus": tr("MV Chorus Type Main"),
                "pre_chorus": tr("MV Chorus Type Pre"),
                "post_chorus": tr("MV Chorus Type Post"),
            }.get(chorus_type, chorus_type)
            label = f"{tr('MV Chorus Selection Label')} {idx} · {type_label} · {start:.1f}s-{end:.1f}s ({duration:.0f}s) · {tr('MV Chorus Confidence')} {confidence:.2f}"
            chorus_options.append(idx - 1)
            chorus_labels.append(label)

        # 选中的高潮段 ()
        chorus_select_key = "mv_dlg_chorus_selected"
        if chorus_select_key not in st.session_state:
            st.session_state[chorus_select_key] = 0  # 默认选第一个

        selected_idx = st.radio(
            tr("MV Chorus Section Title"),
            options=chorus_options,
            format_func=lambda i: chorus_labels[i] if 0 <= i < len(chorus_labels) else "",
            index=st.session_state[chorus_select_key],
            key=chorus_select_key,
            horizontal=True,
        )

        # 回显高潮对应的搜索关键字 ()
        # 从 video_prompts 里找匹配 section_index == selected_idx 的 prompt
        video_prompts = plan.get("video_prompts", [])
        matching_prompts = []
        for vp in video_prompts:
            if vp.get("section_index") == selected_idx:
                matching_prompts.append(vp)
                break
        if matching_prompts:
            vp = matching_prompts[0]
            chorus_prompt_en = vp.get("prompt", "")
            chorus_style_cn = vp.get("style", "")
            chorus_label_text = vp.get("label", "")
            st.markdown(f"**{tr('MV Chorus Match Keywords')}:** `{chorus_prompt_en}`")
            if chorus_style_cn:
                st.caption(f"{tr('MV Chorus Match Style')}: {chorus_style_cn}")

            # Apply 按钮 - 把该段关键词加入 video_terms
            # 同时存 chorus 段 start/end 到 pending,
            # 后面 _render_application() 读取时一并填到 video_script_params.
            # 这样用户点一下 "金生成高潮段 MV", 整案就能生成该段独立 MV.
            def _apply_chorus_keywords_callback(prompt: str, idx: int, start: float, end: float, ver: int):
                pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
                existing_terms = pending.get("video_terms_append", "")
                new_terms = prompt.strip()
                if existing_terms.strip():
                    pending["video_terms_append"] = (
                        existing_terms.rstrip().rstrip(",") + ", " + new_terms
                    )
                else:
                    pending["video_terms_append"] = new_terms
                pending["keyword_count"] = pending.get("keyword_count", 0) + 1
                pending["chorus_index"] = idx
                # 高潮段独立 MV - 存 start/end 到 pending
                pending["chorus_range_start"] = round(start, 2)
                pending["chorus_range_end"] = round(end, 2)
                pending["source_version"] = pending.get("source_version", ver)
                st.session_state[_MV_PENDING_APPLY_KEY] = pending
                # 全 app rerun 强制 page widget 看到新值
                st.rerun(scope="app")

            st.button(
                tr("Apply Chorus Keywords Button"),
                key="mv_dlg_apply_chorus_kws",
                use_container_width=True,
                type="secondary",
                disabled=not chorus_prompt_en,
                on_click=_apply_chorus_keywords_callback,
                # 传入 displayed_segments[selected_idx] (选中的 chorus segment)
                #   .start / .end - 传个参数让 callback 知道当前选中段
                args=(
                    chorus_prompt_en,
                    selected_idx,
                    displayed_segments[selected_idx].get("start", 0.0) if 0 <= selected_idx < len(displayed_segments) else 0.0,
                    displayed_segments[selected_idx].get("end", 0.0) if 0 <= selected_idx < len(displayed_segments) else 0.0,
                    version,
                ),
            )
        else:
            st.caption(tr("MV Chorus No Matching Section"))


@st.dialog(tr("MV Plan History"), width="large", on_dismiss="rerun")
def render_mv_analysis_dialog(task_id: str, task: dict):
    """2026-08-09 P2-6: 任务管理点击 MV 按钮 → 弹窗查看这个 task 的 LLM plan

    内容:
    - audio_features (BPM / key / duration / sections / chorus)
    - LLM plan (mood_summary + theme_keywords 中英 + color_palette)
    - Pexels 搜索词 (video_terms)
    - 原始文件路径 (custom_audio / lrc_file)
    - LLM 调用元信息 (latency / model / cost / tokens / version)

    数据流: task_id → mv_intent_history.task_id → IntentRecord
    """
    try:
        # 2026-08-09 bug 修复:
        # 原代码 IntentRepository(db=get_db()) 失败:
        #   get_db() 是懒加载, 首次调用必须传 db_path
        #   但 webui 启动时只调 init_mv_db() 建表, 没调过 get_db() 设置单例
        #   所以 get_db()  -> ValueError: db_path required on first call
        # 修复: 先调 _init_mv_runtime() (webui 内部 helper)
        #       它 init DB + 设 IntentRepository 单例 + 返回 repo
        from app.services.mv import get_intent_repository
        _init_mv_runtime()  # 确保 IntentRepository 单例已设
        repo = get_intent_repository()  # 走单例
        record = repo.get_by_task_id(task_id)
    except Exception as exc:
        st.error(f"加载 MV plan 失败: {type(exc).__name__}: {exc}")
        return

    if not record:
        st.warning(tr("MV Plan History Not Found"))
        st.caption(
            f"task_id={task_id}\n"
            f"可能原因: 该任务不是 MV 模式生成的, 或者 plan 未写入数据库"
        )
        if st.button(tr("Close"), key=f"mv_dlg_close_{task_id}"):
            st.rerun()
        return

    # ---- 顶部元信息 ----
    import json as _json
    st.caption(
        f"📋 task_id={task_id[:12]}... | audio_id={record.audio_id} | "
        f"version={record.version} | source={record.source} | "
        f"created_at={record.created_at}"
    )

    # ---- LLM 元信息 ----
    if record.llm_latency_ms is not None or record.llm_model:
        with st.expander(tr("MV LLM Meta"), expanded=False):
            cols = st.columns(4)
            cols[0].metric("latency_ms", record.llm_latency_ms or "-")
            cols[1].metric("model", record.llm_model or "-")
            if record.cost_usd is not None:
                cols[2].metric("cost_usd", f"${record.cost_usd:.5f}")
            if record.prompt_tokens is not None:
                cols[3].metric(
                    "tokens",
                    f"{record.prompt_tokens}+{record.completion_tokens}",
                )
            if record.llm_error:
                st.error(f"LLM error: {record.llm_error}")

    # ---- 音频特征 (从 audio_features JSON 读) ----
    try:
        features = task.get("params", {}).get("audio_features", {}) if isinstance(task.get("params"), dict) else {}
    except Exception:
        features = {}

    # 跨 audio_id 不一定能在 task 里读到 features, 但 record.duration_seconds 是权威
    audio_info_cols = st.columns(4)
    audio_info_cols[0].metric("duration_s", f"{record.duration_seconds:.1f}")
    audio_info_cols[1].metric("artist", record.artist or "-")
    audio_info_cols[2].metric("title", record.title or "-")
    audio_info_cols[3].metric("signature", record.song_signature[:12] + "...")

    # ---- LLM plan 主内容 ----
    try:
        plan_dict = _json.loads(record.intent_json)
    except Exception as exc:
        st.error(f"intent_json 解析失败: {exc}")
        plan_dict = {}

    mood = plan_dict.get("mood_summary", "") or plan_dict.get("mood", "")
    if mood:
        st.subheader(tr("MV Mood Summary"))
        st.write(mood)

    # 中文 / 英文关键词
    kw_cn = plan_dict.get("theme_keywords_cn", [])
    kw_en = plan_dict.get("theme_keywords_en", [])

    kw_cols = st.columns(2)
    if kw_cn:
        with kw_cols[0]:
            st.subheader("🎨 主题关键词 (中)")
            for k in kw_cn:
                st.write(f"- {k}")
    if kw_en:
        with kw_cols[1]:
            st.subheader("🎬 Theme Keywords (EN) — Pexels")
            for k in kw_en:
                st.code(k, language="text")

    # ---- Pexels 搜索词 (从 task.params.video_terms) ----
    try:
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
        video_terms = params.get("video_terms", [])
    except Exception:
        video_terms = []
    if video_terms:
        with st.expander(f"📺 Pexels 搜索词 ({len(video_terms)} 个)", expanded=True):
            st.code(", ".join(video_terms), language="text")

    # ---- OGG / LRC / 原始文件路径 ----
    try:
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
    except Exception:
        params = {}
    file_cols = st.columns(2)
    ogg = params.get("custom_audio_file") or ""
    lrc = params.get("lrc_file") or ""
    file_cols[0].caption(f"🎵 OGG: {Path(ogg).name if ogg else '-'}")
    file_cols[1].caption(f"📝 LRC: {Path(lrc).name if lrc else '-'}")

    if st.button(tr("Close"), key=f"mv_dlg_close_bottom_{task_id}", use_container_width=True):
        st.rerun()


def render_audio_analysis_panel(uploaded_audio_file, key_prefix: str = "voice"):
    """的 UI: 上传音频后点按钮 → 弹窗里看分析结果 + 一键应用

    改用 st.dialog 弹窗代替 expander, 宽屏看分析结果

    2026-08-10 首次上传弹版权声明 (st.session_state 记忆, 不再每次打断)

    2026-08-13 加 key_prefix 参数, 支持同一页面多处调用
        - 音频设置区上传语音： key_prefix="voice"
        - 背景音乐区上传音频：key_prefix="bgm"
        避免 widget key 冲突 (streamlit 不允许同页面两个同名 widget)
    """
    if uploaded_audio_file is None:
        st.caption(tr("Audio Analysis No Audio"))
        return

    # 2026-08-10 首次上传弹版权声明, session_state 记忆（全局共享, 不分 prefix）
    _COPYRIGHT_ACK_KEY = "_mpt_audio_copyright_acknowledged"
    if not st.session_state.get(_COPYRIGHT_ACK_KEY, False):
        with st.container(border=True):
            st.markdown("##### ⚠️ 音频版权与使用声明")
            st.caption(
                "本工具仅分析您自行上传的**明文音频**（不提供解密、存储、传播）。"
                "请确保您对上传的音频享有合法使用权（CC0 / 自制 / 已获授权）。\n\n"
                "**VIP / 加密音乐** 请使用 [UnlockMusic](https://git.unlock-music.dev/) "
                "在本地解密后重新上传明文文件。\n\n"
                "严禁将分析结果用于侵犯第三方版权的内容生成。"
            )
            if st.button("✅ 我已阅读并接受上述声明", key=f"audio_copyright_ack_btn_{key_prefix}"):
                st.session_state[_COPYRIGHT_ACK_KEY] = True
                st.rerun()
            st.stop()  # 未勾选不进入下面的分析流程

    # 两个按钮横排: 分析 (主) + 查看结果 (有结果才启用)
    has_result = _MV_AUDIO_SESSION_KEY in st.session_state

    def _analyze_audio_callback(uploaded_file):
        """widget 实例化前调用, 在这里调分析服务写 session_state 安全"""
        with st.spinner(tr("Analyze Music Running")):
            try:
                # WebUI 上传 mp3 后, 自动找匹配 lrc 加载歌词
                # 根因: 不加载歌词, mv_planner 拿不到 "半生烟雨走天涯" 等古典意象,
                #       LLM 只看音频特征, 输出"golden hour / wheat field" 等西式场景.
                # 修法: 按 mp3 md5[:12] (跟 LRC 上传时 webui/Main.py:4943 同一算法) 
                #       去 storage/lrc/{hash}_*.lrc 找匹配文件, 读到歌词文本传下去.
                lyrics_text = _find_matching_lrc_for_uploaded_audio(uploaded_file)
                if lyrics_text:
                    logger.info(
                        f"mv_audio_analysis: auto-loaded lyrics "
                        f"({len(lyrics_text)} chars) from matching LRC"
                    )
                else:
                    logger.info(
                        "mv_audio_analysis: no matching LRC found, "
                        "mv_planner will fall back to audio-only mode"
                    )
                result = _run_audio_mv_analysis(uploaded_file, lyrics_text=lyrics_text)
                st.session_state[_MV_AUDIO_SESSION_KEY] = result
                # 按段落拼接 - 存 plan + features 供视频生成用
                st.session_state["_mv_current_plan"] = result.get("plan")
                st.session_state["_mv_current_features"] = result.get("features")
                # bug 修复: 用一次性信号代替持久 flag
                # 原因: flag=True 会让后续所有 rerun 都重复弹窗, 哪怕主界面其他改动
                # 用 _MV_DIALOG_REQUEST_KEY: 在下轮 rerun 弹, 弹后立即清
                st.session_state[_MV_DIALOG_REQUEST_KEY] = True
            except Exception as exc:
                logger.error(f"mv_audio_analysis failed: {exc}")
                st.error(tr("Audio Analysis Failed").format(error=str(exc)))

    def _open_dialog_callback():
        # bug 修复: 一次性信号代替持久 flag
        st.session_state[_MV_DIALOG_REQUEST_KEY] = True

    btn_cols = st.columns([1, 1], gap="small")
    with btn_cols[0]:
        st.button(
            tr("Analyze Music Button"),
            key=f"mv_analyze_music_button_{key_prefix}",
            use_container_width=True,
            type="primary",
            help="调用音频分析服务, 提取曲调特征 + AI 意境方案",
            on_click=_analyze_audio_callback,
            args=(uploaded_audio_file,),
        )
    with btn_cols[1]:
        st.button(
            tr("View Analysis Result"),
            key=f"mv_view_analysis_button_{key_prefix}",
            use_container_width=True,
            type="secondary",
            disabled=not has_result,
            on_click=_open_dialog_callback,
        )

    # === Debug 控件 () ===
    # 同一首歌测试不同 LLM 输出时: 勾选 "强制重跑" 绕过缓存
    # 调试清缓存: 勾选后点 "清 MV 缓存" 删除该歌的所有历史 LLM 输出
    debug_cols = st.columns([1, 1], gap="small")
    with debug_cols[0]:
        st.checkbox(
            "🛠 Force Rerun (bypass cache)",
            key=f"{_MV_FORCE_RERUN_KEY}_{key_prefix}",
            value=False,
            help="勾选后下次点分析会绕过缓存强制调用 LLM",
        )
    with debug_cols[1]:
        def _clear_mv_cache_callback():
            """清当前 signature 的 MV 缓存记录"""
            from app.services.mv.db import init_db as init_mv_db
            from app.services.mv import get_intent_repository
            from app.config import config
            db_path = config.app.get("mv_intent_db_path", "storage/mv/mv_intent.db")
            try:
                init_mv_db(db_path)
            except Exception:
                pass
            repo = get_intent_repository(db_path)
            sig = st.session_state.get("_mv_last_signature", "")
            deleted = 0
            if sig:
                deleted = repo.delete_by_signature(sig)
            st.session_state.pop(_MV_AUDIO_SESSION_KEY, None)
            st.session_state[_MV_DIALOG_FLAG_KEY] = False
            st.toast(f"已清除 MV 缓存 (signature={sig[:20]}..., {deleted} 条)", icon="🗑")
            logger.info(f"mv_cache_clear: signature={sig[:20]} deleted={deleted}")
        st.button(
            "🗑 Clear MV Cache",
            key=f"mv_clear_cache_button_{key_prefix}",
            use_container_width=True,
            help="清除当前音频的 MV 缓存记录, 下次点分析会重新调用 LLM",
            on_click=_clear_mv_cache_callback,
        )

    # bug 修复: 用一次性信号代替持久 flag
    # 原 bug: 主界面其他选项改动触发 rerun → flag 还是 True → dialog 又弹
    # 修法: 一次性信号 key, 弹后立即 pop. 只有用户主动点击才产生新信号
    if st.session_state.pop(_MV_DIALOG_REQUEST_KEY, False):
        _render_audio_analysis_dialog()


def render_mv_analysis_settings(panel):
    """MV 意境分析设置 ()

    放在 Settings dialog 里, 供调试:
    - MV LLM 重跑上限 (同 signature 重跑 N 次后复用缓存)
    - 高潮检测参数 (top_k)
    - 缓存时间 (TTL 天)
    """
    from app.services.mv import get_intent_repository
    from app.services.mv.db import init_db as init_mv_db
    from app.config import config as app_config

    global _MV_CACHE_REANALYZE_LIMIT, _MV_CACHE_TTL_DAYS

    with panel:
        st.caption(tr("MV Analysis Settings Help"))

        # 重跑上限 slider
        st.session_state.setdefault(
            "mv_cache_reanalyze_limit", _MV_CACHE_REANALYZE_LIMIT
        )
        new_limit = st.slider(
            tr("MV Cache Reanalyze Limit"),
            min_value=1,
            max_value=20,
            value=int(st.session_state["mv_cache_reanalyze_limit"]),
            step=1,
            help=tr("MV Cache Reanalyze Limit Help"),
            key="mv_cache_reanalyze_limit_slider",
        )
        if new_limit != _MV_CACHE_REANALYZE_LIMIT:
            _MV_CACHE_REANALYZE_LIMIT = new_limit
            st.session_state["mv_cache_reanalyze_limit"] = new_limit

        # TTL 天数 slider
        st.session_state.setdefault("mv_cache_ttl_days", _MV_CACHE_TTL_DAYS)
        new_ttl = st.slider(
            tr("MV Cache TTL Days"),
            min_value=7,
            max_value=365,
            value=int(st.session_state["mv_cache_ttl_days"]),
            step=1,
            help=tr("MV Cache TTL Days Help"),
            key="mv_cache_ttl_days_slider",
        )
        if new_ttl != _MV_CACHE_TTL_DAYS:
            _MV_CACHE_TTL_DAYS = new_ttl
            st.session_state["mv_cache_ttl_days"] = new_ttl

        # MV 缓存统计
        try:
            db_path = app_config.app.get(
                "mv_intent_db_path", "storage/mv/mv_intent.db"
            )
            init_mv_db(db_path)
            repo = get_intent_repository(db_path)
            stats = repo.get_stats() if hasattr(repo, "get_stats") else None
            if stats:
                cache_cols = st.columns(2)
                cache_cols[0].metric(
                    tr("MV Cache Total Records"), stats.get("total", 0)
                )
                cache_cols[1].metric(
                    tr("MV Cache Unique Signatures"), stats.get("unique_signatures", 0)
                )
        except Exception as exc:
            st.caption(f"MV cache stats error: {exc}")

        # 一键清除所有 MV 缓存
        def _purge_all_mv_cache_callback():
            try:
                db_path = app_config.app.get(
                    "mv_intent_db_path", "storage/mv/mv_intent.db"
                )
                init_mv_db(db_path)
                repo = get_intent_repository(db_path)
                deleted = repo.delete_all() if hasattr(repo, "delete_all") else 0
                st.toast(
                    f"🗑 Cleared all MV cache: {deleted} records",
                    icon="🗑",
                )
                logger.info(f"mv_cache_purge_all: deleted={deleted}")
            except Exception as exc:
                st.error(f"purge failed: {exc}")

        st.button(
            tr("MV Cache Purge All"),
            key="mv_cache_purge_all_button",
            use_container_width=True,
            type="secondary",
            help=tr("MV Cache Purge All Help"),
            on_click=_purge_all_mv_cache_callback,
        )


def apply_pending_mv_audio():
    """消费 _MV_PENDING_APPLY_KEY 队列, 在 widget 实例化前将内容合并到 video_script / video_terms

    bug 修复: dialog button click 只 rerun dialog function, 不能直接
    改 page widget 的 session_state key (会触发 StreamlitAPIException)。
    解法: dialog callback 只写 pending dict, _render_application() 顶部 widget
    实例化前从 pending 读出来合并到 widget key。 (跟 _apply_pending_task_restore 同模式)

    Returns:
        bool: 是否处理了 pending (供 _render_application() 提示 toast)

    去掉 st.toast() 避免 fragment rerun 乘 2
    改为写一个 transient session_state 标志位, top bar 显示一个临时 '✓ 已应用' 提示
    """
    pending = st.session_state.pop(_MV_PENDING_APPLY_KEY, None)
    if not pending:
        return False

    script_append = pending.get("video_script_append", "")
    terms_append = pending.get("video_terms_append", "")
    keyword_count = pending.get("keyword_count", 0)

    if script_append:
        existing = st.session_state.get("video_script", "") or ""
        separator = "\n\n" if existing.strip() else ""
        st.session_state["video_script"] = (
            (existing.rstrip() + separator + script_append).strip()
        )
    if terms_append:
        existing_terms = st.session_state.get("video_terms", "") or ""
        if existing_terms.strip():
            st.session_state["video_terms"] = (
                existing_terms.rstrip().rstrip(",") + ", " + terms_append
            )
        else:
            st.session_state["video_terms"] = terms_append

    # 不用 st.toast() (会调 st.rerun + 乘 2 fragment rerun)
    # 改为 session_state 标志 + top bar 渲染, 50s 后过期
    if script_append and terms_append:
        st.session_state["_mv_apply_message"] = "✅ Applied to both fields"
    elif script_append:
        st.session_state["_mv_apply_message"] = (
            f"✅ Applied mood (v{pending.get('source_version', 0)})"
        )
    elif terms_append and keyword_count:
        # 高潮段独立 MV - 拿到 chorus_range_start/end
        # 写到 session_state. 后交参数采集时使用生成高潮段 MV
        chorus_start = pending.get("chorus_range_start")
        chorus_end = pending.get("chorus_range_end")
        if chorus_start is not None and chorus_end is not None:
            st.session_state["audio_clip_range_start"] = float(chorus_start)
            st.session_state["audio_clip_range_end"] = float(chorus_end)
            chorus_idx = pending.get("chorus_index", 0)
            st.session_state["_mv_apply_message"] = (
                f"✅ 第 {chorus_idx + 1} 高潮段: {chorus_start:.1f}s-{chorus_end:.1f}s, 一键生成高频 MV"
            )
        else:
            st.session_state["_mv_apply_message"] = (
                f"✅ Applied {keyword_count} English keywords"
            )
    else:
        st.session_state["_mv_apply_message"] = "✅ Applied"
    st.session_state["_mv_apply_message_ts"] = time.time()
    return True
