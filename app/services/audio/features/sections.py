"""
audio.features.sections — 段落切分 (前奏/主歌/副歌)
"""
import librosa
import numpy as np

from ..models import SectionInfo


def detect_sections(y, sr, n_sections: int = 6) -> list:
    """段落切分: 用 RMS 突变检测段落边界, 然后按强度分类

    Args:
        y: 音频数组
        sr: 采样率
        n_sections: 目标段落数 (实际可能略少, 取决于音乐结构)

    Returns:
        List[SectionInfo]
    """
    duration = len(y) / sr

    # 1. 计算 RMS 帧能量
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    # 2. 检测段落边界 (RMS 突变点)
    #    用差分找峰值
    if len(rms) < 2:
        return [SectionInfo(0, 0.0, duration, duration, "medium")]

    diff = np.diff(rms)
    threshold = np.percentile(np.abs(diff), 80)  # 前 20% 的突变点
    boundaries = np.where(np.abs(diff) > threshold)[0]

    # 3. 合并相邻边界, 控制段落数 ≈ n_sections
    if len(boundaries) > n_sections - 1:
        # 取能量差异最大的 n_sections - 1 个
        diff_values = np.abs(diff[boundaries])
        top_indices = np.argsort(diff_values)[-(n_sections - 1):]
        boundaries = sorted(boundaries[top_indices])

    # 4. 构造段落列表
    section_times = [0.0] + [float(times[b]) for b in boundaries] + [duration]
    section_times = sorted(set(section_times))

    sections = []
    for i in range(len(section_times) - 1):
        start = section_times[i]
        end = section_times[i + 1]
        seg_rms = rms[
            int(start * sr / hop_length): int(end * sr / hop_length)
        ]
        if len(seg_rms) == 0:
            intensity = "medium"
        else:
            seg_mean = seg_rms.mean()
            # 用全曲 RMS 中位数分类
            global_median = np.median(rms)
            if seg_mean > global_median * 1.5:
                intensity = "high"
            elif seg_mean < global_median * 0.7:
                intensity = "low"
            else:
                intensity = "medium"
        sections.append(SectionInfo(
            index=i,
            start=round(start, 1),
            end=round(end, 1),
            duration=round(end - start, 1),
            intensity=intensity,
        ))

    return sections