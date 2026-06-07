"""
/api/v1/fixtures — 测试夹具（冻结输入快照）

每个 fixture = (趋势JSON, 款图, 色号子集, 性别比, K) 一组冻结输入，
可以反复跑不同的 PromptBundle，做横向对比。
"""

from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException

from backend.db import get_conn
from backend.schemas import Fixture, FixtureCreateRequest

router = APIRouter()


@router.get("", response_model=list[Fixture])
async def list_fixtures():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM fixtures ORDER BY created_at DESC").fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


@router.get("/{fixture_id}", response_model=Fixture)
async def get_fixture(fixture_id: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM fixtures WHERE id = ?", (fixture_id,)).fetchone()
        if not row:
            raise HTTPException(404, "fixture not found")
        return _row(row)
    finally:
        conn.close()


@router.post("", response_model=Fixture)
async def create_fixture(req: FixtureCreateRequest):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO fixtures (id, name, trend_json_path, ref_image_path, color_folder,
                       selected_colors, gender_ratio, num_designs_k, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                req.id, req.name, req.trend_json_path, req.ref_image_path,
                req.color_folder,
                json.dumps(req.selected_colors) if req.selected_colors else None,
                req.gender_ratio, req.num_designs_k, req.description,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM fixtures WHERE id = ?", (req.id,)).fetchone()
        return _row(row)
    finally:
        conn.close()


@router.delete("/{fixture_id}")
async def delete_fixture(fixture_id: str):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM fixtures WHERE id = ?", (fixture_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _row(r) -> Fixture:
    return Fixture(
        id=r["id"],
        name=r["name"],
        trend_json_path=r["trend_json_path"],
        ref_image_path=r["ref_image_path"],
        color_folder=r["color_folder"],
        selected_colors=json.loads(r["selected_colors"]) if r["selected_colors"] else None,
        gender_ratio=r["gender_ratio"],
        num_designs_k=r["num_designs_k"],
        description=r["description"],
        created_at=r["created_at"],
    )
