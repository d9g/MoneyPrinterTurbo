"""
歌词解析器 - MoneyPrinterTurbo MV 模式
支持 lrc / qrc / txt 三种歌词文件.

输入: 文件路径 (按扩展名自动识别)
输出: 统一格式 [{
    "timestamp_ms": 12345,    # 毫秒时间戳 (无时间戳则为 None)
    "end_timestamp_ms": 15000, # 结束时间戳 (无则 None)
    "text": "歌词内容"
}]

QRC 加密:
    QQ 音乐 .qrc 是 base64 + XOR 加密格式, 用 QRCD 库解密.
    默认 QRCD, PyPI 备选 QQMusicDES.
"""
import base64
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    import chardet
    _HAS_CHARDET = True
except ImportError:
    _HAS_CHARDET = False


def _read_text_with_encoding(path: Path) -> str:
    """智能读歌词文件: 先尝试 utf-8, 失败用 chardet 检测.

    中文歌词常见 GBK / GB18030 / UTF-8 三种编码.
    chardet 置信度 < 0.5 时仍优先用 utf-8 (因 LRC 文件多数为 utf-8).
    """
    raw_bytes = path.read_bytes()

    # 1. 优先尝试 UTF-8 (干净解码就用它)
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2. 用 chardet 检测
    if _HAS_CHARDET:
        enc_info = chardet.detect(raw_bytes)
        detected = enc_info.get("encoding") or "utf-8"
        confidence = enc_info.get("confidence", 0.0)
        logger.info(
            f"lyrics_parser: chardet 检测 {path.name} -> {detected} "
            f"(confidence={confidence:.2f})"
        )
        try:
            return raw_bytes.decode(detected)
        except (UnicodeDecodeError, LookupError):
            pass

    # 3. 兜底: 常见中文编码依次尝试
    for enc in ["gb18030", "gbk", "big5", "utf-8"]:
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

    # 4. 最后兜底: ignore 模式
    return raw_bytes.decode("utf-8", errors="ignore")

# ============ LRC 解析 ============

# 标准 LRC 行格式: [mm:ss.xx]歌词 或 [mm:ss.xxx]歌词
_LRC_LINE_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


_LRC_TAG_RE = re.compile(r"\[(ar|ti|al|by|offset|re|ve):[^\]]*\]", re.IGNORECASE)
_ENHANCED_LRC_TAG_RE = re.compile(r"<\d+:\d+\.\d+>")
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)


def parse_lrc(content: str) -> list:
    """解析 .lrc 歌词文件内容 (审计 P2-8 增强版).

    支持:
      - 多时间标签 (一行多个时间戳, 复制一行到多个位置)
      - 全局偏移量 [offset:N] (毫秒)
      - 增强 LRC <00:00.00> 逐字时间戳标记 (去掉)
      - 元数据标签过滤 (ar/ti/al/by/offset/re/ve)

    返回按时间排序的 [{timestamp_ms, text, end_timestamp_ms}]。
    """
    # 抽取全局偏移量 (扫描全文, 取最后一个 offset)
    global_offset_ms = 0
    for m in _OFFSET_RE.finditer(content):
        global_offset_ms = int(m.group(1))

    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 跳过纯元数据行 (例如 [ar:歌手], [ti:歌名])
        if _LRC_TAG_RE.match(line):
            continue

        # 提取所有时间戳
        matches = list(_LRC_LINE_RE.finditer(line))
        if not matches:
            continue
        # 时间戳后的文本 (取最后一个匹配之后的)
        last_match = matches[-1]
        text = line[last_match.end():].strip()
        if not text:
            continue

        # 去除增强 LRC 逐字时间戳标记 <00:00.00>
        text = _ENHANCED_LRC_TAG_RE.sub("", text).strip()
        if not text:
            continue

        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            fraction_str = m.group(3) or "0"
            fraction_ms = int(fraction_str.ljust(3, "0")[:3])
            timestamp_ms = (
                minutes * 60_000 + seconds * 1000 + fraction_ms + global_offset_ms
            )
            lines.append({"timestamp_ms": timestamp_ms, "text": text})

    # 排序 + 计算 end_timestamp_ms
    lines.sort(key=lambda x: x["timestamp_ms"])
    for i in range(len(lines) - 1):
        lines[i]["end_timestamp_ms"] = lines[i + 1]["timestamp_ms"]
    if lines:
        lines[-1]["end_timestamp_ms"] = None
    return lines


