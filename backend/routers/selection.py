"""
/api/v1/selection — 选款中心（已确认上架的 SKU 仓库）

  GET    /selection              仓库列表（含上传图静态 URL）
  GET    /selection/image-ids    已入仓 image_id 集合（Gallery 角标用，轻量）
  POST   /selection              批量入仓（生成图；幂等，已在仓的跳过）
  POST   /selection/upload       上传图片入仓（类目+款号必填，不限比例，≤20MB）
  DELETE /selection/{id}         移除——被 look 引用时 409 阻断并返回引用列表

硬约束：Fitting Room 的 look 成员图必须在仓（POST /looks 校验）。
访客只读：写操作不进公网白名单。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.db import (
    delete_selected_style,
    get_selected_style,
    insert_selected_style,
    list_selected_styles,
    looks_referencing_image,
    selected_image_ids,
)
from backend.schemas import SelectionAddRequest, SelectionStyle

router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = ROOT / "data" / "selection_uploads"

_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_UPLOAD = 20 * 1024 * 1024   # 20MB，与拍摄参考上传一致
_VALID_CATEGORY = {"top", "bottom"}


def _row_to_model(row: dict) -> SelectionStyle:
    upload_url = None
    if row.get("source_kind") == "uploaded" and row.get("upload_path"):
        upload_url = f"/static/selection-uploads/{row['upload_path']}"
    return SelectionStyle(
        id=row["id"],
        image_id=row["image_id"],
        source_kind=row.get("source_kind") or "generated",
        category=row.get("category"),
        style_no=row.get("style_no"),
        color_code=row.get("color_code"),
        color_name=row.get("color_name"),
        note=row.get("note"),
        origin=row.get("origin") or "手动选款",
        upload_url=upload_url,
        created_at=row["created_at"],
    )


def _new_id() -> str:
    return f"sel-{int(time.time())}-{uuid.uuid4().hex[:6]}"


@router.get("", response_model=list[SelectionStyle])
async def list_selection():
    return [_row_to_model(r) for r in list_selected_styles()]


@router.get("/image-ids")
async def list_selection_image_ids():
    """轻量接口：Gallery 侧渲染「已入仓」角标用。"""
    return {"image_ids": sorted(selected_image_ids())}


@router.post("")
async def add_selection(req: SelectionAddRequest):
    """批量入仓（生成图）。幂等：已在仓的 image_id 跳过。"""
    if not req.items:
        raise HTTPException(400, "items 为空")
    added, skipped = 0, 0
    for it in req.items:
        if not it.image_id or ":" not in it.image_id:
            raise HTTPException(400, f"image_id 格式不合法：{it.image_id}")
        if it.category is not None and it.category not in _VALID_CATEGORY:
            raise HTTPException(400, f"category 只能是 top/bottom：{it.category}")
        ok = insert_selected_style(
            id=_new_id(),
            image_id=it.image_id,
            source_kind="generated",
            category=it.category,
            style_no=it.style_no,
            color_code=it.color_code,
            color_name=it.color_name,
            origin="手动选款",
        )
        added += 1 if ok else 0
        skipped += 0 if ok else 1
    return {"ok": True, "added": added, "skipped": skipped}


@router.post("/upload", response_model=SelectionStyle)
async def upload_selection(
    file: UploadFile = File(...),
    category: str = Form(...),
    style_no: str = Form(...),
    color_name: str = Form(None),
    note: str = Form(None),
):
    """上传图片入仓。类目 + 款号必填；不限比例；≤20MB。"""
    if category not in _VALID_CATEGORY:
        raise HTTPException(400, "category 只能是 top / bottom")
    if not (style_no or "").strip():
        raise HTTPException(400, "款号必填")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(400, f"不支持的图片格式：{ext}（支持 jpg/png/webp）")
    content = await file.read()
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(400, "图片超过 20MB 上限")

    uid = uuid.uuid4().hex[:12]
    fname = f"{uid}{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / fname).write_bytes(content)

    sel_id = _new_id()
    insert_selected_style(
        id=sel_id,
        image_id=f"upload:{uid}",
        source_kind="uploaded",
        category=category,
        style_no=style_no.strip(),
        color_name=(color_name or "").strip() or None,
        note=(note or "").strip() or None,
        origin="上传",
        upload_path=fname,
    )
    row = get_selected_style(sel_id)
    return _row_to_model(row)


@router.delete("/{sel_id}")
async def remove_selection(sel_id: str):
    """移除（= 撤销选款）。被 look 引用时阻断（409），先拆引用再移除。"""
    row = get_selected_style(sel_id)
    if not row:
        raise HTTPException(404, f"仓库条目 {sel_id} 不存在")
    refs = looks_referencing_image(row["image_id"])
    if refs:
        raise HTTPException(
            409,
            detail={
                "message": f"该款被 {len(refs)} 套 look 引用，先在 Fitting Room 拆掉引用再移除",
                "looks": refs,
            },
        )
    delete_selected_style(sel_id)
    # 上传图顺带删文件（未被引用，删除安全）
    if row.get("source_kind") == "uploaded" and row.get("upload_path"):
        try:
            (UPLOAD_DIR / row["upload_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}
