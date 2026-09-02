import hashlib
import html
import json
import math
import time
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger
from streamlit_tour import Tour

# WebUI 作为独立入口运行时，需要让项目根目录优先于第三方依赖，
# 避免依赖中的同名 app 包遮蔽 MoneyPrinterTurbo 自己的 app 包。
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services import (
    cache_manager,
    llm,
    loomloom,
    material,
    video,
    volcengine_seedance,
    voice,
    webui_task,
)
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import state as sm
from app.services import task as tm
from app.services import version_checker
from app.utils.logging_utils import configure_terminal_logger
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


# Streamlit 1.59 会在页面右上角默认展示 Deploy、skills nudge 等平台入口。
# MoneyPrinterTurbo 是面向终端用户的本地工具，这些入口会造成顶部大块空白，
# 也会让新用户误以为需要安装额外组件。这里统一隐藏 Streamlit 平台工具栏，
# 并压缩主容器顶部留白，只保留项目自己的标题、语言选择和业务设置区域。
style_file = Path(__file__).with_name("styles.css")
streamlit_style = f"<style>{style_file.read_text(encoding='utf-8')}</style>"
st.markdown(streamlit_style, unsafe_allow_html=True)
# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# 语言列表必须在会话状态初始化前可用，首次访问时才能把浏览器 locale 映射到
# 项目真正支持的语言；自动识别结果只进入当前会话，不修改全局配置。
locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# WebUI 按素材能力分组展示视频来源，但底层仍保存原有 video_source 值。
# 这样既能让用户先判断需要“搜索库存素材”还是“AI 生成”，又不会改变
# config.toml、历史任务和 API 请求中的字段语义，旧用户升级后无需迁移配置。
VIDEO_SOURCE_GROUPS = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    "ai_video": ("wavespeed", "volcengine_seedance", "loomloom"),
    "ai_image": ("openai_image",),
    "local": ("local",),
}
# Upload-Post 的 API Key 与发布用户分别在两个页面管理，并且发布用户名称
# 不等于登录邮箱。集中维护入口可以避免多语言文案各自硬编码 URL 后发生偏差，
# 也方便用户从 WebUI 直接完成首次配置和后续账号维护。
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# “默认”是 WebUI 专用哨兵，不会写入 config.toml，也不会传给 FFmpeg。
# 后端在 video_codec 未配置时继续采用稳定的 libx264；单独保留该哨兵可以区分
# “跟随项目默认策略”和“用户明确固定 libx264”，便于未来安全调整默认策略。
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "subtitle_position": "bottom",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
}
LOCAL_MATERIAL_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
}
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    # Win32 还会把 Latin-1 上标数字 ¹、²、³ 识别为设备编号。虽然这类主题
    # 很少见，但仍会导致 Windows 下载失败，因此与普通数字保留名统一处理。
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("¹", "²", "³")
    }
)
_RUNTIME_CONFIG_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "chatterbox": config.chatterbox,
    "elevenlabs": config.elevenlabs,
    "minimax_tts": config.minimax_tts,
    "siliconflow": config.siliconflow,
    "fish_audio": config.fish_audio,
    "ui": config.ui,
}
# 设置预设与密钥备份使用各自的文件标识。导入时先校验 schema 和版本，
# 避免把任务记录、config.toml 或其它 JSON 误当成本功能的导出文件。
SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
# 预设只描述生成参数。素材、配音和配乐都是本机文件路径，预设通常要在另一台
# 机器或另一个容器里导入，带上这些路径只会指向不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# 密钥按配置项名称后缀识别。新增 Provider 只要沿用现有命名，就会自动进入
# 备份，不需要再维护第二份密钥清单。
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# 只恢复密钥而不恢复配套配置项时，凭据仍然不可用。这些配套项与密钥一起备份。
CREDENTIAL_COMPANION_KEYS = {
    # Azure 语音必须同时知道区域。
    "azure": ("speech_region",),
    # Provider 的额外字段由 Registry 声明，例如 Cloudflare AI Gateway 的
    # Account ID 和 Gateway ID。只恢复 API Key 而丢掉这些字段时，换到另一台
    # 机器后该 Provider 仍然无法调用。从 Registry 读取可以让以后新增的
    # Provider 自动进入备份，不需要在这里维护第二份字段清单。
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# 同一个密钥在不同面板可能使用各自的控件 key：音频面板直接编辑 Gemini 和
# MiMo 的 LLM 密钥，胜算云密钥的控件没有 _input 后缀。恢复备份时必须清除
# 每一个别名，否则遗留的旧值会在下一次 rerun 覆盖刚刚恢复的密钥。
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
    ("app", "loomloom_api_token"): ("loomloom_user_api_token",),
}
# ui 分区只保存界面偏好，不含任何凭据，备份时整体跳过。
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


# -----------------------------------------------------------------------------
# 启动配置、会话状态与本地化
# -----------------------------------------------------------------------------


def _set_runtime_config(section_name, key, value):
    """
    更新 WebUI 配置，但不等待正在生成视频的后台任务。

    后台任务结束前，配置层只保留同一配置项的最新值；任务释放配置锁时会自动
    应用并保存。页面控件值仍由 Streamlit session_state 维护，因此暂存期间的
    rerun 不会把用户刚输入的内容重置为旧配置。
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """删除 WebUI 配置项；后台任务占用配置时延后执行。"""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """请求保存 WebUI 配置；后台任务占用配置时立即返回。"""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default):
    """读取一个持久化选择，并把旧配置或手工编辑的非法值降级为默认值。"""
    options = list(options)
    saved = config.ui.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool 是 int 的子类，``True == 1``。手工把数值选项写成 TOML
    # 布尔值时必须拒绝，不能让它伪装成第一个数值 option。
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # 返回 options 中的真实值，顺便把 TOML 1.0 等价归一化为
            # 整数选项 1，避免下游参数类型随配置写法漂移。
            return option

    # TOML 中的数值通常保留原类型；仍兼容用户手工写成字符串的情况。
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """读取并限幅持久化数值，避免非法配置破坏 Streamlit slider。"""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """兼容 TOML 布尔值和常见手工字符串，拒绝含义不明的旧值。"""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """只把标准六位十六进制颜色传给 Streamlit color picker。"""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """读取持久化文本并遵守对应 WebUI 控件的长度上限。"""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value


def _run_llm_read_operation(operation_name, operation):
    """
    使用稳定的当前 LLM 配置执行只读请求，并避免等待视频生成任务。

    能立即取得配置锁时继续沿用原来的互斥保护；锁已被后台视频任务持有时，
    全局配置在任务结束前不会发生变化，因此可以安全复制当前配置，并叠加页面
    尚未落盘的 Provider、模型和密钥。这样新文案使用界面中的最新选择，同时
    不会改变正在生成的视频任务。
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # 配置层在复制全局值和叠加待更新值期间持有队列锁，因此快照只能看到
        # 更新前或更新后的完整状态，不会混用两组 Provider 参数。
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)


def _parse_chatterbox_voices(voices):
    # Chatterbox 是自托管服务，音色列表由用户在 WebUI 中手动输入。
    # 这里统一兼容 TOML 数组和输入框里的逗号分隔字符串，避免下拉框、
    # 试听按钮和后续生成流程使用不同格式导致状态不一致。
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit 的按钮会触发整页 rerun，而 Chatterbox 配置输入框位于
    # “试听语音合成”按钮之后。如果试听时只读取 config.chatterbox，可能拿不到
    # 用户刚在输入框里填入的 base_url/model/voices。先从 session_state 同步一次，
    # 可以保证按钮逻辑和输入框显示逻辑使用同一份最新配置。
    _set_runtime_config(
        "chatterbox",
        "base_url",
        (
            st.session_state.get(
                "chatterbox_base_url_input",
                config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "api_key",
        st.session_state.get(
            "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "chatterbox",
        "model_id",
        (
            st.session_state.get(
                "chatterbox_model_input",
                config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            )
            or DEFAULT_CHATTERBOX_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "chatterbox_voices_input",
                config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
            )
        ),
    )


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # 即使请求 response_format=mp3，也会返回 WAV 内容。WebUI 试听如果固定
    # 使用 audio/mp3，浏览器可能无法播放，因此这里按文件头识别真实格式。
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    ):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """为浏览器上传文件生成受控的服务端保存路径。"""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # 不复用浏览器传入的文件名，避免路径分隔符、控制字符或同名覆盖。UUID 只用于
    # 服务端落盘，不改变用户在上传控件中看到的原始名称。
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path


def _initialize_session_state():
    """集中初始化跨 rerun 保留的页面状态。"""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI 可以不经过 FastAPI 独立运行，因此也需要在首次会话初始化时处理
        # 进程重启留下的发布状态。恢复失败时不写标记，后续 rerun 会再次尝试。
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # 已落盘的本地素材允许用户只修改文案后继续复用。
        "local_video_materials": [],
        # 生成按钮回调先登记任务，使顶部入口能立即显示运行中数量。
        "active_generation_tasks": {},
        # 最近一次从当前页面提交的任务。生成改为后台执行后，页面 Fragment
        # 通过这个 ID 查询状态；刷新时不再依赖正在执行的旧页面脚本。
        "current_generation_task_id": "",
        # LoomLoom 询价与执行必须跨 Streamlit rerun 保留完全相同的输入和
        # clientRequestId，避免网络重试产生重复付费任务。
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        # AI 视频按素材段计费，默认只生成一段，用户确认效果后再主动增加数量。
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_initialize_session_state()


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # 新功能优先维护中英文。其它语言缺少单项翻译时统一回退英文，避免在多个
    # locale 中复制相同英文后长期失去同步；英文也没有该键时才显示原始 key。
    return locales.get("en", {}).get("Translation", {}).get(key, key)


# -----------------------------------------------------------------------------
# 任务管理：历史扫描、运行状态、参数恢复与列表交互
# -----------------------------------------------------------------------------


def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."


def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def _find_final_task_video(task_path: str) -> str:
    """
    返回任务目录中序号最小的最终成片。

    合成流程还会产生 combined、temp-clip 和 MoviePy 临时文件，这些文件不能
    表示任务已成功完成，因此这里只接受 ``final-<序号>.<扩展名>``。
    """
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    记录历史任务中无法由 Streamlit 自动恢复的上传文件依赖。

    浏览器不允许程序重新填充 file_uploader，因此恢复任务时需要单独记录本地
    素材和自定义音频依赖，并在用户重新生成前检查是否已经主动补充或替换。
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """返回当前表单仍未满足的历史上传文件依赖。"""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # 新版 WebUI 使用显式配音方式。用户切换到自动配音或无配音，表示
            # 已主动替换历史上传音频；只有继续选择上传模式时才要求重新上传。
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # 保留旧调用方按音色判断的兼容行为，避免影响 API 和已有测试工具。
            unmet.add("custom_audio")

    return unmet


def _queue_task_restore(task_id):
    # 任务列表运行在 fragment 中，不能直接修改已经创建的主表单控件状态。
    # 这里只记录候选任务并触发整页 rerun，确认和参数恢复由主页面统一处理。
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")


def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state


def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks


def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()


def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]


def _prepare_generation_task():
    # st.button 的 on_click 会在页面脚本重新执行前触发。这里提前生成任务 ID，
    # 顶部任务管理入口就能在同一次 rerun 中显示“生成中”数量。
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)


def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")


def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"


def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # 任务管理 fragment 每两秒刷新一次。先只读取低成本的目录元数据并截取最近
    # 的任务，再解析 script.json 和视频列表，避免历史任务很多时反复扫描全部内容。
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # 单个任务目录可能正在被删除，不应因此让整个任务面板失效。
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {}) if script_data else {}
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_data.get("script", "")[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks


def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        # 2026-08-09 老杨 20:03 bug 修复:
        # 老代码 task.get("video_subject") 只拿 Redis 顶层 video_subject 字段
        # mv_batch 提交的 task 只存 params dict (params.video_subject 嵌套),
        # webui 提交的 task 才顶层 video_subject.
        # -> mv_batch 的 task 在任务管理里显示 subject='-' (空)
        # -> Play 按钮 disabled (因为 video_file 也是空 + history_task 补不上)
        # -> 用户看到 '正在生成视频请稍候' (因为 disabled Play 不跳详情, 但 session 还记得 current_generation_task_id)
        # 修复: 额外从 params 拿, 兼容两种提交路径
        params_data = task.get("params") or {}
        if isinstance(params_data, str):
            try:
                import json as _json
                params_data = _json.loads(params_data)
            except Exception:
                params_data = {}
        subject = (
            task.get("video_subject")
            or params_data.get("video_subject") if isinstance(params_data, dict) else None
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": os.path.getmtime(task_path)
            if os.path.isdir(task_path)
            else history_task.get("mtime", 0),
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in _active_generation_tasks().items():
        history_task = history_tasks.get(task_id, {})
        # 2026-08-09 老杨 20:03 bug 修复:
        # 老代码只跳 complete/failed 状态的任务, 但 mv_batch 提交的 task
        # (老代码) 在 history 里 state=None, 没有 final-1.mp4 -> video_file=空
        # (因为 P2-7 修复前 script.json 没写 final-1.mp4 路径).
        # 实际上 mv_batch 的 task 有 final-1.mp4 + Redis state=1, 但 history scan 不读这些.
        # -> active 列表会覆盖成 state=PROCESSING + video_file=空
        # -> 用户看到 '正在生成视频请稍候' 但 task 已完成
        # 修复: 如果 history_task 有 video_file (final-1.mp4 实际存在), 不允许 active 覆盖
        history_video = history_task.get("video_file") if history_task else ""
        if history_task and (
            _task_state_filter_key(history_task) in {"complete", "failed"}
            or (history_video and os.path.isfile(history_video))
        ):
            # 会话中的 active 标记只负责覆盖任务刚提交到状态存储前的极短窗口。
            # 后台任务结束后必须以真实终态为准，不能把失败任务重新显示为生成中。
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    return sorted(tasks, key=lambda item: item["mtime"], reverse=True)[:limit]


def _is_headless_server():
    # Docker 或无桌面的服务器部署中，WebUI 进程接触不到用户的桌面环境：
    # xdg-open / webbrowser 只会在容器内静默失败。此时应改为浏览器内预览
    # 视频、以路径提示代替打开目录。macOS/Windows 桌面部署不受影响。
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_task_path(task_path):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path: {normalized_path}")
        return
    if not os.path.isdir(normalized_path):
        return
    if _is_headless_server():
        # storage 目录通常以卷挂载映射回宿主机，提示相对路径即可定位文件。
        rel_path = os.path.relpath(normalized_path, os.path.dirname(tasks_root))
        st.toast(f"{tr('Open Task Folder')}: ./storage/{rel_path}", icon="📂")
        return
    webbrowser.open(f"file://{normalized_path}")


def _open_task_video(video_file):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_file = os.path.abspath(video_file)

    # 视频路径来自任务目录扫描或运行期状态。这里仍然限制只能打开任务目录
    # 内的文件，避免 UI 操作被异常路径扩展成任意本地文件打开能力。
    if not normalized_file.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # 无桌面环境时在任务面板内嵌播放器预览，代替调用系统播放器。
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")


def _delete_task(task_id, task_path, task_state=None):
    # 页面展示的状态可能落后于后台任务。删除前同时检查传入状态、当前会话的
    # 活跃任务和最新状态，避免任务刚开始或已产出中间视频时被误删。
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # 删除任务会移除任务状态和本地生成文件。这里必须限定在 storage/tasks
    # 下，避免异常 task_path 造成误删其它本地目录。
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        logger.info(f"deleted task: {task_id}")
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False


def _count_processing_tasks(tasks):
    # 顶部任务管理入口只需要展示“生成中”任务数量。
    # 这里复用内部状态 key 判断，避免依赖多语言展示文案导致不同语言下统计不一致。
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)


def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"


def _build_video_download_name(subject, index, total):
    """根据视频主题生成跨平台安全的下载文件名。"""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"
    # Win32 在识别设备名时会忽略扩展名前的尾随空格和句点。与背景音乐上传
    # 的现有规则保持一致，避免 ``CON .topic`` 绕过保留名保护。
    windows_basename = safe_subject.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        safe_subject = f"_{safe_subject}"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"


def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns([1.1, 1.7, 3.0, 0.8, 1.6], vertical_alignment="center")
        header_cols[0].caption(tr("Task Status"))
        header_cols[1].caption(tr("Task Updated At"))
        header_cols[2].caption(tr("Task Subject"))
        header_cols[3].caption(tr("Task Progress"))
        header_cols[4].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:12]
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for task in visible_tasks:
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # 使用 Streamlit 原生 bordered container + columns 保留每行操作。
            # 相比自定义 HTML/CSS 表格，这种方式对 Streamlit 版本变更更稳；
            # 相比 dataframe，又能保留播放、打开目录、删除等行内动作。
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    [1.1, 1.7, 3.0, 0.8, 1.6],
                    vertical_alignment="center",
                )
                row_cols[0].write(_task_state_label(task["state"], has_video))
                row_cols[1].write(_format_task_time(task["mtime"]))
                row_cols[2].write(_format_task_subject(task["subject"]))
                row_cols[3].write(f"{task['progress']}%")

                action_cols = row_cols[4].columns(
                    5,  # 2026-08-09 P2-6: 加 1 个 "🎵 MV" 弹窗按钮
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    # 2026-08-09 P2-6: MV 弹窗入口 (点击展开音频特征 + LLM plan)
                    mv_label = tr("MV")
                    if st.button(
                        mv_label,
                        key=f"mv_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/queue_music:",
                        help=f"{mv_label}: 音频特征 + LLM plan",
                    ):
                        _render_mv_analysis_dialog(task_id, task)
                with action_cols[1]:
                    # 老 0 (P2-6 后变为 1)
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        # 2026-08-09 老杨 19:19 拍板 B 方案: 跳转任务详情页 (那里已有 st.video)
                        # 之前调 _open_task_video -> 服务器端 xdg-open -> 远端无桌面失败
                        # Streamlit popover 没 API 直接 close, 老杨点 popover 外关掉即可
                        st.session_state["current_generation_task_id"] = task_id
                        st.session_state["task_manager_popover_nonce"] = (
                            st.session_state.get("task_manager_popover_nonce", 0) + 1
                        )
                        st.rerun(scope="app")

                with action_cols[2]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/folder_open:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[3]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[4]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    if st.button(
                        delete_label,
                        key=f"delete_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/delete:",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        if _delete_task(task_id, task["task_path"], task["state"]):
                            st.toast(tr("Task Deleted"))
                            st.rerun()
                        else:
                            st.error(tr("Task Delete Failed"))


def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 支持有状态 Tabs 的惰性渲染。切换时只重新构建当前列表，
    # 避免定时 Fragment 每两秒重复创建四套任务行和操作按钮。
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()


def _render_task_video_preview():
    # 无桌面部署下“播放”按钮的浏览器内回退：在任务面板底部渲染播放器。
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        preview_file.startswith(tasks_root + os.sep) and os.path.isfile(preview_file)
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        "✕",
        key="close_task_video_preview",
        use_container_width=True,
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    st.video(preview_file)


@st.fragment(run_every="2s")
def _render_task_manager_entry():
    # 任务可能由当前页面或其它页面触发生成。入口单独用 fragment 定时刷新，
    # 只更新任务数量和 popover 内容，不打断主页面表单输入。
    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    with st.container(key="task_manager_entry", width="content"):
        with st.popover(
            _task_manager_label(processing_task_count),
            width="content",
            key=(
                "task_manager_popover_"
                f"{st.session_state.get('task_manager_popover_nonce', 0)}"
            ),
        ):
            _render_task_manager_panel(task_summaries)


def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }


def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"


def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value


def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True


def _apply_restored_params(params):
    """
    把一份完整的生成参数写回页面控件状态。

    历史任务恢复和设置预设导入使用同一份参数模型，因此共用同一个实现，避免
    新增字段时只更新其中一条路径。调用方必须在渲染任何控件之前执行，否则
    Streamlit 会拒绝修改已经实例化的控件状态。
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # 文案与高级脚本设置。
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # 视频设置。素材上传控件不能由服务端写入，因此本地素材需要用户重新选择。
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_fit_mode_select",
        params.get("video_fit_mode") or VideoFitMode.cover.value,
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # API 可以写入超过 WebUI 范围的速度，任务生成阶段会安全归一化，但
        # 历史记录仍可能保留原值。恢复任务前再次归一化，避免给 Streamlit
        # slider 注入越界值、NaN 或无穷值导致控件状态异常。
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # 音频设置。TTS server 未写入旧任务，根据历史 voice_name 推断。
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # 字幕设置。对旧任务中的越界数值做最小限幅，避免 Slider 无法初始化。
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # 历史任务只保存素材路径，不能保证这些文件在当前环境仍然存在。
    # 同时清空当前页面已缓存的上传素材，避免恢复后误用另一个任务的文件。
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True


def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)


@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        use_container_width=True,
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")


def _dismiss_settings_dialog():
    """关闭设置弹窗，并确保下一次整页 rerun 不会再次自动打开。"""
    st.session_state["settings_dialog_open"] = False


def _open_settings_dialog(target_tab=None):
    """打开设置弹窗，并可直接定位到指定业务标签页。"""
    st.session_state["settings_dialog_open"] = True
    if target_tab:
        # 这里只保存稳定的业务 ID，不保存翻译文本；真正创建 tabs 前再根据
        # 当前界面语言解析 label，避免用户切换语言后旧文案成为非法选项。
        st.session_state["settings_dialog_target_tab"] = target_tab


def _open_material_settings_dialog():
    """供视频来源组件回调使用：直接打开素材服务设置。"""
    _open_settings_dialog("material")


def _render_brand(available_update: str | None = None):
    """渲染项目名称、当前版本和可选的更新入口。"""
    update_link = ""
    if available_update:
        update_label = html.escape(
            tr("Update Available").format(version=available_update)
        )
        # Streamlit 会继续用 Markdown 解析传入的 HTML。这里保持链接为单行，
        # 避免多行字符串的缩进被识别成代码块，导致页面直接显示 HTML 源码。
        update_link = (
            '<a class="mpt-brand__update" '
            f'href="{version_checker.LATEST_RELEASE_PAGE_URL}" '
            'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{update_label}" title="{update_label}">'
            f"{update_label}</a>"
        )
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">MoneyPrinterTurbo</span>
            <a class="mpt-brand__version"
               href="https://github.com/harry0703/MoneyPrinterTurbo"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="Open MoneyPrinterTurbo on GitHub"
               title="Open project on GitHub">v{html.escape(str(config.project_version))}</a>
            {update_link}
        </h1>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="1s")
def _render_pending_version_check():
    """检查未完成时只刷新品牌区域，避免阻塞或反复执行整页表单。"""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # 检查完成后刷新一次整页，让顶部栏改为静态渲染并停止 fragment 轮询。
        # 该刷新发生在后台请求完成之后，不会延迟初始页面的其它内容。
        st.rerun(scope="app")
    _render_brand()


def _render_top_bar():
    """渲染品牌、任务管理、设置和语言切换组成的页面顶部栏。"""
    # 顶部栏分为品牌区和操作区两个独立区域。窄屏下由 Streamlit
    # 将两个区域整体换行，操作区内部再根据剩余宽度自动换行。
    with st.container(key="top_bar"):
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()

    with actions_col:
        # 老杨 8/8 17:34: 临时成功提示 (avoid st.toast fragment rerun x2)
        _apply_msg = st.session_state.pop("_mv_apply_message", None)
        if _apply_msg:
            apply_ts = st.session_state.pop("_mv_apply_message_ts", 0)
            # 50秒内有效, 超时不清
            if time.time() - apply_ts < 50:
                st.success(_apply_msg)
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
                on_click=_open_settings_dialog,
            )

            language_codes = list(locales.keys())
            selected_index = 0
            for i, code in enumerate(language_codes):
                if code == st.session_state.get("ui_language", ""):
                    selected_index = i

            selected_language_code = st.selectbox(
                "Language / 语言",
                options=language_codes,
                index=selected_index,
                format_func=lambda code: locales[code].get("Language", code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                width=180,
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # 浏览器自动识别只影响当前会话；只有用户主动切换下拉框时才
                    # 写入 config.toml，后续新会话将优先使用该明确选择。
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # 切换语言后强制刷新，避免 selectbox 继续展示旧语言文案。
                    st.rerun()


support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# -----------------------------------------------------------------------------
# 通用 UI 组件、资源缓存与日志
# -----------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def get_all_fonts():
    # 字体目录很少变化，但 Streamlit 每次控件交互都会 rerun 页面。短周期缓存
    # 可以避免连续重复 os.walk，同时保证新增字体后最多 30 秒即可被发现。
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # 背景音乐与字体使用相同的短周期策略，不做永久缓存，兼顾 rerun 性能和
    # 用户运行期间手动添加音乐文件的场景。
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")


@st.cache_resource
def init_log():
    # 基础日志 Handler 属于进程级资源，而不是页面会话状态。Streamlit 每次组件
    # 交互都会 rerun 页面脚本，代码热重载也可能让缓存失效。日志初始化只能
    # 精确替换终端 Handler，不能清空正在生成任务使用的 WebUI 临时 Handler。
    _lvl = "DEBUG"

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


init_log()


def tr_optional(key, fallback_language=""):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""


def render_onboarding_tour():
    # 引导只覆盖三个稳定入口，不尝试控制 Dialog、Tabs 或业务表单。这样既能让
    # 新用户理解完整流程，也不会把引导状态与 Streamlit 的动态组件生命周期耦合。
    steps = [
        Tour.bind(
            "open_settings_dialog_button",
            title=tr("Onboarding Model Settings Title"),
            desc=tr("Onboarding Model Settings Description"),
            side="bottom",
            align="end",
        ),
        Tour.bind(
            "main_settings_grid",
            title=tr("Onboarding Creation Settings Title"),
            desc=tr("Onboarding Creation Settings Description"),
            side="top",
            align="center",
        ),
        Tour.bind(
            "generate_video_button",
            title=tr("Onboarding Generate Video Title"),
            desc=tr("Onboarding Generate Video Description"),
            side="top",
            align="center",
        ),
    ]

    # streamlit-tour 1.1.0 没有在 Python 构造参数中暴露导航文案，但底层
    # Driver.js 支持在每一步的 popover 配置中覆盖按钮文本。这里统一注入本地化
    # 文案，并对内容做 HTML 转义，因为组件会通过 innerHTML 渲染这些字段。
    previous_text = html.escape(tr("Onboarding Previous"))
    next_text = html.escape(tr("Onboarding Next"))
    done_text = html.escape(tr("Onboarding Done"))
    for index, step in enumerate(steps):
        step.popover["prevBtnText"] = f"&larr; {previous_text}"
        # Driver.js 会在合并单步配置时覆盖已经替换过变量的进度模板，因此直接
        # 写入当前步骤和总步骤数，避免页面显示未解析的 {{current}} 占位符。
        step.popover["progressText"] = f"{index + 1} / {len(steps)}"
        if index == len(steps) - 1:
            step.popover["doneBtnText"] = done_text
        else:
            step.popover["nextBtnText"] = f"{next_text} &rarr;"

    tour = Tour(
        steps=steps,
        key=ONBOARDING_TOUR_KEY,
        show_progress=True,
        animate=True,
        overlay_opacity=0.55,
        one_time_tour=True,
    )

    # 每个 Streamlit 会话只主动启动一次。是否已经完成则由组件通过浏览器
    # localStorage 判断，避免页面 rerun 或普通控件交互反复弹出引导。
    auto_start_key = f"{ONBOARDING_TOUR_KEY}-auto-started"
    if not st.session_state.get(auto_start_key, False):
        st.session_state[auto_start_key] = True
        tour.start()


def _render_generation_logs(task_id):
    """渲染后台任务日志快照，不从工作线程访问 Streamlit 会话状态。"""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))


def _render_generation_task_snapshot(task_id, task):
    """根据状态存储中的快照渲染进度、失败原因或最终成片。"""
    if not task:
        # 2026-08-09 老杨 20:18 bug 修复:
        # 老代码: task=None 时直接 st.info("Generating Video"), 用户看到 '正在生成' 永远不消失
        # 真根因: 老 task (Redis 状态过期被清理) 在 session current_generation_task_id 里残留
        # 修复: task=None 时先看本地 storage/tasks/<id>/ 有没有 final-1.mp4,
        #       有就当完成处理 (从 script.json 拿 subject/video_file)
        task_path = os.path.join(utils.task_dir(), task_id)
        video_file = _find_final_task_video(task_path) if os.path.isdir(task_path) else ""
        if video_file:
            script_data = _safe_load_task_script(task_path) or {}
            params_data = script_data.get("params") if isinstance(script_data, dict) else {}
            subject = (
                params_data.get("video_subject") if isinstance(params_data, dict) else None
                or (script_data.get("script", "")[:40] if isinstance(script_data, dict) else "")
                or task_id
            )
            st.success(tr("Video Generation Completed"))
            st.video(video_file)
            return
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    st.success(tr("Video Generation Completed"))
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    try:
        player_cols = st.columns(len(video_files) * 2 + 1)
        for i, url in enumerate(video_files):
            with player_cols[i * 2 + 1]:
                st.video(url)
                if not os.path.isfile(url):
                    logger.warning(
                        f"generated video is unavailable for download: "
                        f"task_id={task_id}, video_file={url}"
                    )
                    continue

                download_label = tr("Download Video")
                if len(video_files) > 1:
                    download_label = f"{download_label} {i + 1}"
                download_name = _build_video_download_name(
                    task.get("video_subject"),
                    i + 1,
                    len(video_files),
                )
                with open(url, "rb") as video_file:
                    st.download_button(
                        download_label,
                        data=video_file,
                        file_name=download_name,
                        mime=mimetypes.guess_type(url)[0] or "video/mp4",
                        key=f"download_generated_video_{task_id}_{i}",
                        icon=":material/download:",
                        on_click="ignore",
                        use_container_width=True,
                    )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_generation_logs(task_id)
    if st.session_state.get("handled_generation_task_id") != task_id:
        # Fragment 可能重复渲染同一个完成任务。无论是否开启自动打开目录，
        # 每个任务都只处理一次完成事件，避免重复弹出资源管理器或重复写入日志。
        st.session_state["handled_generation_task_id"] = task_id
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """只在任务运行期间轮询；结束后切回静态结果，停止不必要的定时刷新。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    # 2026-08-09 老杨 20:18 bug 修复:
    # 老代码: task=None 时直接 _render_generation_task_snapshot(task_id, None)
    #         -> snapshot 显示 'Generating Video' + fragment 每 2s 轮询
    # -> 用户看到 '正在生成视频请稍候' 永远不消失
    # 真根因: 老 task (Redis 状态过期被清理) 在 session current_generation_task_id 里残留
    # 修复: task=None 时调 snapshot 那里加了 history fallback (看 final-1.mp4)
    if task is None:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, None)
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # 完整页面脚本现在没有耗时生成逻辑，可以安全 rerun 并把结果改为静态
        # 渲染。这样任务结束后不会让浏览器永久保留一个两秒轮询的 Fragment。
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    """在生成按钮下方恢复当前页面最近提交任务的可查询 UI。"""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    # 2026-08-09 老杨 20:18 bug 修复:
    # 老代码: task=None 时走 _render_running_generation_task -> fragment 反复轮询
    # 修复: task=None 也调 snapshot (那里加了 history fallback 看 final-1.mp4)
    if task is None:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, None)
        return

    _render_running_generation_task(task_id)


