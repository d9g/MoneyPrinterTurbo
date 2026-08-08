import json
import os.path
import re
from timeit import default_timer as timer

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from loguru import logger

from app.config import config
from app.utils import utils

model_size = config.whisper.get("model_size", "large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
initial_prompt = config.whisper.get("initial_prompt", "") or None
model = None


def create(audio_file, subtitle_file: str = ""):
    global model
    if WhisperModel is None:
        logger.warning("faster_whisper not available, skipping whisper subtitle generation")
        return ""
    if not model:
        model_path = f"{utils.root_dir()}/models/whisper-{model_size}"
        model_bin_file = f"{model_path}/model.bin"
        if not os.path.isdir(model_path) or not os.path.isfile(model_bin_file):
            model_path = model_size

        logger.info(
            f"loading model: {model_path}, device: {device}, compute_type: {compute_type}"
        )
        try:
            model = WhisperModel(
                model_size_or_path=model_path, device=device, compute_type=compute_type
            )
        except Exception as e:
            logger.error(
                f"failed to load model: {e} \n\n"
                f"********************************************\n"
                f"this may be caused by network issue. \n"
                f"please download the model manually and put it in the 'models' folder. \n"
                f"see [README.md FAQ](https://github.com/harry0703/MoneyPrinterTurbo) for more details.\n"
                f"********************************************\n\n"
            )
            return None

    logger.info(f"start, output file: {subtitle_file}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    segments, info = model.transcribe(
        audio_file,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        **({"initial_prompt": initial_prompt} if initial_prompt else {}),
    )

    logger.info(
        f"detected language: '{info.language}', probability: {info.language_probability:.2f}"
    )

    start = timer()
    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return

        msg = "[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text)
        logger.debug(msg)

        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    for segment in segments:
        words_idx = 0
        words_len = len(segment.words)

        seg_start = 0
        seg_end = 0
        seg_text = ""

        if segment.words:
            is_segmented = False
            for word in segment.words:
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True

                seg_end = word.end
                # If it contains punctuation, then break the sentence.
                seg_text += word.word

                if utils.str_contains_punctuation(word.word):
                    # remove last char
                    seg_text = seg_text[:-1]
                    if not seg_text:
                        continue

                    recognized(seg_text, seg_start, seg_end)

                    is_segmented = False
                    seg_text = ""

                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if not seg_text:
            continue

        recognized(seg_text, seg_start, seg_end)

    end = timer()

    diff = end - start
    logger.info(f"complete, elapsed: {diff:.2f} s")

    idx = 1
    lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            lines.append(
                utils.text_to_srt(
                    idx, text, subtitle.get("start_time"), subtitle.get("end_time")
                )
            )
            idx += 1

    sub = "\n".join(lines) + "\n"
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write(sub)
    logger.info(f"subtitle file created: {subtitle_file}")


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_lines = utils.split_string_by_punctuations(normalized_script)

    corrected = False
    new_subtitle_items = []
    script_index = 0
    subtitle_index = 0

    while script_index < len(script_lines) and subtitle_index < len(subtitle_items):
        script_line = script_lines[script_index].strip()
        subtitle_line = subtitle_items[subtitle_index][2].strip()

        if script_line == subtitle_line:
            new_subtitle_items.append(subtitle_items[subtitle_index])
            script_index += 1
            subtitle_index += 1
        else:
            combined_subtitle = subtitle_line
            start_time = subtitle_items[subtitle_index][1].split(" --> ")[0]
            end_time = subtitle_items[subtitle_index][1].split(" --> ")[1]
            next_subtitle_index = subtitle_index + 1

            while next_subtitle_index < len(subtitle_items):
                next_subtitle = subtitle_items[next_subtitle_index][2].strip()
                if similarity(
                    script_line, combined_subtitle + " " + next_subtitle
                ) > similarity(script_line, combined_subtitle):
                    combined_subtitle += " " + next_subtitle
                    end_time = subtitle_items[next_subtitle_index][1].split(" --> ")[1]
                    next_subtitle_index += 1
                else:
                    break

            if similarity(script_line, combined_subtitle) > 0.8:
                logger.warning(
                    f"Merged/Corrected - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True
            else:
                logger.warning(
                    f"Mismatch - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True

            script_index += 1
            subtitle_index = next_subtitle_index

    # Process the remaining lines of the script.
    while script_index < len(script_lines):
        logger.warning(f"Extra script line: {script_lines[script_index]}")
        if subtitle_index < len(subtitle_items):
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    subtitle_items[subtitle_index][1],
                    script_lines[script_index],
                )
            )
            subtitle_index += 1
        else:
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    "00:00:00,000 --> 00:00:00,000",
                    script_lines[script_index],
                )
            )
        script_index += 1
        corrected = True

    if corrected:
        with open(subtitle_file, "w", encoding="utf-8") as fd:
            for i, item in enumerate(new_subtitle_items):
                fd.write(f"{i + 1}\n{item[1]}\n{item[2]}\n\n")
        logger.info("Subtitle corrected")
    else:
        logger.success("Subtitle is correct")


