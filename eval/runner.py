"""
Eval Runner — 把一份 step2 final JSON 转成一行指标 dict
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import all_metrics


def eval_step2_json(json_path: Path, *, run_label: str | None = None) -> dict[str, Any]:
    """对单份 step2 final JSON 跑全部指标。"""
    d = json.loads(Path(json_path).read_text(encoding="utf-8"))
    row = all_metrics(d)
    row["_label"] = run_label or json_path.stem
    row["_source_file"] = str(json_path)
    row["_evaluated_at"] = datetime.now().isoformat(timespec="seconds")
    return row


def write_csv(rows: list[dict], csv_path: Path) -> Path:
    """把多行结果落 CSV。union 所有键，按字母序列出。"""
    if not rows:
        raise ValueError("no rows to write")
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())
    # 元数据列前置，指标列字母序
    meta_cols = [k for k in ["_label", "_source_file", "_evaluated_at", "schema_version"] if k in keys]
    metric_cols = sorted(k for k in keys if k not in meta_cols)
    fieldnames = meta_cols + metric_cols

    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return csv_path


def append_csv(row: dict, csv_path: Path) -> Path:
    """每次 run 追加一行。若 csv 不存在则建。"""
    csv_path = Path(csv_path)
    exists = csv_path.is_file()
    fieldnames = sorted(row.keys())
    with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow(row)
    return csv_path
