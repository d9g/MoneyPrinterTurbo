#!/usr/bin/env python3
"""
mv_nightly.py — 老杨 8/8 21:09 拍板

夜间批量生成 MV 脚本：
- 读歌单 (JSON / YAML / CSV)
- 串行调 main.py API POST /tasks
- 同步加载 .lrc 歌词文件 (歌词当字幕)
- 跑完输出 final-*.mp4 路径

歌单格式 (JSON):
[
  {
    "audio": "/abs/path/to/song.mp3",
    "lrc": "/abs/path/to/song.lrc",  # 可选, 歌词当字幕
    "video_subject": "歌曲意境",
    "search_terms": ["关键词1", "关键词2"],
    "audio_clip_range_start": 50.5,  # 可选, 高潮段起点 (秒)
    "audio_clip_range_end": 75.5,    # 可选, 高潮段终点 (秒)
    "video_aspect": "9:16",
    "video_clip_duration": 3,
  },
  ...
]

使用方法:
    ~/MoneyPrinterTurbo/.venv/bin/python scripts/mv_nightly.py playlist.json
    ~/MoneyPrinterTurbo/.venv/bin/python scripts/mv_nightly.py playlist.json --api http://127.0.0.1:8080
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests


def load_playlist(path: str) -> List[Dict[str, Any]]:
    """Load playlist.json (auto-detect format)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"歌单文件不存在: {path}")
    suffix = p.suffix.lower()
    if suffix in (".json",):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "tasks" in data:
            return data["tasks"]
        if isinstance(data, list):
            return data
        raise ValueError(f"歌单 JSON 格式不对: {path}")
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ImportError("请安装 pyyaml: pip install pyyaml")
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, list) else data.get("tasks", [])
    elif suffix in (".csv",):
        import csv
        with open(p, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)
    else:
        raise ValueError(f"不支持的格式: {suffix}")


def build_params(task: Dict[str, Any]) -> Dict[str, Any]:
    """Build /tasks POST params from a task entry."""
    audio_path = task.get("audio") or task.get("audio_file")
    if not audio_path:
        raise ValueError(f"任务必须有 audio 字段: {task}")
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    lrc_path = task.get("lrc") or task.get("lrc_file")
    if lrc_path and not Path(lrc_path).exists():
        print(f"  ⚠️  LRC 文件不存在: {lrc_path}, 跳过字幕")
        lrc_path = None

    params = {
        "video_subject": task.get("video_subject", Path(audio_path).stem),
        "video_script": task.get("video_script", ""),
        "video_terms": task.get("search_terms") or task.get("video_terms") or [],
        "video_aspect": task.get("video_aspect", "9:16"),
        "video_concat_mode": task.get("video_concat_mode", "random"),
        "video_transition_mode": task.get("video_transition_mode", "None"),
        "video_clip_duration": task.get("video_clip_duration", 3),
        "video_clip_speed": task.get("video_clip_speed", 1.0),
        "video_count": task.get("video_count", 1),
        "video_source": task.get("video_source", "pexels"),
        "video_language": task.get("video_language", ""),
        "voice_name": task.get("voice_name", ""),
        "voice_volume": task.get("voice_volume", 1.0),
        "voice_rate": task.get("voice_rate", 1.0),
        "bgm_type": task.get("bgm_type", "random"),
        "bgm_volume": task.get("bgm_volume", 0.2),
        "video_music_prompt": task.get("video_music_prompt", ""),
        "subtitle_enabled": task.get("subtitle_enabled", True),
        "lrc_file": lrc_path,
        "subtitle_position": task.get("subtitle_position", "bottom"),
        "font_name": task.get("font_name", "MicrosoftYaHeiBold.ttc"),
        "text_fore_color": task.get("text_fore_color", "#FFFFFF"),
        "text_background_color": task.get("text_background_color", False),
        "font_size": task.get("font_size", 60),
        "stroke_color": task.get("stroke_color", "#000000"),
        "stroke_width": task.get("stroke_width", 1.5),
        "n_threads": task.get("n_threads", 2),
        "paragraph_number": task.get("paragraph_number", 1),
        "custom_audio_file": audio_path,
    }

    # 老杨 8/8 21:09: 高潮段独立 MV
    if "audio_clip_range_start" in task and "audio_clip_range_end" in task:
        params["audio_clip_range_start"] = float(task["audio_clip_range_start"])
        params["audio_clip_range_end"] = float(task["audio_clip_range_end"])

    return params