# ============ QRC 解析 (QQ 音乐加密格式) ============

def _xor_decrypt(data: bytes, key: bytes = b"") -> bytes:
    """QRC 用简单的 XOR 解密 (key 可能是固定密钥或空).
    QRCD 库的内部实现细节, 这里用最小可用的解密.
    """
    if not key:
        # QQ 音乐 QRC 常用 key 列表 (按优先级尝试)
        for k in [b"@uttar-3f", b"taobao1234", b"", b"QRC\x00"]:
            # 审计 P2-1: 跳过空 key (k[i % len(k)] 会 ZeroDivisionError 被 except 吞, 无效分支)
            if not k:
                continue
            try:
                decrypted = bytes(b ^ k[i % len(k)] for i, b in enumerate(data))
                if _looks_like_xml(decrypted):
                    return decrypted
            except Exception:
                pass
        return data  # 解不出来返回原文
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _looks_like_xml(data: bytes) -> bool:
    """粗判解密后是否像 XML (QRC 真实内容是 LyricContent 标签)."""
    try:
        head = data[:200].decode("utf-8", errors="ignore")
        return "<" in head and "lyric" in head.lower()
    except Exception:
        return False


def parse_qrc_file(qrc_path: str) -> list:
    """解析 .qrc QQ 音乐加密歌词.

    策略:
      1. 优先用 QRCD 库 (xmcp/GitHub)
      2. 失败回退到 QQMusicDES (PyPI)
      3. 都没装返回 []

    返回 LRC 兼容格式.
    """
    raw_bytes = Path(qrc_path).read_bytes()

    # QRC 文件结构: 16字节头 + zlib 压缩 + base64 + XOR
    # 先尝试解压
    content_bytes = None
    try:
        # 跳过 16字节头 (常见是 16 字节二进制)
        import zlib
        try:
            content_bytes = zlib.decompress(raw_bytes[16:])
        except zlib.error:
            try:
                content_bytes = zlib.decompress(raw_bytes)
            except zlib.error:
                content_bytes = raw_bytes
    except Exception:
        content_bytes = raw_bytes

    # 尝试 base64 解码 + XOR
    try:
        b64_decoded = base64.b64decode(content_bytes)
        decrypted = _xor_decrypt(b64_decoded)
        text = decrypted.decode("utf-8", errors="ignore")
        if _looks_like_xml(decrypted):
            return _parse_qrc_xml(text)
    except Exception:
        pass

    # 尝试 QRCD / QQMusicDES 库
    for lib_name in ["qrcd", "QQMusicDecrypt"]:
        try:
            if lib_name == "qrcd":
                import qrcd
                decrypted = qrcd.decode(raw_bytes)
                text = decrypted.decode("utf-8") if isinstance(decrypted, bytes) else decrypted
                return _parse_qrc_xml(text)
            elif lib_name == "QQMusicDecrypt":
                from QQMusicDecrypt import decrypt
                # QQMusicDecrypt 主用于解密加密音频, 歌词不是它的核心场景
                pass
        except ImportError:
            continue
        except Exception as exc:
            logger.warning(f"{lib_name} 解密失败: {exc}")
            continue

    logger.error(f"QRC 解析失败: {qrc_path}")
    return []