def get_llm_provider_tips(provider_id, **kwargs):
    # LLM provider 说明文案统一使用 `llm_provider_tips.<provider_id>` 规则。
    # 这样新增 provider 时只需要在 locale 中补文案；没有文案时不展示提示块，
    # 避免 Main.py 里继续堆叠大量中英文硬编码说明。
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Provider 配置说明目前统一维护中文和英文两套规范模板；其它界面语言
    # 统一使用英文，避免在 locale 中复制英文后长期不同步。后续某个语种完成
    # 全量翻译后，再将它加入这里的独立维护范围。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": service_endpoint.model_docs_url if service_endpoint else "",
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips


def format_llm_connection_error(provider_id, base_url, error):
    """为可明确定位的鉴权错误补充配置检查建议，同时保留原始响应。"""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)


def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label


def get_tts_provider_tips(provider_id):
    # TTS 配置说明与 LLM Provider 采用相同维护策略：只维护中英文，
    # 其它界面语言统一回退英文，避免复制后长期不同步。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )


def localized_widget_key(name, *parts):
    # 部分 Streamlit selectbox 使用稳定 key 记住选择状态，但展示文本来自 locale。
    # 语言切换时把语言也放进 key，可以强制重建控件，避免选中项仍显示旧语言。
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)


def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 对 selectbox 的状态复用更敏感：如果控件没有固定 key，
    # 或者真实选项只是一组临时下标，页面 rerun 后容易被重新计算的 index 覆盖，
    # 表现为用户第一次选择不生效、需要再选一次。这个 helper 统一用稳定业务值
    # 作为真实选项，并在 session_state 里保存该值；展示文案只通过 format_func
    # 转换，避免翻译文案、选项顺序或上游配置变化影响选择状态。
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # 如果上游选项发生变化（例如切换 TTS provider 后声音列表变了），
        # 旧值已经不合法。控件创建前直接初始化 session_state，之后只让 key
        # 管理状态，不再同时传入 index。这样可以避免 Streamlit 在 rerun 时
        # 用重新计算的 index 覆盖用户刚选择的值，导致第一次选择不生效。
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


# Streamlit 原生 selectbox 暂不支持 HTML optgroup。这里使用 1.59 自带的
# Components v2 封装原生 <select>/<optgroup>，无需引入前端依赖，同时保留浏览器
# 原生的键盘导航、无障碍语义和移动端选择体验。组件只传递固定业务值和翻译文本，
# 不接收任意 HTML，从边界上避免配置内容进入 innerHTML。
_GROUPED_SELECT_COMPONENT = st.components.v2.component(
    "mpt_grouped_select",
    html="""
        <div class="mpt-grouped-select">
            <div class="mpt-grouped-select__label-row">
                <label class="mpt-grouped-select__label"></label>
                <button class="mpt-grouped-select__settings" type="button"></button>
            </div>
            <div class="mpt-grouped-select__control">
                <select></select>
            </div>
        </div>
    """,
    css="""
        .mpt-grouped-select {
            width: 100%;
            color: var(--st-text-color);
            font-family: var(--st-font);
        }

        .mpt-grouped-select__label-row {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.45rem;
            margin-bottom: 0.35rem;
        }

        .mpt-grouped-select__label {
            font-size: 0.875rem;
            line-height: 1.25rem;
        }

        .mpt-grouped-select__settings {
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--st-link-color);
            font: inherit;
            font-size: 0.8rem;
            line-height: 1.25rem;
            cursor: pointer;
        }

        .mpt-grouped-select__settings:hover {
            text-decoration: underline;
            text-underline-offset: 0.15rem;
        }

        .mpt-grouped-select__settings:focus-visible {
            border-radius: 0.2rem;
            outline: 2px solid var(--st-primary-color);
            outline-offset: 2px;
        }

        .mpt-grouped-select__control {
            position: relative;
        }

        .mpt-grouped-select__control::after {
            position: absolute;
            top: 50%;
            right: 1rem;
            width: 0.55rem;
            height: 0.55rem;
            border-right: 2px solid currentColor;
            border-bottom: 2px solid currentColor;
            content: "";
            pointer-events: none;
            transform: translateY(-70%) rotate(45deg);
        }

        .mpt-grouped-select select {
            width: 100%;
            min-height: 2.5rem;
            padding: 0.45rem 2.75rem 0.45rem 0.75rem;
            border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            border-radius: 0.5rem;
            outline: none;
            appearance: none;
            background: var(--st-secondary-background-color);
            color: inherit;
            font: inherit;
            cursor: pointer;
        }

        .mpt-grouped-select select:hover {
            border-color: color-mix(in srgb, currentColor 36%, transparent);
        }

        .mpt-grouped-select select:focus-visible {
            border-color: var(--st-primary-color);
            box-shadow: 0 0 0 1px var(--st-primary-color);
        }
    """,
    js="""
        export default function(component) {
            const { data, parentElement, setTriggerValue } = component;
            const label = parentElement.querySelector("label");
            const settings = parentElement.querySelector(".mpt-grouped-select__settings");
            const select = parentElement.querySelector("select");

            label.textContent = data.label;
            settings.textContent = data.settingsLabel;
            settings.hidden = !data.settingsLabel;
            select.id = data.controlId;
            label.htmlFor = data.controlId;
            select.setAttribute("aria-label", data.label);
            select.replaceChildren();

            for (const groupData of data.groups) {
                const group = document.createElement("optgroup");
                group.label = groupData.label;
                for (const optionData of groupData.options) {
                    const option = document.createElement("option");
                    option.value = optionData.value;
                    option.textContent = optionData.label;
                    group.appendChild(option);
                }
                select.appendChild(group);
            }

            select.value = data.value;
            const handleChange = () => {
                setTriggerValue("selected", select.value);
            };
            const handleSettings = () => {
                setTriggerValue("settings", true);
            };
            select.addEventListener("change", handleChange);
            settings.addEventListener("click", handleSettings);

            return () => {
                select.removeEventListener("change", handleChange);
                settings.removeEventListener("click", handleSettings);
            };
        }
    """,
)


def grouped_selectbox(
    label,
    groups,
    default_value,
    key,
    format_func=None,
    settings_label="",
    on_settings=None,
):
    """渲染带不可选分组标题的单个下拉框，并返回稳定业务值。"""
    if format_func is None:
        format_func = str

    normalized_groups = []
    valid_values = []
    for group_label, options in groups:
        normalized_options = []
        for option in options:
            valid_values.append(option)
            normalized_options.append(
                {"value": option, "label": str(format_func(option))}
            )
        if normalized_options:
            normalized_groups.append(
                {"label": str(group_label), "options": normalized_options}
            )

    if not valid_values:
        raise ValueError(f"grouped selectbox options cannot be empty: {key}")
    if len(set(valid_values)) != len(valid_values):
        raise ValueError(f"grouped selectbox options must be unique: {key}")
    if default_value not in valid_values:
        default_value = valid_values[0]

    # 业务选择保存在与旧 selectbox 相同的 session key 中，设置预设恢复和
    # 语言切换逻辑无需分叉；组件自身使用独立 key，避免与业务状态冲突。
    widget_key = localized_widget_key(key)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = default_value
    selected_value = st.session_state[widget_key]
    if selected_value not in valid_values:
        selected_value = default_value
        st.session_state[widget_key] = selected_value

    result = _GROUPED_SELECT_COMPONENT(
        key=f"{widget_key}_component",
        data={
            "label": label,
            "settingsLabel": settings_label,
            # 显式关联可见 label 与原生 select。组件 key 由固定业务名称和
            # 语言代码组成，在页面内唯一，既方便鼠标点击标签聚焦控件，
            # 也不会引入随机 ID 导致每次 rerun 都重建前端状态。
            "controlId": f"{widget_key}_control",
            "value": selected_value,
            "groups": normalized_groups,
        },
        on_selected_change=lambda: None,
        on_settings_change=on_settings or (lambda: None),
    )
    changed_value = getattr(result, "selected", None)
    if changed_value in valid_values and changed_value != selected_value:
        st.session_state[widget_key] = changed_value
        # Components v2 在当前脚本轮次返回事件时，本轮传给前端的 data 仍是
        # 事件发生前的旧值。立即自动 rerun，让组件和依赖 video_source 的控件
        # 同时收到新值；否则下拉框会被旧 data 短暂覆盖，用户只能再选一次。
        st.rerun()

    return selected_value


def sync_script_order_concat_mode():
    """在文案顺序匹配开启时固定使用顺序拼接，并在关闭后恢复原选择。"""
    widget_key = localized_widget_key("video_concat_mode_select")
    previous_key = "video_concat_mode_before_script_order_match"
    match_script_order = bool(st.session_state.get("match_materials_to_script", False))

    if match_script_order:
        current_mode = st.session_state.get(widget_key, VideoConcatMode.random.value)
        if current_mode != VideoConcatMode.sequential.value:
            st.session_state[previous_key] = current_mode
        st.session_state[widget_key] = VideoConcatMode.sequential.value
        return

    previous_mode = st.session_state.pop(previous_key, None)
    if previous_mode in {
        VideoConcatMode.sequential.value,
        VideoConcatMode.random.value,
    }:
        st.session_state[widget_key] = previous_mode


def reset_script_system_prompt():
    """将高级脚本设置中的系统提示词恢复为当前版本的默认内容。"""
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT


def reset_subtitle_settings():
    """恢复 WebUI 字幕控件和持久化配置中的默认值。"""
    defaults = DEFAULT_SUBTITLE_SETTINGS
    st.session_state["subtitle_enabled_checkbox"] = defaults["subtitle_enabled"]
    _set_stable_widget_value("font_name_select", defaults["font_name"])
    _set_stable_widget_value("subtitle_position_select", defaults["subtitle_position"])
    st.session_state["custom_position_input"] = str(defaults["custom_position"])
    st.session_state["font_color_picker"] = defaults["text_fore_color"]
    st.session_state["font_size_slider"] = defaults["font_size"]
    st.session_state["stroke_color_picker"] = defaults["stroke_color"]
    st.session_state["stroke_width_slider"] = defaults["stroke_width"]
    st.session_state["subtitle_background_enabled_checkbox"] = defaults[
        "subtitle_background_enabled"
    ]
    st.session_state["subtitle_background_color_picker"] = defaults[
        "subtitle_background_color"
    ]
    st.session_state["rounded_subtitle_background_checkbox"] = defaults[
        "rounded_subtitle_background"
    ]

    # 同步会持久化的 UI 选项，确保恢复后刷新页面仍保持默认设置。
    for key in (
        "subtitle_enabled",
        "font_name",
        "subtitle_position",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "subtitle_background_enabled",
        "subtitle_background_color",
        "rounded_subtitle_background",
    ):
        _set_runtime_config("ui", key, defaults[key])


@st.dialog(tr("Final Prompt Preview"), width="large")
def render_script_prompt_preview(prompt):
    """展示将要发送给大模型的完整脚本生成提示词。"""
    st.code(prompt, language="markdown", wrap_lines=True)


def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """使用稳定业务值创建单选分段控件，避免语言切换后状态被展示文案覆盖。"""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (
        (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    )
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _get_material_api_keys(config_key):
    """将配置中的素材 API Key 统一转换为 WebUI 可编辑字符串。"""
    api_keys = config.app.get(config_key, [])
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    return ", ".join(api_keys)


def _save_material_api_keys(config_key, value):
    """保存逗号分隔的素材 API Key，并允许用户显式清空旧配置。"""
    normalized_value = value.replace(" ", "")
    _set_runtime_config(
        "app",
        config_key,
        normalized_value.split(",") if normalized_value else [],
    )


def _format_file_size(size_bytes):
    """将字节数格式化为适合设置页展示的紧凑容量文本。"""
    size = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


@st.cache_data(ttl=30, show_spinner=False)
def _get_video_cache_stats(max_age_days=None):
    """
    短周期缓存目录统计，避免设置弹窗内普通控件交互反复扫描大量文件。

    缓存键包含清理天数，因此切换范围只会为每个范围扫描一次；主动刷新或清理
    完成后会显式清空，最多 30 秒的缓存不会影响实际删除时的二次扫描。
    """
    return cache_manager.get_video_cache_stats(max_age_days=max_age_days)


def _render_cache_management_settings(panel):
    """渲染默认在线视频素材缓存的统计、预览和安全清理操作。"""
    with panel:
        cleanup_message = st.session_state.pop("video_cache_cleanup_message", None)
        if cleanup_message:
            message_type, message = cleanup_message
            if message_type == "success":
                st.success(message)
            else:
                st.warning(message)

        st.caption(tr("Video Cache Directory"))
        st.code(cache_manager.video_cache_dir(), language="text")

        total_stats = _get_video_cache_stats()
        metric_count, metric_size, metric_oldest = st.columns(3)
        metric_count.metric(tr("Cache File Count"), total_stats.file_count)
        metric_size.metric(
            tr("Cache Total Size"), _format_file_size(total_stats.total_size)
        )
        oldest_text = (
            datetime.fromtimestamp(total_stats.oldest_mtime).strftime("%Y-%m-%d")
            if total_stats.oldest_mtime is not None
            else "-"
        )
        metric_oldest.metric(tr("Oldest Cache Date"), oldest_text)

        st.caption(tr("Video Cache Management Help"))
        cleanup_options = (30, 7, 90, None)
        cleanup_labels = {
            30: tr("Cache Older Than 30 Days"),
            7: tr("Cache Older Than 7 Days"),
            90: tr("Cache Older Than 90 Days"),
            None: tr("All Video Cache"),
        }
        max_age_days = st.selectbox(
            tr("Cache Cleanup Range"),
            options=cleanup_options,
            format_func=lambda value: cleanup_labels[value],
            key="video_cache_cleanup_range",
        )
        cleanup_preview = _get_video_cache_stats(max_age_days=max_age_days)
        st.info(
            tr("Cache Cleanup Preview").format(
                count=cleanup_preview.file_count,
                size=_format_file_size(cleanup_preview.total_size),
            )
        )

        confirm_nonce = st.session_state.get("video_cache_cleanup_confirm_nonce", 0)
        confirmed = st.checkbox(
            tr("Confirm Cache Cleanup"),
            key=f"video_cache_cleanup_confirm_{confirm_nonce}",
        )
        refresh_col, open_col, cleanup_col = st.columns(3)
        if refresh_col.button(
            tr("Refresh Cache Stats"),
            key="refresh_video_cache_stats",
            use_container_width=True,
            icon=":material/refresh:",
        ):
            _get_video_cache_stats.clear()
            st.rerun(scope="fragment")

        if open_col.button(
            tr("Open Cache Directory"),
            key="open_video_cache_directory",
            use_container_width=True,
            icon=":material/folder_open:",
        ):
            webbrowser.open(Path(cache_manager.video_cache_dir()).as_uri())

        cleanup_disabled = not confirmed or cleanup_preview.file_count == 0
        if cleanup_col.button(
            tr("Clean Cache Now"),
            key="clean_video_cache_now",
            type="primary",
            disabled=cleanup_disabled,
            use_container_width=True,
            icon=":material/delete_sweep:",
        ):
            result = cache_manager.clean_video_cache(max_age_days=max_age_days)
            message_key = (
                "Cache Cleanup Completed With Failures"
                if result.failed_count
                else "Cache Cleanup Completed"
            )
            st.session_state["video_cache_cleanup_message"] = (
                "warning" if result.failed_count else "success",
                tr(message_key).format(
                    count=result.deleted_count,
                    size=_format_file_size(result.deleted_size),
                    failed=result.failed_count,
                ),
            )
            # Streamlit 不允许在控件实例化后修改同名 session_state。通过递增
            # nonce 让下一次 fragment rerun 创建未勾选的新控件，避免清理完成后
            # 危险确认状态被继续保留。
            st.session_state["video_cache_cleanup_confirm_nonce"] = confirm_nonce + 1
            _get_video_cache_stats.clear()
            st.rerun(scope="fragment")


# -----------------------------------------------------------------------------
# 设置预设导出导入与密钥备份
# -----------------------------------------------------------------------------


def _is_credential_config_key(key):
    """判断一个配置项名称是否表示凭据。"""
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES)


