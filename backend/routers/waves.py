"""
/api/v1/waves — 波段上新管理（Fitting Room 升级）

  GET    /waves                 全部波段（含派生统计：look 数 / 款式数去重）
  POST   /waves                 创建波段（可顺带初始归组 look_ids）
  PATCH  /waves/{id}            改名 / 改预备上架时间 / 改状态（规划中|已上架）
  DELETE /waves/{id}            删除波段——成员 look 回未分波段池，不删 look
  POST   /waves/{id}/looks      批量归组（look 在其他波段则自动移过来）
  POST   /waves/unassign        批量移出波段（回未分波段池）

约定（PRD 拍板）：look 至多属一个波段；款式数 = 波段内款色图去重；
状态为手动切换；公网 guest 只读（写操作不进白名单）。
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException

from backend.db import (
    assign_looks_to_wave,
    delete_wave as db_delete_wave,
    get_look as db_get_look,
    get_wave as db_get_wave,
    insert_wave as db_insert_wave,
    list_waves as db_list_waves,
    unassign_looks as db_unassign_looks,
    update_wave as db_update_wave,
)
from backend.schemas import Wave, WaveAssignRequest, WaveCreateRequest, WaveUpdateRequest

router = APIRouter()

_VALID_STATUS = {"规划中", "已上架"}


def _wave_from_row(row: dict) -> Wave:
    return Wave(
        id=row["id"],
        name=row["name"],
        planned_launch_date=row.get("planned_launch_date"),
        status=row.get("status") or "规划中",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        look_count=row.get("look_count", 0),
        style_count=row.get("style_count", 0),
    )


@router.get("", response_model=list[Wave])
async def list_waves_endpoint():
    return [_wave_from_row(r) for r in db_list_waves()]


@router.post("", response_model=Wave)
async def create_wave(req: WaveCreateRequest):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "波段名不能为空")
    wave_id = f"wv-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    db_insert_wave(id=wave_id, name=name, planned_launch_date=req.planned_launch_date or None)
    if req.look_ids:
        missing = [lid for lid in req.look_ids if not db_get_look(lid)]
        if missing:
            raise HTTPException(400, f"look 不存在：{missing}")
        assign_looks_to_wave(wave_id, req.look_ids)
    # 重新查一遍拿派生统计
    row = next((r for r in db_list_waves() if r["id"] == wave_id), None)
    if not row:
        raise HTTPException(500, "波段创建后读取失败")
    return _wave_from_row(row)


@router.patch("/{wave_id}", response_model=Wave)
async def update_wave_endpoint(wave_id: str, req: WaveUpdateRequest):
    if not db_get_wave(wave_id):
        raise HTTPException(404, f"波段 {wave_id} 不存在")
    if req.status is not None and req.status not in _VALID_STATUS:
        raise HTTPException(400, f"status 只能是 {sorted(_VALID_STATUS)}")
    if req.name is not None and not req.name.strip():
        raise HTTPException(400, "波段名不能为空")
    db_update_wave(
        wave_id,
        name=req.name.strip() if req.name is not None else None,
        planned_launch_date=req.planned_launch_date,
        status=req.status,
    )
    row = next((r for r in db_list_waves() if r["id"] == wave_id), None)
    return _wave_from_row(row)


@router.delete("/{wave_id}")
async def delete_wave_endpoint(wave_id: str):
    if not db_get_wave(wave_id):
        raise HTTPException(404, f"波段 {wave_id} 不存在")
    released = db_delete_wave(wave_id)
    return {"ok": True, "released_looks": released}


@router.post("/unassign")
async def unassign_looks_endpoint(req: WaveAssignRequest):
    """批量移出波段（回未分波段池）。注意路由顺序：必须在 /{wave_id}/looks 之前定义无冲突。"""
    if not req.look_ids:
        raise HTTPException(400, "look_ids 为空")
    n = db_unassign_looks(req.look_ids)
    return {"ok": True, "updated": n}


@router.post("/{wave_id}/looks")
async def assign_looks_endpoint(wave_id: str, req: WaveAssignRequest):
    if not db_get_wave(wave_id):
        raise HTTPException(404, f"波段 {wave_id} 不存在")
    if not req.look_ids:
        raise HTTPException(400, "look_ids 为空")
    missing = [lid for lid in req.look_ids if not db_get_look(lid)]
    if missing:
        raise HTTPException(400, f"look 不存在：{missing}")
    n = assign_looks_to_wave(wave_id, req.look_ids)
    return {"ok": True, "updated": n}