# ================ LRC 解析 (Diana 8/8 老杨拍板) ================
# LRC 格式:
#   [00:01.23]歌词文本
#   [00:05.67]歌词文本
#   [ar:歌手]
#   [ti:标题]
#   [al:专辑]
#   [offset:0]  (可选: 全局偏移毫秒数)
#
# 支持多时间戳 (同一句多个时间点, 例如合唱): [00:01.23][00:10.45]歌词
# 支持扩展 LRC (含字): [00:01.23]<00:01.45>字<00:01.67>字<00:01.89>字
# 增强 LRC 仅取时间戳 + 整句文本, 忽略逐字时间

def parse_lrc(lrc_text: str) -> list:
    """解析 LRC 文本为 [(time_seconds, text), ...] 列表 (审计 P2-8)

    审计 P2-8 修复: 委托给 lyrics_parser.parse_lrc (合并重复实现)。
    lyrics_parser 版本支持多时间戳 + 全局 offset + 增强 LRC。
    返回格式 [timestamp_ms, text] 转换为 (time_sec, text)。
    """
    from app.services.lyrics_parser import parse_lrc as _parse_lrc_ms
    parsed = _parse_lrc_ms(lrc_text)
    return [(item["timestamp_ms"] / 1000.0, item["text"]) for item in parsed]


def _seconds_to_srt_time(t: float) -> str:
    """转换秒为 SRT 时间戳 HH:MM:SS,mmm"""
    if t < 0:
        t = 0
    hours = int(t // 3600)
    minutes = int((t % 3600) // 60)
    seconds = int(t % 60)
    millis = int(round((t - int(t)) * 1000))
    # 处理 999ms 进位
    if millis >= 1000:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def lrc_to_srt(lrc_text: str, output_path: str, default_duration_sec: float = 3.0) -> int:
    """把 LRC 文本转换为 SRT 文件

    每条字幕结束时间 = 下一条起始时间 (如果没有下一条, 用 default_duration_sec)

    Args:
        lrc_text: LRC 文件内容 (UTF-8)
        output_path: 输出的 .srt 路径
        default_duration_sec: 最后一条字幕的默认时长

    Returns:
        写入的字幕条数
    """
    entries = parse_lrc(lrc_text)
    if not entries:
        logger.warning(f"lrc_to_srt: no valid LRC entries found")
        return 0

    lines = []
    for i, (start_t, text) in enumerate(entries):
        # 结束时间 = 下一条起始 (减去 0.05s 重叠避免同时出现)
        if i + 1 < len(entries):
            end_t = entries[i + 1][0] - 0.05
            if end_t <= start_t:
                end_t = start_t + default_duration_sec
        else:
            end_t = start_t + default_duration_sec

        srt_start = _seconds_to_srt_time(start_t)
        srt_end = _seconds_to_srt_time(end_t)
        lines.append(f"{i + 1}\n{srt_start} --> {srt_end}\n{text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"lrc_to_srt: wrote {len(entries)} subtitles to {output_path}")
    return len(entries)


def lrc_file_to_srt(lrc_path: str, output_path: str, default_duration_sec: float = 3.0) -> int:
    """从 LRC 文件读内容并转换为 SRT 文件"""
    if not os.path.isfile(lrc_path):
        logger.warning(f"lrc_file_to_srt: file not found: {lrc_path}")
        return 0
    # LRC 文件可能是 GBK/UTF-8-BOM/UTF-8, 多编码尝试
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(lrc_path, "r", encoding=encoding) as f:
                content = f.read()
            return lrc_to_srt(content, output_path, default_duration_sec)
        except UnicodeDecodeError:
            continue
    logger.warning(f"lrc_file_to_srt: cannot decode {lrc_path} with utf-8/gbk")
    return 0


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