def _is_backup_config_key(section_name, key):
    """凭据本身及其配套配置项都属于密钥备份范围。"""
    if _is_credential_config_key(key):
        return True
    if key in CREDENTIAL_COMPANION_KEYS.get(section_name, ()):
        return True
    return key in NON_LLM_COMPANION_KEYS.get(section_name, ())


def _credential_widget_state_keys(section_name, key):
    """
    返回某个凭据配置项对应的全部 Streamlit 控件 key。

    密码输入框都带 key，Streamlit 中 session_state 的值优先于控件的 value
    参数。恢复备份后必须清除这些残留控件状态，否则页面会继续显示旧密钥，
    并在下一次 rerun 把旧值重新写回配置，让恢复看起来没有生效。多个面板
    共用同一个密钥时会各自持有控件状态，因此返回默认 key 和全部别名。
    """
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def _normalize_backup_value(value):
    """归一化备份值，丢弃空字符串和空列表，避免恢复时覆盖成空配置。"""
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _collect_key_backup(config_sections):
    """从运行期配置分区中收集所有已填写的密钥及其配套配置项。"""
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def _count_backup_keys(backup):
    """统计备份中的配置项数量，用于界面提示和禁用空导出。"""
    return sum(len(entries) for entries in backup.values())


def _build_key_backup_payload(config_sections, app_version):
    """构造密钥备份文件内容。"""
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": _collect_key_backup(config_sections),
    }


def _load_transfer_payload(raw_bytes, schema, version):
    """
    解析导出文件，并校验它确实来自本功能的同一版本。

    用户可能上传任意 JSON。这里只接受声明了正确 schema 和版本的文件，让错误
    提示停留在导入入口，而不是把无法识别的内容写进配置或控件状态。
    Windows 编辑器可能保存带 BOM 的 JSON，因此按 utf-8-sig 解码。
    """
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        raise ValueError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("version") != version:
        raise ValueError(f"unsupported version: {payload.get('version')!r}")
    return payload


def _parse_key_backup(raw_bytes, config_sections):
    """
    解析密钥备份文件，只保留当前版本认识的分区和配置项。

    备份文件可以手工编辑，也可能来自更新的版本。未知分区或非密钥配置项一律
    忽略，避免通过导入功能改写与凭据无关的配置。
    """
    payload = _load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def _build_settings_preset_payload(params, app_version):
    """构造生成参数预设文件内容。"""
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def _parse_settings_preset(raw_bytes):
    """
    解析预设文件并交给 VideoParams 校验。

    预设可以在其它机器上生成，也可能被手工编辑。统一走模型校验可以复用既有
    的取值范围约束，非法预设在导入时就被拒绝，而不是在生成任务时才失败。
    """
    payload = _load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    # video_subject 是 VideoParams 的必填字段，但预设允许只保存风格设置。
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")


def _apply_key_backup(restored_keys):
    """把解析后的密钥写回运行期配置，并清除对应控件的残留状态。"""
    restored_count = 0
    for section_name, entries in restored_keys.items():
        for key, value in entries.items():
            _set_runtime_config(section_name, key, value)
            for widget_key in _credential_widget_state_keys(section_name, key):
                st.session_state.pop(widget_key, None)
            restored_count += 1
    # ElevenLabs 音色列表按密钥缓存，换用另一份备份后必须重新拉取。
    for cache_key in list(st.session_state.keys()):
        if str(cache_key).startswith("elevenlabs_voices_"):
            del st.session_state[cache_key]
    return restored_count


def _apply_pending_settings_preset():
    """在渲染任何控件之前应用已导入的预设。"""
    preset_params = st.session_state.pop("settings_preset_payload", None)
    if not preset_params:
        return False

    _apply_restored_params(preset_params)
    logger.info("applied imported settings preset")
    return True


