#!/usr/bin/env python3
"""
mv_batch_playlist.py — 老杨 8/8 23:51 拍板

扫 /root/MoneyPrinterTurbo/resource/songs/*_L.ogg 目录,
对每首歌跑 7 步流程 (LLM 分析 + 歌词字幕 + 按段落拼接),
串行提交到 main.py (http://127.0.0.1:8080), 输出 final-1.mp4 路径。

老杨 23:48 设计要求:
- 不设置背景音乐 (bgm_type="")
- 字幕用 LRC 歌词 (lrc_file=)
- 视频转场随机 (shuffle / fade_in / slide_in / zoom_in / zoom_out)
- 不用视频文案 (video_script=""), 直接用歌词字幕

老杨 23:51 补充:
- MV 不用视频文案, 直接走歌词字幕

对每首歌 7 步:
Step 1: analyze_audio() → features (BPM/调性/sections/chorus)
Step 2: parse_lrc() → [{timestamp_ms, text, end_timestamp_ms}]
Step 3: MvPlanner.build(features, lyrics) → plan (意境/关键词/video_prompts)
Step 4: 构造 VideoParams (无BGM + LRC字幕 + 随机转场 + 按段落拼接)
Step 5: POST /videos 提交 (params dict) → main.py:8080
Step 6: 轮询 /tasks/{id} 直到 completed
Step 7: 输出 final-1.mp4 路径

使用:
    ~/MoneyPrinterTurbo/.venv/bin/python scripts/mv_batch_playlist.py
    ~/MoneyPrinterTurbo/.venv/bin/python scripts/mv_batch_playlist.py --dry-run
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


# ===== 配置 =====
SONGS_DIR = Path("/root/MoneyPrinterTurbo/resource/songs")
API_BASE = "http://127.0.0.1:8080"
DB_PATH = "storage/mv/mv_intent.db"

# 老杨 23:48: 视频转场在 5 个里随机
TRANSITION_MODES = [
    "Shuffle",
    "FadeIn",
    "SlideIn",
    "ZoomIn",
    "ZoomOut",
]


def analyze_features(audio_path: str) -> Dict[str, Any]:
    """Step 1: 提取音频特征 (BPM/调性/sections/chorus_segments)"""
    from app.services.audio import analyze_audio

    features_obj = analyze_audio(audio_path)
    return features_obj.to_dict(), features_obj.id3_metadata


def parse_lyrics(lrc_path: Path) -> str:
    """Step 2: 解析 LRC 歌词为纯文本 (按段落切片)"""
    from app.services.lyrics_parser import parse_lrc

    content = lrc_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_lrc(content)
    # 返回纯文本 (LLM 用) + 结构化 (日志用)
    text_lines = [line["text"] for line in parsed if line.get("text")]
    return "\n".join(text_lines), parsed


def build_plan(features: Dict[str, Any], lyrics_text: str, signature: str) -> Dict[str, Any]:
    """Step 3: LLM 出方案 (意境/关键词/视频prompts)"""
    from app.services.mv_planner import MvPlanner

    planner = MvPlanner(db_path=DB_PATH)
    plan = planner.build(
        audio_features=features,
        lyrics=lyrics_text,
        song_signature=signature,
        duration_seconds=features["duration_seconds"],
    )
    return plan


def build_params(
    plan: Dict[str, Any],
    features: Dict[str, Any],
    ogg_path: Path,
    lrc_path: Path,
    transition: str,
) -> Dict[str, Any]:
    """Step 4: 构造 VideoParams dict"""
    # 老杨 8/8 schema 拍板:
    # - mood_summary (中文意境描述)
    # - theme_keywords_en (英文搜索词 -> pexels 用)
    # - theme_keywords_cn (中文意境词)
    # - color_palette (调色)
    # - video_prompts (按段落视频 prompt)
    search_terms = plan.get("theme_keywords_en", []) or plan.get("search_terms", [])
    if isinstance(search_terms, str):
        search_terms = [t.strip() for t in search_terms.split(",") if t.strip()]

    video_prompts = plan.get("video_prompts", []) or []

    params = {
        # 基础
        "video_subject": (plan.get("mood_summary") or plan.get("mood") or ogg_path.stem)[:200],
        # 老杨 23:51: MV 不用视频文案, 直接走歌词字幕
        "video_script": "",
        "video_terms": search_terms,
        "video_aspect": "9:16",
        "video_concat_mode": "random",
        "video_transition_mode": transition,
        "video_clip_duration": 3,
        "video_clip_speed": 1.0,
        "video_count": 1,
        "video_source": "pexels",
        "video_language": "",
        # 老杨 23:48: 不设置背景音乐
        "bgm_type": "",
        "bgm_volume": 0.0,
        # 老杨 23:48/23:51: 字幕用 LRC 歌词
        "subtitle_enabled": True,
        "lrc_file": str(lrc_path),
        "subtitle_position": "bottom",
        "font_name": "MicrosoftYaHeiBold.ttc",
        "font_size": 60,
        "text_fore_color": "#FFFFFF",
        "text_background_color": True,
        "stroke_color": "#000000",
        "stroke_width": 1.5,
        # 自定义音频 (OGG)
        "custom_audio_file": str(ogg_path),
        # 老杨 8/8 21:31: 按段落拼接
        "use_segmented_concat": True,
        "mv_plan": plan,
        "mv_features": features,
        # 性能
        "n_threads": 2,
        "paragraph_number": 1,
    }
    return params


def submit_task(api_base: str, params: Dict[str, Any]) -> str:
    """Step 5: POST /api/v1/videos 提交任务 (main.py 的实际路径)

    老杨 8/8 main.py 路由: TaskVideoRequest(VideoParams) 直接接 body
    所以 body 就是 params dict, 不是 {"params": ...} 包裹
    """
    url = f"{api_base.rstrip('/')}/api/v1/videos"
    print(f"  📤 POST {url}")
    # 老杨 8/8 fix: body 直接是 params dict (TaskVideoRequest 继承 VideoParams)
    resp = requests.post(url, json=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # 响应包装: {"status": 200, "data": {...}, "message": "..."}
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        task_id = data["data"].get("task_id")
    else:
        task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"no task_id in response: {data}")
    return task_id


def poll_task(
    api_base: str,
    task_id: str,
    timeout_s: int = 7200,
    poll_interval: float = 10.0,
    max_404_retries: int = 3,
) -> Dict[str, Any]:
    """Step 6: 轮询 /api/v1/tasks/{id} 直到 completed (审计 D 修复)

    老杨 8/9 07:59 拍板修复:
    - 之前 HTTPError/404 被 except Exception 抓住 → 死循环到 timeout_s
    - task 在 main.py 内存中丢失 (重启 / 不同进程) 时, poll 永远 404, 傻等 2 小时才报错
    - 正确做法: 连续 max_404_retries 次 404 立即报错, 不傻等

    Args:
        api_base: API 基础 URL (例: http://127.0.0.1:8080)
        task_id: 提交时返回的 task UUID
        timeout_s: 总超时秒数 (默认 7200 = 2 小时)
        poll_interval: 轮询间隔秒数 (默认 10s)
        max_404_retries: 连续 404 多少次后立即报错 (默认 3)
    """
    url = f"{api_base.rstrip('/')}/api/v1/tasks/{task_id}"
    start = time.time()
    last_state = None
    consecutive_404 = 0
    consecutive_other_err = 0

    print(f"  📡 poll start: url={url}, timeout={timeout_s}s, poll_interval={poll_interval}s")
    print(f"  📡 max_404_retries={max_404_retries} (连续 {max_404_retries} 次 404 → 立即报错, 不傻等)")

    while True:
        elapsed = time.time() - start
        if elapsed > timeout_s:
            print(f"  ❌ [{elapsed:>5.0f}s] TIMEOUT after {timeout_s}s, last_state={last_state}")
            raise TimeoutError(
                f"task {task_id} timeout after {elapsed:.0f}s "
                f"(last_state={last_state}, consecutive_404={consecutive_404})"
            )

        poll_start = time.time()
        try:
            resp = requests.get(url, timeout=10)
            http_latency = time.time() - poll_start

            # ---- 关键修复: 404 单独计数, 不死循环 ----
            if resp.status_code == 404:
                consecutive_404 += 1
                consecutive_other_err = 0
                print(
                    f"  ⚠️  [{elapsed:>5.0f}s] HTTP 404 task not found "
                    f"({consecutive_404}/{max_404_retries}) latency={http_latency:.2f}s "
                    f"→ task 在 main.py 内存中丢失 (重启 / 不同进程?)"
                )
                if consecutive_404 >= max_404_retries:
                    raise RuntimeError(
                        f"task {task_id} 连续 {consecutive_404} 次 HTTP 404, "
                        f"task 在 main.py 内存中不存在, 不再继续轮询. "
                        f"可能原因: main.py 重启 / 任务在另一个进程 / 任务从未提交成功."
                    )
                # 第一次 404 等待 30s (给 main.py 启动时间), 后续 5s
                wait = 30 if consecutive_404 == 1 else 5
                print(f"  ⏳ [{elapsed:>5.0f}s] 404 后等待 {wait}s 再试...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            # 成功: 重置所有错误计数
            if consecutive_404 > 0 or consecutive_other_err > 0:
                print(f"  ✅ [{elapsed:>5.0f}s] 恢复成功 (前累计 404={consecutive_404}, err={consecutive_other_err})")
            consecutive_404 = 0
            consecutive_other_err = 0

        except requests.exceptions.HTTPError as exc:
            # 非 404 的 HTTP 错误 (500/502/503 等) - 短重试
            consecutive_other_err += 1
            http_latency = time.time() - poll_start
            print(
                f"  ⚠️  [{elapsed:>5.0f}s] HTTP {exc.response.status_code if exc.response else '?'} "
                f"({consecutive_other_err}×) latency={http_latency:.2f}s, retry in 5s"
            )
            if consecutive_other_err >= 10:
                raise RuntimeError(
                    f"task {task_id} 连续 {consecutive_other_err} 次 HTTP 错误, 放弃"
                )
            time.sleep(5)
            continue
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            # 连接失败 / 超时 - main.py 可能没启动
            consecutive_other_err += 1
            print(
                f"  ⚠️  [{elapsed:>5.0f}s] 连接失败 ({consecutive_other_err}×) "
                f"{type(exc).__name__}: {exc}, retry in 5s"
            )
            if consecutive_other_err >= 10:
                raise RuntimeError(
                    f"task {task_id} 连续 {consecutive_other_err} 次连接失败, main.py 可能没启动"
                )
            time.sleep(5)
            continue
        except RuntimeError:
            raise
        except Exception as exc:
            # 兜底 (JSON 解析错误等)
            consecutive_other_err += 1
            print(f"  ⚠️  [{elapsed:>5.0f}s] poll 异常 ({consecutive_other_err}×) {type(exc).__name__}: {exc}, retry in 5s")
            if consecutive_other_err >= 10:
                raise RuntimeError(f"task {task_id} 连续 {consecutive_other_err} 次异常, 放弃")
            time.sleep(5)
            continue

        # ---- 正常分支: 解析 task 状态 ----
        state = data.get("state", "unknown")
        progress = data.get("progress", 0)
        if state != last_state:
            print(f"  ⏳ [{elapsed:>5.0f}s] state={state} progress={progress:.1f}%")
            last_state = state

        if state == "completed":
            print(f"  ✅ [{elapsed:>5.0f}s] completed!")
            return data
        if state == "failed":
            err = data.get("error", "unknown")
            print(f"  ❌ [{elapsed:>5.0f}s] failed: {err}")
            raise RuntimeError(f"task {task_id} failed: {err}")

        time.sleep(poll_interval)


def find_final_mp4(task_id: str) -> Optional[str]:
    """Step 7: 找 final-*.mp4 路径"""
    from app.utils import utils

    task_dir = utils.task_dir(task_id)
    if not task_dir:
        return None
    for f in Path(task_dir).glob("final-*.mp4"):
        if f.exists() and f.stat().st_size > 0:
            return str(f)
    return None


def compute_signature(features: Dict[str, Any]) -> str:
    """跟 webui 一致: 三层签名 (BPM/key/duration + spectral + sections)"""
    # 简化版, 不调 _compute_song_signature_for_upload (那是 webui 内部)
    # 用 features 的核心特征拼一个临时 signature 即可
    sig_parts = [
        str(int(features.get("duration_seconds", 0))),
        str(features.get("tempo", {}).get("bpm", 0)),
        str(features.get("key_info", {}).get("key", "")),
        str(len(features.get("sections", []))),
    ]
    return ":".join(sig_parts)


def process_one_song(ogg_path: Path, dry_run: bool = False) -> Optional[str]:
    """单首歌 7 步流程"""
    print(f"\n{'=' * 70}")
    print(f"🎵 {ogg_path.name}")
    print(f"{'=' * 70}")

    lrc_path = ogg_path.with_suffix(".lrc")
    if not lrc_path.exists():
        print(f"  ❌ LRC 文件不存在: {lrc_path}, 跳过")
        return None

    t0 = time.time()

    # Step 1: 音频分析
    print("\n[Step 1/7] analyze_audio() ...")
    features, id3_meta = analyze_features(str(ogg_path))
    print(f"  ✅ duration={features['duration_seconds']:.1f}s, "
          f"BPM={features.get('tempo', {}).get('bpm', 0):.1f}, "
          f"key={features.get('key_info', {}).get('key', '?')}, "
          f"sections={len(features.get('sections', []))}")
    print(f"  ✅ chorus_segments={len(features.get('chorus_segments', []))}")
    if id3_meta:
        print(f"  🎤 ID3: artist={id3_meta.artist}, title={id3_meta.title}")

    # Step 2: LRC 解析
    print("\n[Step 2/7] parse_lrc() ...")
    lyrics_text, parsed_lyrics = parse_lyrics(lrc_path)
    print(f"  ✅ 解析 {len(parsed_lyrics)} 行歌词, 文本 {len(lyrics_text)} 字符")

    # Step 3: LLM 出方案
    print("\n[Step 3/7] MvPlanner.build() (LLM) ...")
    signature = compute_signature(features)
    plan = build_plan(features, lyrics_text, signature)
    print(f"  ✅ plan source={plan.get('_source')}, latency={plan.get('_latency_ms', 0)}ms")
    print(f"  🎨 mood_summary: {(plan.get('mood_summary') or plan.get('mood') or '')[:100]}")
    print(f"  🔑 keywords (en): {plan.get('theme_keywords_en', [])[:5]}")
    print(f"  🎬 video_prompts: {len(plan.get('video_prompts', []))} 段")

    # Step 4: 构造 params
    transition = random.choice(TRANSITION_MODES)
    print(f"\n[Step 4/7] 构造 params (transition={transition}) ...")
    params = build_params(plan, features, ogg_path, lrc_path, transition)
    print(f"  ✅ bgm_type={params['bgm_type']!r} (空=无BGM)")
    print(f"  ✅ lrc_file={Path(params['lrc_file']).name}")
    print(f"  ✅ use_segmented_concat={params['use_segmented_concat']}")

    if dry_run:
        print(f"\n[DRY-RUN] 跳过 Step 5-7")
        print(f"  would POST params keys: {list(params.keys())}")
        return None

    # Step 5: 提交
    print(f"\n[Step 5/7] POST /api/v1/videos ...")
    task_id = submit_task(API_BASE, params)
    print(f"  ✅ task_id={task_id}")

    # Step 6: 轮询
    print(f"\n[Step 6/7] 轮询 ...")
    result = poll_task(API_BASE, task_id, timeout_s=7200, poll_interval=15.0)
    print(f"  ✅ task completed: state={result.get('state')}")

    # Step 7: 找 final
    print(f"\n[Step 7/7] 找 final-*.mp4 ...")
    final_path = find_final_mp4(task_id)
    elapsed = time.time() - t0
    if final_path:
        size_mb = Path(final_path).stat().st_size / 1024 / 1024
        print(f"  ✅ {final_path} ({size_mb:.1f} MB)")
        print(f"\n⏱️  总耗时: {elapsed:.0f} 秒 ({elapsed/60:.1f} 分钟)")
        return final_path
    else:
        print(f"  ❌ task completed 但 final-*.mp4 不存在")
        print(f"     task_dir: storage/tasks/{task_id}/")
        return None


def main():
    parser = argparse.ArgumentParser(description="批量跑 MV (LLM + LRC + 按段落拼接)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只跑 Step 1-4, 不提交任务",
    )
    parser.add_argument(
        "--songs-dir",
        type=str,
        default=str(SONGS_DIR),
        help="歌曲目录",
    )
    parser.add_argument(
        "--api",
        type=str,
        default=API_BASE,
        help="FastAPI 地址",
    )
    args = parser.parse_args()

    songs_dir = Path(args.songs_dir)
    api_base = args.api

    # 找所有 *_L.ogg
    songs = sorted(songs_dir.glob("*_L.ogg"))
    if not songs:
        print(f"❌ 目录 {songs_dir} 里没有 *_L.ogg 文件")
        sys.exit(1)

    print(f"🎬 mv_batch_playlist.py")
    print(f"   songs_dir: {songs_dir}")
    print(f"   api: {api_base}")
    print(f"   found: {len(songs)} 首歌")
    for s in songs:
        print(f"   - {s.name} ({s.stat().st_size / 1024:.0f} KB)")
    print(f"   dry_run: {args.dry_run}")

    results = []
    for idx, ogg in enumerate(songs, 1):
        print(f"\n\n{'#' * 70}")
        print(f"# [{idx}/{len(songs)}] {ogg.name}")
        print(f"{'#' * 70}\n")
        try:
            final = process_one_song(ogg, dry_run=args.dry_run)
            results.append((ogg.name, final))
        except Exception as exc:
            print(f"❌ {ogg.name} 失败: {exc}")
            import traceback
            traceback.print_exc()
            results.append((ogg.name, None))
            # 不中断, 继续下一首
            continue

    # 汇总
    print(f"\n\n{'=' * 70}")
    print(f"📊 汇总 ({len(results)} 首歌)")
    print(f"{'=' * 70}")
    for name, final in results:
        status = "✅" if final else "❌"
        path = final if final else "(未生成)"
        print(f"  {status} {name}")
        print(f"     → {path}")

    success = sum(1 for _, f in results if f)
    print(f"\n成功: {success}/{len(results)}")


if __name__ == "__main__":
    main()