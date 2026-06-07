"""
/api/v1/trends — 趋势报告浏览（只读）

prompt-refine-agent 不重复存数据；指向 ai-supply/趋势报告/ 或用户其他位置。
通过 backend/config.py（待加）或环境变量 TRENDS_ROOT 配置数据根。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _trends_root() -> Path:
    """优先环境变量；默认指向 ../ai-supply/趋势报告/。"""
    env = os.environ.get("TRENDS_ROOT")
    if env:
        return Path(env).resolve()
    # 项目根 → 找 sibling ai-supply
    here = Path(__file__).parent.parent.parent.resolve()
    candidate = here.parent / "ai-supply" / "趋势报告"
    return candidate


@router.get("")
async def list_trends():
    root = _trends_root()
    if not root.is_dir():
        return {"root": str(root), "trends": [], "error": "TRENDS_ROOT not found"}
    items = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        json_file = d / f"{d.name}.json"
        items.append({
            "name": d.name,
            "path": str(d),
            "page_count": len(list(d.glob("page_*.png"))),
            "parsed": json_file.is_file(),
            "json_path": str(json_file) if json_file.is_file() else None,
        })
    return {"root": str(root), "trends": items}


@router.get("/{trend_name}")
async def get_trend(trend_name: str):
    root = _trends_root()
    trend_dir = root / trend_name
    if not trend_dir.is_dir():
        raise HTTPException(404, f"trend {trend_name} not found in {root}")
    json_file = trend_dir / f"{trend_name}.json"
    parsed = None
    if json_file.is_file():
        parsed = json.loads(json_file.read_text(encoding="utf-8"))
    pages = sorted(trend_dir.glob("page_*.png"))
    return {
        "name": trend_name,
        "trend_dir": str(trend_dir),
        "pages": [p.name for p in pages],
        "json_path": str(json_file) if parsed else None,
        "parsed": parsed,
    }