def _render_settings_transfer(params):
    """渲染生成参数预设的导出与导入入口。"""
    with st.expander(tr("Settings Preset"), expanded=False):
        st.caption(tr("Settings Preset Help"))
        preset_payload = _build_settings_preset_payload(
            params.model_dump(mode="json"), config.project_version
        )
        st.download_button(
            tr("Export Settings"),
            data=json.dumps(preset_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=SETTINGS_PRESET_FILE_NAME,
            mime="application/json",
            use_container_width=True,
            key="export_settings_preset_button",
            icon=":material/download:",
        )
        uploaded_preset = st.file_uploader(
            tr("Import Settings"),
            type=["json"],
            key="settings_preset_uploader",
        )
        if uploaded_preset is None:
            return
        # 上传的文件在之后每次 rerun 都会重新出现。记录已处理的文件标识，
        # 避免用户改完控件后被同一个预设反复覆盖。
        if st.session_state.get("settings_preset_file_id") == uploaded_preset.file_id:
            return

        st.session_state["settings_preset_file_id"] = uploaded_preset.file_id
        try:
            preset_params = _parse_settings_preset(uploaded_preset.getvalue())
        except Exception as e:
            logger.warning(f"failed to import settings preset: {e}")
            st.error(tr("Settings Preset Import Failed"))
            return

        st.session_state["settings_preset_payload"] = preset_params
        st.rerun()


def _render_key_backup_settings(panel):
    """渲染密钥备份的导出与恢复入口。"""
    with panel:
        backup_message = st.session_state.pop("key_backup_message", None)
        if backup_message:
            message_type, message = backup_message
            if message_type == "success":
                st.success(message)
            else:
                st.error(message)

        st.caption(tr("Key Backup Help"))
        st.warning(tr("Key Backup Warning"))

        backup_payload = _build_key_backup_payload(
            _RUNTIME_CONFIG_SECTIONS, config.project_version
        )
        backup_key_count = _count_backup_keys(backup_payload["keys"])
        st.caption(tr("Key Backup Summary").format(count=backup_key_count))
        st.download_button(
            tr("Export Keys"),
            data=json.dumps(backup_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=KEY_BACKUP_FILE_NAME,
            mime="application/json",
            disabled=backup_key_count == 0,
            use_container_width=True,
            key="export_key_backup_button",
            icon=":material/download:",
        )

        uploaded_backup = st.file_uploader(
            tr("Import Keys"),
            type=["json"],
            key="key_backup_uploader",
        )
        if uploaded_backup is None:
            return
        if st.session_state.get("key_backup_file_id") == uploaded_backup.file_id:
            return

        st.session_state["key_backup_file_id"] = uploaded_backup.file_id
        try:
            restored_keys = _parse_key_backup(
                uploaded_backup.getvalue(), _RUNTIME_CONFIG_SECTIONS
            )
        except Exception as e:
            logger.warning(f"failed to import key backup: {e}")
            st.session_state["key_backup_message"] = (
                "error",
                tr("Key Restore Failed"),
            )
        else:
            restored_count = _apply_key_backup(restored_keys)
            _save_runtime_config()
            logger.info(f"restored keys from backup file: count={restored_count}")
            st.session_state["key_backup_message"] = (
                "success",
                tr("Keys Restored").format(count=restored_count),
            )
        # 主页面上的 TTS 密钥输入框也需要读取恢复后的配置，因此整页刷新。
        # 设置弹窗的打开状态保存在 session_state 中，刷新后会重新展开。
        st.rerun(scope="app")


# -----------------------------------------------------------------------------
# 设置与提示词弹窗
# -----------------------------------------------------------------------------


# 设置属于低频操作，使用中等尺寸 Dialog 避免长期占用主页面纵向空间，
# 同时控制阅读行宽，避免弹窗在宽屏设备上显得过于松散。
# Dialog 继承 fragment 行为，内部控件交互只重绘弹窗；函数末尾单独保存配置，
# 关闭时通过回调触发整页同步，确保生成流程读取最新 Provider 和界面设置。
@st.dialog(
    tr("Settings"),
    width="medium",
    on_dismiss=_dismiss_settings_dialog,
)
def _render_mv_analysis_settings(panel):
    """MV 意境分析设置 (老杨 8/8 17:40 拍板)

    放在 Settings dialog 里, 供老杨调:
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


def _render_settings_dialog():
    with st.container():
        # 历史 hide_config 只用于隐藏旧基础设置面板。改为固定设置入口后，该值
        # 不再有用户可见意义，统一迁移为 false，避免旧配置影响后续版本。
        _set_runtime_config("app", "hide_config", False)
        settings_tab_labels = [
            tr("LLM Settings Tab"),
            tr("Material API Tab"),
            tr("Auto-Publish Settings"),
            tr("Interface Settings Tab"),
            tr("Key Backup Tab"),
            tr("Cache Management Tab"),
            tr("MV Analysis Tab"),
        ]
        settings_tab_targets = {
            "llm": tr("LLM Settings Tab"),
            "material": tr("Material API Tab"),
            "mv": tr("MV Analysis Tab"),
        }
        settings_tabs_key = localized_widget_key("settings_dialog_tabs")
        target_tab = st.session_state.pop("settings_dialog_target_tab", None)
        if target_tab in settings_tab_targets:
            # st.tabs 使用显示 label 作为状态值。入口按钮只保存稳定业务 ID，
            # 到这里再写入当前语言的 label，即可精确定位且兼容语言切换。
            st.session_state[settings_tabs_key] = settings_tab_targets[target_tab]

        (
            middle_config_panel,
            right_config_panel,
            publish_config_panel,
            left_config_panel,
            key_backup_panel,
            cache_config_panel,
            mv_config_panel,
        ) = st.tabs(
            settings_tab_labels,
            key=settings_tabs_key,
            on_change="rerun",
        )

        with publish_config_panel:
            st.write(tr("Automatically publish generated videos to social media using upload-post.com"))
            st.info(
                tr("Upload-Post Setup Guide").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL,
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL,
                )
            )

            is_enabled = config.app.get("upload_post_enabled", False)
            is_auto = config.app.get("upload_post_auto_upload", False)

            # 两个键各自独立:enabled 允许外部流程调用 Upload-Post,
            # auto_upload 才决定渲染完成后是否自动发布。合并成一个复选框会在
            # 两键不一致的配置下,仅打开设置对话框就把 enabled 改写为 False。
            upload_post_enabled = st.checkbox(
                tr("Enable Upload-Post Integration"),
                value=is_enabled,
                key="upload_post_enabled_checkbox"
            )
            if upload_post_enabled != is_enabled:
                _set_runtime_config("app", "upload_post_enabled", upload_post_enabled)

            upload_post_auto_upload = st.checkbox(
                tr("Enable Auto-Publish"),
                value=is_auto,
                key="upload_post_auto_upload_checkbox"
            )
            if upload_post_auto_upload != is_auto:
                _set_runtime_config("app", "upload_post_auto_upload", upload_post_auto_upload)

            upload_post_api_key = st.text_input(
                tr("Upload-Post API Key"),
                value=config.app.get("upload_post_api_key", ""),
                type="password",
                help=tr("Upload-Post API Key Help").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL
                ),
                key="upload_post_api_key_input"
            )
            if upload_post_api_key != config.app.get("upload_post_api_key", ""):
                _set_runtime_config("app", "upload_post_api_key", upload_post_api_key)

            upload_post_username = st.text_input(
                tr("Upload-Post Profile Username"),
                value=config.app.get("upload_post_username", ""),
                help=tr("Upload-Post Profile Username Help").format(
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL
                ),
                key="upload_post_username_input"
            )
            if upload_post_username != config.app.get("upload_post_username", ""):
                _set_runtime_config("app", "upload_post_username", upload_post_username)

            upload_post_platforms = st.multiselect(
                tr("Platforms"),
                options=["tiktok", "instagram", "youtube"],
                default=config.app.get("upload_post_platforms", ["tiktok", "instagram"]),
                help="Select platforms to publish to",
                key="upload_post_platforms_multiselect"
            )
            if upload_post_platforms != config.app.get("upload_post_platforms", ["tiktok", "instagram"]):
                _set_runtime_config("app", "upload_post_platforms", upload_post_platforms)

            if "youtube" in upload_post_platforms:
                yt_status_options = ["public", "private", "unlisted"]
                yt_saved = config.app.get("upload_post_youtube_privacy_status", "public")
                if yt_saved not in yt_status_options:
                    yt_saved = "public"
                upload_post_youtube_privacy_status = st.selectbox(
                    tr("YouTube Privacy Status"),
                    options=yt_status_options,
                    index=yt_status_options.index(yt_saved),
                    key="upload_post_youtube_privacy_status_selectbox"
                )
                if upload_post_youtube_privacy_status != config.app.get("upload_post_youtube_privacy_status", "public"):
                    _set_runtime_config("app", "upload_post_youtube_privacy_status", upload_post_youtube_privacy_status)

        # 左侧面板 - 日志设置
        with left_config_panel:
            hide_log = st.checkbox(
                tr("Hide Log"),
                value=config.ui.get("hide_log", False),
                key="hide_log_checkbox",
            )
            _set_runtime_config("ui", "hide_log", hide_log)

        _render_cache_management_settings(cache_config_panel)
        # 密钥恢复会写回配置并清除密码控件状态，必须在下面渲染这些控件之前执行。
        _render_key_backup_settings(key_backup_panel)

        # === MV 分析设置 (老杨 8/8 17:40) ===
        _render_mv_analysis_settings(mv_config_panel)

        # 中间面板 - LLM 设置

        with middle_config_panel:
            # 下拉顺序、默认 label 和稳定 provider id 全部来自 Registry；locale
            # 只覆盖展示文案，不再让 Main.py 维护第二份 Provider 列表。
            llm_provider_ids = [
                provider.provider_id for provider in LLM_PROVIDER_REGISTRY
            ]
            llm_provider_labels = {
                provider.provider_id: get_llm_provider_label(provider)
                for provider in LLM_PROVIDER_REGISTRY
            }
            saved_llm_provider = config.app.get(
                "llm_provider", DEFAULT_LLM_PROVIDER_ID
            ).lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = DEFAULT_LLM_PROVIDER_ID

            llm_provider = stable_selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                default_value=saved_llm_provider,
                key="llm_provider_select",
                format_func=lambda provider_id: llm_provider_labels[provider_id],
            )
            # 配置表单和 Provider 说明并排展示，减少长说明在窄列中的换行，
            # 同时充分利用基础设置面板的横向空间。
            llm_form_panel, llm_help_panel = st.columns(
                [0.9, 1.1],
                gap="large",
                vertical_alignment="top",
            )
            llm_helper = llm_help_panel.container()
            _set_runtime_config("app", "llm_provider", llm_provider)
            llm_provider_spec = get_llm_provider(llm_provider)
            if llm_provider_spec is None:
                # 正常情况下下拉选项全部来自 Registry，不会进入该分支；保留
                # 明确错误用于诊断损坏的 session state 或后续接入遗漏。
                raise RuntimeError(f"unsupported llm provider: {llm_provider}")

            llm_api_key = config.app.get(llm_provider_spec.config_key("api_key"), "")
            configured_llm_base_url = config.app.get(
                llm_provider_spec.config_key("base_url"), ""
            )
            llm_default_base_url = llm_provider_spec.effective_default_base_url
            llm_base_url = configured_llm_base_url or llm_default_base_url
            llm_model_name = llm_provider_spec.resolve_model_name(
                config.app.get(llm_provider_spec.config_key("model_name"), "")
            )

            provider_tip_context = {}
            selected_service_endpoint = None
            if llm_provider_spec.service_endpoints:
                # Kimi 等 Provider 的中国站和国际站使用不同账号体系。只让用户
                # 选择服务区域，再由 Registry 同步 API 申请入口和 Base URL，
                # 避免手工组合错误。已有空 Base URL 配置继续沿用中国站，只有
                # 尚未填写 Key 的全新配置才根据界面语言推荐对应入口。
                selected_service_endpoint = (
                    llm_provider_spec.select_service_endpoint(
                        configured_llm_base_url,
                        has_api_key=bool(str(llm_api_key).strip()),
                        prefer_international=(
                            st.session_state.get("ui_language", "en") != "zh"
                        ),
                    )
                )
                endpoint_options = [
                    endpoint.endpoint_id
                    for endpoint in llm_provider_spec.service_endpoints
                ] + [CUSTOM_LLM_ENDPOINT_ID]
                default_endpoint_id = (
                    selected_service_endpoint.endpoint_id
                    if selected_service_endpoint
                    else CUSTOM_LLM_ENDPOINT_ID
                )
                endpoint_labels = {
                    endpoint.endpoint_id: (
                        tr_optional(
                            llm_provider_spec.endpoint_label_key(endpoint.endpoint_id),
                            fallback_language="en",
                        )
                        or endpoint.default_label
                    )
                    for endpoint in llm_provider_spec.service_endpoints
                }
                endpoint_labels[CUSTOM_LLM_ENDPOINT_ID] = (
                    tr_optional("Custom API Endpoint", fallback_language="en")
                    or "Custom API Endpoint"
                )
                with llm_form_panel:
                    selected_endpoint_id = stable_selectbox(
                        tr_optional(
                            llm_provider_spec.endpoint_selector_label_key,
                            fallback_language="en",
                        )
                        or tr("API Platform"),
                        options=endpoint_options,
                        default_value=default_endpoint_id,
                        key=f"{llm_provider}_service_endpoint_select",
                        format_func=lambda endpoint_id: endpoint_labels[endpoint_id],
                        help=(
                            tr_optional(
                                llm_provider_spec.endpoint_selector_help_key,
                                fallback_language="en",
                            )
                            or None
                        ),
                    )
                selected_service_endpoint = next(
                    (
                        endpoint
                        for endpoint in llm_provider_spec.service_endpoints
                        if endpoint.endpoint_id == selected_endpoint_id
                    ),
                    None,
                )
                if selected_service_endpoint:
                    llm_base_url = selected_service_endpoint.base_url
                    provider_tip_context.update(
                        {
                            "api_key_url": selected_service_endpoint.api_key_url,
                            "default_base_url": selected_service_endpoint.base_url,
                            "model_docs_url": selected_service_endpoint.model_docs_url,
                        }
                    )
                else:
                    # 自定义模式只保留用户明确保存的地址，不将某个标准区域伪装
                    # 成自定义值。输入为空时配置不会持久化，下一次仍回到兼容默认。
                    llm_base_url = str(configured_llm_base_url or "").strip()

            if llm_provider == "ollama":
                llm_default_base_url = config.get_default_ollama_base_url()
                if not llm_base_url:
                    llm_base_url = llm_default_base_url
                docker_hint = ""
                if config.is_running_in_container():
                    docker_hint = tr_optional(
                        "llm_provider_tips.ollama.docker_hint",
                        fallback_language="en",
                    )
                provider_tip_context["docker_hint"] = docker_hint

            tips = get_llm_provider_tips(llm_provider, **provider_tip_context)
            if tips:
                with llm_helper:
                    st.info(tips)

            st_llm_api_key = llm_api_key
            if llm_provider_spec.show_api_key:
                st_llm_api_key = llm_form_panel.text_input(
                    tr("API Key"),
                    value=llm_api_key,
                    type="password",
                    key=f"{llm_provider}_api_key_input",
                )

            st_llm_base_url = llm_base_url
            if llm_provider_spec.show_base_url:
                st_llm_base_url = llm_form_panel.text_input(
                    tr("Base Url"),
                    value=llm_base_url,
                    key=(
                        f"{llm_provider}_base_url_"
                        f"{selected_service_endpoint.endpoint_id}_input"
                        if selected_service_endpoint
                        else f"{llm_provider}_base_url_custom_input"
                    ),
                    disabled=selected_service_endpoint is not None,
                )
            st_llm_model_name = ""
            if llm_provider == "groq":
                effective_api_key = st_llm_api_key or llm_api_key
                effective_base_url = st_llm_base_url or llm_base_url
                groq_models = get_groq_model_ids(
                    api_key=effective_api_key,
                    base_url=effective_base_url,
                )

                if groq_models:
                    selected_index = 0
                    if llm_model_name in groq_models:
                        selected_index = groq_models.index(llm_model_name)

                    st_llm_model_name = llm_form_panel.selectbox(
                        tr("Model Name"),
                        options=groq_models,
                        index=selected_index,
                        key="groq_model_name_select",
                    )
                else:
                    st_llm_model_name = llm_form_panel.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key="groq_model_name_input",
                    )
                    if effective_api_key:
                        llm_form_panel.caption(tr("Groq Model List Load Failed"))
                    else:
                        llm_form_panel.caption(
                            tr("Groq API Key Required for Model List")
                        )
            else:
                st_llm_model_name = llm_form_panel.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
            # 输入框展示 Registry 默认值，但配置只保存真实的用户覆盖值。
            # 这样默认模型、Base URL 更新后，未自定义的用户能够自动跟随。
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("api_key"),
                st_llm_api_key,
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("base_url"),
                normalize_provider_override(
                    st_llm_base_url,
                    llm_default_base_url,
                ),
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("model_name"),
                normalize_provider_override(
                    st_llm_model_name,
                    llm_provider_spec.default_model,
                ),
            )

            # Provider 专用字段也由 Registry 声明。例如 Cloudflare AI Gateway
            # 需要 Account ID；以后新增类似字段时无需再在 Main.py 增加判断。
            for field in llm_provider_spec.extra_fields:
                field_config_key = llm_provider_spec.config_key(field.config_suffix)
                field_value = llm_form_panel.text_input(
                    tr(field.label_key),
                    value=(config.app.get(field_config_key, "") or field.default_value),
                    type="password" if field.secret else "default",
                    key=f"{llm_provider}_{field.config_suffix}_input",
                )
                _set_runtime_config(
                    "app",
                    field_config_key,
                    normalize_provider_override(
                        field_value,
                        field.default_value,
                    ),
                )

            if llm_form_panel.button(
                tr("Test LLM Connection"),
                key="test_llm_connection_button",
                use_container_width=True,
                type="secondary",
                icon=":material/network_check:",
            ):
                with config.try_runtime_config_lock() as lock_acquired:
                    if not lock_acquired:
                        llm_form_panel.warning(tr("Runtime Configuration Busy"))
                    else:
                        with llm_form_panel.spinner(tr("Testing LLM Connection")):
                            connection_ok, connection_error, connection_elapsed = (
                                llm.test_connection()
                            )

                if not lock_acquired:
                    connection_ok = None
                elif connection_ok:
                    llm_form_panel.success(
                        tr("LLM Connection Test Succeeded").format(
                            provider=llm_provider_labels[llm_provider],
                            model=st_llm_model_name or "-",
                            elapsed=f"{connection_elapsed:.2f}",
                        )
                    )
                else:
                    connection_error = format_llm_connection_error(
                        llm_provider,
                        st_llm_base_url,
                        connection_error,
                    )
                    llm_form_panel.error(
                        tr("LLM Connection Test Failed").format(error=connection_error)
                    )

        # 右侧面板 - API 密钥设置
        with right_config_panel:
            # 素材 Provider 按「搜索库存素材 / AI 生成视频 / AI 生成图片」
            # 分组，避免随着 Provider 增多后所有字段在一个长列表中混排。
            # 分组只调整展示层级，不改动已有配置键，旧用户升级后
            # 会继续读取原有 config.toml 值。
            with st.container(border=True):
                st.markdown(f"#### {tr('Stock Video APIs')}")
                st.caption(tr("Stock Video APIs Help"))

                pexels_api_key = _get_material_api_keys("pexels_api_keys")
                pixabay_api_key = _get_material_api_keys("pixabay_api_keys")
                coverr_api_key = _get_material_api_keys("coverr_api_keys")
                pexels_api_key = st.text_input(
                    tr("Pexels API Key"),
                    value=pexels_api_key,
                    type="password",
                    key="pexels_api_keys_input",
                )
                _save_material_api_keys("pexels_api_keys", pexels_api_key)

                pixabay_api_key = st.text_input(
                    tr("Pixabay API Key"),
                    value=pixabay_api_key,
                    type="password",
                    key="pixabay_api_keys_input",
                )
                _save_material_api_keys("pixabay_api_keys", pixabay_api_key)

                coverr_api_key = st.text_input(
                    tr("Coverr API Key"),
                    value=coverr_api_key,
                    type="password",
                    key="coverr_api_keys_input",
                )
                _save_material_api_keys("coverr_api_keys", coverr_api_key)

            with st.container(border=True):
                st.markdown(f"#### {tr('AI Video Generation APIs')}")
                st.caption(tr("AI Video Generation APIs Help"))

                wavespeed_api_key = _get_material_api_keys("wavespeed_api_keys")
                st.markdown("**WaveSpeed**")
                wavespeed_api_key = st.text_input(
                    tr("WaveSpeed API Key"),
                    value=wavespeed_api_key,
                    type="password",
                    key="wavespeed_api_keys_input",
                )
                _save_material_api_keys("wavespeed_api_keys", wavespeed_api_key)

                st.divider()
                seedance_api_key_value = str(
                    config.app.get("volcengine_seedance_api_key", "") or ""
                ).strip()
                shared_ark_api_key = str(
                    config.app.get("volcengine_api_key", "") or ""
                ).strip()
                environment_ark_api_key = os.getenv(
                    "VOLCENGINE_ARK_API_KEY", ""
                ).strip()
                seedance_reuses_llm_key = bool(
                    not seedance_api_key_value
                    and not environment_ark_api_key
                    and shared_ark_api_key
                )
                seedance_title = f"**{tr('Volcano Engine Seedance')}**"
                if seedance_reuses_llm_key:
                    # 只有复用大模型密钥无法从当前输入框直接看出，保留该提示
                    # 可以避免用户误以为必须重复填写；普通配置状态不再赘述。
                    seedance_title += f" :blue[{tr('Reusing LLM API Key')}]"
                st.markdown(seedance_title)
                seedance_api_key = st.text_input(
                    tr("Volcano Engine Ark API Key"),
                    value=seedance_api_key_value,
                    type="password",
                    help=tr("Volcano Engine Ark API Key Help"),
                    key="volcengine_seedance_api_key_input",
                )
                _set_runtime_config(
                    "app", "volcengine_seedance_api_key", seedance_api_key.strip()
                )
                configured_seedance_model = str(
                    config.app.get(
                        "volcengine_seedance_model",
                        volcengine_seedance.DEFAULT_MODEL_ID,
                    )
                    or volcengine_seedance.DEFAULT_MODEL_ID
                ).strip()
                seedance_model = st.text_input(
                    tr("Volcano Engine Seedance Model"),
                    # 内置默认值通过 placeholder 展示，用户自定义的
                    # 模型或接入点 ID 仍作为真实值展示和保存。
                    value=(
                        ""
                        if configured_seedance_model
                        == volcengine_seedance.DEFAULT_MODEL_ID
                        else configured_seedance_model
                    ),
                    placeholder=volcengine_seedance.DEFAULT_MODEL_ID,
                    key="volcengine_seedance_model_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_model",
                    seedance_model.strip() or volcengine_seedance.DEFAULT_MODEL_ID,
                )
                configured_seedance_base_url = str(
                    config.app.get(
                        "volcengine_seedance_base_url",
                        volcengine_seedance.DEFAULT_BASE_URL,
                    )
                    or volcengine_seedance.DEFAULT_BASE_URL
                ).strip()
                seedance_base_url = st.text_input(
                    tr("Volcano Engine Ark Base URL"),
                    value=(
                        ""
                        if configured_seedance_base_url
                        == volcengine_seedance.DEFAULT_BASE_URL
                        else configured_seedance_base_url
                    ),
                    placeholder=volcengine_seedance.DEFAULT_BASE_URL,
                    key="volcengine_seedance_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_base_url",
                    seedance_base_url.strip() or volcengine_seedance.DEFAULT_BASE_URL,
                )

            with st.container(border=True):
                st.markdown(f"#### {tr('AI Image Generation APIs')}")
                st.caption(tr("AI Image Generation APIs Help"))
                st.markdown(f"**{tr('OpenAI Compatible Text-to-Image')}**")

                openai_image_base_url = st.text_input(
                    tr("OpenAI Image Base URL"),
                    value=str(config.app.get("openai_image_base_url", "") or ""),
                    placeholder="https://api.openai.com/v1",
                    key="openai_image_base_url_input",
                )
                _set_runtime_config(
                    "app", "openai_image_base_url", openai_image_base_url.strip()
                )

                openai_image_api_key = _get_material_api_keys(
                    "openai_image_api_keys"
                )
                openai_image_api_key = st.text_input(
                    tr("OpenAI Image API Key"),
                    value=openai_image_api_key,
                    type="password",
                    help=tr("OpenAI Image API Key Help"),
                    key="openai_image_api_keys_input",
                )
                _save_material_api_keys(
                    "openai_image_api_keys", openai_image_api_key
                )

                openai_image_model = st.text_input(
                    tr("OpenAI Image Model"),
                    value=str(config.app.get("openai_image_model", "") or ""),
                    placeholder="gpt-image-2",
                    key="openai_image_model_input",
                )
                _set_runtime_config(
                    "app", "openai_image_model", openai_image_model.strip()
                )
                # 只展示参考值，不将 OpenAI 官方端点写成默认配置。
                # 兼容服务的 Base URL 和模型 ID 没有统一值；留空不会让
                # 用户在未知情时误连官方付费接口，也不会覆盖旧配置。
                st.caption(tr("OpenAI Image Configuration Example"))

                with st.expander(
                    tr("OpenAI Image Advanced Settings"), expanded=False
                ):
                    openai_image_size = st.text_input(
                        tr("OpenAI Image Size"),
                        value=str(config.app.get("openai_image_size", "") or ""),
                        placeholder="1024x1536",
                        help=tr("OpenAI Image Size Help"),
                        key="openai_image_size_input",
                    )
                    _set_runtime_config(
                        "app", "openai_image_size", openai_image_size.strip()
                    )

                    openai_image_prompt_template = st.text_input(
                        tr("OpenAI Image Prompt Template"),
                        value=str(
                            config.app.get("openai_image_prompt_template", "") or ""
                        ),
                        placeholder="cinematic photo of {term}, photorealistic",
                        help=tr("OpenAI Image Prompt Template Help"),
                        key="openai_image_prompt_template_input",
                    )
                    _set_runtime_config(
                        "app",
                        "openai_image_prompt_template",
                        openai_image_prompt_template.strip(),
                    )

    _save_runtime_config()


# -----------------------------------------------------------------------------
# 主生成表单：文案、视频、音频与字幕面板
# -----------------------------------------------------------------------------


def _create_loomloom_script_backend():
    """从当前 WebUI/config.toml 配置创建批量文案客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.LoomLoomSettings.from_mapping(app_config_snapshot)
    return loomloom.LoomLoomScriptBackend(settings)


def _create_loomloom_video_backend():
    """使用项目默认 SkillBot 和当前有效凭证创建视频客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.video_settings_from_mapping(app_config_snapshot)
    return loomloom.LoomLoomVideoBackend(settings)


def _effective_loomloom_api_token():
    """读取 WebUI 尚未落盘或 config.toml 中的胜算云 API Key。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    return loomloom.resolve_api_token(app_config_snapshot)


def _effective_script_generation_backend():
    """读取包含 WebUI 待保存修改的文案生成方式。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    backend = str(
        app_config_snapshot.get("script_generation_backend", "local") or "local"
    ).strip()
    return backend if backend in {"local", "loomloom"} else "local"


def _render_loomloom_api_token_input():
    """仅在未选择胜算云 Provider 时显示独立 LoomLoom 密钥输入。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    if str(app_config_snapshot.get("llm_provider", "") or "").lower() == "shengsuanyun":
        st.caption(tr("Shengsuan Cloud API Key Reused"))
        return loomloom.resolve_api_token(app_config_snapshot)

    configured_token = loomloom.resolve_api_token(app_config_snapshot)
    st.session_state.setdefault("loomloom_user_api_token", configured_token)
    api_token = st.text_input(
        tr("Shengsuan Cloud API Key"),
        type="password",
        key="loomloom_user_api_token",
        help=tr("Shengsuan Cloud API Key Help"),
        placeholder=tr("Shengsuan Cloud API Key Placeholder"),
    ).strip()
    _set_runtime_config("app", "loomloom_api_token", api_token)
    return _effective_loomloom_api_token()


def _loomloom_video_scene_prompts(video_terms, subject, scene_count):
    """按素材关键词生成有限数量的场景描述，供视频模型逐段生成素材。"""
    if isinstance(video_terms, str):
        terms = [
            term.strip() for term in re.split(r"[,，\n]", video_terms) if term.strip()
        ]
    elif isinstance(video_terms, list):
        terms = [
            str(term or "").strip() for term in video_terms if str(term or "").strip()
        ]
    else:
        terms = []
    fallback = str(subject or "").strip()
    if not terms and fallback:
        terms = [fallback]
    if not terms:
        return ()
    return tuple(
        (
            terms[index % len(terms)]
            if index < len(terms)
            else f"{terms[index % len(terms)]}; alternative camera angle {index + 1}"
        )
        for index in range(int(scene_count))
    )


def _loomloom_video_signature(batch, credential_fingerprint):
    """将全部计费输入和凭证摘要纳入签名，参数变化后强制重新报价。"""
    payload = {
        "inputRows": [dict(row) for row in batch.input_rows],
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _current_loomloom_video_quote_context(params):
    """根据当前页面参数构建默认 SkillBot 的视频报价批次。"""
    token = _effective_loomloom_api_token()
    scene_count = int(st.session_state.get("loomloom_video_scene_count", 1) or 1)
    prompts = _loomloom_video_scene_prompts(
        params.video_terms,
        params.video_subject or params.video_script,
        scene_count,
    )
    if not token or not prompts:
        return None, ""
    try:
        batch = _create_loomloom_video_backend().prepare_video_batch(
            subject=params.video_subject or params.video_script,
            scene_prompts=prompts,
            aspect_ratio=str(
                params.video_aspect.value
                if isinstance(params.video_aspect, VideoAspect)
                else params.video_aspect
            ),
        )
    except (loomloom.LoomLoomError, ValueError):
        return None, ""
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return batch, _loomloom_video_signature(batch, fingerprint)


def _render_loomloom_video_settings(params):
    """渲染默认视频 SkillBot 的报价、报价失效和付费确认流程。"""
    st.caption(tr("Shengsuan Cloud AI Video Help"))
    if _effective_script_generation_backend() != "loomloom":
        _render_loomloom_api_token_input()
    elif (
        str(
            config.snapshot_config_with_pending(config.app).get("llm_provider", "")
            or ""
        ).lower()
        == "shengsuanyun"
    ):
        st.caption(tr("Shengsuan Cloud API Key Reused"))

    token = _effective_loomloom_api_token()

    scene_count = st.number_input(
        tr("AI Video Scene Count"),
        min_value=1,
        max_value=loomloom.MAX_VIDEO_SCENES,
        step=1,
        key="loomloom_video_scene_count",
    )
    _set_runtime_config("ui", "loomloom_video_scene_count", int(scene_count))
    batch, input_signature = _current_loomloom_video_quote_context(params)
    if not token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_videos",
        use_container_width=True,
        type="secondary",
        icon=":material/request_quote:",
        disabled=not token or batch is None,
    ):
        try:
            quote_result = _create_loomloom_video_backend().quote(batch)
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(f"failed to quote LoomLoom videos: error={exc}")
            st.error(str(exc))
        else:
            st.session_state["loomloom_video_batch"] = batch
            st.session_state["loomloom_video_quote"] = quote_result
            st.session_state["loomloom_video_input_signature"] = input_signature
            st.session_state["loomloom_video_client_request_id"] = (
                f"mpt-video-{uuid4()}"
            )
            st.session_state["loomloom_video_confirm_charge"] = False
            logger.info(
                "LoomLoom video quote ready: "
                f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
            )

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    if quote_result is not None and quoted_batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "AI Video Quote Summary Singular"
                if quote_result.task_count == 1
                else "AI Video Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_video_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        st.checkbox(
            tr("Confirm AI Video Charge"),
            key="loomloom_video_confirm_charge",
            help=tr("Confirm AI Video Charge Help"),
            disabled=not quote_is_current,
        )


def _loomloom_script_signature(
    *,
    subject,
    language,
    candidate_count,
    duration_seconds,
    style,
    credential_fingerprint,
):
    payload = {
        "subject": str(subject or "").strip(),
        "language": str(language or "auto").strip() or "auto",
        "candidateCount": int(candidate_count),
        "durationSeconds": int(duration_seconds),
        "style": str(style or "").strip(),
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render_local_script_generation(params):
    """保留 MoneyPrinterTurbo 原有的本地 LLM 脚本生成路径。"""
    if not st.button(
        tr("Generate Video Script and Keywords"),
        key="auto_generate_script",
        use_container_width=True,
        type="secondary",
        icon=":material/auto_awesome:",
    ):
        return

    if not params.video_subject:
        st.toast(tr("Please Enter the Video Subject First"))
        st.warning(tr("Please Enter the Video Subject First"))
        return

    with st.spinner(tr("Generating Video Script and Keywords")):

        def generate_script_and_terms(app_config_snapshot):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                app_config=app_config_snapshot,
            )
            terms = llm.generate_terms(
                params.video_subject,
                script,
                amount=8 if params.match_materials_to_script else 5,
                match_script_order=params.match_materials_to_script,
                app_config=app_config_snapshot,
            )
            return script, terms

        script, terms = _run_llm_read_operation(
            "generate_script_and_terms",
            generate_script_and_terms,
        )
        if "Error: " in script:
            st.error(tr(script))
        elif "Error: " in terms:
            st.error(tr(terms))
        else:
            st.session_state["video_script"] = script
            st.session_state["video_terms"] = ", ".join(terms)


def _render_loomloom_candidates():
    candidates = tuple(st.session_state.get("loomloom_script_candidates") or ())
    errors = tuple(st.session_state.get("loomloom_candidate_errors") or ())
    if errors:
        st.warning(
            tr("LoomLoom Candidate Errors").format(
                count=len(errors),
                details="; ".join(
                    f"#{error.row_index + 1}: {error.message}" for error in errors
                ),
            )
        )
    if not candidates:
        return

    selected_index = st.radio(
        tr("Choose Script Candidate"),
        options=list(range(len(candidates))),
        key="loomloom_selected_candidate",
        format_func=lambda index: (
            f"#{candidates[index].row_index + 1} {candidates[index].script[:80]}"
        ),
    )
    selected = candidates[selected_index]
    st.code(selected.script, language=None, wrap_lines=True)
    st.caption(", ".join(selected.video_terms))
    if st.button(
        tr("Use Selected Candidate"),
        key="loomloom_apply_candidate",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["video_script"] = selected.script
        st.session_state["video_terms"] = ", ".join(selected.video_terms)
        st.toast(tr("LoomLoom Candidate Applied"))


def _handle_loomloom_poll_error(run_id, exc):
    """对脚本任务轮询错误做有限退避，确定性错误立即停止轮询。"""
    logger.warning(f"failed to poll LoomLoom run: run_id={run_id}, error={exc}")
    failure_count = int(st.session_state.get("loomloom_poll_failure_count", 0) or 0) + 1
    retryable = isinstance(exc, loomloom.LoomLoomAPIError) and exc.retryable
    if not retryable or failure_count >= LOOMLOOM_MAX_POLL_FAILURES:
        st.session_state["loomloom_run_error"] = str(exc)
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        # 查询失败不等于远端付费任务失败。保留 run_id 并暂停自动轮询，让用户
        # 可以继续查询同一个任务；如果直接丢弃 ID 后重新提交，可能重复付费。
        st.session_state["loomloom_poll_paused"] = True
        st.rerun(scope="app")
        return

    retry_delay = min(2**failure_count, 30)
    st.session_state["loomloom_poll_failure_count"] = failure_count
    st.session_state["loomloom_poll_retry_after"] = time.monotonic() + retry_delay
    st.warning(
        tr("LoomLoom Poll Retry Warning").format(
            attempt=failure_count,
            max_attempts=LOOMLOOM_MAX_POLL_FAILURES,
        )
    )


@st.fragment(run_every="2s")
def _render_loomloom_run_progress():
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if not run_id or st.session_state.get("loomloom_poll_paused", False):
        return
    retry_after = float(st.session_state.get("loomloom_poll_retry_after", 0.0) or 0.0)
    retry_wait_seconds = max(0, int(math.ceil(retry_after - time.monotonic())))
    if retry_wait_seconds > 0:
        st.info(
            tr("LoomLoom Poll Retry Pending").format(
                seconds=retry_wait_seconds,
            )
        )
        return
    try:
        backend = _create_loomloom_script_backend()
        run = backend.get_run(run_id)
    except loomloom.LoomLoomError as exc:
        _handle_loomloom_poll_error(run_id, exc)
        return

    st.session_state["loomloom_run_status"] = run.status
    if run.status == "completed":
        try:
            result = backend.get_script_results(run_id)
        except loomloom.LoomLoomError as exc:
            _handle_loomloom_poll_error(run_id, exc)
            return
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        st.session_state["loomloom_poll_paused"] = False
        st.session_state["loomloom_script_candidates"] = result.candidates
        st.session_state["loomloom_candidate_errors"] = result.errors
        st.session_state["loomloom_selected_candidate"] = 0
        st.session_state["loomloom_run_id"] = ""
        st.rerun(scope="app")
        return
    if run.status in {"failed", "cancelled", "canceled"}:
        st.session_state["loomloom_run_error"] = run.first_error_message or run.status
        st.session_state["loomloom_run_id"] = ""
        st.session_state["loomloom_poll_paused"] = False
        st.rerun(scope="app")
        return

    st.session_state["loomloom_poll_failure_count"] = 0
    st.session_state["loomloom_poll_retry_after"] = 0.0
    st.info(
        tr("LoomLoom Run Progress").format(
            completed=run.completed_tasks,
            total=run.total_tasks,
        )
    )


def _render_loomloom_script_generation(params):
    st.caption(tr("LoomLoom Batch Script Generation Help"))
    effective_token = _render_loomloom_api_token_input()
    if not effective_token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    candidate_col, duration_col = st.columns(2)
    candidate_count = candidate_col.number_input(
        tr("Script Candidate Count"),
        min_value=1,
        max_value=loomloom.MAX_SCRIPT_CANDIDATES,
        step=1,
        key="loomloom_candidate_count",
    )
    duration_seconds = duration_col.number_input(
        tr("Target Script Duration Seconds"),
        min_value=10,
        max_value=600,
        step=10,
        key="loomloom_script_duration_seconds",
    )
    _set_runtime_config("ui", "loomloom_candidate_count", int(candidate_count))
    _set_runtime_config(
        "ui", "loomloom_script_duration_seconds", int(duration_seconds)
    )
    input_signature = _loomloom_script_signature(
        subject=params.video_subject,
        language=params.video_language,
        candidate_count=candidate_count,
        duration_seconds=duration_seconds,
        style=params.video_script_prompt,
        credential_fingerprint=(
            hashlib.sha256(effective_token.encode("utf-8")).hexdigest()
            if effective_token
            else ""
        ),
    )

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_scripts",
        use_container_width=True,
        type="secondary",
        icon=":material/request_quote:",
        disabled=not effective_token or bool(st.session_state.get("loomloom_run_id")),
    ):
        if not params.video_subject:
            st.toast(tr("Please Enter the Video Subject First"))
            st.warning(tr("Please Enter the Video Subject First"))
        else:
            try:
                backend = _create_loomloom_script_backend()
                batch = backend.prepare_script_batch(
                    subject=params.video_subject,
                    candidate_count=int(candidate_count),
                    language=params.video_language,
                    duration_seconds=int(duration_seconds),
                    style=params.video_script_prompt,
                )
                quote_result = backend.quote(batch)
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to quote LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_script_batch"] = batch
                st.session_state["loomloom_script_quote"] = quote_result
                st.session_state["loomloom_script_input_signature"] = input_signature
                st.session_state["loomloom_client_request_id"] = f"mpt-{uuid4()}"
                st.session_state["loomloom_run_id"] = ""
                st.session_state["loomloom_run_status"] = "quoted"
                st.session_state["loomloom_run_error"] = ""
                st.session_state["loomloom_poll_failure_count"] = 0
                st.session_state["loomloom_poll_retry_after"] = 0.0
                st.session_state["loomloom_poll_paused"] = False
                st.session_state["loomloom_script_candidates"] = ()
                st.session_state["loomloom_candidate_errors"] = ()
                st.session_state["loomloom_confirm_charge"] = False
                logger.info(
                    "LoomLoom script quote ready: "
                    f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                    f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
                )

    quote_result = st.session_state.get("loomloom_script_quote")
    batch = st.session_state.get("loomloom_script_batch")
    if quote_result is not None and batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "LoomLoom Quote Summary Singular"
                if quote_result.task_count == 1
                else "LoomLoom Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_script_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        confirm_charge = st.checkbox(
            tr("Confirm LoomLoom Charge"),
            key="loomloom_confirm_charge",
            disabled=not quote_is_current,
        )
        run_in_progress = bool(st.session_state.get("loomloom_run_id"))
        if st.button(
            tr("Run LoomLoom Batch"),
            key="loomloom_execute_scripts",
            use_container_width=True,
            type="primary",
            disabled=(not quote_is_current or not confirm_charge or run_in_progress),
        ):
            try:
                execution = _create_loomloom_script_backend().execute(
                    batch,
                    client_request_id=st.session_state["loomloom_client_request_id"],
                    listing_version_id=quote_result.listing_version_id,
                    confirm=True,
                )
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to execute LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_run_id"] = execution.run_id
                st.session_state["loomloom_run_status"] = "running"
                st.session_state["loomloom_poll_paused"] = False
                # 一次报价只允许启动一次付费批次。后台状态只依赖 run_id，提交
                # 后即可丢弃报价与幂等请求 ID；失败后用户需要重新报价再重试。
                st.session_state["loomloom_script_batch"] = None
                st.session_state["loomloom_script_quote"] = None
                st.session_state["loomloom_script_input_signature"] = ""
                st.session_state["loomloom_client_request_id"] = ""
                logger.info(
                    f"LoomLoom script run submitted: run_id={execution.run_id}, "
                    f"tasks={len(batch.input_rows)}"
                )
                st.toast(tr("LoomLoom Run Submitted"))

    run_error = str(st.session_state.get("loomloom_run_error", "") or "").strip()
    if run_error:
        st.error(tr("LoomLoom Run Failed").format(error=run_error))
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if run_id and st.session_state.get("loomloom_poll_paused", False):
        retry_col, stop_col = st.columns(2)
        if retry_col.button(
            tr("Resume LoomLoom Status Check"),
            key="loomloom_resume_status_check",
            use_container_width=True,
            type="secondary",
        ):
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_failure_count"] = 0
            st.session_state["loomloom_poll_retry_after"] = 0.0
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
        if stop_col.button(
            tr("Stop Tracking LoomLoom Run"),
            key="loomloom_stop_tracking_run",
            use_container_width=True,
            type="secondary",
            help=tr("Stop Tracking LoomLoom Run Help"),
        ):
            # 这里只停止本地状态查询，不声称取消远端执行。用户确认放弃跟踪后
            # 才清理 run_id，下一次付费运行仍需重新报价和确认。
            st.session_state["loomloom_run_id"] = ""
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
    # 只有真实运行中的批次才启动两秒轮询，报价阶段和结果展示阶段不创建
    # 定时 fragment，避免用户停留在页面时产生无意义的网络请求和 rerun。
    if run_id and not st.session_state.get("loomloom_poll_paused", False):
        _render_loomloom_run_progress()
    _render_loomloom_candidates()


