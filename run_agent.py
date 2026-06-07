#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt-refine-agent — Step 2 Agentic 改款设计 CLI

把旧的单 prompt step2_design.py 拆成 7 节点 agent loop：
  2.1 款式分析  → 2.2 颜色识别(并发) → 2.3 性别规划 → 2.4 单色设计(串行+累积)
  → 2.5 分布审计(纯 Python tool) → 2.6 决策 → 2.7 局部重设计(条件)

每个 LLM 节点的 input/output 落地到 runs/{run_id}/ 目录，支持后续 fork-from-node。

本目录定位：prompt 迭代/调试工作台，独立于生产流水线。
  生产 CLI：ai-supply/tool/step2_design.py    单次大 prompt，与现有 step3_generate.py 直接对接
  迭代 CLI：ai-supply/prompt-refine-agent/run_agent.py    本文件，agent loop 版

用法示例（从 ai-supply 根目录执行）：
  python prompt-refine-agent/run_agent.py \
      --trend-json   "趋势报告/.../*.json" \
      --ref-image    "款图/YK250609-牛仔服.jpg" \
      --color-folder "款图/YK250609-牛仔服" \
      --gender-ratio "男女比接近1:1" \
      --num-designs  1 \
      --dry-run

环境变量:
  OPENAI_API_KEY — OpenAI API Key（也可用 --api-key；自动从 ai-supply/.env 读取）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

# orchestrator 包位于 tool/orchestrator/ —— 把 tool/ 加到 sys.path
sys.path.insert(0, str(Path(__file__).parent / "tool"))

from orchestrator.llm import LLMClient
from orchestrator.orchestrator import Step2Config, Step2Fixture, Step2Run
from orchestrator.prompts import PromptBundle


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def load_env():
    """从项目根目录的 .env 加载 OPENAI_API_KEY 等（如果存在）。"""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def parse_color_images(folder):
    results = []
    pattern = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        m = pattern.match(f.stem)
        if not m:
            results.append({"code": f.stem, "name": f.stem, "path": f})
            continue
        code = m.group(1).strip()
        raw_label = m.group(2).strip()
        label = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw_label).strip()
        if not label:
            label = raw_label
        results.append({"code": code, "name": label, "path": f})
    return results


def main():
    load_env()
    parser = argparse.ArgumentParser(
        description="prompt-refine-agent Step 2 Agentic CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--trend-json", required=True)
    parser.add_argument("--ref-image", required=True)
    parser.add_argument("--color-folder", required=True)
    parser.add_argument("--gender-ratio", default="男女比接近1:1")
    parser.add_argument("--num-designs", type=int, default=1)
    parser.add_argument(
        "--output-folder",
        default=str(Path(__file__).parent / "runs"),
        help="默认 prompt-refine-agent/runs/，与生产 trend2design/ 隔离",
    )
    parser.add_argument(
        "--prompt-root",
        default=str(Path(__file__).parent / "prompts" / "step2"),
        help="step2 子 prompt 目录（默认 prompts/step2/）",
    )
    parser.add_argument(
        "--prompt-versions",
        default="",
        help="如 2.1:v3,2.4:v5",
    )
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--max-tokens", type=int, default=500000)
    parser.add_argument("--color-concurrency", type=int, default=3)
    parser.add_argument("--max-audit-rounds", type=int, default=2)
    parser.add_argument("--a-tier-quota", type=int, default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    trend_json_path = Path(args.trend_json).resolve()
    if not trend_json_path.is_file():
        sys.exit(f"错误：趋势 JSON 不存在 -> {trend_json_path}")
    ref_path = Path(args.ref_image).resolve()
    if not ref_path.is_file():
        sys.exit(f"错误：款式图不存在 -> {ref_path}")
    color_folder = Path(args.color_folder).resolve()
    if not color_folder.is_dir():
        sys.exit(f"错误：色号图文件夹不存在 -> {color_folder}")

    color_items = parse_color_images(color_folder)
    if not color_items:
        sys.exit(f"错误：色号图文件夹中无有效图片 -> {color_folder}")

    versions = {}
    if args.prompt_versions:
        for kv in args.prompt_versions.split(","):
            if ":" not in kv:
                continue
            k, v = kv.split(":", 1)
            versions[k.strip()] = v.strip()

    bundle = PromptBundle(prompt_root=Path(args.prompt_root), versions=versions)

    trend_json = json.loads(trend_json_path.read_text(encoding="utf-8"))
    trend_name = trend_json_path.stem

    fixture = Step2Fixture(
        trend_name=trend_name,
        style_no=ref_path.stem,
        ref_image_path=ref_path,
        color_items=color_items,
        trend_json=trend_json,
        gender_ratio=args.gender_ratio,
        num_designs_k=args.num_designs,
    )

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "dryrun-placeholder")
    if not args.dry_run and api_key == "dryrun-placeholder":
        sys.exit("错误：未设置 OPENAI_API_KEY 且非 dry-run 模式")

    llm = LLMClient(
        api_key=api_key,
        default_model=args.model,
        default_max_tokens=args.max_tokens,
    )

    config = Step2Config(
        model=args.model,
        max_tokens=args.max_tokens,
        color_recognition_concurrency=args.color_concurrency,
        max_audit_rounds=args.max_audit_rounds,
        a_tier_quota=args.a_tier_quota,
        dry_run=args.dry_run,
    )

    output_root = Path(args.output_folder).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    run = Step2Run(
        fixture=fixture,
        bundle=bundle,
        llm=llm,
        output_root=output_root,
        config=config,
        verbose=True,
    )

    print("=" * 60)
    print("Step 2 Agentic 启动")
    print(f"  Run ID    : {run.run_id}")
    print(f"  趋势      : {trend_name}")
    print(f"  款号      : {fixture.style_no}")
    print(f"  色号数    : {len(color_items)}")
    print(f"  性别比    : {args.gender_ratio}")
    print(f"  K         : {args.num_designs}")
    print(f"  模型      : {args.model}  dry_run={args.dry_run}")
    print(f"  Prompt    : {bundle.prompt_root}")
    print(f"  版本组    : {bundle.to_meta_dict()}")
    print(f"  Run 目录  : {run.run_dir}")
    print("=" * 60)

    result = asyncio.run(run.run())

    print()
    print("=" * 60)
    print("Step 2 Agentic 完成")
    print(f"  Run ID         : {result['run_id']}")
    print(f"  最终输出       : {result['final_output_path']}")
    print(f"  审计通过       : {result['audit_passed']}")
    print(f"  审计轮数       : {len(result['audit_history'])}")
    print(f"  耗时           : {result['total_elapsed_ms']/1000:.1f}s")
    print(f"  Trace 目录     : {result['run_dir']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