def submit_task(api_base: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """POST /tasks and return task_id + initial state."""
    url = f"{api_base.rstrip('/')}/tasks"
    print(f"  📤 POST {url}: {params['video_subject']}")
    resp = requests.post(url, json={"params": params}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def poll_task(api_base: str, task_id: str, timeout_s: int = 7200) -> Dict[str, Any]:
    """Poll /tasks/{task_id} until completed or failed."""
    url = f"{api_base.rstrip('/')}/tasks/{task_id}"
    start = time.time()
    while True:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state", "unknown")
        progress = data.get("progress", 0)
        elapsed = time.time() - start
        print(f"  ⏳ [{elapsed:>5.0f}s] state={state} progress={progress:.1f}%")
        if state == "completed":
            return data
        if state == "failed":
            err = data.get("error", "unknown")
            raise RuntimeError(f"Task {task_id} failed: {err}")
        if elapsed > timeout_s:
            raise TimeoutError(f"Task {task_id} 超时 ({timeout_s}s)")
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="夜间批量生成 MV (Diana 8/8 21:09)",
    )
    parser.add_argument("playlist", help="歌单 JSON/YAML/CSV 路径")
    parser.add_argument("--api", default="http://127.0.0.1:8080", help="main.py API base URL")
    parser.add_argument("--timeout", type=int, default=7200, help="单任务超时 (秒)")
    parser.add_argument("--dry-run", action="store_true", help="只打印参数, 不真提交")
    args = parser.parse_args()

    print(f"📂 加载歌单: {args.playlist}")
    playlist = load_playlist(args.playlist)
    print(f"  共 {len(playlist)} 个任务")

    results = []
    for i, task in enumerate(playlist, 1):
        print(f"\n========== 任务 {i}/{len(playlist)} ==========")
        try:
            params = build_params(task)
        except (ValueError, FileNotFoundError) as e:
            print(f"  ❌ 参数错误: {e}")
            results.append({"index": i, "task": task.get("audio", "?"), "status": "param_error", "error": str(e)})
            continue

        print(f"  🎵 音频: {params['custom_audio_file']}")
        if params.get("lrc_file"):
            print(f"  📝 LRC: {params['lrc_file']}")
        if params.get("audio_clip_range_start") is not None:
            print(
                f"  📍 高潮段: {params['audio_clip_range_start']:.1f}s-"
                f"{params['audio_clip_range_end']:.1f}s"
            )

        if args.dry_run:
            print(f"  🔍 DRY RUN — 不提交")
            results.append({"index": i, "task": params["video_subject"], "status": "dry_run"})
            continue

        try:
            resp = submit_task(args.api, params)
            task_id = resp.get("task_id") or resp.get("id")
            if not task_id:
                # 审计 P1-4: API 返回 200 但 body 无 task_id (错误响应), 立即报错
                raise RuntimeError(f"API 返回无 task_id: {resp}")
            print(f"  ✅ task_id: {task_id}")
            final = poll_task(args.api, task_id, timeout_s=args.timeout)
            print(f"  🎬 完成: {final}")
            results.append({
                "index": i,
                "task": params["video_subject"],
                "status": "completed",
                "task_id": task_id,
                "final": final,
            })
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            results.append({"index": i, "task": task.get("audio", "?"), "status": "failed", "error": str(e)})
            continue

    # 总结
    print("\n========== 总结 ==========")
    completed = [r for r in results if r.get("status") == "completed"]
    failed = [r for r in results if r.get("status") in ("failed", "param_error")]
    print(f"✅ 成功: {len(completed)}/{len(results)}")
    print(f"❌ 失败: {len(failed)}/{len(results)}")
    print(f"\n详细结果: {json.dumps(results, indent=2, ensure_ascii=False, default=str)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
