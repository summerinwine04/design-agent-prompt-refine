"""
/api/v1/templates — 视觉模板库集成（只读）

  GET   /templates                      读 <TEMPLATES_ROOT>/data/templates-data.json 全量
  GET   /templates/stats                快速统计
  POST  /looks/{id}/shooting-slot/upload  上传本地图到 <TEMPLATES_ROOT>/templates/user_uploads/
  POST  /looks/{id}/shooting-slot/set-template  从模板库选定一张 → 保存到 look
  DELETE /looks/{id}/shooting-slot                清空槽位

TEMPLATES_ROOT 从 .env 读，兜底走 ../视觉模板库。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.db import get_look, update_look

router = APIRouter()                    # 挂在 /api/v1/templates
look_slot_router = APIRouter()          # 挂在 /api/v1/looks/{id}/shooting-slot

ROOT = Path(__file__).parent.parent.parent.resolve()


def get_templates_root() -> Path:
    """.env 里的 TEMPLATES_ROOT → 兜底 <project_root>/../视觉模板库"""
    env = os.environ.get("TEMPLATES_ROOT")
    if env:
        return Path(env).resolve()
    return (ROOT.parent / "视觉模板库").resolve()


def get_user_uploads_dir() -> Path:
    """上传的图存到 <TEMPLATES_ROOT>/templates/user_uploads/（Q2 = 选项 B）"""
    d = get_templates_root() / "templates" / "user_uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================================
# 模板库读取
# ============================================================================

@router.get("")
async def get_templates_data():
    """
    返回视觉模板库的完整 manifest。前端 TemplatePickerModal 消费此接口。
    结构与 templates-data.json 一致：{ meta, tag_dict, groups: [...] }
    """
    root = get_templates_root()
    data_file = root / "data" / "templates-data.json"
    if not data_file.is_file():
        return {
            "error": f"templates-data.json not found at {data_file}",
            "hint": "Set TEMPLATES_ROOT in .env to your 视觉模板库 folder, or run its rebuild_manifest.py",
            "root": str(root),
            "meta": {"categories": []},
            "tag_dict": {"schemas": {}},
            "groups": [],
        }
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"failed to parse templates-data.json: {e}")
    return data


@router.get("/stats")
async def get_templates_stats():
    """快速统计——不加载全部 groups 数据。"""
    root = get_templates_root()
    data_file = root / "data" / "templates-data.json"
    if not data_file.is_file():
        return {"available": False, "root": str(root)}
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "root": str(root)}
    meta = data.get("meta") or {}
    return {
        "available": True,
        "root": str(root),
        "categories": meta.get("categories", []),
        "group_count": meta.get("group_count", len(data.get("groups") or [])),
        "image_count": meta.get("image_count", 0),
        "generated_at": meta.get("generated_at"),
    }


# ============================================================================
# 单个 look 的 shooting_slot 三种操作
# ============================================================================

@look_slot_router.post("/upload")
async def upload_shooting_slot(
    look_id: str,
    file: UploadFile = File(...),
    remark: Optional[str] = Form(None),
):
    """
    上传本地图作为拍摄参考。落盘到 <TEMPLATES_ROOT>/templates/user_uploads/，
    然后更新 look 的 shooting_slot_* 三个字段。
    """
    if not get_look(look_id):
        raise HTTPException(404, f"look {look_id} not found")

    # 校验后缀
    fn = (file.filename or "").lower()
    ext = None
    for e in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        if fn.endswith(e):
            ext = e
            break
    if not ext:
        raise HTTPException(400, "只支持 jpg/jpeg/png/webp/gif")

    body = await file.read()
    if len(body) == 0:
        raise HTTPException(400, "上传文件为空")
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB 上限")

    # 命名：<look_id>-<md5前8位>.ext，避免同 look 反复上传冲突
    hash8 = hashlib.md5(body).hexdigest()[:8]
    safe_name = f"{look_id}-{hash8}{ext}"
    up_dir = get_user_uploads_dir()
    dst = up_dir / safe_name
    try:
        dst.write_bytes(body)
    except OSError as e:
        raise HTTPException(500, f"落盘失败：{e}")

    # 前端可访问 URL — 因为 /static/templates 已经挂了 <TEMPLATES_ROOT>/templates
    url = f"/static/templates/user_uploads/{safe_name}"
    meta = {
        "original_filename": file.filename,
        "size_kb": round(len(body) / 1024, 1),
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if remark:
        meta["remark"] = remark

    update_look(
        look_id,
        shooting_slot_kind="upload",
        shooting_slot_url=url,
        shooting_slot_meta=meta,
    )
    return {"ok": True, "url": url, "meta": meta}


from pydantic import BaseModel as _BM


class SetTemplateRequest(_BM):
    """从视觉模板库选定后回传的 payload。"""
    category: str
    group_name: str
    image_filename: str
    group_tags: dict | None = None
    image_tags: dict | None = None
    remark: str | None = None


@look_slot_router.post("/set-template")
async def set_shooting_slot_template(look_id: str, req: SetTemplateRequest):
    """
    从模板库选定一张图 → 保存到 look。
    URL 组装规则：/static/templates/<category>/<group_name>/<image_filename>
    """
    if not get_look(look_id):
        raise HTTPException(404, f"look {look_id} not found")

    # 组装 URL — 视觉模板库的目录结构是 templates/<类目>/<图组>/<图>
    from urllib.parse import quote
    url = (
        f"/static/templates/{quote(req.category)}"
        f"/{quote(req.group_name)}/{quote(req.image_filename)}"
    )
    meta = {
        "category": req.category,
        "group_name": req.group_name,
        "image_filename": req.image_filename,
        "group_tags": req.group_tags or {},
        "image_tags": req.image_tags or {},
    }
    if req.remark:
        meta["remark"] = req.remark

    update_look(
        look_id,
        shooting_slot_kind="template",
        shooting_slot_url=url,
        shooting_slot_meta=meta,
    )
    return {"ok": True, "url": url, "meta": meta}


@look_slot_router.delete("")
async def clear_shooting_slot(look_id: str):
    """
    清空 shooting_slot。如果是 upload 类型，顺手删本地文件。
    如果是 template 类型只清 look 里的引用，不动源文件。
    """
    row = get_look(look_id)
    if not row:
        raise HTTPException(404, f"look {look_id} not found")

    # 如果是上传的，试着删除本地文件
    if row.get("shooting_slot_kind") == "upload":
        url = row.get("shooting_slot_url") or ""
        # url 形如 /static/templates/user_uploads/xxx.jpg
        if url.startswith("/static/templates/user_uploads/"):
            fn = url.rsplit("/", 1)[-1]
            local = get_user_uploads_dir() / fn
            try:
                if local.is_file():
                    local.unlink()
            except OSError:
                pass  # 删失败不影响清 DB

    # 三个字段一起清（注意 update_look 里 None 表示不动，所以用空字符串 + 直接 SQL？
    # 不行。用 direct SQL 清空三个字段）
    from backend.db import get_conn
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE looks
               SET shooting_slot_kind = NULL,
                   shooting_slot_url = NULL,
                   shooting_slot_meta = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (look_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
