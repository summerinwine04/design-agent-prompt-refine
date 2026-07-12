"""
/api/v1/prompts — 子 prompt 文件 + 版本管理

  GET    /prompts                        所有子节点 + 各自版本树
  GET    /prompts/{node_id}              某节点的当前 prompt（system + user）
  GET    /prompts/{node_id}/versions/{v} 某节点指定版本
  POST   /prompts/{node_id}/versions     存为新版本
  GET    /prompts/{node_id}/diff?v1=&v2= 两版本 diff
  GET    /prompts/{node_id}/preview?fixture_id=  用 fixture 渲染 user prompt 预览
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.schemas import PromptUpdateRequest, PromptVersion

router = APIRouter()

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts" / "step2"

# 子节点 → 当前 prompt 文件名（与 orchestrator/prompts.py 中 DEFAULT_PROMPT_FILES 对齐）
NODE_FILES = {
    # v4 节点映射（旧 04_single_color_design / 04b_redesign 保留为 v3 历史快照不删，不在此映射）
    "2.1": "01_style_analysis.md",
    "2.2": "02_color_recognition.md",
    "2.3": "03_gender_planning.md",
    "2.4": "04_topic_selection.md",
    "2.5": "05_single_color_design.md",
    "2.8": "05b_redesign_constraint.md",
    # v5 CONVERGE 图（强单主题 / 上下装成套）专用节点
    "2.4s": "04s_single_topic_selection.md",
    "2.4.5": "045_pattern_blueprint.md",
    "2.5L": "05L_look_variant_design.md",
}


@router.get("")
async def list_prompts() -> dict:
    """列出 5 个子节点 + 各自磁盘上所有可见版本。"""
    out = {}
    for node_id, fname in NODE_FILES.items():
        versions = _list_versions(node_id)
        out[node_id] = {
            "current_file": fname,
            "versions": versions,
        }
    return out


@router.get("/{node_id}")
async def get_current_prompt(node_id: str) -> dict:
    return _read_prompt(node_id, version="current")


@router.get("/{node_id}/versions/{version}")
async def get_versioned_prompt(node_id: str, version: str) -> dict:
    return _read_prompt(node_id, version=version)


@router.post("/{node_id}/versions")
async def save_prompt_version(node_id: str, req: PromptUpdateRequest) -> dict:
    """
    把 new_content 落盘：
      - save_as 为空 → 覆盖 current 文件（高危，前端应警告）
      - save_as = "v3" → 写 01_style_analysis.v3.md，不动 current
    """
    if node_id not in NODE_FILES:
        raise HTTPException(404, f"unknown node_id {node_id}")
    base = NODE_FILES[node_id]
    if not req.save_as or req.save_as == "current":
        target = PROMPTS_DIR / base
    else:
        stem, ext = base.rsplit(".", 1)
        target = PROMPTS_DIR / f"{stem}.{req.save_as}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(req.new_content, encoding="utf-8")
    return {"ok": True, "path": str(target), "version": req.save_as or "current"}


@router.get("/{node_id}/diff")
async def diff_versions(node_id: str, v1: str = "current", v2: str = "current") -> dict:
    a = _read_prompt(node_id, version=v1)["raw"]
    b = _read_prompt(node_id, version=v2)["raw"]
    diff = difflib.unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=f"{node_id}@{v1}",
        tofile=f"{node_id}@{v2}",
    )
    return {"diff": "".join(diff)}


# ============================================================================
# 工具
# ============================================================================

def _list_versions(node_id: str) -> list[dict]:
    """扫盘罗列某节点的所有版本（含 current）。"""
    if node_id not in NODE_FILES:
        return []
    base = NODE_FILES[node_id]
    stem, ext = base.rsplit(".", 1)
    versions = [{"version": "current", "file": base, "exists": (PROMPTS_DIR / base).is_file()}]
    pattern = re.compile(rf"^{re.escape(stem)}\.(v\d+)\.{re.escape(ext)}$")
    for f in sorted(PROMPTS_DIR.glob(f"{stem}.v*.{ext}")):
        m = pattern.match(f.name)
        if m:
            versions.append({"version": m.group(1), "file": f.name, "exists": True})
    return versions


def _read_prompt(node_id: str, version: str) -> dict:
    if node_id not in NODE_FILES:
        raise HTTPException(404, f"unknown node_id {node_id}")
    base = NODE_FILES[node_id]
    if version == "current":
        path = PROMPTS_DIR / base
    else:
        stem, ext = base.rsplit(".", 1)
        path = PROMPTS_DIR / f"{stem}.{version}.{ext}"
    if not path.is_file():
        raise HTTPException(404, f"version {version} not found at {path}")
    raw = path.read_text(encoding="utf-8")

    # 解析 system + user template（与 orchestrator/prompts.py load_prompt_file 一致）
    sys_match = re.search(r"═══\s*SYSTEM PROMPT\s*═══\s*\n([\s\S]*?)\n---", raw, re.DOTALL)
    user_match = re.search(r"═══\s*USER PROMPT TEMPLATE\s*═══\s*\n([\s\S]*?)$", raw, re.DOTALL)
    system_prompt = sys_match.group(1).strip() if sys_match else ""
    user_template = ""
    if user_match:
        user_lines = [line for line in user_match.group(1).splitlines() if not line.strip().startswith(">")]
        user_template = "\n".join(user_lines).strip()

    return {
        "node_id": node_id,
        "version": version,
        "path": str(path),
        "raw": raw,
        "system_prompt": system_prompt,
        "user_template": user_template,
        "placeholders": re.findall(r"\{\{[^}]+\}\}", user_template),
    }