def _render_script_settings(panel, params):
    """渲染文案设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Video Script Settings"))
            # 标签行需要容纳“配置大模型”入口，因此无法继续使用 text_area
            # 内置标签。把标签和输入框收进同一个字段容器后，可覆盖内部间距，
            # 同时让该字段与页面上的其它表单控件保持一致的外部节奏。
            with st.container(key="video_subject_field"):
                with st.container(
                    key="video_subject_label_row",
                    horizontal=True,
                    vertical_alignment="center",
                    gap="small",
                ):
                    st.markdown(
                        tr("Video Subject"),
                        help=tr("Video Subject Help"),
                        width="content",
                    )
                    st.button(
                        tr("Configure LLM"),
                        key="open_llm_settings_from_subject",
                        type="tertiary",
                        on_click=_open_settings_dialog,
                        args=("llm",),
                    )
                params.video_subject = st.text_area(
                    tr("Video Subject"),
                    placeholder=tr("Video Subject Placeholder"),
                    height=96,
                    key="video_subject",
                    label_visibility="collapsed",
                ).strip()

            video_languages = [
                (tr("Auto Detect"), ""),
            ]
            for code in support_locales:
                video_languages.append((code, code))

            selected_language_code = stable_selectbox(
                tr("Script Language"),
                options=[value for _, value in video_languages],
                default_value=_saved_ui_choice(
                    "video_language",
                    [value for _, value in video_languages],
                    "",
                ),
                key="script_language_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_languages
                )[value],
            )
            params.video_language = selected_language_code
            _set_runtime_config("ui", "video_language", params.video_language)

            # 使用带 key 的局部容器限定折叠入口样式，保持 expander 的原生交互，
            # 同时避免样式误伤页面顶部的“基础设置”等其他折叠区域。
            with st.container(key="advanced_settings_script"):
                with st.expander(tr("Advanced Script Settings"), expanded=False):
                    script_backend_options = ["local", "loomloom"]
                    script_backend_labels = {
                        "local": tr("Local LLM Script Generation"),
                        "loomloom": tr("Shengsuan Cloud Batch Script Generation"),
                    }
                    script_generation_backend = stable_selectbox(
                        tr("Script Generation Method"),
                        options=script_backend_options,
                        default_value=_effective_script_generation_backend(),
                        key="script_generation_backend_select",
                        format_func=lambda value: script_backend_labels[value],
                        help=tr("Script Generation Method Help"),
                    )
                    _set_runtime_config(
                        "app", "script_generation_backend", script_generation_backend
                    )

                    params.paragraph_number = st.slider(
                        tr("Script Paragraph Number"),
                        min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                        max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                        key="paragraph_number_input",
                    )
                    _set_runtime_config(
                        "ui", "paragraph_number", params.paragraph_number
                    )
                    params.video_script_prompt = st.text_area(
                        tr("Custom Script Requirements"),
                        height=100,
                        max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                        placeholder=tr("Custom Script Requirements Placeholder"),
                        key="video_script_prompt",
                    ).strip()
                    _set_runtime_config(
                        "ui", "video_script_prompt", params.video_script_prompt
                    )

                    system_prompt = st.text_area(
                        tr("Custom System Prompt"),
                        height=240,
                        max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                        key="custom_system_prompt",
                    ).strip()
                    # 默认内容由服务层统一维护。界面虽然直接展示默认提示词，但只有
                    # 用户实际修改后才随任务传递，避免历史任务固化旧版本默认规则。
                    params.custom_system_prompt = (
                        ""
                        if system_prompt == llm.DEFAULT_SCRIPT_SYSTEM_PROMPT.strip()
                        else system_prompt
                    )
                    _set_runtime_config(
                        "ui", "custom_system_prompt", params.custom_system_prompt
                    )

                    restore_prompt_col, preview_prompt_col = st.columns(2)
                    if restore_prompt_col.button(
                        tr("Restore Default System Prompt"),
                        key="restore_default_system_prompt",
                        icon=":material/restart_alt:",
                        on_click=reset_script_system_prompt,
                        use_container_width=True,
                    ):
                        st.toast(tr("Default System Prompt Restored"))
                    if preview_prompt_col.button(
                        tr("Preview Final Prompt"),
                        key="preview_final_script_prompt",
                        icon=":material/preview:",
                        use_container_width=True,
                    ):
                        render_script_prompt_preview(
                            llm.build_script_prompt(
                                video_subject=params.video_subject,
                                language=params.video_language,
                                paragraph_number=params.paragraph_number,
                                video_script_prompt=params.video_script_prompt,
                                custom_system_prompt=params.custom_system_prompt,
                            )
                        )

            if _effective_script_generation_backend() == "loomloom":
                _render_loomloom_script_generation(params)
            else:
                _render_local_script_generation(params)
            params.video_script = st.text_area(
                tr("Video Script"),
                help=tr("Video Script Help"),
                height=180,
                key="video_script",
            )
            using_loomloom_scripts = (
                _effective_script_generation_backend() == "loomloom"
            )
            if using_loomloom_scripts:
                st.caption(tr("LoomLoom Video Terms Reuse Help"))
            elif st.button(
                tr("Generate Video Keywords"),
                key="auto_generate_terms",
                use_container_width=True,
                type="secondary",
                icon=":material/auto_awesome:",
            ):
                if not params.video_script:
                    # 视频关键词需要基于文案提取，文案为空时提前提示并跳过模型调用。
                    st.toast(tr("Please Enter the Video Subject"))
                    st.warning(tr("Please Enter the Video Subject"))
                else:
                    with st.spinner(tr("Generating Video Keywords")):
                        terms = _run_llm_read_operation(
                            "generate_terms",
                            lambda app_config_snapshot: llm.generate_terms(
                                params.video_subject,
                                params.video_script,
                                amount=8 if params.match_materials_to_script else 5,
                                match_script_order=params.match_materials_to_script,
                                app_config=app_config_snapshot,
                            ),
                        )
                        if "Error: " in terms:
                            st.error(tr(terms))
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"),
                help=tr("Video Keywords Help"),
                key="video_terms",
            )


def _render_video_settings(panel, params):
    """渲染视频设置并返回本次选择的本地素材。"""
    uploaded_files = []
    with panel:
        with st.container(border=True):
            st.write(tr("Video Settings"))
            video_concat_modes = [
                (tr("Sequential"), "sequential"),
                (tr("Random"), "random"),
            ]
            video_source_labels = {
                "pexels": tr("Pexels"),
                "pixabay": tr("Pixabay"),
                "coverr": tr("Coverr"),
                "wavespeed": tr("WaveSpeed AI Video"),
                "volcengine_seedance": tr("Volcano Engine Seedance"),
                "loomloom": tr("Shengsuan Cloud AI Video"),
                "openai_image": tr("OpenAI Compatible Text-to-Image"),
                "local": tr("Local file"),
            }
            saved_video_source_name = str(
                config.app.get("video_source", "pexels") or "pexels"
            )
            params.video_source = grouped_selectbox(
                tr("Video Source"),
                groups=(
                    (tr("Stock Video"), VIDEO_SOURCE_GROUPS["stock_video"]),
                    (tr("AI Video"), VIDEO_SOURCE_GROUPS["ai_video"]),
                    (tr("AI Image"), VIDEO_SOURCE_GROUPS["ai_image"]),
                    (tr("Local Material"), VIDEO_SOURCE_GROUPS["local"]),
                ),
                default_value=saved_video_source_name,
                key="video_source_select",
                format_func=video_source_labels.get,
                settings_label=tr("Configure Material Sources"),
                on_settings=_open_material_settings_dialog,
            )
            _set_runtime_config("app", "video_source", params.video_source)

            if params.video_source == "wavespeed":
                st.caption(tr("WaveSpeed AI Video Help"))
            if params.video_source == "volcengine_seedance":
                st.caption(tr("Volcano Engine Seedance Help"))
            if params.video_source == "local":
                # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
                local_file_types = sorted(
                    extension.removeprefix(".")
                    for extension in LOCAL_MATERIAL_EXTENSIONS
                )
                uploaded_files = st.file_uploader(
                    tr("Upload Local Files"),
                    type=local_file_types
                    + [file_type.upper() for file_type in local_file_types],
                    accept_multiple_files=True,
                    key="local_video_materials_uploader",
                )

            # 文案顺序匹配会从关键词生成到最终合成全程保持叙事顺序，因此开启时
            # 顺序拼接是唯一符合实际执行逻辑的选项。同步控件值可避免界面仍显示
            # “随机拼接”，同时保留用户原选择，关闭后自动恢复。
            sync_script_order_concat_mode()
            selected_concat_mode = stable_selectbox(
                tr("Video Concat Mode"),
                options=[value for _, value in video_concat_modes],
                default_value=_saved_ui_choice(
                    "video_concat_mode",
                    [value for _, value in video_concat_modes],
                    VideoConcatMode.random.value,
                ),
                key="video_concat_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_concat_modes
                )[value],
                disabled=bool(st.session_state.get("match_materials_to_script", False)),
            )
            params.video_concat_mode = VideoConcatMode(selected_concat_mode)

            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
                on_change=sync_script_order_concat_mode,
            )
            _set_runtime_config(
                "app",
                "match_materials_to_script",
                params.match_materials_to_script,
            )
            # 顺序匹配开启时，sequential 是派生出的强制值，不应覆盖用户在关闭
            # 该功能时选择的拼接偏好；关闭后仍能恢复此前的 random/sequential。
            if not params.match_materials_to_script:
                _set_runtime_config(
                    "ui", "video_concat_mode", params.video_concat_mode.value
                )

            # 视频转场模式
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
                (tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                (tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
            ]
            selected_transition_mode = stable_selectbox(
                tr("Video Transition Mode"),
                options=[value for _, value in video_transition_modes],
                default_value=_saved_ui_choice(
                    "video_transition_mode",
                    [value for _, value in video_transition_modes],
                    VideoTransitionMode.none.value,
                ),
                key="video_transition_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_transition_modes
                )[value],
            )
            params.video_transition_mode = VideoTransitionMode(selected_transition_mode)
            _set_runtime_config(
                "ui",
                "video_transition_mode",
                params.video_transition_mode.value,
            )

            video_aspect_ratios = [
                (tr("Portrait"), VideoAspect.portrait.value),
                (tr("Landscape"), VideoAspect.landscape.value),
            ]
            # Coverr 库 99% 是 16:9 横屏,默认竖屏会让画面被大量黑边包围。
            # 用 source-specific widget key 让每个 source 各自记忆 aspect 选择:
            #   - 首次切到 coverr → 默认 Landscape(index=1)
            #   - 其他 source 沿用 Portrait(index=0)
            #   - 用户在某 source 下手动改过 aspect,session_state 会记住,
            #     下次回到同一 source 时尊重用户选择,不会再被强制覆盖。
            default_aspect_index = 1 if params.video_source == "coverr" else 0
            video_aspect_values = [value for _, value in video_aspect_ratios]
            video_aspect_config_key = f"video_aspect_{params.video_source}"
            selected_aspect_ratio = stable_selectbox(
                tr("Video Ratio"),
                options=video_aspect_values,
                default_value=_saved_ui_choice(
                    video_aspect_config_key,
                    video_aspect_values,
                    video_aspect_ratios[default_aspect_index][1],
                ),
                key=f"video_aspect_for_{params.video_source}",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_aspect_ratios
                )[value],
            )
            params.video_aspect = VideoAspect(selected_aspect_ratio)
            _set_runtime_config(
                "ui", video_aspect_config_key, params.video_aspect.value
            )

            video_fit_modes = [
                (tr("Fill and Crop"), VideoFitMode.cover.value),
                (tr("Fit with Black Bars"), VideoFitMode.contain.value),
            ]
            selected_fit_mode = stable_selectbox(
                tr("Video Fit Mode"),
                options=[value for _, value in video_fit_modes],
                default_value=_saved_ui_choice(
                    "video_fit_mode",
                    [value for _, value in video_fit_modes],
                    VideoFitMode.cover.value,
                ),
                key="video_fit_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_fit_modes
                )[value],
                help=tr("Video Fit Mode Help"),
            )
            params.video_fit_mode = VideoFitMode(selected_fit_mode)
            _set_runtime_config(
                "ui", "video_fit_mode", params.video_fit_mode.value
            )

            video_clip_durations = [2, 3, 4, 5, 6, 7, 8, 9, 10]
            params.video_clip_duration = stable_selectbox(
                tr("Clip Duration"),
                options=video_clip_durations,
                default_value=_saved_ui_choice(
                    "video_clip_duration", video_clip_durations, 3
                ),
                key="video_clip_duration_select",
                help=tr("Clip Duration Help"),
            )
            _set_runtime_config(
                "ui", "video_clip_duration", params.video_clip_duration
            )
            clip_speed_key = localized_widget_key("video_clip_speed_slider")
            # session_state 可能来自旧任务、API 参数或旧版页面状态。控件创建前
            # 统一归一化，既保留合法选择，也确保 slider 始终收到 0.5～2.0
            # 范围内的有限浮点数。
            st.session_state[clip_speed_key] = utils.normalize_clip_speed(
                st.session_state.get(
                    clip_speed_key,
                    _saved_ui_number("video_clip_speed", 1.0, 0.5, 2.0),
                )
            )
            params.video_clip_speed = st.slider(
                tr("Clip Speed"),
                min_value=0.5,
                max_value=2.0,
                step=0.05,
                format="%.2fx",
                key=clip_speed_key,
                help=tr("Clip Speed Help"),
            )
            _set_runtime_config("ui", "video_clip_speed", params.video_clip_speed)
            video_count_options = [1, 2, 3, 4, 5]
            params.video_count = stable_selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=video_count_options,
                default_value=_saved_ui_choice(
                    "video_count", video_count_options, 1
                ),
                key="video_count_select",
            )
            _set_runtime_config("ui", "video_count", params.video_count)

            video_codec_options = [
                (tr("Default Video Encoder"), DEFAULT_VIDEO_CODEC_OPTION),
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get(
                "video_codec", DEFAULT_VIDEO_CODEC_OPTION
            )
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                # 旧版本或手工配置可能留下无效值。UI 回到“默认”而不是替用户
                # 固定某个编码器，后端仍会按稳定策略解析为 libx264。
                saved_video_codec = DEFAULT_VIDEO_CODEC_OPTION
            selected_video_codec = stable_selectbox(
                tr("Video Encoder"),
                options=saved_video_codec_values,
                default_value=saved_video_codec,
                key="video_encoder_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_codec_options
                )[value],
                help=tr("Video Encoder Help"),
            )
            if selected_video_codec == DEFAULT_VIDEO_CODEC_OPTION:
                # 默认模式不持久化具体编码器，让配置表达“跟随项目默认值”。
                _delete_runtime_config("app", "video_codec")
            else:
                _set_runtime_config("app", "video_codec", selected_video_codec)

            if params.video_source == "loomloom":
                _render_loomloom_video_settings(params)

            if params.video_source == "wavespeed":
                _render_wavespeed_video_settings(params)
            if params.video_source == "volcengine_seedance":
                _render_seedance_video_settings(params)
    return uploaded_files


def _render_wavespeed_video_settings(params):
    """
    渲染 WaveSpeed 生成数量估算与计费确认。

    生成按条计费，提交前必须让用户看到大致会生成多少段。估算完全在本地
    完成：用配音时长估算区间除以片段时长得到需要覆盖的片段数。素材流程
    本身按需逐段生成、凑够所需时长即停，因此实际生成数以运行时为准，
    估算只用于量级提示，不参与任务执行。
    """
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""),
        params.voice_rate,
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("WaveSpeed Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("WaveSpeed Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm WaveSpeed Charge"),
        key="wavespeed_confirm_charge",
        help=tr("Confirm WaveSpeed Charge Help"),
    )


def _render_seedance_video_settings(params):
    """展示预计付费任务数量，并要求用户明确确认方舟生成费用。"""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Volcano Engine Seedance Billing Notice").format(
                min=min_clips, max=max_clips
            )
        )
    else:
        st.warning(tr("Volcano Engine Seedance Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Volcano Engine Seedance Charge"),
        key="volcengine_seedance_confirm_charge",
        help=tr("Confirm Volcano Engine Seedance Charge Help"),
    )


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    在本地估算完整配音时长，返回保守的上下界秒数。

    该估算只用于帮助用户在调用付费 TTS 前判断文案量级，不参与任务执行。
    中文、日文和韩文按字符速度估算，其它使用空格分词的语言按单词速度估算，
    再计入常见标点停顿。不同 Provider、音色和语气会造成实际偏差，因此界面
    必须展示区间而不是伪精确的单一结果。
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4.2 字/秒和 2.6 词/秒接近日常解说语速；标点按 0.12 秒加入轻微停顿。
    # voice_rate 只作为估算修正项。部分生成式 TTS 不严格执行倍率，所以最终
    # 仍保留 ±15% 区间，避免让用户误以为该值等同于服务端真实结果。
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    """返回适合当前音色的短试听文案，不使用用户的完整视频文案。"""
    # ElevenLabs 音色缺少明确语言字段时，根据展示名称中的越南语字符选择
    # 试听文案，避免用明显不匹配的语言判断音色效果。
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
) -> str:
    """生成试听缓存指纹，任一配音参数变化后自动让旧试听结果失效。"""
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """
    生成只用于缓存失效判断的凭证摘要。

    摘要不会写入配置、日志或任务文件。用户修改 API Key 后摘要会变化，从而
    强制重新调用当前配音服务，避免旧试听缓存让无效的新凭证看起来可用。
    """
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    """
    返回会影响试听结果的非敏感 Provider 配置。

    API Key 只以单向摘要参与缓存指纹，原始凭证不会进入缓存或日志。模型、
    服务地址、区域或凭证发生变化时都必须重新生成试听，否则界面可能继续播放
    旧 Provider 配置下的音频，让用户误判当前设置已经生效。
    """
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(config.azure.get("speech_key", "")),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(config.siliconflow.get("api_key", ""))
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(config.app.get("gemini_api_key", ""))
        }
    if tts_server == "mimo-tts":
        return {"credential": _credential_signature(config.app.get("mimo_api_key", ""))}
    if tts_server == "minimax-tts":
        return {
            "base_url": voice.get_minimax_tts_endpoint(),
            "model_id": config.minimax_tts.get("model_id", ""),
            "voice_id": config.minimax_tts.get("voice_id", ""),
            "credential": _credential_signature(voice.get_minimax_tts_api_key()),
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(config.elevenlabs.get("api_key", "")),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(config.chatterbox.get("api_key", "")),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
) -> dict | None:
    """生成一次试听并转为内存缓存，临时文件不会跨会话长期保留。"""
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
    logger.info(
        f"generating {preview_type} voice preview: "
        f"voice={voice_name}, rate={voice_rate}, volume={voice_volume}, "
        f"text_length={len(content)}"
    )
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            sub_maker = voice.tts(
                text=content,
                voice_name=voice_name,
                voice_rate=voice_rate,
                voice_file=audio_file,
                voice_volume=voice_volume,
            )
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(f"{preview_type} voice preview did not produce an audio file")
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None

        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
        }
    finally:
        # 浏览器播放器使用内存字节，文件读取完即可清理，避免频繁试听积累临时文件。
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # 清理失败不应覆盖真正的 TTS 响应或异常，但需要保留路径和系统错误，
            # 方便排查权限、只读文件系统等环境问题。
            logger.warning(
                f"failed to delete voice preview file {audio_file}: {str(exc)}"
            )


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    """渲染低成本短试听、完整文案时长估算和按需完整配音预览。"""
    if not friendly_names:
        return

    script_content = str(params.video_script or "").strip()
    estimated_range = _estimate_voiceover_duration_range(
        script_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(selected_tts_server)
    preview_columns = st.columns(2)
    short_preview_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        icon=":material/graphic_eq:",
        use_container_width=True,
    )
    full_preview_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        icon=":material/article:",
        help=tr("Full Voiceover Preview Cost Hint"),
        use_container_width=True,
        disabled=not bool(script_content),
    )

    preview_type = ""
    preview_content = ""
    if short_preview_requested:
        preview_type = "sample"
        preview_content = sample_content
    elif full_preview_requested:
        preview_type = "full"
        preview_content = script_content

    sample_fingerprint = _voice_preview_fingerprint(
        preview_type="sample",
        content=sample_content,
        tts_server=selected_tts_server,
        voice_name=voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=provider_signature,
    )
    full_fingerprint = (
        _voice_preview_fingerprint(
            preview_type="full",
            content=script_content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
        )
        if script_content
        else ""
    )

    if preview_type:
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached_preview = st.session_state.get("voice_preview_audio")
        if (
            not cached_preview
            or cached_preview.get("fingerprint") != requested_fingerprint
        ):
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                    )
            except Exception as exc:
                logger.exception(f"failed to generate {preview_type} voice preview")
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    preview_result["fingerprint"] = requested_fingerprint
                    st.session_state["voice_preview_audio"] = preview_result
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached_preview = st.session_state.get("voice_preview_audio")
    valid_fingerprints = {sample_fingerprint, full_fingerprint}
    if (
        cached_preview
        and cached_preview.get("fingerprint") in valid_fingerprints
        and cached_preview.get("audio_bytes")
    ):
        # 只在用户本次明确点击“试听音色”时自动播放。Streamlit 的其它控件
        # 也会触发页面 rerun；如果对缓存音频永久开启 autoplay，修改任意设置
        # 都可能让旧试听从头播放。完整试听继续保留手动播放，避免较长音频在
        # 生成完成后意外打断用户。
        should_autoplay = bool(
            short_preview_requested
            and cached_preview.get("preview_type") == "sample"
            and cached_preview.get("fingerprint") == sample_fingerprint
        )
        st.audio(
            cached_preview["audio_bytes"],
            format=cached_preview.get("mime_type", "audio/mp3"),
            autoplay=should_autoplay,
        )
        if cached_preview.get("preview_type") == "full":
            duration = cached_preview.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(duration=f"{duration:.1f}")
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _get_reusable_full_voice_preview(params, voice_mode: str) -> dict | None:
    """
    返回与当前生成参数完全匹配的完整试听缓存。

    只复用完整文案试听，短音色样例永远不能进入正式任务。指纹统一覆盖文案、
    Provider、音色、语速、音量和非敏感配置摘要；任何参数变化都会自然回退到
    正常 TTS 流程。字幕时间轴和有效时长同样是必需条件，避免只复用音频后让
    Edge 字幕链路失去 SubMaker。
    """
    if voice_mode != VOICE_MODE_TTS:
        return None

    script_content = str(params.video_script or "").strip()
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    if (
        not script_content
        or not params.voice_name
        # 正式视频会在 MoviePy 合成阶段统一应用配音音量；部分 Provider 又会
        # 在 TTS 阶段直接写入音量增益。非默认音量下复用试听可能造成二次增益，
        # 因此先保守回退原流程，避免为少量场景引入 Provider 特判。
        or not math.isclose(float(params.voice_volume), 1.0)
    ):
        return None

    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script_content,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(selected_tts_server),
    )
    cached_preview = st.session_state.get("voice_preview_audio")
    if (
        not cached_preview
        or cached_preview.get("fingerprint") != expected_fingerprint
        or cached_preview.get("preview_type") != "full"
        or not cached_preview.get("audio_bytes")
        or cached_preview.get("sub_maker") is None
    ):
        return None

    duration = cached_preview.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None

    return {
        "audio_bytes": bytes(cached_preview["audio_bytes"]),
        "duration": float(duration),
        "sub_maker": cached_preview["sub_maker"],
        "script": script_content,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }


def _sync_minimax_tts_api_key_input():
    """
    同步 MiniMax TTS 密码控件，并返回当前有效 Key。

    TTS 专用 Key 为空时允许复用 MiniMax LLM Key。共享 Key 只用于当前控件和
    请求，不自动复制到 [minimax_tts]，避免同一凭证在配置文件中重复维护。
    """
    widget_key = "minimax_tts_api_key_input"
    configured_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    shared_key = str(
        config.app.get("minimax_api_key", "") or os.getenv("MINIMAX_API_KEY", "") or ""
    ).strip()
    effective_key = configured_key or shared_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 浏览器重连可能重放空密码状态。恢复已配置凭证，防止空值覆盖配置，
        # 同时确保当前 rerun 的试听请求可以直接使用有效 Key。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored MiniMax TTS API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = effective_key
        entered_key = effective_key

    if entered_key and entered_key != effective_key:
        _set_runtime_config("minimax_tts", "api_key", entered_key)

    return entered_key


def _get_cached_minimax_voices(api_key: str, endpoint: str) -> list[dict[str, str]]:
    """按站点和凭证摘要读取当前会话中的 MiniMax 音色查询结果。"""
    cache = st.session_state.get("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cached_voices = cache.get(cache_key, [])
    return cached_voices if isinstance(cached_voices, list) else []


def _cache_minimax_voices(
    api_key: str,
    endpoint: str,
    voices: list[dict[str, str]],
):
    """缓存主动查询到的音色，避免普通控件 rerun 后重复请求 MiniMax。"""
    cache = st.session_state.setdefault("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cache[cache_key] = voices


def _render_minimax_tts_settings() -> tuple[list[str], dict[str, str]]:
    """渲染 MiniMax TTS 配置，并返回统一音色选择器使用的选项和文案。"""
    effective_api_key = _sync_minimax_tts_api_key_input()
    effective_api_key = st.text_input(
        tr("MiniMax TTS API Key"),
        type="password",
        key="minimax_tts_api_key_input",
    ).strip()

    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    minimax_tts_endpoints = [voice.MINIMAX_TTS_GLOBAL_URL, voice.MINIMAX_TTS_CN_URL]
    effective_endpoint = voice.get_minimax_tts_endpoint()
    if effective_endpoint not in minimax_tts_endpoints:
        effective_endpoint = voice.MINIMAX_TTS_GLOBAL_URL
    minimax_tts_base_url = stable_selectbox(
        tr("MiniMax TTS Endpoint"),
        options=minimax_tts_endpoints,
        default_value=effective_endpoint,
        key="minimax_tts_endpoint_select",
        # 复用 LLM Key 时必须跟随 LLM 所在区域，避免界面允许选择一个实际
        # 不会生效的地址；填写独立 TTS Key 后即可单独选择站点。
        disabled=not dedicated_key,
    )
    if dedicated_key:
        _set_runtime_config("minimax_tts", "base_url", minimax_tts_base_url)

    configured_model = config.minimax_tts.get(
        "model_id", voice.MINIMAX_TTS_DEFAULT_MODEL
    )
    if configured_model not in voice.MINIMAX_TTS_MODELS:
        configured_model = voice.MINIMAX_TTS_DEFAULT_MODEL
    minimax_tts_model = stable_selectbox(
        tr("MiniMax TTS Model"),
        options=list(voice.MINIMAX_TTS_MODELS),
        default_value=configured_model,
        key="minimax_tts_model_select",
    )
    _set_runtime_config("minimax_tts", "model_id", minimax_tts_model)

    if st.button(
        tr("Load MiniMax Voices"),
        key="load_minimax_voices_button",
        icon=":material/refresh:",
        use_container_width=True,
    ):
        try:
            available_voices = voice.get_minimax_voice_catalog(
                api_key=effective_api_key,
                endpoint=minimax_tts_base_url,
                voice_type="all",
            )
        except Exception as exc:
            # 这里必须把异常暴露给用户并记录日志。账号区域不匹配、Key 权限不足
            # 或网络失败都很常见，静默返回空列表会让用户误以为账号没有音色。
            logger.warning(f"load MiniMax voices failed: {exc}")
            st.error(tr("MiniMax Voices Load Failed").format(error=str(exc)))
        else:
            _cache_minimax_voices(
                effective_api_key,
                minimax_tts_base_url,
                available_voices,
            )
            st.success(tr("MiniMax Voices Loaded").format(count=len(available_voices)))

    available_voices = _get_cached_minimax_voices(
        effective_api_key,
        minimax_tts_base_url,
    )
    voice_labels = {
        f"minimax:{item['voice_id']}": (
            f"{item['voice_name']} ({item['voice_id']})"
            if item["voice_name"] != item["voice_id"]
            else item["voice_id"]
        )
        for item in available_voices
    }
    configured_voice_id = str(
        config.minimax_tts.get("voice_id", voice.MINIMAX_TTS_DEFAULT_VOICE)
        or voice.MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    configured_voice = f"minimax:{configured_voice_id}"
    # 尚未点击获取音色、接口暂时不可用或配置使用列表外克隆音色时，仍保留
    # 当前 Voice ID，确保原有生成流程不依赖远端音色查询结果。
    voice_labels.setdefault(configured_voice, configured_voice_id)
    return list(voice_labels), voice_labels


def _sync_elevenlabs_api_key_input():
    """
    同步 ElevenLabs 密码控件、持久化配置和环境变量，并返回当前有效 Key。

    Streamlit 在浏览器标签页连接到重启后的服务时，可能重放一个空的密码控件
    状态。这个空值无法与用户主动清空可靠区分，因此当配置文件或环境变量仍有
    Key 时，优先恢复有效值，防止空状态覆盖配置并确保本次 rerun 能立即加载
    音色。需要彻底删除 Key 时应修改配置文件或环境变量，避免重连误判。
    """
    widget_key = "elevenlabs_api_key_input"
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    env_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    effective_key = configured_key or env_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 重连后的空状态不能覆盖有效凭证，同时必须在渲染音色列表之前恢复，
        # 否则配置文件虽然没有被清空，当前页面仍会使用空 Key 请求 ElevenLabs。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored ElevenLabs API key after empty session replay")
    elif not had_widget_state:
        # 先初始化再创建控件，避免同时传 value 和 session_state 触发 Streamlit
        # 的默认值冲突警告；没有任何 Key 时初始化为空即可。
        st.session_state[widget_key] = entered_key

    if entered_key and entered_key != effective_key:
        # 用户主动输入的新值才落入 config.toml。环境变量作为有效值回填时不会
        # 被复制到文件，容器或部署平台注入的密钥仍只保留在运行环境中。
        for cache_key in list(st.session_state.keys()):
            if str(cache_key).startswith("elevenlabs_voices_"):
                del st.session_state[cache_key]
        _set_runtime_config("elevenlabs", "api_key", entered_key)

    return entered_key


def _render_elevenlabs_api_key_input(label_key):
    """
    渲染 ElevenLabs TTS 与配乐共用的唯一 API Key 输入状态。

    同一页面若为 TTS 和配乐分别使用两个 widget key，Streamlit 会各自保留旧值，
    后渲染的输入框还会覆盖共享配置。这里统一使用一个 key，并集中处理环境变量
    回填、配置更新和音色缓存失效，确保界面显示与后台任务始终读取同一个值。
    """
    _sync_elevenlabs_api_key_input()
    return st.text_input(
        tr(label_key),
        type="password",
        key="elevenlabs_api_key_input",
    ).strip()


# ================ 音频分析模块 (老杨 8/8 13:19 拍板) ================
# 上传音频后点"分析这首音乐"按钮 → 调老杨新开发的 mv.analyze 服务 →
# 回显曲调特征 + 意境总结 + 一键填充到 video_script / video_terms 两个输入框。
# 缓存策略: 同 song_signature 重跑 ≤ 3 次, 超过读缓存; 缓存超过 180 天才允许重跑 (Q5 老杨拍板).

_MV_CACHE_REANALYZE_LIMIT = 5     # 同 signature 重跑上限 (老杨 8/8 17:34: 默认5,调试时可调)
_MV_CACHE_TTL_DAYS = 180          # 超过 N 天才允许重新调 LLM (半年到 1 年阈值下限)
_MV_AUDIO_SESSION_KEY = "mv_audio_analysis_result"  # session_state 里存结果的 key
# 老杨 8/8 21:41: 改为一次性信号. 原因: flag=True 会让后续所有 rerun 都弹, 主界面其他
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

    老杨 8/8 14:00 bug: 同一个歌分析三次都 first_run
    根因: webui 每次生成新 uuid file_id (storage/mv/mva-aaa.mp3), DB 按 file_id 查
    永远查不到历史记录
    修法: 改为按 song_signature 查询 (相同歌永远同 signature, 跟上传次数无关)
    audio_id 参数保留以便调用者存历史

    Returns:
        (should_run, latest_record_or_None, reason)
        reason: 'first_run' / 'within_limit' / 'cache_fresh' / 'cache_expired' / 'force_rerun'
    """
    from datetime import datetime, timedelta

    # 老杨 8/8 17:34: 调试开关, 勾选后绕过缓存强制重跑 LLM
    import streamlit as st
    if st.session_state.get(_MV_FORCE_RERUN_KEY, False):
        return True, None, "force_rerun (debug)"

    # 老杨 14:00 bug fix: 按 song_signature 查 (跨 audio_id 重用)
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
    """老杨 2026-08-17 16:08 拍板: 弹窗内增加 AI 歌曲提示词 (Mureka) 折叠段

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
        # 老杨 8/17 21:23 修复: 用 from_dict() 递归还原嵌套 dataclass
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
        "（老杨原话:warm/intimate/Chinese folk singer style raw vocal quality）"
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

    # 风格识别 (Diana 3.1)
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
    """老杨 2026-08-15 23:23 拍板: WebUI 上传 mp3 后, 自动匹配 storage/lrc/ 下的 LRC.

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
    """老杨 8/8 拍板的核心函数: 上传音频 → 调 mv.analyze 服务 → 返回 plan + features

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
    # 老杨 8/8 17:34: 记下当前 signature 供 clear_cache 用
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
    """老杨 8/8 13:34 拍板的弹窗: 曲调特征 + 意境 + 关键词 + 配色 + 段落拍摄 + 3 个应用按钮

    老杨 13:34 原话: "折叠栏位太窄, 改成弹窗 (st.dialog) 更宽屏看"
    - width="large" 加大宽度
    - 弹窗内可调用 callback (在弹窗本身之前执行, 不会触发 StreamlitAPIException)
    - 弹窗内改 video_script / video_terms 的 session_state 在 dialog 关闭后生效
      (rerun 之后 video_script widget 重新实例化时看到新值)

    Trigger: _MV_DIALOG_FLAG_KEY = True → _render_audio_analysis_panel() 会调它
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
    # 老杨 8/8 13:53 + 13:58 bug 修复:
    # 1. dialog button click 触发的是完整 script run (不是 partial rerun)
    # 2. on_click callback 在 script run 之前执行, 它改 session_state 对后续
    #    widget render 可见 (这是 streamlit 设计)
    # 3. callback 里调 st.rerun() 是 no-op + warning, 跳过即可
    # 4. dialog 关闭机制: 设 _MV_DIALOG_FLAG_KEY=False, 下次 _render_audio_analysis_panel
    #    看到 flag=False 不调 _render_audio_analysis_dialog(), dialog 自动隐藏
    # 5. 不能直接改 page widget session_state (video_script / video_terms),
    #    改用 _MV_PENDING_APPLY_KEY pending dict, _render_application() 顶部消费
    def _apply_mood_callback(ms: str, ver: int):
        pending = st.session_state.get(_MV_PENDING_APPLY_KEY) or {}
        pending["video_script_append"] = (pending.get("video_script_append") or "") + ("\n\n" if pending.get("video_script_append") else "") + ms
        pending["source_version"] = ver
        st.session_state[_MV_PENDING_APPLY_KEY] = pending
        # 老杨 8/8 21:41: 不需手动设 flag=False. 一次性 signal 被 _render_audio_analysis_panel 弹后 pop.
        # callback 直接 st.rerun(scope='app') 触发 page rerun → _apply_pending_mv_audio 消费
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

    # === AI 歌曲提示词 (Mureka) — 老杨 8/17 16:08 拍板 ===
    # 折叠段默认收起, 不抩高弹窗. 详细程度 radio 动态切换精简/详细.
    with st.expander("🎵 AI 歌曲提示词 (Mureka)", expanded=False):
        _render_mureka_prompts_section(features, version)

    st.divider()

    # === 高潮段检测 (Diana 8/8 老杨拍板) ===
    # 老杨 17:34 原话: "有多少高潮就选几个, 识别不出来高潮就不选"
    # detect_chorus_segments 返回 1-N 个 (实际几个就几个)
    # 老杨 17:40: UI 最多展示 3 个 -> 如果识别出 >3 个取 confidence 最高的 3 个, <3 个按数量显示
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

        # 选中的高潮段 (Diana 8/8)
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

        # 回显高潮对应的搜索关键字 (Diana 8/8)
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
            # 老杨 8/8 21:09: 同时存 chorus 段 start/end 到 pending,
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
                # 老杨 8/8 21:09: 高潮段独立 MV - 存 start/end 到 pending
                pending["chorus_range_start"] = round(start, 2)
                pending["chorus_range_end"] = round(end, 2)
                pending["source_version"] = pending.get("source_version", ver)
                st.session_state[_MV_PENDING_APPLY_KEY] = pending
                # 老杨 8/8 18:09: 全 app rerun 强制 page widget 看到新值
                st.rerun(scope="app")

            st.button(
                tr("Apply Chorus Keywords Button"),
                key="mv_dlg_apply_chorus_kws",
                use_container_width=True,
                type="secondary",
                disabled=not chorus_prompt_en,
                on_click=_apply_chorus_keywords_callback,
                # 老杨 8/8 21:09: 传入 displayed_segments[selected_idx] (选中的 chorus segment)
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
def _render_mv_analysis_dialog(task_id: str, task: dict):
    """2026-08-09 P2-6: 任务管理点击 MV 按钮 → 弹窗查看这个 task 的 LLM plan

    老杨 8/9 11:06 拍板: '这些功能是需要的, 你先加一下代码'

    内容:
    - audio_features (BPM / key / duration / sections / chorus)
    - LLM plan (mood_summary + theme_keywords 中英 + color_palette)
    - Pexels 搜索词 (video_terms)
    - 原始文件路径 (custom_audio / lrc_file)
    - LLM 调用元信息 (latency / model / cost / tokens / version)

    数据流: task_id → mv_intent_history.task_id → IntentRecord
    """
    try:
        # 2026-08-09 老杨 19:31 bug 修复:
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


def _render_audio_analysis_panel(uploaded_audio_file, key_prefix: str = "voice"):
    """老杨 8/8 拍板的 UI: 上传音频后点按钮 → 弹窗里看分析结果 + 一键应用

    老杨 8/8 13:34 拍板: 改用 st.dialog 弹窗代替 expander, 宽屏看分析结果

    2026-08-10 老杨拍板: 首次上传弹版权声明 (st.session_state 记忆, 不再每次打断)

    2026-08-13 老杨拍板: 加 key_prefix 参数, 支持同一页面多处调用
        - 音频设置区上传语音： key_prefix="voice"
        - 背景音乐区上传音频：key_prefix="bgm"
        避免 widget key 冲突 (streamlit 不允许同页面两个同名 widget)
    """
    if uploaded_audio_file is None:
        st.caption(tr("Audio Analysis No Audio"))
        return

    # 2026-08-10 老杨拍板: 首次上传弹版权声明, session_state 记忆（全局共享, 不分 prefix）
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
                # 老杨 2026-08-15 23:23 拍板: WebUI 上传 mp3 后, 自动找匹配 lrc 加载歌词
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
                # 老杨 8/8 21:31: 按段落拼接 - 存 plan + features 供视频生成用
                st.session_state["_mv_current_plan"] = result.get("plan")
                st.session_state["_mv_current_features"] = result.get("features")
                # 老杨 8/8 21:41 bug 修复: 用一次性信号代替持久 flag
                # 原因: flag=True 会让后续所有 rerun 都重复弹窗, 哪怕主界面其他改动
                # 用 _MV_DIALOG_REQUEST_KEY: 在下轮 rerun 弹, 弹后立即清
                st.session_state[_MV_DIALOG_REQUEST_KEY] = True
            except Exception as exc:
                logger.error(f"mv_audio_analysis failed: {exc}")
                st.error(tr("Audio Analysis Failed").format(error=str(exc)))

    def _open_dialog_callback():
        # 老杨 8/8 21:41 bug 修复: 一次性信号代替持久 flag
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

    # === Debug 控件 (老杨 8/8 17:34 拍板) ===
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

    # 老杨 8/8 21:41 bug 修复: 用一次性信号代替持久 flag
    # 原 bug: 主界面其他选项改动触发 rerun → flag 还是 True → dialog 又弹
    # 修法: 一次性信号 key, 弹后立即 pop. 只有用户主动点击才产生新信号
    if st.session_state.pop(_MV_DIALOG_REQUEST_KEY, False):
        _render_audio_analysis_dialog()


def _render_background_music_settings(params, elevenlabs_api_key_rendered=False):
    """渲染背景音乐来源与音量设置，并返回本次待保存的上传文件。"""
    uploaded_bgm_file = None
    previous_bgm_type = st.session_state.get("last_rendered_bgm_type")
    st.divider()
    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    selected_bgm_type = stable_selectbox(
        tr("Background Music Source"),
        options=[value for _, value in bgm_options],
        default_value=_saved_ui_choice(
            "bgm_type",
            [value for _, value in bgm_options],
            "random",
        ),
        key="bgm_type_select",
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
    )
    params.bgm_type = selected_bgm_type
    _set_runtime_config("ui", "bgm_type", params.bgm_type)
    if params.bgm_type == "sonilo":
        configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
        effective_key = configured_key or os.getenv("SONILO_API_KEY", "").strip()
        entered_key = st.text_input(
            tr("Sonilo API Key"),
            value=effective_key,
            type="password",
            key="sonilo_api_key_input",
        ).strip()
        # 用户要求已配置的 Key 直接回填到密码输入框。配置值优先于环境变量；
        # 仅当用户确实修改输入或本来就使用配置时写回，避免把环境变量中的 Key
        # 在无操作的情况下复制进 config.toml。
        if configured_key or entered_key != effective_key:
            _set_runtime_config("app", "sonilo_api_key", entered_key)
    elif params.bgm_type == "elevenlabs":
        if elevenlabs_api_key_rendered:
            # TTS 区域已经渲染共享输入框时不再创建第二个 widget，避免两个独立
            # session_state 值互相覆盖。说明文字帮助用户定位上方的共用配置。
            st.caption(tr("ElevenLabs API Key Help"))
        else:
            _render_elevenlabs_api_key_input("ElevenLabs Music API Key")

    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    params.bgm_volume = stable_selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        default_value=_saved_ui_choice("bgm_volume", bgm_volume_options, 0.2),
        key="bgm_volume_select",
        format_func=lambda value: f"{int(value * 100)}%",
        disabled=not params.bgm_type,
    )
    _set_runtime_config("ui", "bgm_volume", params.bgm_volume)
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)

    if params.bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Background Music"),
            type=[
                extension.removeprefix(".")
                for extension in bgm_service.SUPPORTED_BGM_EXTENSIONS
            ],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
            help=tr("Upload Background Music Help"),
            # Streamlit 默认会在控件上展示全局 200MB 上限。这里必须与服务层
            # 30MB 硬限制保持一致，避免界面允许选择、提交时才被服务端拒绝。
            max_upload_size=bgm_service.MAX_BGM_UPLOAD_BYTES // (1024 * 1024),
        )
        if uploaded_bgm_file is not None and bgm_enabled:
            try:
                safe_name = bgm_service.sanitize_upload_filename(uploaded_bgm_file.name)
                # Streamlit 在调整音量等任意控件后都会重新执行页面。使用内容哈希
                # 区分上传文件，并在当前会话内缓存完整解码结果，既不能只凭同名、
                # 同大小文件误用旧结果，也避免每次 rerun 都重复调用 FFmpeg。
                validation_key = (
                    safe_name,
                    uploaded_bgm_file.size,
                    hashlib.sha256(uploaded_bgm_file.getbuffer()).hexdigest(),
                )
                cached_validation = st.session_state.get("custom_bgm_validation")
                if (
                    not cached_validation
                    or cached_validation.get("key") != validation_key
                ):
                    try:
                        bgm_service.validate_bgm_upload(
                            uploaded_bgm_file.name, uploaded_bgm_file
                        )
                    except bgm_service.BgmUploadError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "upload",
                        }
                        # 同一个文件指纹的失败结果会进入会话缓存，因此这里只在
                        # 首次真实执行校验时记录一次，避免普通控件 rerun 刷屏。
                        logger.warning(
                            "WebUI background music validation rejected: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    except bgm_service.BgmServiceError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "service",
                        }
                        logger.error(
                            "WebUI background music validation failed: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    else:
                        cached_validation = {
                            "key": validation_key,
                            "error": "",
                            "error_type": "",
                        }
                    st.session_state["custom_bgm_validation"] = cached_validation

                if cached_validation.get("error"):
                    if cached_validation.get("error_type") == "service":
                        raise bgm_service.BgmServiceError(cached_validation["error"])
                    raise bgm_service.BgmUploadError(cached_validation["error"])
            except bgm_service.BgmUploadError:
                # 非法文件不能沿用上一次有效上传的名称，否则任务参数可能仍指向
                # 历史 BGM。保留 UploadedFile 返回值，让用户点击生成时仍会被最终
                # 服务端校验拦截，而不是静默生成一条没有背景音乐的视频。
                params.bgm_file = ""
                st.error(tr("Invalid Background Music"))
            except bgm_service.BgmServiceError:
                params.bgm_file = ""
                st.error(tr("Background Music Validation Failed"))
            else:
                # 完整解码校验通过后才展示播放器和“已就绪”。文件仍只在点击
                # 生成时持久化，用户仅预览或随后移除文件不会污染 storage/bgm。
                uploaded_mime_type = str(getattr(uploaded_bgm_file, "type", "") or "")
                preview_mime_type = (
                    uploaded_mime_type
                    if uploaded_mime_type.startswith("audio/")
                    else mimetypes.guess_type(safe_name)[0] or "audio/mpeg"
                )
                st.audio(uploaded_bgm_file, format=preview_mime_type)
                st.info(f"{tr('Background Music Ready')}: {safe_name}")
                # 老杨 8/13 拍板: 背景音乐也加 MV 歌曲评价入口，跟音频设置里的上传语音一致
                _render_audio_analysis_panel(uploaded_bgm_file, key_prefix="bgm")
                params.bgm_file = safe_name

        # Streamlit 会在条件控件暂时不渲染时清理其 widget state。
        # 从其它 BGM 来源切回时用已持久化值恢复；同一来源下
        # 用户主动清空时 previous_bgm_type 不变，因此不会被旧值反弹。
        if previous_bgm_type != "custom":
            st.session_state["custom_bgm_file_input"] = _saved_ui_text(
                "custom_bgm_file"
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        _set_runtime_config(
            "ui", "custom_bgm_file", custom_bgm_file.strip()
        )
        if uploaded_bgm_file is None and custom_bgm_file and bgm_enabled:
            # 文件名由服务层映射到 storage/bgm 或 resource/songs 后校验，
            # UI 不接受两个白名单目录之外的任意路径。
            params.bgm_file = custom_bgm_file.strip()
        elif not bgm_enabled:
            # 上传控件继续保留用户已选择的文件，调高音量后的下一次 rerun 会自动
            # 完整校验；当前任务参数必须清空，避免 0 音量任务保存或解析该文件。
            params.bgm_file = ""

    if params.bgm_type == "sonilo":
        if previous_bgm_type != "sonilo":
            st.session_state["sonilo_bgm_prompt_input"] = _saved_ui_text(
                "sonilo_bgm_prompt",
                max_length=sonilo_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            key="sonilo_bgm_prompt_input",
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            help=tr("Sonilo Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "sonilo_bgm_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("Sonilo Multiple Videos Warning"))
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
            use_container_width=True,
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                logger.warning(f"Sonilo connection test failed: {exc}")
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
    elif params.bgm_type == "elevenlabs":
        if previous_bgm_type != "elevenlabs":
            st.session_state["elevenlabs_music_prompt_input"] = _saved_ui_text(
                "elevenlabs_music_prompt",
                max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            key="elevenlabs_music_prompt_input",
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            help=tr("ElevenLabs Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "elevenlabs_music_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("ElevenLabs Multiple Videos Warning"))
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
            use_container_width=True,
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                logger.warning(f"ElevenLabs connection test failed: {exc}")
                st.error(tr("ElevenLabs Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
    if params.bgm_type == "sonilo" and bgm_enabled and not sonilo_service.is_enabled():
        # 音量为 0 时任务层不会生成或混合 Sonilo 配乐，因此无需提示 Key；
        # 该判断与任务入口共用服务层规则，避免界面提示和实际执行条件分叉。
        st.warning(tr("Sonilo API Key Required"))
    elif (
        params.bgm_type == "elevenlabs"
        and bgm_enabled
        and not elevenlabs_music_service.is_enabled()
    ):
        st.warning(tr("ElevenLabs API Key Required"))
    st.session_state["last_rendered_bgm_type"] = params.bgm_type
    return uploaded_bgm_file


def _render_audio_settings(panel, params):
    """渲染音频设置并返回上传音频与当前配音模式。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Audio Settings"))

            # 配音方式是音频设置的一级状态，负责明确区分自动配音、用户上传和无配音。
            # 旧配置没有 voice_mode 时，根据原 tts_server 的无配音哨兵保持兼容。
            saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
            saved_voice_mode = config.ui.get("voice_mode")
            if saved_voice_mode not in {
                VOICE_MODE_TTS,
                VOICE_MODE_UPLOAD,
                VOICE_MODE_NONE,
            }:
                saved_voice_mode = (
                    VOICE_MODE_NONE
                    if saved_tts_server == voice.NO_VOICE_NAME
                    else VOICE_MODE_TTS
                )
            voice_mode_options = [VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE]
            voice_mode_labels = {
                VOICE_MODE_TTS: tr("Automatic Voiceover"),
                VOICE_MODE_UPLOAD: tr("Upload Voiceover"),
                VOICE_MODE_NONE: tr("No Voiceover"),
            }
            voice_mode = stable_segmented_control(
                tr("Voiceover Mode"),
                options=voice_mode_options,
                default_value=saved_voice_mode,
                key="voice_mode_control",
                format_func=lambda value: voice_mode_labels[value],
                width="stretch",
            )
            _set_runtime_config("ui", "voice_mode", voice_mode)
            tts_mode_enabled = voice_mode == VOICE_MODE_TTS

            # Provider 下拉只负责选择自动配音服务；无配音已经由上方模式控制，
            # 不再作为 TTS Provider 混入列表，避免两个入口表达同一状态。
            tts_servers = [
                ("azure-tts-v1", "Azure TTS V1"),
                ("azure-tts-v2", "Azure TTS V2"),
                ("siliconflow", "SiliconFlow TTS"),
                ("gemini-tts", "Google Gemini TTS"),
                ("mimo-tts", "Xiaomi MiMo TTS"),
                ("minimax-tts", "MiniMax TTS"),
                ("elevenlabs", "ElevenLabs TTS"),
                ("chatterbox", "Chatterbox TTS"),
                ("fish_audio", "Fish Audio TTS"),
            ]

            tts_server_values = [server_value for server_value, _ in tts_servers]
            if saved_tts_server not in tts_server_values:
                saved_tts_server = "azure-tts-v1"

            if tts_mode_enabled:
                selected_tts_server = stable_selectbox(
                    tr("Voiceover Service"),
                    options=tts_server_values,
                    default_value=saved_tts_server,
                    key="tts_server_select",
                    format_func=lambda value: dict(
                        (v, label) for v, label in tts_servers
                    )[value],
                )
            else:
                # 非自动配音模式不渲染 TTS 控件，但保留上次选择，切回后可以继续使用。
                selected_tts_server = saved_tts_server

            _set_runtime_config("ui", "tts_server", selected_tts_server)

            # 服务说明紧跟 Provider 选择，先告诉用户需要准备什么，再进入音色和
            # 凭证配置。没有说明的 Provider 不渲染空提示块。
            if tts_mode_enabled:
                provider_tips = get_tts_provider_tips(selected_tts_server)
                if provider_tips:
                    st.info(provider_tips)

            # MiniMax 只复用下方通用“配音声音”选择器。Provider 配置函数负责
            # 刷新远端音色并返回友好文案，不再额外渲染 Voice ID 和音色下拉框。
            minimax_voices = []
            minimax_voice_labels = {}
            if tts_mode_enabled and selected_tts_server == "minimax-tts":
                minimax_voices, minimax_voice_labels = _render_minimax_tts_settings()

            # 根据选择的TTS服务器获取声音列表
            filtered_voices = []
            saved_voice_name = config.ui.get("voice_name", "")
            elevenlabs_api_key_rendered = False

            if not tts_mode_enabled:
                # 上传音频和无配音模式不加载远程音色，减少无意义的网络请求和界面噪音。
                filtered_voices = []
            elif selected_tts_server == "siliconflow":
                # 获取硅基流动的声音列表
                filtered_voices = voice.get_siliconflow_voices()
            elif selected_tts_server == "gemini-tts":
                # 获取Gemini TTS的声音列表
                filtered_voices = voice.get_gemini_voices()
            elif selected_tts_server == "mimo-tts":
                # 获取 Xiaomi MiMo TTS 的预置音色列表
                filtered_voices = voice.get_mimo_voices()
            elif selected_tts_server == "minimax-tts":
                filtered_voices = minimax_voices
            elif selected_tts_server == "elevenlabs":
                # 音色列表位于 Key 输入框之前渲染，必须先统一恢复重连状态并读取
                # 配置/环境变量，否则页面会用空 Key 加载并缓存空音色列表。
                saved_elevenlabs_api_key = _sync_elevenlabs_api_key_input()
                cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
                if cache_key not in st.session_state:
                    st.session_state[cache_key] = voice.get_elevenlabs_voices(
                        saved_elevenlabs_api_key
                    )
                filtered_voices = st.session_state[cache_key]
            elif selected_tts_server == "chatterbox":
                # 自托管 Chatterbox 服务的预置音色（来自 [chatterbox] voices 配置）
                _sync_chatterbox_config_from_session_state()
                filtered_voices = voice.get_chatterbox_voices()
            elif selected_tts_server == "fish_audio":
                filtered_voices = voice.get_fish_audio_voices()
            else:
                # 获取Azure的声音列表
                all_voices = voice.get_all_azure_voices(filter_locals=None)

                # 根据选择的TTS服务器筛选声音
                for v in all_voices:
                    if selected_tts_server == "azure-tts-v2":
                        # V2版本的声音名称中包含"v2"
                        if "V2" in v:
                            filtered_voices.append(v)
                    else:
                        # V1版本的声音名称中不包含"v2"
                        if "V2" not in v:
                            filtered_voices.append(v)

            def _friendly(v):
                if voice.is_no_voice(v):
                    return tr("No Voice Selected")
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                if voice.is_minimax_voice(v):
                    return minimax_voice_labels.get(v, v.split(":", 1)[1])
                if voice.is_fish_audio_voice(v):
                    parts = v.split(":", 2)
                    display_name = parts[2] if len(parts) >= 3 else v
                    return (
                        display_name.replace("Female", tr("Female"))
                        .replace("Male", tr("Male"))
                    )
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )

            friendly_names = {v: _friendly(v) for v in filtered_voices}

            # Gemini 旧目录把推测的性别放在值里（例如 Charon-Male）。按基础
            # voice name 映射到新的官方风格值，升级后继续保留用户原来的音色。
            if (
                selected_tts_server == "gemini-tts"
                and saved_voice_name not in friendly_names
            ):
                saved_gemini_voice = voice.parse_gemini_voice_name(saved_voice_name)
                saved_voice_name = next(
                    (
                        candidate
                        for candidate in filtered_voices
                        if voice.parse_gemini_voice_name(candidate)
                        == saved_gemini_voice
                    ),
                    saved_voice_name,
                )

            saved_voice_name_index = 0

            # 检查保存的声音是否在当前筛选的声音列表中
            if saved_voice_name in friendly_names:
                saved_voice_name_index = list(friendly_names.keys()).index(
                    saved_voice_name
                )
            else:
                # 如果不在，则根据当前UI语言选择一个默认声音
                for i, v in enumerate(filtered_voices):
                    if v.lower().startswith(st.session_state["ui_language"].lower()):
                        saved_voice_name_index = i
                        break

            # 如果没有找到匹配的声音，使用第一个声音
            if saved_voice_name_index >= len(friendly_names) and friendly_names:
                saved_voice_name_index = 0

            # 确保有声音可选
            if tts_mode_enabled and friendly_names:
                voice_name = stable_selectbox(
                    tr("Voiceover Voice"),
                    options=list(friendly_names.keys()),
                    default_value=list(friendly_names.keys())[saved_voice_name_index],
                    key=f"speech_synthesis_select_{selected_tts_server}",
                    format_func=lambda value: friendly_names.get(
                        value,
                        str(value).removeprefix("minimax:"),
                    ),
                    # MiniMax 支持用户直接输入列表外的克隆或生成音色 ID；其它
                    # Provider 维持原选择器行为，不扩大本次修改的影响范围。
                    accept_new_options=selected_tts_server == "minimax-tts",
                )

                if selected_tts_server == "minimax-tts":
                    custom_voice_id = str(voice_name or "").strip()
                    if custom_voice_id and not voice.is_minimax_voice(custom_voice_id):
                        voice_name = f"minimax:{custom_voice_id}"
                    if voice.is_minimax_voice(voice_name):
                        _set_runtime_config(
                            "minimax_tts",
                            "voice_id",
                            voice_name.split(":", 1)[1],
                        )

                params.voice_name = voice_name
                if not voice.is_no_voice(voice_name):
                    # 占位 sentinel 仅用于非自动模式的禁用展示，不覆盖用户上一次
                    # 真正选择的音色，切回自动配音后可以恢复原设置。
                    _set_runtime_config("ui", "voice_name", voice_name)
            elif tts_mode_enabled:
                # 如果没有声音可选，显示提示信息
                st.warning(
                    tr(
                        "No voices available for the selected TTS server. Please select another server."
                    )
                )
                voice_name = ""
                params.voice_name = ""
                _set_runtime_config("ui", "voice_name", "")
            else:
                # 非自动配音模式不显示音色控件，只复用保存值维持参数结构稳定。
                voice_name = saved_voice_name or voice.NO_VOICE_NAME
                params.voice_name = voice_name

            # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
            if tts_mode_enabled and (
                selected_tts_server == "azure-tts-v2"
                or (voice_name and voice.is_azure_v2_voice(voice_name))
            ):
                saved_azure_speech_region = config.azure.get("speech_region", "")
                saved_azure_speech_key = config.azure.get("speech_key", "")
                azure_speech_region = st.text_input(
                    tr("Speech Region"),
                    value=saved_azure_speech_region,
                    key="azure_speech_region_input",
                )
                azure_speech_key = st.text_input(
                    tr("Speech Key"),
                    value=saved_azure_speech_key,
                    type="password",
                    key="azure_speech_key_input",
                )
                _set_runtime_config("azure", "speech_region", azure_speech_region)
                _set_runtime_config("azure", "speech_key", azure_speech_key)

            if tts_mode_enabled and selected_tts_server == "gemini-tts":
                # Gemini TTS 与 Gemini LLM 共用同一份密钥；在音频面板提供直接入口，
                # 用户无需先切换 LLM Provider 才能完成语音配置。
                gemini_tts_api_key = st.text_input(
                    tr("Gemini API Key"),
                    value=config.app.get("gemini_api_key", ""),
                    type="password",
                    key="gemini_tts_api_key_input",
                )
                _set_runtime_config("app", "gemini_api_key", gemini_tts_api_key)

            # 当选择硅基流动时，显示API key输入框和说明信息
            if tts_mode_enabled and (
                selected_tts_server == "siliconflow"
                or (voice_name and voice.is_siliconflow_voice(voice_name))
            ):
                saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

                siliconflow_api_key = st.text_input(
                    tr("SiliconFlow API Key"),
                    value=saved_siliconflow_api_key,
                    type="password",
                    key="siliconflow_api_key_input",
                )

                _set_runtime_config("siliconflow", "api_key", siliconflow_api_key)

            # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
            # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
            if tts_mode_enabled and (
                selected_tts_server == "mimo-tts"
                or (voice_name and voice.is_mimo_voice(voice_name))
            ):
                saved_mimo_api_key = config.app.get("mimo_api_key", "")

                mimo_api_key = st.text_input(
                    tr("MiMo API Key"),
                    value=saved_mimo_api_key,
                    type="password",
                    key="mimo_tts_api_key_input",
                )

                _set_runtime_config("app", "mimo_api_key", mimo_api_key)

            # ElevenLabs API key section
            if tts_mode_enabled and (
                selected_tts_server == "elevenlabs"
                or (voice_name and voice.is_elevenlabs_voice(voice_name))
            ):
                _render_elevenlabs_api_key_input(
                    "ElevenLabs API Key",
                )
                elevenlabs_api_key_rendered = True

                _elevenlabs_models = [
                    "eleven_multilingual_v2",
                    "eleven_flash_v2_5",
                    "eleven_v3",
                ]
                saved_elevenlabs_model = config.elevenlabs.get(
                    "model_id", "eleven_multilingual_v2"
                )
                if saved_elevenlabs_model not in _elevenlabs_models:
                    saved_elevenlabs_model = "eleven_multilingual_v2"
                elevenlabs_model = stable_selectbox(
                    tr("ElevenLabs Model"),
                    options=_elevenlabs_models,
                    default_value=saved_elevenlabs_model,
                    key="elevenlabs_model_select",
                )
                _set_runtime_config("elevenlabs", "model_id", elevenlabs_model)

            # Fish Audio API settings section
            if tts_mode_enabled and (
                selected_tts_server == "fish_audio"
                or (voice_name and voice.is_fish_audio_voice(voice_name))
            ):
                saved_fish_api_key = (
                    config.fish_audio.get("api_key", "")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else ""
                )
                fish_audio_api_key = st.text_input(
                    tr("Fish Audio API Key"),
                    value=saved_fish_api_key,
                    type="password",
                    key="fish_audio_api_key_input",
                )
                _set_runtime_config("fish_audio", "api_key", fish_audio_api_key)

                _fish_audio_models = [
                    "s2.1-pro-free",
                    "s2.1-pro",
                    "s2-pro",
                ]
                saved_fish_model = (
                    config.fish_audio.get("model", "s2.1-pro-free")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else "s2.1-pro-free"
                )
                if saved_fish_model not in _fish_audio_models:
                    saved_fish_model = "s2.1-pro-free"
                fish_model = stable_selectbox(
                    tr("Fish Audio Model"),
                    options=_fish_audio_models,
                    default_value=saved_fish_model,
                    key="fish_audio_model_select",
                )
                _set_runtime_config("fish_audio", "model", fish_model)

            # Chatterbox API settings section (self-hosted, OpenAI-compatible)
            if tts_mode_enabled and (
                selected_tts_server == "chatterbox"
                or (voice_name and voice.is_chatterbox_voice(voice_name))
            ):
                chatterbox_base_url = st.text_input(
                    tr("Chatterbox Base URL"),
                    value=config.chatterbox.get("base_url")
                    or DEFAULT_CHATTERBOX_BASE_URL,
                    key="chatterbox_base_url_input",
                    placeholder=tr("Chatterbox Base URL Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox", "base_url", (chatterbox_base_url or "").strip()
                )

                chatterbox_api_key = st.text_input(
                    tr("Chatterbox API Key"),
                    value=config.chatterbox.get("api_key", ""),
                    type="password",
                    key="chatterbox_api_key_input",
                )
                _set_runtime_config("chatterbox", "api_key", chatterbox_api_key)

                chatterbox_model = st.text_input(
                    tr("Chatterbox Model"),
                    value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                    key="chatterbox_model_input",
                )
                _set_runtime_config(
                    "chatterbox",
                    "model_id",
                    (chatterbox_model or DEFAULT_CHATTERBOX_MODEL).strip(),
                )

                _saved_chatterbox_voices = (
                    _parse_chatterbox_voices(config.chatterbox.get("voices"))
                    or DEFAULT_CHATTERBOX_VOICES
                )
                if isinstance(_saved_chatterbox_voices, list):
                    _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
                chatterbox_voices = st.text_input(
                    tr("Chatterbox Voices"),
                    value=str(_saved_chatterbox_voices or ""),
                    key="chatterbox_voices_input",
                    placeholder=tr("Chatterbox Voices Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox",
                    "voices",
                    _parse_chatterbox_voices(chatterbox_voices),
                )

            # 三种模式只渲染当前任务真正需要的控件。自动配音可调音量和语速；
            # 上传音频只需要文件和音量；无配音不再展示无效设置。
            params.voice_name = (
                voice.NO_VOICE_NAME if voice_mode == VOICE_MODE_NONE else voice_name
            )
            params.voice_volume = 1.0
            params.voice_rate = 1.0
            uploaded_audio_file = None
            voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
            voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]

            if tts_mode_enabled:
                voice_control_cols = st.columns(2)
                with voice_control_cols[0]:
                    params.voice_volume = stable_selectbox(
                        tr("Voiceover Volume"),
                        options=voice_volume_options,
                        default_value=_saved_ui_choice(
                            "voice_volume", voice_volume_options, 1.0
                        ),
                        key="voice_volume_select",
                        format_func=lambda value: f"{int(value * 100)}%",
                        help=tr("Voiceover Volume Help"),
                    )

                with voice_control_cols[1]:
                    params.voice_rate = stable_selectbox(
                        tr("Voiceover Speed"),
                        options=voice_rate_options,
                        default_value=_saved_ui_choice(
                            "voice_rate", voice_rate_options, 1.0
                        ),
                        key="voice_rate_select",
                        format_func=lambda value: f"{value:.1f}×",
                        help=tr("Voiceover Speed Help"),
                    )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                _set_runtime_config("ui", "voice_rate", params.voice_rate)

                # 试听必须位于音量和语速控件之后，确保调用使用当前控件值。
                _render_voice_preview(
                    params,
                    friendly_names,
                    selected_tts_server,
                    voice_name,
                )
            elif voice_mode == VOICE_MODE_UPLOAD:
                custom_audio_file_types = sorted(
                    extension.removeprefix(".") for extension in CUSTOM_AUDIO_EXTENSIONS
                )
                uploaded_audio_file = st.file_uploader(
                    tr("Upload Voiceover File"),
                    type=custom_audio_file_types
                    + [file_type.upper() for file_type in custom_audio_file_types],
                    accept_multiple_files=False,
                    key="custom_audio_file_uploader",
                    help=tr("Upload Voiceover File Help"),
                )
                params.voice_volume = stable_selectbox(
                    tr("Voiceover Volume"),
                    options=voice_volume_options,
                    default_value=_saved_ui_choice(
                        "voice_volume", voice_volume_options, 1.0
                    ),
                    key="voice_volume_select",
                    format_func=lambda value: f"{int(value * 100)}%",
                    help=tr("Voiceover Volume Help"),
                )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                if uploaded_audio_file:
                    st.audio(uploaded_audio_file, format="audio/mp3")
                    st.info(
                        tr(
                            "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                        )
                    )
                    # 老杨 8/8 13:19 拍板: 上传音频后点按钮调 mv.analyze 服务
                    _render_audio_analysis_panel(uploaded_audio_file, key_prefix="voice")
                    # 老杨 8/8 21:09: 高潮段独立 MV - 从 mv.audio.apply 后存到 session_state
                    # audio_clip_range_start/audio_clip_range_end 这里以 expander 显示
                    _audio_clip_range_start = st.session_state.get("audio_clip_range_start")
                    _audio_clip_range_end = st.session_state.get("audio_clip_range_end")
                    if _audio_clip_range_start is not None and _audio_clip_range_end is not None:
                        with st.expander(f"📍 高潮段 MV: {_audio_clip_range_start:.1f}s - {_audio_clip_range_end:.1f}s ({_audio_clip_range_end - _audio_clip_range_start:.1f}s)", expanded=False):
                            st.caption("选中高潮段后 即可生成该段 MV. 默认从你点击的精选段提取.")
                            _range_cols = st.columns(2)
                            with _range_cols[0]:
                                _new_start = st.number_input(
                                    "起始秒",
                                    min_value=0.0,
                                    max_value=600.0,
                                    value=float(_audio_clip_range_start),
                                    step=0.1,
                                    key="audio_clip_range_start_input",
                                )
                            with _range_cols[1]:
                                _new_end = st.number_input(
                                    "结束秒",
                                    min_value=0.0,
                                    max_value=600.0,
                                    value=float(_audio_clip_range_end),
                                    step=0.1,
                                    key="audio_clip_range_end_input",
                                )
                            if st.button("✅ 应用选定区间", key="audio_clip_range_apply", use_container_width=True):
                                st.session_state["audio_clip_range_start"] = float(_new_start)
                                st.session_state["audio_clip_range_end"] = float(_new_end)
                                st.session_state["_mv_apply_message"] = (
                                    f"✅ 高潮段: {_new_start:.1f}s-{_new_end:.1f}s ({_new_end - _new_start:.1f}s)"
                                )
                                st.session_state["_mv_apply_message_ts"] = time.time()
                                st.rerun(scope="app")
                            if st.button("🚮 清除高潮段", key="audio_clip_range_clear", use_container_width=True):
                                st.session_state.pop("audio_clip_range_start", None)
                                st.session_state.pop("audio_clip_range_end", None)
                                st.session_state["_mv_apply_message"] = "🚮 高潮段已清除"
                                st.session_state["_mv_apply_message_ts"] = time.time()
                                st.rerun(scope="app")
                        # 老杨 8/8 21:31: 按段落拼接开关 - 默认 false, 只有音频分析后才能启用
                        _plan_in_session = st.session_state.get("_mv_current_plan")
                        _n_prompts = len((_plan_in_session or {}).get("video_prompts", []))
                        st.checkbox(
                            f"🎬 按段落拼接 ({_n_prompts} 段独立 prompt, 默认随机拼接)",
                            key="use_segmented_concat",
                            value=st.session_state.get("use_segmented_concat", False),
                            disabled=_plan_in_session is None,
                            help="勾选后 LLM 返回的段落拍摄提示会在视频生成时按段独立拼接. 例如前奏/主歌/副歌/尾奏 各自 pexels 搜索 → 独立拼接. 不勾选则保持现有随机拼接逻辑.",
                        )
            uploaded_bgm_file = _render_background_music_settings(
                params,
                elevenlabs_api_key_rendered=elevenlabs_api_key_rendered,
            )
    return uploaded_audio_file, uploaded_bgm_file, voice_mode


def _render_subtitle_settings(panel, params):
    """渲染字幕设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Subtitle Settings"))
            st.session_state.setdefault(
                "subtitle_enabled_checkbox",
                _saved_ui_bool(
                    "subtitle_enabled",
                    DEFAULT_SUBTITLE_SETTINGS["subtitle_enabled"],
                ),
            )
            params.subtitle_enabled = st.checkbox(
                tr("Enable Subtitles"),
                key="subtitle_enabled_checkbox",
            )
            _set_runtime_config("ui", "subtitle_enabled", params.subtitle_enabled)
            subtitle_settings_disabled = not params.subtitle_enabled

            # === LRC 歌词精准对齐 (Diana 8/8 老杨拍板) ===
            # 勾选后上传 LRC 文件, 字幕按歌词时间戳精准对齐 (不用 TTS/Whisper 推断)
            st.session_state.setdefault(
                "subtitle_use_lrc_checkbox", False
            )
            use_lrc = st.checkbox(
                tr("Use LRC Subtitles"),
                key="subtitle_use_lrc_checkbox",
                disabled=subtitle_settings_disabled,
                help=tr("Use LRC Subtitles Help"),
            )
            lrc_upload_key = "subtitle_lrc_uploader"
            if use_lrc:
                uploaded_lrc = st.file_uploader(
                    tr("Upload LRC File"),
                    type=["lrc"],
                    key=lrc_upload_key,
                    disabled=subtitle_settings_disabled,
                    help=tr("Upload LRC File Help"),
                )
                if uploaded_lrc is not None:
                    # 保存上传的 LRC 到 workspace 目录
                    # 老杨 8/8 17:40 bug fix:
                    #   1. utils.root_dir() 返回 str, 不能用 / 运算符
                    #   2. 原 re.sub(r"[^\\w\\-_\\.]", "_") 会把中文文件名换成下划线
                    #      重写为只过滤路径分隔符 / 控制字符, 保留中英文
                    lrc_save_dir = Path(utils.root_dir()) / "storage" / "lrc"
                    lrc_save_dir.mkdir(parents=True, exist_ok=True)
                    # 用文件内容 hash + 原文件名作为保存名
                    import hashlib
                    file_hash = hashlib.md5(uploaded_lrc.getvalue()).hexdigest()[:12]
                    # 只去除路径分隔符 + 控制字符, 保留中文/英文/数字/空格/常见符号
                    safe_name = re.sub(r"[\\/:\*\?\"<>\|\\\x00-\x1f]", "_", uploaded_lrc.name)
                    if not safe_name or safe_name.startswith("."):
                        safe_name = "upload.lrc"
                    saved_lrc_path = lrc_save_dir / f"{file_hash}_{safe_name}"
                    with open(saved_lrc_path, "wb") as f:
                        f.write(uploaded_lrc.getbuffer())
                    st.session_state["subtitle_lrc_path"] = str(saved_lrc_path)
                    st.success(tr("LRC Upload Success").format(name=uploaded_lrc.name))
                    logger.info(f"LRC uploaded: {saved_lrc_path}")
                # 显示已上传的 LRC 路径
                existing_lrc = st.session_state.get("subtitle_lrc_path", "")
                if existing_lrc and Path(existing_lrc).is_file():
                    lrc_size = Path(existing_lrc).stat().st_size
                    st.caption(
                        tr("LRC Current File").format(
                            path=Path(existing_lrc).name, size=lrc_size
                        )
                    )
                    params.lrc_file = existing_lrc
                else:
                    params.lrc_file = None
            else:
                params.lrc_file = None
            font_names = get_all_fonts()
            saved_font_name = config.ui.get(
                "font_name", DEFAULT_SUBTITLE_SETTINGS["font_name"]
            )
            saved_font_name_index = 0
            if saved_font_name in font_names:
                saved_font_name_index = font_names.index(saved_font_name)
            params.font_name = stable_selectbox(
                tr("Font"),
                options=font_names,
                default_value=font_names[saved_font_name_index] if font_names else "",
                key="font_name_select",
                disabled=subtitle_settings_disabled,
            )
            _set_runtime_config("ui", "font_name", params.font_name)

            subtitle_positions = [
                (tr("Top"), "top"),
                (tr("Center"), "center"),
                (tr("Bottom"), "bottom"),
                (tr("Custom"), "custom"),
            ]
            saved_subtitle_position = config.ui.get(
                "subtitle_position", DEFAULT_SUBTITLE_SETTINGS["subtitle_position"]
            )
            saved_position_index = 2
            for i, (_, pos_value) in enumerate(subtitle_positions):
                if pos_value == saved_subtitle_position:
                    saved_position_index = i
                    break
            selected_subtitle_position = stable_selectbox(
                tr("Position"),
                options=[value for _, value in subtitle_positions],
                default_value=subtitle_positions[saved_position_index][1],
                key="subtitle_position_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_positions
                )[value],
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_position = selected_subtitle_position
            _set_runtime_config("ui", "subtitle_position", params.subtitle_position)

            if params.subtitle_position == "custom":
                saved_custom_position = config.ui.get(
                    "custom_position", DEFAULT_SUBTITLE_SETTINGS["custom_position"]
                )
                st.session_state.setdefault(
                    "custom_position_input", str(saved_custom_position)
                )
                custom_position = st.text_input(
                    tr("Custom Position (% from top)"),
                    key="custom_position_input",
                    disabled=subtitle_settings_disabled,
                )
                try:
                    params.custom_position = float(custom_position)
                    if params.custom_position < 0 or params.custom_position > 100:
                        st.error(tr("Please enter a value between 0 and 100"))
                    else:
                        _set_runtime_config(
                            "ui", "custom_position", params.custom_position
                        )
                except ValueError:
                    st.error(tr("Please enter a valid number"))

            # 非中文语言的颜色标签通常比中文更长。为颜色选择器保留适当宽度，
            # 避免标签换行，同时仍给字号滑块保留足够的可操作空间。
            font_cols = st.columns([0.42, 0.58])
            with font_cols[0]:
                saved_text_fore_color = config.ui.get(
                    "text_fore_color", DEFAULT_SUBTITLE_SETTINGS["text_fore_color"]
                )
                st.session_state.setdefault("font_color_picker", saved_text_fore_color)
                params.text_fore_color = st.color_picker(
                    tr("Font Color"),
                    key="font_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "text_fore_color", params.text_fore_color)

            with font_cols[1]:
                saved_font_size = config.ui.get(
                    "font_size", DEFAULT_SUBTITLE_SETTINGS["font_size"]
                )
                st.session_state.setdefault("font_size_slider", saved_font_size)
                params.font_size = st.slider(
                    tr("Font Size"),
                    30,
                    100,
                    key="font_size_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "font_size", params.font_size)

            stroke_cols = st.columns([0.42, 0.58])
            with stroke_cols[0]:
                st.session_state.setdefault(
                    "stroke_color_picker",
                    _saved_ui_color(
                        "stroke_color", DEFAULT_SUBTITLE_SETTINGS["stroke_color"]
                    ),
                )
                params.stroke_color = st.color_picker(
                    tr("Stroke Color"),
                    key="stroke_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_color", params.stroke_color)
            with stroke_cols[1]:
                st.session_state.setdefault(
                    "stroke_width_slider",
                    _saved_ui_number(
                        "stroke_width",
                        DEFAULT_SUBTITLE_SETTINGS["stroke_width"],
                        0.0,
                        10.0,
                    ),
                )
                params.stroke_width = st.slider(
                    tr("Stroke Width"),
                    0.0,
                    10.0,
                    key="stroke_width_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_width", params.stroke_width)

            # 背景开关的本地化名称普遍比颜色标签更长，因此让开关占据略多空间。
            subtitle_bg_cols = st.columns([0.55, 0.45])
            saved_subtitle_background_enabled = config.ui.get(
                "subtitle_background_enabled",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_enabled"],
            )
            st.session_state.setdefault(
                "subtitle_background_enabled_checkbox",
                saved_subtitle_background_enabled,
            )
            with subtitle_bg_cols[0]:
                subtitle_background_enabled = st.checkbox(
                    tr("Enable Subtitle Background"),
                    key="subtitle_background_enabled_checkbox",
                    disabled=subtitle_settings_disabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_enabled",
                subtitle_background_enabled,
            )

            # 背景颜色和圆角样式都从属于字幕背景开关。子控件始终保留在页面中，
            # 父开关关闭时统一禁用，避免一个控件消失而另一个控件禁用造成布局跳动。
            # 颜色值仍保存在 UI 配置中，重新启用背景后可以恢复用户之前的选择；
            # 传给生成服务的参数则设为 False，确保关闭状态不会实际渲染背景。
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_color"],
            )
            st.session_state.setdefault(
                "subtitle_background_color_picker",
                saved_subtitle_background_color,
            )
            with subtitle_bg_cols[1]:
                selected_subtitle_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    key="subtitle_background_color_picker",
                    disabled=subtitle_settings_disabled
                    or not subtitle_background_enabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_color",
                selected_subtitle_background_color,
            )
            params.text_background_color = (
                selected_subtitle_background_color
                if subtitle_background_enabled
                else False
            )

            saved_rounded_subtitle_background = config.ui.get(
                "rounded_subtitle_background",
                DEFAULT_SUBTITLE_SETTINGS["rounded_subtitle_background"],
            )
            # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件但保留原配置，
            # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
            rounded_background_disabled = (
                subtitle_settings_disabled or not subtitle_background_enabled
            )
            st.session_state.setdefault(
                "rounded_subtitle_background_checkbox",
                saved_rounded_subtitle_background,
            )
            selected_rounded_subtitle_background = st.checkbox(
                tr("Rounded Subtitle Background"),
                help=tr("Rounded Subtitle Background Help"),
                disabled=rounded_background_disabled,
                key="rounded_subtitle_background_checkbox",
            )
            params.rounded_subtitle_background = (
                selected_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            )
            if not subtitle_settings_disabled and subtitle_background_enabled:
                _set_runtime_config(
                    "ui",
                    "rounded_subtitle_background",
                    selected_rounded_subtitle_background,
                )

            if video.subtitle_colors_are_indistinguishable(params):
                # 同色配置仍然是合法的用户选择，因此只在字幕设置区域就近提示，
                # 不阻止生成。用户可以根据实际视觉需求决定是否继续。
                st.warning(tr("Subtitle Colors Are Indistinguishable"))

            subtitle_preview_text = params.video_script or params.video_subject
            selected_font_path = os.path.join(font_dir, params.font_name)
            if (
                params.subtitle_enabled
                and subtitle_preview_text
                and not video.subtitle_font_supports_text(
                    selected_font_path, subtitle_preview_text
                )
            ):
                st.warning(tr("Subtitle Font Does Not Support Text"))

            if st.button(
                tr("Restore Default Subtitle Settings"),
                key="restore_default_subtitle_settings",
                icon=":material/restart_alt:",
                on_click=reset_subtitle_settings,
                use_container_width=True,
            ):
                st.toast(tr("Default Subtitle Settings Restored"))


def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    校验生成依赖、提交任务，并渲染日志与成片结果。

    返回本次页面执行是否成功提交了新任务。提交前已经请求非阻塞保存，调用方
    据此跳过页面末尾的重复请求。主脚本必须及时结束，定时 Fragment 才能持续
    刷新进度和任务日志。
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    has_local_materials = bool(
        uploaded_files or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # 用户已重新上传文件，或主动切换了素材来源/音色。此时历史任务的上传依赖
        # 已经得到明确处理，清除标记，避免后续普通生成继续显示旧提示。
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    start_button = st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    render_onboarding_tour()
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        if params.video_source not in [
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "loomloom",
            "openai_image",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        if params.video_source == "openai_image" and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Configure the OpenAI Image Source"))
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # 本地素材为空时继续执行会先产生 TTS/字幕，最后才在素材预处理阶段失败。
            # 在任务启动前拦截，可以避免无意义的 API 调用和中间文件。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # 上传音频是用户显式选择的配音方式，缺少文件时不能静默退回 TTS。
            # 在任务启动前拦截，避免产生与用户选择不一致的成片。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # 历史自定义音频不能自动回填。用户尚未重新上传且也没有主动更换音色时，
            # 必须阻止静默退回 TTS，否则重新生成的结果会与原任务语音不一致。
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {str(exc)}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {str(exc)}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # 保存成功后只把文件名写入任务参数。视频服务会在两个 BGM 白名单
            # 目录中重新解析，避免把服务器绝对路径持久化或展示给用户。
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # 0 音量时视频服务不会使用任何 BGM，因此不再把已经预览的上传文件
            # 持久化到 storage。用户之后调高音量时可直接再次点击生成完成保存。
            params.bgm_file = ""

        if uploaded_audio_file:
            task_dir = utils.task_dir(task_id)
            try:
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            with open(custom_audio_path, "wb") as f:
                f.write(uploaded_audio_file.getbuffer())
            params.custom_audio_file = custom_audio_path
            # 老杨 8/8 21:09: 高潮段独立 MV - 从 session_state 拿 audio_clip_range_start/end
            _audio_clip_range_start = st.session_state.get("audio_clip_range_start")
            _audio_clip_range_end = st.session_state.get("audio_clip_range_end")
            if _audio_clip_range_start is not None and _audio_clip_range_end is not None:
                params.audio_clip_range_start = float(_audio_clip_range_start)
                params.audio_clip_range_end = float(_audio_clip_range_end)
            else:
                params.audio_clip_range_start = None
                params.audio_clip_range_end = None
            # 老杨 8/8 21:31: 按段落拼接 - 从 session_state 拿 mv_plan / mv_features
            _mv_plan = st.session_state.get("_mv_current_plan")
            _mv_features = st.session_state.get("_mv_current_features")
            _use_seg = st.session_state.get("use_segmented_concat", False)
            if _use_seg and _mv_plan is not None and _mv_features is not None:
                params.use_segmented_concat = True
                params.mv_plan = _mv_plan
                params.mv_features = _mv_features
                logger.info(
                    f"segmented_concat: {len(_mv_plan.get('video_prompts', []))} prompts, "
                    f"{len(_mv_features.get('sections', []))} sections"
                )
            else:
                params.use_segmented_concat = False
                params.mv_plan = None
                params.mv_features = None

        if uploaded_files:
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
            params.video_materials = []
            persisted_local_materials = []
            for file in uploaded_files:
                try:
                    file_path = _build_uploaded_file_path(
                        file,
                        local_videos_dir,
                        LOCAL_MATERIAL_EXTENSIONS,
                        "material",
                    )
                except ValueError:
                    _remove_active_generation_task(task_id)
                    st.error(tr("Unsupported Upload File Type"))
                    st.stop()
                with open(file_path, "wb") as f:
                    f.write(file.getbuffer())
                    m = MaterialInfo()
                    m.provider = "local"
                    m.url = file_path
                    params.video_materials.append(m)
                    persisted_local_materials.append(
                        {
                            "provider": m.provider,
                            "url": m.url,
                            "duration": m.duration,
                        }
                    )
            # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local" and st.session_state["local_video_materials"]
        ):
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material_entry in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material_entry.get("provider", "local")
                m.url = material_entry.get("url", "")
                m.duration = material_entry.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # 试听缓存只存在当前 Streamlit 会话。提交前把音频写入目标任务目录，
            # 后台线程随后只读取任务自己的文件；即使页面 rerun、浏览器关闭或
            # 用户试听其它音色，也不会影响已经入队的生成任务。
            preview_audio_file = os.path.join(
                utils.task_dir(task_id),
                "audio.mp3",
            )
            with open(preview_audio_file, "wb") as file:
                file.write(reusable_voice_preview.pop("audio_bytes"))
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
            )
            if loomloom_video_request is not None:
                # 一个报价只允许提交一次。后台请求自带稳定幂等 ID；提交成功后
                # 清除页面报价，下一次生成必须重新询价和确认。
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button


def _apply_pending_mv_audio():
    """消费 _MV_PENDING_APPLY_KEY 队列, 在 widget 实例化前将内容合并到 video_script / video_terms

    老杨 8/8 13:53 bug 修复: dialog button click 只 rerun dialog function, 不能直接
    改 page widget 的 session_state key (会触发 StreamlitAPIException)。
    解法: dialog callback 只写 pending dict, _render_application() 顶部 widget
    实例化前从 pending 读出来合并到 widget key。 (跟 _apply_pending_task_restore 同模式)

    Returns:
        bool: 是否处理了 pending (供 _render_application() 提示 toast)

    老杨 8/8 17:34: 去掉 st.toast() 避免 fragment rerun 乘 2
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

    # 老杨 8/8 17:34: 不用 st.toast() (会调 st.rerun + 乘 2 fragment rerun)
    # 改为 session_state 标志 + top bar 渲染, 50s 后过期
    if script_append and terms_append:
        st.session_state["_mv_apply_message"] = "✅ Applied to both fields"
    elif script_append:
        st.session_state["_mv_apply_message"] = (
            f"✅ Applied mood (v{pending.get('source_version', 0)})"
        )
    elif terms_append and keyword_count:
        # 老杨 8/8 21:09: 高潮段独立 MV - 拿到 chorus_range_start/end
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


def _render_application():
    """按固定顺序渲染顶部栏、弹窗、生成表单和任务结果。"""
    _render_top_bar()

    if st.session_state.get("settings_dialog_open", False):
        _render_settings_dialog()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

    # 老杨 8/8 13:53: 在 video_script / video_terms widget 实例化之前消费音频分析 pending
    # (跟 _apply_pending_task_restore 同模式)
    _apply_pending_mv_audio()

    with st.container(key="main_settings_grid"):
        panel = st.columns(4)
    left_panel = panel[0]
    middle_panel = panel[1]
    audio_panel = panel[2]
    right_panel = panel[3]

    params = VideoParams(video_subject="")
    params.match_materials_to_script = bool(
        st.session_state.get("match_materials_to_script", False)
    )
    _render_script_settings(left_panel, params)

    uploaded_files = _render_video_settings(middle_panel, params)
    uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
        audio_panel, params
    )

    _render_subtitle_settings(right_panel, params)

    generation_submitted = _render_generation_controls(
        params,
        uploaded_files,
        uploaded_audio_file,
        uploaded_bgm_file,
        voice_mode,
    )

    # 生成分支在启动后台线程前已经请求过保存。普通控件交互继续请求非阻塞保存；
    # 如果后台任务正在使用配置，配置层会在任务结束时自动应用并落盘最新值。
    if not generation_submitted:
        _save_runtime_config()


_render_application()
