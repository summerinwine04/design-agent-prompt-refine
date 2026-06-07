#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke Test —— 验证 backend POST /runs + SSE 联调通

用法（要先在另一个终端起 uvicorn）：
  Terminal 1:  uvicorn backend.main:app --reload --port 8000
  Terminal 2:  python scripts/smoke_test_backend.py [--dry-run | --real]

默认 --dry-run（不烧 token，2 秒跑完，验证骨架）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("请先装 httpx: pip install httpx")


BACKEND = "http://127.0.0.1:8000"
ROOT = Path(__file__).parent.parent


def post_run(*, dry_run: bool) -> dict:
    payload = {
        "trend_json_path":  str(ROOT.parent / "ai-supply" / "趋势报告" / "山系户外童装花型TOP热榜" / "山系户外童装花型TOP热榜.json"),
        "ref_image_path":   str(ROOT.parent / "ai-supply" / "款图" / "YK250609-牛仔服.jpg"),
        "color_folder":     str(ROOT.parent / "ai-supply" / "款图" / "YK250609-牛仔服"),
        "gender_ratio":     "男女比接近1:1",
        "num_designs_k":    1,
        "model":            "gpt-5.5",
        "color_concurrency": 3,
        "max_audit_rounds": 2,
        "dry_run":          dry_run,
        "prompt_bundle":    {"versions": {}},
    }
    print(f"[POST /api/v1/runs] dry_run={dry_run}")
    resp = httpx.post(f"{BACKEND}/api/v1/runs", json=payload, timeout=15.0)
    if resp.status_code >= 400:
        sys.exit(f"POST 失败: HTTP {resp.status_code}\n  body: {resp.text}")
    return resp.json()


def stream_run(run_id: str, *, max_seconds: int = 1500) -> None:
    """订阅 SSE，把事件 pretty-print。"""
    url = f"{BACKEND}/api/v1/runs/{run_id}/stream"
    print(f"[GET {url}] 订阅中...")
    print("-" * 70)

    t0 = time.time()
    event_count = 0
    with httpx.stream("GET", url, timeout=None) as resp:
        if resp.status_code >= 400:
            sys.exit(f"订阅失败: HTTP {resp.status_code}")
        current_event = None
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith(": "):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                event_count += 1
                data_str = line[len("data:"):].strip()
                try:
                    data = json.loads(data_str)
                except Exception:
                    print(f"  [parse error] {data_str[:80]}")
                    continue
                _print_event(current_event or data.get("event", "?"), data)
                if (current_event or "") == "run_finished" or data.get("event") == "run_finished":
                    print("-" * 70)
                    print(f"[完成] {event_count} 个事件，耗时 {time.time()-t0:.1f}s")
                    return
            if time.time() - t0 > max_seconds:
                print(f"[超时] 25 分钟仍未 run_finished，断开")
                return


def _print_event(event_type: str, data: dict) -> None:
    """简洁打印一个事件。"""
    # 短摘要
    if event_type == "run_started":
        print(f"  ▶ run_started  run_id={data.get('run_id')}")
    elif event_type == "node_started":
        nid = data.get("node_id", "?")
        name = data.get("name", "?")
        nt = data.get("node_type", "?")
        ver = data.get("prompt_version", "")
        print(f"  ┌ [{nt:8s}] {nid:<14s} {name}  {ver}")
    elif event_type == "node_completed":
        nid = data.get("node_id", "?")
        toks = data.get("tokens") or {}
        ms = data.get("elapsed_ms", "?")
        tin = toks.get("input", 0)
        tout = toks.get("output", 0)
        print(f"  └ [done    ] {nid:<14s} tokens in={tin} out={tout}  elapsed={ms}ms")
    elif event_type == "node_failed":
        nid = data.get("node_id", "?")
        err = data.get("error", "?")
        print(f"  ✗ [failed  ] {nid:<14s}  {err[:80]}")
    elif event_type == "decision_made":
        cond = data.get("condition", "?")
        branch = data.get("branch", "?")
        print(f"  ◇ decision     {cond}  →  {branch}")
    elif event_type == "node_streaming":
        # 流式 delta 太频繁，不打
        pass
    elif event_type == "run_finished":
        status = data.get("status", "?")
        tt = data.get("total_tokens") or {}
        ms = data.get("total_elapsed_ms", "?")
        print(f"  ◆ run_finished status={status}  tokens={tt.get('input_tokens', '?')}+{tt.get('output_tokens', '?')}  elapsed={ms}ms")
    else:
        print(f"  · [{event_type}] {json.dumps(data, ensure_ascii=False)[:100]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--real", action="store_true", help="禁用 dry-run，跑真 API（贵）")
    args = parser.parse_args()
    if args.real:
        args.dry_run = False

    try:
        run = post_run(dry_run=args.dry_run)
    except httpx.ConnectError:
        sys.exit(
            "无法连接 backend。请先在另一个终端起:\n"
            "  uvicorn backend.main:app --reload --port 8000"
        )
    print(f"[创建成功] run_id = {run['id']}  status = {run['status']}")
    print()

    # 稍微等一下让 orchestrator 启动；缓冲机制会回放早期事件
    time.sleep(0.2)
    stream_run(run["id"])

    # 拉最终详情
    print()
    detail = httpx.get(f"{BACKEND}/api/v1/runs/{run['id']}", timeout=10.0).json()
    print(f"[最终状态]")
    print(f"  status            : {detail.get('status')}")
    print(f"  audit_passed      : {detail.get('audit_passed')}")
    print(f"  audit_rounds      : {detail.get('audit_rounds')}")
    print(f"  total_tokens_in   : {detail.get('total_tokens_in')}")
    print(f"  total_tokens_out  : {detail.get('total_tokens_out')}")
    print(f"  elapsed_ms        : {detail.get('elapsed_ms')}")
    print(f"  trace events      : {len(detail.get('trace') or [])}")


if __name__ == "__main__":
    main()
