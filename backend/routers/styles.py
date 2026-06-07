"""
/api/v1/styles — 款图库浏览（只读）

指向 ai-supply/款图/ 或 STYLES_ROOT 环境变量。

URL 字段约定（由 main.py 把 /static/styles 静态挂载到 ai-supply/款图/ 实现）：
  - 款图正面图：/static/styles/{style_no}.jpg
  - 色号图    ：/static/styles/{style_no}/{filename}
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _styles_root() -> Path:
    env = os.environ.get("STYLES_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).parent.parent.parent.resolve()
    return here.parent / "ai-supply" / "款图"


@router.get("")
async def list_styles():
    root = _styles_root()
    if not root.is_dir():
        return {"root": str(root), "styles": [], "error": "STYLES_ROOT not found"}
    styles = []
    # 款号正面图 = {款号}.jpg，色号文件夹 = {款号}/
    for f in sorted(root.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            style_no = f.stem
            color_dir = root / style_no
            color_count = 0
            cover_color_url = None
            if color_dir.is_dir():
                color_files = sorted(
                    [p for p in color_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
                )
                color_count = len(color_files)
                if color_files:
                    cover_color_url = f"/static/styles/{style_no}/{color_files[0].name}"
            styles.append({
                "style_no": style_no,
                "ref_image": str(f),
                "ref_image_url": f"/static/styles/{f.name}",       # 款图正面 URL
                "color_folder": str(color_dir) if color_dir.is_dir() else None,
                "color_count": color_count,
                "cover_color_url": cover_color_url,                 # 第一个色号图 URL（封面）
            })
    return {"root": str(root), "styles": styles}


@router.get("/{style_no}")
async def get_style(style_no: str):
    root = _styles_root()
    ref_files = [root / f"{style_no}.jpg", root / f"{style_no}.png"]
    ref_file = next((f for f in ref_files if f.is_file()), None)
    if not ref_file:
        raise HTTPException(404, f"style {style_no} not found")
    color_dir = root / style_no
    colors = []
    pattern = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')
    if color_dir.is_dir():
        for f in sorted(color_dir.iterdir()):
            if f.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            m = pattern.match(f.stem)
            if m:
                code = m.group(1).strip()
                raw_label = m.group(2).strip()
                label = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw_label).strip() or raw_label
            else:
                code, label = f.stem, f.stem
            colors.append({
                "filename": f.name,
                "code": code,
                "name": label,
                "path": str(f),
                "url": f"/static/styles/{style_no}/{f.name}",       # 色号图 URL（前端 img src）
            })
    return {
        "style_no": style_no,
        "ref_image": str(ref_file),
        "ref_image_url": f"/static/styles/{ref_file.name}",
        "color_folder": str(color_dir) if color_dir.is_dir() else None,
        "colors": colors,
    }
