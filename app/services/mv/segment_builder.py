#!/usr/bin/env python3
"""
segment_builder.py — Diana 8/8 21:31 拍板

把 plan.video_prompts + audio features.sections 映射成视频生成的 segment 列表

输入:
    - plan: dict, LLM 输出的完整方案
    - features: dict, AudioFeatures.to_dict() 输出
    - audio_clip_range: Optional[Tuple[start, end]] - 高潮段独立 MV 截取区间

输出:
    - List[Segment]: [
        {
            "section_index": 0,
            "label": "前奏",
            "prompt": "english keyword for pexels",
            "style": "中文风格描述",
            "start": 0.0,
            "end": 15.0,
            "duration": 15.0,
        },
        ...
      ]

设计原则:
- 不强制 6 段, 根据 LLM 返回的 video_prompts 数量决定 (通常 4-8 段)
- 段落 start/end 来自 audio features.sections[i]
- 如果 video_prompts 数量 != sections 数量, 按 min 截断
- 如果有 audio_clip_range, 过滤掉区间外的段落 + 裁剪区间边界
- 每个 segment 包含 start/end/duration/prompt/style/label
"""

from typing import Dict, List, Optional, Tuple


def build_segments(
    plan: dict,
    features: dict,
    audio_clip_range: Optional[Tuple[float, float]] = None,
) -> List[Dict]:
    """从 plan + features 构建视频段列表

    Args:
        plan: dict 包含 'video_prompts' 字段 (LLM 输出)
        features: dict 包含 'sections' 字段 (AudioFeatures.to_dict() 输出)
        audio_clip_range: 可选, 高潮段截取区间 (start, end) 秒

    Returns:
        List[dict]: 段落列表, 每个含 section_index/label/prompt/style/start/end/duration
    """
    if not plan:
        return []

    sections = features.get("sections", []) if isinstance(features, dict) else []
    video_prompts = plan.get("video_prompts", [])

    if not video_prompts:
        return []

    # 把 video_prompts 按 section_index 排序
    prompts_by_idx = {}
    for vp in video_prompts:
        idx = vp.get("section_index", 0)
        prompts_by_idx[idx] = vp

    # 按 section 顺序遍历 (audio sections 优先)
    segments = []
    n_sections = len(sections)
    n_prompts = len(prompts_by_idx)

    # 决定总段数 = min(sections 数量, prompts 数量, 默认 6)
    total = min(n_sections, n_prompts) if n_sections > 0 else min(n_prompts, 6)

    if total == 0:
        # 没有任何 sections, 用 prompts 数量构造 (无 start/end 精确值)
        for vp in video_prompts:
            segments.append({
                "section_index": vp.get("section_index", 0),
                "label": vp.get("label", "段落"),
                "prompt": vp.get("prompt", ""),
                "style": vp.get("style", ""),
                "start": 0.0,
                "end": 0.0,
                "duration": 0.0,
            })
    else:
        # 用 audio sections[i] 的 start/end 作为段落时间轴
        for i in range(total):
            sec = sections[i] if i < n_sections else {}
            vp = prompts_by_idx.get(i, video_prompts[i] if i < n_prompts else {})

            start = float(sec.get("start", 0.0))
            end = float(sec.get("end", 0.0))
            duration = max(0.0, end - start)

            segments.append({
                "section_index": i,
                "label": vp.get("label", f"段落{i+1}"),
                "prompt": vp.get("prompt", ""),
                "style": vp.get("style", ""),
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(duration, 2),
            })

    # 如果有 audio_clip_range (高潮段截取), 过滤 + 调整边界
    if audio_clip_range is not None:
        clip_start, clip_end = audio_clip_range
        clip_start = float(clip_start)
        clip_end = float(clip_end)
        filtered = []
        for seg in segments:
            # 段落完全在区间外 → 跳过
            if seg["end"] <= clip_start or seg["start"] >= clip_end:
                continue
            # 段落部分重叠 → 裁剪
            new_start = max(seg["start"], clip_start)
            new_end = min(seg["end"], clip_end)
            filtered.append({
                **seg,
                "start": round(new_start, 2),
                "end": round(new_end, 2),
                "duration": round(max(0.0, new_end - new_start), 2),
            })
        segments = filtered

    return segments


def get_section_keywords(segments: List[Dict]) -> List[str]:
    """把所有 segment 的 prompt 拍平成关键词列表 (用于全曲 search fallback)"""
    out = []
    for seg in segments:
        p = seg.get("prompt", "").strip()
        if p:
            out.append(p)
    return out