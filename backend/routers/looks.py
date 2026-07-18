"""
/api/v1/looks — Fitting Room：跨任务 look 组套管理

  GET    /looks                        列所有 look
  POST   /looks                        批量新建（Gallery 组套完成后 push）
  GET    /looks/{id}                   单个详情
  PATCH  /looks/{id}                   部分更新
  DELETE /looks/{id}                   删除
  POST   /looks/{id}/duplicate         复制一个新 look（用户想改变体）
  GET    /looks/export                 导出全部为 JSON（浏览器下载）

/api/v1/image-categories — 上装 / 下装归类持久化

  GET    /image-categories             全批映射 image_id → category
  POST   /image-categories             批量 upsert
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from backend.db import (
    delete_look as db_delete_look,
    get_generation_task,
    get_look as db_get_look,
    insert_look as db_insert_look,
    list_image_categories as db_list_image_categories,
    list_looks as db_list_looks,
    update_look as db_update_look,
    upsert_image_categories_bulk,
)
from backend.schemas import (
    ImageCategoryBulkRequest,
    Look,
    LookBulkCreateRequest,
    LooksExportRequest,
    LookUpdate,
)

ROOT = Path(__file__).resolve().parent.parent.parent   # <project_root>/

router = APIRouter()
img_cat_router = APIRouter()


def _row_to_look(row: dict) -> Look:
    """DB row → Pydantic Look（把 tags/shooting_slot_meta JSON string 反序列化）"""
    tags_raw = row.get("tags")
    if tags_raw:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except Exception:
            tags = []
    else:
        tags = []
    # shooting_slot_meta 反序列化
    meta_raw = row.get("shooting_slot_meta")
    meta: dict | None = None
    if meta_raw:
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except Exception:
            meta = None
    return Look(
        id=row["id"],
        name=row["name"],
        top_kind=row.get("top_kind"),
        top_image_id=row.get("top_image_id"),
        top_text=row.get("top_text"),
        bottom_kind=row.get("bottom_kind"),
        bottom_image_id=row.get("bottom_image_id"),
        bottom_text=row.get("bottom_text"),
        tags=tags if isinstance(tags, list) else [],
        shooting_slot_kind=row.get("shooting_slot_kind"),
        shooting_slot_url=row.get("shooting_slot_url"),
        shooting_slot_meta=meta,
        wave_id=row.get("wave_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ============================================================================
# looks
# ============================================================================

@router.get("", response_model=list[Look])
async def list_looks_endpoint(limit: int = 500):
    return [_row_to_look(r) for r in db_list_looks(limit=limit)]


@router.get("/export")
async def export_looks_json():
    """整批导出 JSON，浏览器可另存。"""
    rows = db_list_looks(limit=10000)
    payload = [_row_to_look(r).model_dump() for r in rows]
    return JSONResponse(
        content={
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(payload),
            "looks": payload,
        },
        headers={
            "Content-Disposition": (
                f'attachment; filename="fitting_room_export_'
                f'{time.strftime("%Y%m%d_%H%M%S")}.json"'
            ),
        },
    )


@router.get("/{look_id}", response_model=Look)
async def get_look_endpoint(look_id: str):
    row = db_get_look(look_id)
    if not row:
        raise HTTPException(404, f"look {look_id} not found")
    return _row_to_look(row)


@router.post("", response_model=list[Look])
async def create_looks_bulk(req: LookBulkCreateRequest):
    """一次批量新建（选款中心组套 → 同步到看板的入口）。

    硬约束：look 的成员图必须已在选款中心仓库（selected_styles）。
    """
    if not req.looks:
        return []
    from backend.db import selected_image_ids
    in_stock = selected_image_ids()
    missing = []
    for item in req.looks:
        for img_id, kind in ((item.top_image_id, item.top_kind), (item.bottom_image_id, item.bottom_kind)):
            if kind == "image" and img_id and img_id not in in_stock:
                missing.append(img_id)
    if missing:
        raise HTTPException(
            400,
            f"以下款图未入选款中心仓库，请先选款：{missing[:10]}"
            + (f" 等 {len(missing)} 张" if len(missing) > 10 else ""),
        )
    # 生成 id + 自动命名（基于现有 look 数量 + 起始序号）
    existing_count = len(db_list_looks(limit=10000))
    created_ids: list[str] = []
    ts = int(time.time())
    for i, item in enumerate(req.looks):
        look_id = f"look-{ts}-{uuid.uuid4().hex[:6]}"
        auto_name = f"look-{str(existing_count + i + 1).zfill(2)}"
        db_insert_look(
            id=look_id,
            name=item.name or auto_name,
            top_kind=item.top_kind,
            top_image_id=item.top_image_id,
            top_text=item.top_text,
            bottom_kind=item.bottom_kind,
            bottom_image_id=item.bottom_image_id,
            bottom_text=item.bottom_text,
            tags=item.tags,
        )
        created_ids.append(look_id)
    # 返回刚建的完整对象
    return [_row_to_look(db_get_look(lid)) for lid in created_ids if db_get_look(lid)]


@router.patch("/{look_id}", response_model=Look)
async def update_look_endpoint(look_id: str, req: LookUpdate):
    row = db_get_look(look_id)
    if not row:
        raise HTTPException(404, f"look {look_id} not found")
    db_update_look(
        look_id,
        name=req.name,
        top_kind=req.top_kind,
        top_image_id=req.top_image_id,
        top_text=req.top_text,
        bottom_kind=req.bottom_kind,
        bottom_image_id=req.bottom_image_id,
        bottom_text=req.bottom_text,
        tags=req.tags,
        shooting_slot_kind=req.shooting_slot_kind,
        shooting_slot_url=req.shooting_slot_url,
        shooting_slot_meta=req.shooting_slot_meta,
    )
    row = db_get_look(look_id)
    return _row_to_look(row)


@router.delete("/{look_id}")
async def delete_look_endpoint(look_id: str):
    if not db_get_look(look_id):
        raise HTTPException(404, f"look {look_id} not found")
    db_delete_look(look_id)
    return {"ok": True}


@router.post("/{look_id}/duplicate", response_model=Look)
async def duplicate_look_endpoint(look_id: str):
    src = db_get_look(look_id)
    if not src:
        raise HTTPException(404, f"look {look_id} not found")
    new_id = f"look-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    tags_raw = src.get("tags")
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) and tags_raw else []
    except Exception:
        tags = []
    # Q5 = A：复制 look 时顺带复制 shooting_slot（用户想改自己 detach 就行）
    meta_raw = src.get("shooting_slot_meta")
    slot_meta: dict | None = None
    if meta_raw:
        try:
            slot_meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except Exception:
            slot_meta = None
    db_insert_look(
        id=new_id,
        name=f"{src['name']}-copy",
        top_kind=src.get("top_kind"),
        top_image_id=src.get("top_image_id"),
        top_text=src.get("top_text"),
        bottom_kind=src.get("bottom_kind"),
        bottom_image_id=src.get("bottom_image_id"),
        bottom_text=src.get("bottom_text"),
        tags=tags,
        shooting_slot_kind=src.get("shooting_slot_kind"),
        shooting_slot_url=src.get("shooting_slot_url"),
        shooting_slot_meta=slot_meta,
    )
    return _row_to_look(db_get_look(new_id))


# ============================================================================
# 批量导出到桌面
# ============================================================================

# Windows 保留 + 换行/制表
_INVALID_FN_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _sanitize_filename(s: str, fallback: str = "unknown") -> str:
    """替换文件名中的非法字符，避免 Windows 落盘失败。"""
    s = (s or "").strip()
    s = _INVALID_FN_CHARS.sub("_", s)
    return s or fallback


def _resolve_gallery_image_disk_path(image_url: str, image_path: str) -> Path | None:
    """
    task.results[].image_path 可能是绝对路径，也可能是相对，或缺失。
    双保底：先看 image_path 是否存在，否则用 image_url 反解到 data/task_images/。
    """
    if image_path:
        p = Path(image_path)
        if p.is_file():
            return p
    if image_url and image_url.startswith("/static/tasks/"):
        rel = unquote(image_url[len("/static/tasks/"):])
        p = ROOT / "data" / "task_images" / rel
        if p.is_file():
            return p
    return None


def _copy_gallery_image_for_look(
    image_id: Optional[str],
    category_label: str,      # "上装" | "下装"
    dest_dir: Path,
) -> dict:
    """从 image_id({task_id}:{plan_id}) 找图 → 复制并按规则重命名。
    返回 {"ok": True, "filename": ...} 或 {"skipped": <reason>}"""
    if not image_id or ":" not in image_id:
        return {"skipped": f"未指定 {category_label}"}
    task_id, _, plan_id = image_id.partition(":")
    task = get_generation_task(task_id)
    if not task:
        return {"skipped": f"{category_label} 源 task {task_id} 不存在"}
    try:
        results = json.loads(task.get("results") or "[]")
    except Exception:
        results = []
    match = next(
        (r for r in results if r.get("plan_id") == plan_id and r.get("status") == "succeeded"),
        None,
    )
    if not match:
        return {"skipped": f"{category_label} plan {plan_id} 未成功生成"}

    disk = _resolve_gallery_image_disk_path(
        match.get("image_url") or "", match.get("image_path") or ""
    )
    if not disk:
        return {"skipped": f"{category_label} 图片文件缺失（image_id={image_id}）"}

    # 元数据反查
    try:
        plans = json.loads(task.get("snapshot_plans") or "[]")
    except Exception:
        plans = []
    plan = next((p for p in plans if p.get("方案编号") == plan_id), {}) or {}
    trend_name = task.get("trend_name") or "未知趋势"
    topic_name = plan.get("选用子主题名称") or "未知主题"
    style_no = task.get("style_no") or "未知款号"

    ext = disk.suffix or ".png"
    filename = _sanitize_filename(
        f"{category_label}-{trend_name}-{topic_name}-{style_no}"
    ) + ext
    try:
        shutil.copy2(str(disk), str(dest_dir / filename))
    except OSError as e:
        return {"skipped": f"{category_label} 复制失败：{e}"}
    return {"ok": True, "filename": filename}


def _copy_shooting_slot_for_look(look_row: dict, dest_dir: Path) -> dict:
    """从 look.shooting_slot_url 反解磁盘路径 → 复制并按规则命名（用图组名）。"""
    slot_kind = look_row.get("shooting_slot_kind")
    slot_url = look_row.get("shooting_slot_url")
    if not slot_kind or not slot_url:
        return {"skipped": "未配置拍摄参考"}
    if not slot_url.startswith("/static/templates/"):
        return {"skipped": f"URL 格式不识别：{slot_url}"}

    # 反解磁盘路径
    from backend.routers.templates import get_templates_root

    rel = unquote(slot_url[len("/static/templates/"):])
    src = get_templates_root() / "templates" / rel
    if not src.is_file():
        return {"skipped": f"拍摄参考文件缺失：{src}"}

    ext = src.suffix or ".jpg"
    try:
        meta = json.loads(look_row.get("shooting_slot_meta") or "{}")
    except Exception:
        meta = {}
    if slot_kind == "template":
        label = meta.get("group_name") or "未知模板"
    else:  # upload
        original = meta.get("original_filename") or "自定义"
        label = Path(original).stem
    filename = _sanitize_filename(f"拍摄模特-{label}") + ext
    try:
        shutil.copy2(str(src), str(dest_dir / filename))
    except OSError as e:
        return {"skipped": f"拍摄参考复制失败：{e}"}
    return {"ok": True, "filename": filename}


@router.post("/export-to-desktop")
async def export_looks_to_desktop(req: LooksExportRequest):
    """
    批量导出 look 图片到用户桌面。产物结构：
      Desktop/<YYYY-MM-DD>__<N>套look/
        <look_name>/
          上装-<趋势主题>-<图案名>-<款号>.png
          下装-<趋势主题>-<图案名>-<款号>.png
          拍摄模特-<图组名>.jpg
    成功导出的 look 会自动补上 "已导出" tag。
    """
    if not req.look_ids:
        raise HTTPException(400, "look_ids 为空")

    # 顶层文件夹
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    if not desktop.is_dir():
        raise HTTPException(500, f"桌面路径不存在：{desktop}")

    date_str = datetime.now().strftime("%Y-%m-%d")
    n = len(req.look_ids)
    base_name = f"{date_str}__{n}套look"
    top_dir = desktop / base_name
    # 冲突处理：加序号 _2 _3 ...
    if top_dir.exists():
        i = 2
        while (desktop / f"{base_name}_{i}").exists():
            i += 1
        top_dir = desktop / f"{base_name}_{i}"
    top_dir.mkdir(parents=True, exist_ok=True)

    exported: list[dict] = []
    skipped: list[dict] = []
    # CSV manifest：每一行对应一 look，字段与用户 shooting task.csv 模板一致
    csv_rows: list[dict] = []

    # 注：CSV 三个参考图列（上装/下装/场景）都只填文件名——图片就在该 look 文件夹内，
    # 消费方按 look 行名定位文件夹后直接拼文件名即可，不需要路径前缀。

    for lid in req.look_ids:
        row = db_get_look(lid)
        if not row:
            skipped.append({"look_id": lid, "reason": "look 不存在"})
            continue
        look_name = row.get("name") or lid
        look_dir = top_dir / _sanitize_filename(look_name, fallback=lid)
        look_dir.mkdir(parents=True, exist_ok=True)

        files_written: list[str] = []
        item_skips: list[str] = []

        # 初始化本 look 的 CSV 行
        csv_row = {
            "look": look_name,
            "上装参考图": "",
            "下装参考图": "",
            "上装文本表达": "",
            "下装文本表达": "",
            "场景参考图": "",
        }

        # 上装
        # 「上装参考图」「下装参考图」只填文件名（图片就在本 look 文件夹内，无需路径前缀）
        top_image_id = row.get("top_image_id")
        r = _copy_gallery_image_for_look(top_image_id, "上装", look_dir)
        if r.get("ok"):
            files_written.append(r["filename"])
            csv_row["上装参考图"] = r["filename"]
        else:
            item_skips.append(r.get("skipped", "?"))
        # 上装文本表达：无论有没有款图都随导出写入 CSV
        # （有图 + 有文 → 「上装参考图」「上装文本表达」两列同时有值）
        csv_row["上装文本表达"] = (row.get("top_text") or "").strip()

        # 下装
        bottom_image_id = row.get("bottom_image_id")
        r = _copy_gallery_image_for_look(bottom_image_id, "下装", look_dir)
        if r.get("ok"):
            files_written.append(r["filename"])
            csv_row["下装参考图"] = r["filename"]
        else:
            item_skips.append(r.get("skipped", "?"))
        csv_row["下装文本表达"] = (row.get("bottom_text") or "").strip()

        # 拍摄参考
        r = _copy_shooting_slot_for_look(row, look_dir)
        if r.get("ok"):
            files_written.append(r["filename"])
            csv_row["场景参考图"] = r["filename"]
        else:
            item_skips.append(r.get("skipped", "?"))

        # 只要 look 有任意可读内容（成功导出一项 OR 填入了文本表达），就纳入 CSV
        has_content = bool(files_written) or bool(csv_row["上装文本表达"]) or bool(csv_row["下装文本表达"])

        if files_written:
            try:
                existing_tags = json.loads(row.get("tags") or "[]")
                if not isinstance(existing_tags, list):
                    existing_tags = []
            except Exception:
                existing_tags = []
            # 导出标记带时间戳（前端显示「MM-DD HH:MM 导出」）；重复导出刷新时间
            existing_tags = [t for t in existing_tags if not str(t).startswith("已导出")]
            existing_tags.append(f"已导出:{datetime.now().strftime('%Y-%m-%d %H:%M')}")
            db_update_look(lid, tags=existing_tags)
            exported.append({
                "look_id": lid,
                "look_name": look_name,
                "files": files_written,
                "partial_skips": item_skips,
            })
        else:
            # 该 look 无图导出 → 归为 skipped，且删空文件夹
            try:
                look_dir.rmdir()
            except OSError:
                pass
            skipped.append({
                "look_id": lid,
                "look_name": look_name,
                "reason": "; ".join(item_skips) or "无可导出内容",
            })

        if has_content:
            csv_rows.append(csv_row)

    # 生成 CSV manifest —— 命名与顶级目录同名，放在顶级目录内
    csv_path: Optional[Path] = None
    if csv_rows:
        csv_path = top_dir / f"{top_dir.name}.csv"
        columns = [
            "look", "上装参考图", "下装参考图",
            "上装文本表达", "下装文本表达", "场景参考图",
        ]
        try:
            # UTF-8 BOM：Windows Excel 打开中文不乱码；跨平台兼容
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                writer.writerows(csv_rows)
        except OSError as e:
            # CSV 写入失败不影响图片导出结果，只在响应中告知
            skipped.append({
                "look_id": "",
                "look_name": "CSV manifest",
                "reason": f"CSV 写入失败：{e}",
            })
            csv_path = None

    return {
        "ok": True,
        "output_dir": str(top_dir),
        "csv_path": str(csv_path) if csv_path else None,
        "exported_count": len(exported),
        "exported": exported,
        "skipped": skipped,
    }


# ============================================================================
# image-categories
# ============================================================================

@img_cat_router.get("")
async def list_image_categories_endpoint():
    """返回 image_id → category 映射（前端拿全批用）"""
    return db_list_image_categories()


@img_cat_router.post("")
async def bulk_upsert_image_categories(req: ImageCategoryBulkRequest):
    """批量归类持久化——用户点切换标签后前端调"""
    n = upsert_image_categories_bulk(
        [{"image_id": i.image_id, "category": i.category, "source": i.source} for i in req.items]
    )
    return {"updated": n}
