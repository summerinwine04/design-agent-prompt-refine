"""
/api/v1/pattern-library — 印花图案库（只读）

  GET   /pattern-library              列所有方向文件夹（含每文件夹图列表 + 封面）
  GET   /pattern-library/stats        快速统计

PATTERN_LIBRARY_ROOT 从 .env 读，兜底走 ../ai-supply/印花图案库/
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

ROOT = Path(__file__).parent.parent.parent.resolve()

# 允许的图片后缀
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def get_pattern_library_root() -> Path:
    """.env 里的 PATTERN_LIBRARY_ROOT → 兜底 <project_root>/../ai-supply/印花图案库"""
    env = os.environ.get("PATTERN_LIBRARY_ROOT")
    if env:
        return Path(env).resolve()
    return (ROOT.parent / "ai-supply" / "印花图案库").resolve()


def _list_images_in(folder: Path) -> list[str]:
    """返回文件夹下所有图片文件名（按名字排序），非图跳过。"""
    out: list[str] = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.iterdir(), key=lambda p: p.name):
        if p.is_file() and p.suffix.lower() in _IMG_EXTS:
            out.append(p.name)
    return out


def _parse_folder_name(folder_name: str) -> tuple[str, str]:
    """
    命名规范：<母主题>-<子主题方向>
    如 '山系户外童装花型TOP热榜-植物脉络方向' → ('山系户外童装花型TOP热榜', '植物脉络方向')
    没有 '-' 分隔 → 母主题 = 全部，子主题 = 全部（兜底）
    """
    if "-" in folder_name:
        parent, sub = folder_name.split("-", 1)
        return parent.strip(), sub.strip()
    return folder_name, folder_name


# ============================================================================
# 端点
# ============================================================================

@router.get("")
async def list_pattern_library():
    """
    返回图库全部方向文件夹的 manifest：
    {
      "root": "...绝对路径...",
      "available": true,
      "categories": ["山系户外童装花型TOP热榜", ...],   # 母主题去重列表
      "groups": [
        {
          "folder_name": "山系户外童装花型TOP热榜-植物脉络方向",
          "parent_topic": "山系户外童装花型TOP热榜",
          "sub_topic": "植物脉络方向",
          "image_count": 8,
          "cover_url": "/static/pattern-library/<folder>/<first_img>",
          "images": [
            {"filename": "叶片拓印绿满印.png",
             "url": "/static/pattern-library/<folder>/<filename>"}, ...
          ]
        },
        ...
      ],
      "meta": {"group_count": ..., "image_count": ...}
    }
    """
    root = get_pattern_library_root()
    if not root.is_dir():
        return {
            "root": str(root),
            "available": False,
            "error": "PATTERN_LIBRARY_ROOT 目录不存在，设置 .env 中的 PATTERN_LIBRARY_ROOT",
            "categories": [],
            "groups": [],
            "meta": {"group_count": 0, "image_count": 0},
        }

    from urllib.parse import quote as _q

    groups = []
    total_imgs = 0
    for folder in sorted(root.iterdir(), key=lambda p: p.name):
        if not folder.is_dir():
            continue
        # 跳过 . 开头的隐藏文件夹
        if folder.name.startswith("."):
            continue
        imgs = _list_images_in(folder)
        if not imgs:
            continue
        parent_topic, sub_topic = _parse_folder_name(folder.name)
        cover_url = f"/static/pattern-library/{_q(folder.name)}/{_q(imgs[0])}"
        groups.append({
            "folder_name": folder.name,
            "parent_topic": parent_topic,
            "sub_topic": sub_topic,
            "image_count": len(imgs),
            "cover_url": cover_url,
            "images": [
                {"filename": fn, "url": f"/static/pattern-library/{_q(folder.name)}/{_q(fn)}"}
                for fn in imgs
            ],
        })
        total_imgs += len(imgs)

    categories = sorted({g["parent_topic"] for g in groups})
    return {
        "root": str(root),
        "available": True,
        "categories": categories,
        "groups": groups,
        "meta": {
            "group_count": len(groups),
            "image_count": total_imgs,
        },
    }


@router.get("/stats")
async def get_pattern_library_stats():
    """快速统计——不加载图列表。"""
    root = get_pattern_library_root()
    if not root.is_dir():
        return {"available": False, "root": str(root)}
    n_groups = 0
    n_imgs = 0
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        imgs = _list_images_in(folder)
        if imgs:
            n_groups += 1
            n_imgs += len(imgs)
    return {
        "available": True,
        "root": str(root),
        "group_count": n_groups,
        "image_count": n_imgs,
    }
