#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eval CLI — 对一个或多个 step2 final JSON 跑指标 + 输出对比表

用法：
  # 单个文件，输出全指标总览
  python scripts/eval.py path/to/step2.json

  # 多个文件横向对比（legacy vs agentic_v1 vs agentic_v2）
  python scripts/eval.py \
      ../ai-supply/trend2design/山系户外_YK250609-牛仔服.json \
      runs/山系户外_YK250609-牛仔服.json \
      --label legacy --label agentic_v1

  # 落 CSV
  python scripts/eval.py path/to/*.json --csv data/eval_results.csv

  # 只显示变化的指标
  python scripts/eval.py legacy.json agentic.json --only-diff
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 eval 包可 import
sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.runner import eval_step2_json, write_csv
from eval.compare import compare_rows


def main():
    parser = argparse.ArgumentParser(
        description="Step 2 输出指标评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("step2_jsons", nargs="+", help="一个或多个 step2 final JSON 路径")
    parser.add_argument("--label", action="append", default=[],
                        help="每个 JSON 一个 label（顺序对应），未指定则用 stem")
    parser.add_argument("--csv", help="存为 CSV（可选）")
    parser.add_argument("--only-diff", action="store_true",
                        help="对比模式下只显示有变化的指标")
    args = parser.parse_args()

    rows = []
    for i, p in enumerate(args.step2_jsons):
        label = args.label[i] if i < len(args.label) else None
        rows.append(eval_step2_json(Path(p), run_label=label))

    if args.csv:
        path = write_csv(rows, Path(args.csv))
        print(f"[csv] 已写入 {path}", file=sys.stderr)

    print(compare_rows(rows, show_unchanged=not args.only_diff))


if __name__ == "__main__":
    main()
