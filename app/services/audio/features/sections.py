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


def detect_chorus_segments(y, sr, top_k: int = 3) -> list:
    """高潮段检测 (Diana 8/8 老杨拍板)

    多维特征识别:
      1. RMS 能量 (咅度突变)
      2. spectral centroid (高频亮度, 高潮高频占比增多)
      3. onset strength (节拍密度, 高潮节拍密)
      4. 综合能量 = RMS*0.5 + centroid*0.3 + onset*0.2

    Args:
        y: 音频数组
        sr: 采样率
        top_k: 返回 top_k 个高潮段 (老杨拍板 精选1/2/3)

    Returns:
        List[ChorusSegment] (按置信度降序, 最多 top_k 个)
        如果一个都识别不到, 返回空列表 [] (不强凑数)

    老杨 8/8 17:34:
      - “有多少高潮就选几个, 识别不出来高潮就不选”
      - top_k 默认 3 (足够 UI 列表展示), 但不限定, 识别出 1-2 个就返回 1-2 个
    """
    from ..models import ChorusSegment

    duration = len(y) / sr
    hop_length = 512

    # 1. RMS 能量
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    # 2. spectral centroid (高频亮度)
    centroid = librosa.feature.spectral_centroid(
        y=y, sr=sr, hop_length=hop_length
    )[0]

    # 3. onset strength (节拍密度)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    # 帧长对齐 (取三者最短)
    n_frames = min(len(rms), len(centroid), len(onset_env))
    rms = rms[:n_frames]
    centroid = centroid[:n_frames]
    onset_env = onset_env[:n_frames]
    times = times[:n_frames]

    # 归一化到 0-1
    def normalize(arr):
        a_min, a_max = arr.min(), arr.max()
        if a_max - a_min < 1e-9:
            return np.zeros_like(arr)
        return (arr - a_min) / (a_max - a_min)

    rms_n = normalize(rms)
    centroid_n = normalize(centroid)
    onset_n = normalize(onset_env)

    # 4. 综合能量 = 0.5 * RMS + 0.3 * centroid + 0.2 * onset
    composite = rms_n * 0.5 + centroid_n * 0.3 + onset_n * 0.2

    # 5. 滑动窗口检测高潮区域 (窗口 = 15 秒, 跳跃 = 5 秒)
    #    典型歌曲高潮段持续 15-30 秒, 窗口取 15 秒足够辨识
    win_frames = int(15 * sr / hop_length)
    hop_frames = int(5 * sr / hop_length)

    if n_frames < win_frames:
        # 歌曲太短, 整首当作高潮
        seg = ChorusSegment(
            index=0,
            start=0.0,
            end=round(duration, 1),
            duration=round(duration, 1),
            confidence=1.0,
            chorus_type="main_chorus",
            label="全曲高潮区",
        )
        return [seg]

    # 6. 用 scipy.signal.find_peaks 找 energy 高峰 (避免按窗口滑求同一高点重复)
    from scipy.signal import find_peaks
    # distance= 20s (两个高潮区间隔不会太近)
    peaks, _ = find_peaks(
        composite,
        distance=int(20 * sr / hop_length),
        prominence=0.05,  # 只取较明显的峰
    )

    # 过滤前 15 秒 (前奏一般高能量但不重复)
    min_start_f = int(15 * sr / hop_length)
    peaks = [p for p in peaks if p >= min_start_f]

    candidates = []
    for p_idx in peaks:
        # 高峰点为中心的 15s 区间
        start_f = max(0, int(p_idx) - win_frames // 2)
        end_f = min(n_frames, int(p_idx) + win_frames // 2)
        if end_f - start_f < win_frames // 2:
            continue
        seg_composite = composite[start_f:end_f]
        score = float(seg_composite.mean())
        peak_composite = float(seg_composite.max())
        start_t = float(times[start_f])
        end_t = float(times[min(end_f - 1, n_frames - 1)])
        # 置信度: 峰均比 + 绝对能量 + 与全曲均值对比
        global_mean = composite.mean()
        relative_score = score / (global_mean + 1e-9)
        confidence = round(min(1.0, peak_composite * 0.4 + relative_score * 0.3 / 3 + 0.3), 3)
        candidates.append({
            "start": round(start_t, 1),
            "end": round(end_t, 1),
            "duration": round(end_t - start_t, 1),
            "score": score,
            "peak": peak_composite,
            "confidence": confidence,
        })

    # 6. 按置信度排序, 去重重叠区间 (IoU > 0.5 认为重叠)
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    selected = []
    for c in candidates:
        is_overlap = False
        for s in selected:
            # 计算区间重叠
            overlap_start = max(c["start"], s["start"])
            overlap_end = min(c["end"], s["end"])
            overlap = max(0, overlap_end - overlap_start)
            iou = overlap / min(c["duration"], s["duration"])
            if iou > 0.5:
                is_overlap = True
                break
        if not is_overlap:
            selected.append(c)
        if len(selected) >= top_k:
            break

    # 7. 转 ChorusSegment 列表
    chorus_segments = []
    for i, c in enumerate(selected):
        # 根据位置判断高潮类型
        rel_pos = c["start"] / duration  # 相对位置 0-1
        if rel_pos < 0.25:
            chorus_type = "pre_chorus"      # 前1/4 -> 预高潮
            label = f"预高潮区"
        elif rel_pos > 0.75:
            chorus_type = "post_chorus"      # 后1/4 -> 后高潮
            label = f"后高潮区"
        else:
            chorus_type = "main_chorus"      # 中间 -> 主高潮
            label = f"主高潮区"
        seg = ChorusSegment(
            index=i,
            start=c["start"],
            end=c["end"],
            duration=c["duration"],
            confidence=c["confidence"],
            chorus_type=chorus_type,
            label=label,
        )
        chorus_segments.append(seg)

    return chorus_segments