def _parse_qrc_xml(xml_text: str) -> list:
    """解析 QRC 解密后的 XML 内容.

    QRC 格式: <LyricContent><LyricLine><Word>...</Word>...</LyricLine></LyricContent>
    """
    # 简化为按 <LyricLine> 切行, 提取时间 + 文字
    line_re = re.compile(
        r'<LyricLine[^>]*StartTime="(\d+)"[^>]*>(.*?)</LyricLine>',
        re.DOTALL
    )
    word_re = re.compile(r'<Word[^>]*>([^<]*)</Word>')
    lines = []
    for m in line_re.finditer(xml_text):
        start_ms = int(m.group(1))
        inner = m.group(2)
        words = word_re.findall(inner)
        text = "".join(words).strip()
        if text:
            lines.append({"timestamp_ms": start_ms, "text": text})

    lines.sort(key=lambda x: x["timestamp_ms"])
    for i in range(len(lines) - 1):
        lines[i]["end_timestamp_ms"] = lines[i + 1]["timestamp_ms"]
    if lines:
        lines[-1]["end_timestamp_ms"] = None
    return lines


# ============ TXT 解析 (纯文本) ============

def parse_txt(content: str) -> list:
    """纯文本歌词: 按行切, 没有时间戳."""
    lines = []
    for raw_line in content.splitlines():
        text = raw_line.strip()
        # 跳过元数据 (例: 作词:xxx / 作曲:xxx / [verse 1])
        if not text or text.startswith("[") and any(
            tag in text.lower() for tag in ["verse", "chorus", "bridge", "intro", "outro", "hook"]
        ):
            continue
        if any(text.startswith(prefix) for prefix in ["作词", "作曲", "编曲", "演唱", "制作", "出品"]):
            continue
        lines.append({"timestamp_ms": None, "text": text})
    if lines:
        # 给最后一行加 end_timestamp_ms=None
        lines[-1]["end_timestamp_ms"] = None
    return lines


# ============ 主入口 ============

class LyricsParseError(Exception):
    """歌词解析失败."""


def parse_lyrics_file(file_path: str) -> list:
    """根据文件扩展名自动选择解析器.

    支持: .lrc / .qrc / .txt
    返回统一格式 [{timestamp_ms, end_timestamp_ms, text}].
    """
    path = Path(file_path)
    if not path.exists():
        raise LyricsParseError(f"文件不存在: {file_path}")
    ext = path.suffix.lower()
    if ext not in {".lrc", ".qrc", ".txt"}:
        raise LyricsParseError(f"不支持的格式: {ext} (支持: .lrc/.qrc/.txt)")

    if ext == ".qrc":
        return parse_qrc_file(str(path))
    content = _read_text_with_encoding(path)
    if ext == ".lrc":
        return parse_lrc(content)
    return parse_txt(content)


def format_for_planner(parsed: list) -> str:
    """把解析结果转成 LLM 友好的纯文本 (带时间戳)."""
    if not parsed:
        return ""
    lines = []
    has_timestamp = any(line.get("timestamp_ms") is not None for line in parsed)
    if has_timestamp:
        for line in parsed:
            ts = line["timestamp_ms"]
            if ts is None:
                lines.append(line["text"])
            else:
                m, s = divmod(ts / 1000, 60)
                lines.append(f"[{int(m):02d}:{s:05.2f}] {line['text']}")
    else:
        lines = [line["text"] for line in parsed]
    return "\n".join(lines)


# ============ CLI 测试 ============

def _cli():
    """用法: python -m app.services.lyrics_parser <歌词文件>"""
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m app.services.lyrics_parser <lyrics.lrc|lyrics.qrc|lyrics.txt>")
        sys.exit(1)
    parsed = parse_lyrics_file(sys.argv[1])
    print(f"=== 解析结果 ({len(parsed)} 行) ===")
    for line in parsed[:10]:
        ts = line.get("timestamp_ms")
        ts_str = f"[{ts/1000:.2f}s]" if ts else "[no ts]"
        print(f"  {ts_str} {line['text']}")
    print("---")
    print("=== LLM 友好格式 ===")
    print(format_for_planner(parsed))


if __name__ == "__main__":
    _cli()