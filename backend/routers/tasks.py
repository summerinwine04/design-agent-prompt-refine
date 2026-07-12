"""
/api/v1/tasks —— 生图任务（M6 / M7）

  POST   /tasks                          创建任务（基于某 step2 run 的选中方案）
  GET    /tasks                          列表
  GET    /tasks/{task_id}                详情
  GET    /tasks/{task_id}/stream         SSE 实时进度
  POST   /tasks/{task_id}/regenerate     单张/多张图重生（决策③）
  DELETE /tasks/{task_id}                删除归档

  GET    /tasks/{task_id}/prep-from-run  辅助：用于新建表单预填，返回某 step2 run 的款图+9 方案信息
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.db import (
    delete_generation_task,
    get_generation_task,
    get_run as db_get_run,
    insert_generation_task,
    list_generation_tasks,
    update_generation_task_progress,
)
from backend.sse import event_bus, sse_stream

router = APIRouter()

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "tool"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# ============================================================================
# Pydantic models
# ============================================================================

class TaskCreateRequest(BaseModel):
    source_step2_run_id: str
    selected_plan_ids: list[str]               # 用户从 9 方案里选了哪些
    concurrency: int = 2


class TaskRegenerateRequest(BaseModel):
    plan_ids: list[str]                        # 要重生的方案编号
    concurrency: int = 2
    # True = 旧图先备份到 image_root_dir/_archive/{plan_id}_{ts}.png 再覆盖
    # False（默认）= 直接覆盖
    keep_original: bool = False


class TaskSummary(BaseModel):
    id: str
    source_step2_run_id: str
    trend_name: str
    style_no: str
    status: str
    progress_done: int
    progress_total: int
    total_elapsed_ms: Optional[int] = None
    total_cost_usd: Optional[float] = None
    created_at: str
    completed_at: Optional[str] = None
    # 源 step2 run 的 prompt 版本（来自 runs 表 LEFT JOIN）
    # 形如 {"2.1": "01_style_analysis.md@current", "2.4": "04_single_color_design.md@v3", ...}
    source_prompt_bundle: Optional[dict] = None


class TaskDetail(TaskSummary):
    selected_plan_ids: list[str]
    snapshot_step1: Optional[dict] = None
    snapshot_step2_meta: Optional[dict] = None
    snapshot_plans: list[dict]                  # 选中方案的完整内容
    results: Optional[list[dict]] = None
    image_root_dir: str


# ============================================================================
# 列表 / 详情
# ============================================================================

@router.get("", response_model=list[TaskSummary])
async def list_tasks_endpoint(limit: int = 100):
    rows = list_generation_tasks(limit=limit)
    return [_row_to_summary(r) for r in rows]


@router.get("/all-images")
async def list_all_images(limit: int = 200):
    """
    跨任务全局图片墙数据源：扫所有 task 的 results 字段，扁平化每张成功的图。

    返回字段 per item：
      - image_url, plan_id, task_id, trend_name, style_no, color_code, color_name,
        topic_id, topic_name, created_at, image_prompt_used（用于 Modal 显示完整 prompt）
    """
    rows = list_generation_tasks(limit=limit)
    items = []
    for r in rows:
        try:
            results = json.loads(r.get("results") or "[]")
        except Exception:
            results = []
        try:
            snapshot_plans = json.loads(r.get("snapshot_plans") or "[]")
        except Exception:
            snapshot_plans = []
        plan_by_id = {p.get("方案编号"): p for p in snapshot_plans}

        for res in results:
            if res.get("status") != "succeeded" or not res.get("image_url"):
                continue
            pid = res.get("plan_id")
            plan = plan_by_id.get(pid, {})
            items.append({
                "image_url": res["image_url"],
                "plan_id": pid,
                "task_id": r["id"],
                "trend_name": r.get("trend_name"),
                "style_no": r.get("style_no"),
                "color_code": res.get("color_code") or plan.get("_色号代码") or plan.get("色号代码"),
                "color_name": res.get("color_name") or plan.get("_营销色名") or plan.get("营销色名"),
                "topic_id": plan.get("选用子主题编号"),
                "topic_name": plan.get("选用子主题名称"),
                "elapsed_ms": res.get("elapsed_ms"),
                "task_created_at": r.get("created_at"),
                "task_status": r.get("status"),
                "image_prompt_used": res.get("image_prompt_used"),
                "方案说明": plan.get("方案说明"),
                "适配度": plan.get("适配度"),
                # v5 CONVERGE：Collection View 按 look_id / role 聚合
                "role": plan.get("role"),
                "look_id": plan.get("look_id"),
            })

    # 按 task 创建时间倒序（DB 已排好，这里保留）
    return {"total": len(items), "items": items}


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_endpoint(task_id: str):
    row = get_generation_task(task_id)
    if not row:
        raise HTTPException(404, f"task {task_id} not found")
    return _row_to_detail(row)


# ============================================================================
# 创建（核心）
# ============================================================================

@router.post("", response_model=TaskSummary)
async def create_task(req: TaskCreateRequest):
    """
    基于某 step2 run + 用户选中的方案集，创建一个生图任务。
    创建时立即做完整快照（决策⑥）。
    立即返回 task；orchestrator 在线程池启动。
    """
    src_row = db_get_run(req.source_step2_run_id)
    if not src_row:
        raise HTTPException(404, f"源 step2 run 不存在：{req.source_step2_run_id}")

    # 拿 final JSON 抽快照
    src_run_dir = Path(src_row["run_dir"])
    final_path = src_run_dir / "final_output.json"
    if not final_path.is_file():
        final_path = Path(src_row.get("final_json_path") or "")
    if not final_path.is_file():
        raise HTTPException(400, f"源 step2 run 的 final JSON 不可用")
    final = json.loads(final_path.read_text(encoding="utf-8"))

    # 拿 trend JSON（用于快照）
    src_trace = src_run_dir / "trace.jsonl"
    trend_json_path = None
    color_folder_path = None
    ref_image_path = None
    if src_trace.is_file():
        first = json.loads(src_trace.read_text(encoding="utf-8").splitlines()[0])
        fixture = first.get("fixture") or {}
        if "trend_json_path" in fixture:
            trend_json_path = Path(fixture["trend_json_path"])
        if "color_folder" in fixture:
            color_folder_path = Path(fixture["color_folder"])
        if "ref_image" in fixture or "ref_image_path" in fixture:
            ref_image_path = Path(fixture.get("ref_image") or fixture.get("ref_image_path"))
    # 兜底：从默认布局推
    if trend_json_path is None or not trend_json_path.is_file():
        trend_name = src_row["trend_name"]
        trend_json_path = ROOT.parent / "ai-supply" / "趋势报告" / trend_name / f"{trend_name}.json"
    if ref_image_path is None or not ref_image_path.is_file():
        style_no = src_row["style_no"]
        ref_image_path = ROOT.parent / "ai-supply" / "款图" / f"{style_no}.jpg"
    if color_folder_path is None or not color_folder_path.is_dir():
        color_folder_path = ref_image_path.parent / ref_image_path.stem

    snapshot_step1 = json.loads(trend_json_path.read_text(encoding="utf-8")) if trend_json_path.is_file() else None
    snapshot_step2_meta = {
        "款式分析": final.get("step2_改款方案", {}).get("款式分析"),
        "性别比规划": final.get("step2_改款方案", {}).get("性别比规划"),
        "趋势说明": final.get("step2_改款方案", {}).get("趋势说明"),
        "款号": final.get("step2_改款方案", {}).get("款号"),
        "款式名": final.get("step2_改款方案", {}).get("款式名"),
    }

    # 抽选中方案的完整内容
    all_plans_flat = _flatten_step2_plans(final)
    snapshot_plans = [p for p in all_plans_flat if p.get("方案编号") in req.selected_plan_ids]
    if not snapshot_plans:
        raise HTTPException(400, "选中方案在源 step2 输出中找不到")

    # v5 Mode C（上下装成套）：一个任务里混装上下装方案——
    # 按 role 构建各自的款图/色号资产（role_assets），runner 逐方案取用对应款图。
    src_design_mode = (final.get("step2_改款方案") or {}).get("design_mode") or "MULTI_TOPIC"
    role_assets: dict = {}
    if src_design_mode == "COLLECTION_2SKU":
        fixture_styles = (fixture or {}).get("styles") if src_trace.is_file() else None
        plan_roles = {p.get("role") for p in snapshot_plans if p.get("role")}
        for role in plan_roles:
            slot = next((s for s in (fixture_styles or []) if s.get("role") == role), None)
            if not slot or not slot.get("ref_image_path"):
                raise HTTPException(400, f"源 run 的 fixture meta 缺少款位 {role} 的款图路径，无法生图")
            rp = Path(slot["ref_image_path"])
            if not rp.is_file():
                raise HTTPException(400, f"款位 {role} 的款图不存在：{rp}")
            cf = rp.parent / rp.stem
            if not cf.is_dir():
                raise HTTPException(400, f"款位 {role} 的色号文件夹不存在：{cf}")
            role_assets[role] = {
                "ref_image_path": rp,
                "color_folder": cf,
                "color_filename_map": _build_color_filename_map(cf),
            }
        # 任务级默认款图取 top（尺寸档位兜底用），单侧混不进来时也不影响
        if "top" in role_assets:
            ref_image_path = role_assets["top"]["ref_image_path"]
            color_folder_path = role_assets["top"]["color_folder"]

    # 生成 task_id——秒级时间戳 + 4 位随机 hex。
    # 必须带随机后缀：成套模式按款位拆分时，前端会在同一秒内连续 POST 两次，
    # 纯时间戳 id 会撞 PRIMARY KEY（IntegrityError → 500，第二个任务永远建不起来）。
    import uuid as _uuid
    task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:4] + "-task"
    # 图片落盘目录
    image_root_dir = ROOT / "data" / "task_images" / task_id
    image_root_dir.mkdir(parents=True, exist_ok=True)

    # DB 写入
    insert_generation_task(
        task_id=task_id,
        source_step2_run_id=req.source_step2_run_id,
        trend_name=src_row["trend_name"],
        style_no=src_row["style_no"],
        selected_plan_ids=req.selected_plan_ids,
        snapshot_step1=snapshot_step1,
        snapshot_step2_meta=snapshot_step2_meta,
        snapshot_plans=snapshot_plans,
        image_root_dir=str(image_root_dir),
    )

    # 启动后台线程跑
    _kick_off(
        task_id=task_id,
        ref_image_path=ref_image_path,
        color_folder=color_folder_path,
        plans=snapshot_plans,
        image_root_dir=image_root_dir,
        concurrency=req.concurrency,
        role_assets=role_assets,
    )

    return _row_to_summary(get_generation_task(task_id))


# ============================================================================
# 重生
# ============================================================================

@router.post("/{task_id}/regenerate", response_model=TaskSummary)
async def regenerate_task(task_id: str, req: TaskRegenerateRequest):
    """重生指定方案编号的图（决策③ 单张失败可独立重生）。"""
    row = get_generation_task(task_id)
    if not row:
        raise HTTPException(404, f"task {task_id} not found")
    if row["status"] == "running":
        raise HTTPException(400, "任务还在跑，无法重生")

    snapshot_plans = json.loads(row["snapshot_plans"])
    target_plans = [p for p in snapshot_plans if p.get("方案编号") in req.plan_ids]
    if not target_plans:
        raise HTTPException(400, "指定方案不在 task 快照中")

    src_row = db_get_run(row["source_step2_run_id"])
    if not src_row:
        raise HTTPException(400, "源 step2 run 已删除，无法重生")

    # 再拿一次 ref + color folder
    src_trace = Path(src_row["run_dir"]) / "trace.jsonl"
    ref_image_path = ROOT.parent / "ai-supply" / "款图" / f"{row['style_no']}.jpg"
    color_folder_path = ref_image_path.parent / ref_image_path.stem
    fixture = {}
    if src_trace.is_file():
        first = json.loads(src_trace.read_text(encoding="utf-8").splitlines()[0])
        fixture = first.get("fixture") or {}
        if "color_folder" in fixture:
            color_folder_path = Path(fixture["color_folder"])
        if "ref_image" in fixture or "ref_image_path" in fixture:
            ref_image_path = Path(fixture.get("ref_image") or fixture.get("ref_image_path"))

    # v5 成套：目标方案带 role → 按 role 重建款图/色号资产
    role_assets: dict = {}
    plan_roles = {p.get("role") for p in target_plans if p.get("role")}
    if plan_roles:
        fixture_styles = fixture.get("styles") or []
        for role in plan_roles:
            slot = next((s for s in fixture_styles if s.get("role") == role), None)
            if not slot or not slot.get("ref_image_path"):
                raise HTTPException(400, f"源 run meta 缺少款位 {role} 的款图路径，无法重生")
            rp = Path(slot["ref_image_path"])
            cf = rp.parent / rp.stem
            if not rp.is_file() or not cf.is_dir():
                raise HTTPException(400, f"款位 {role} 的款图/色号文件夹不存在：{rp}")
            role_assets[role] = {
                "ref_image_path": rp,
                "color_folder": cf,
                "color_filename_map": _build_color_filename_map(cf),
            }

    # 标 status = running
    update_generation_task_progress(task_id=task_id, status="running")

    _kick_off(
        task_id=task_id,
        ref_image_path=ref_image_path,
        color_folder=color_folder_path,
        plans=target_plans,
        image_root_dir=Path(row["image_root_dir"]),
        concurrency=req.concurrency,
        merge_with_existing=True,
        keep_original=req.keep_original,
        role_assets=role_assets,
    )

    return _row_to_summary(get_generation_task(task_id))


# ============================================================================
# 删除
# ============================================================================

@router.delete("/{task_id}")
async def delete_task_endpoint(task_id: str):
    row = get_generation_task(task_id)
    if not row:
        raise HTTPException(404, f"task {task_id} not found")
    # 物理删图片目录
    import shutil
    img_dir = Path(row["image_root_dir"])
    if img_dir.is_dir():
        shutil.rmtree(img_dir, ignore_errors=True)
    delete_generation_task(task_id)
    return {"ok": True}


# ============================================================================
# SSE
# ============================================================================

@router.get("/{task_id}/stream")
async def stream_task(task_id: str):
    return StreamingResponse(
        sse_stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ============================================================================
# M8 多任务对比
# ============================================================================

@router.get("/compare/multi")
async def compare_tasks(ids: str):
    """
    多 task 横向对比，按色号代码+色名对齐。

    Args:
        ids: 逗号分隔的 task_id 列表，至少 2 个

    Returns:
        {
          "tasks": [{ id, status, style_no, trend_name, prompt_bundle?, created_at }],
          "alignment_key": "色号代码-营销色名",
          "rows": [
            {
              "color_key": "DSC09253-中灰",
              "color_code": "DSC09253",
              "color_name": "中灰",
              "cells": [
                {
                  "task_id": "...",
                  "plan_id": "...",              # 该 task 中该色号对应的方案编号
                  "image_url": "...",
                  "image_status": "succeeded|failed|missing",
                  "image_prompt_used": "...",    # 实际发给 image API 的 prompt
                  "plan_summary": { 子主题, 面组合, 档组合, 方案说明 }
                }
              ]
            }
          ],
          "summary": {
            "all_same_style_no": true,
            "common_color_count": 9,
            "unique_to_task": { task_id: [color_keys] }
          }
        }
    """
    task_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if len(task_ids) < 2:
        raise HTTPException(400, "至少需要 2 个 task_id 才能对比")

    # 加载每个 task
    tasks = []
    errors = []
    for tid in task_ids:
        row = get_generation_task(tid)
        if not row:
            errors.append({"task_id": tid, "error": "task 不存在"})
            continue
        tasks.append(row)

    if len(tasks) < 2:
        raise HTTPException(400, f"有效 task 不足 2 个：{errors}")

    # 确定对齐键
    # 方案编号格式: YK250609-牛仔服-DSC09253-中灰-01
    # 对齐用 (色号代码, 营销色名)，从 snapshot_plans 直接取字段
    # 收集所有 task 出现过的 color_key
    all_color_keys: list[str] = []
    seen = set()
    task_color_maps = []   # 每个 task：color_key → 该 color 的 cell 数据

    for t in tasks:
        snapshot = json.loads(t["snapshot_plans"])
        results = json.loads(t["results"]) if t.get("results") else []
        results_by_plan = {r["plan_id"]: r for r in results}

        color_map: dict[str, dict] = {}
        for plan in snapshot:
            code = plan.get("_色号代码") or plan.get("色号代码", "")
            name = plan.get("_营销色名") or plan.get("营销色名", "")
            color_key = f"{code}-{name}"
            if color_key not in seen:
                seen.add(color_key)
                all_color_keys.append(color_key)

            plan_id = plan.get("方案编号", "")
            r = results_by_plan.get(plan_id)
            color_map[color_key] = {
                "task_id": t["id"],
                "plan_id": plan_id,
                "color_code": code,
                "color_name": name,
                "image_url": r.get("image_url") if r else None,
                "image_status": r["status"] if r else "missing",
                "image_prompt_used": (r.get("image_prompt_used") if r else None) or plan.get("图生图prompt"),
                "elapsed_ms": r.get("elapsed_ms") if r else None,
                "error": r.get("error") if r else None,
                "plan_summary": {
                    "选用子主题编号": plan.get("选用子主题编号"),
                    "选用子主题名称": plan.get("选用子主题名称"),
                    "面组合": plan.get("面组合"),
                    "档组合": plan.get("档组合"),
                    "适配度": plan.get("适配度"),
                    "方案说明": plan.get("方案说明"),
                    "性别定向": plan.get("_性别定向") or plan.get("性别定向"),
                    "是否需要变色": plan.get("是否需要变色"),
                },
            }
        task_color_maps.append(color_map)

    # 组装 rows
    rows = []
    for color_key in all_color_keys:
        cells = []
        for color_map in task_color_maps:
            cells.append(color_map.get(color_key) or {
                "task_id": None,
                "image_status": "missing",
                "image_url": None,
                "image_prompt_used": None,
                "plan_summary": None,
            })
        code, name = color_key.split("-", 1) if "-" in color_key else (color_key, "")
        rows.append({
            "color_key": color_key,
            "color_code": code,
            "color_name": name,
            "cells": cells,
        })

    # summary
    style_nos = {t["style_no"] for t in tasks}
    summary = {
        "all_same_style_no": len(style_nos) == 1,
        "style_nos": list(style_nos),
        "common_color_count": sum(
            1 for row in rows
            if all(c.get("image_status") != "missing" for c in row["cells"])
        ),
        "total_color_count": len(rows),
    }

    return {
        "tasks": [
            {
                "id": t["id"],
                "status": t["status"],
                "style_no": t["style_no"],
                "trend_name": t["trend_name"],
                "source_step2_run_id": t["source_step2_run_id"],
                "created_at": t["created_at"],
                "progress_done": t["progress_done"],
                "progress_total": t["progress_total"],
                "total_cost_usd": t.get("total_cost_usd"),
                "total_elapsed_ms": t.get("total_elapsed_ms"),
            }
            for t in tasks
        ],
        "alignment_key": "色号代码-营销色名",
        "rows": rows,
        "summary": summary,
        "errors": errors,
    }


# ============================================================================
# 用于新建表单的辅助
# ============================================================================

@router.get("/prep/from-run/{source_run_id}")
async def prep_from_run(source_run_id: str):
    """给前端 TaskNewPage 用：返回某 step2 run 的款图 URL + 9 方案 + 色号图列表。
    前端拿到后展示给用户勾选。"""
    src_row = db_get_run(source_run_id)
    if not src_row:
        raise HTTPException(404, f"源 step2 run 不存在")

    src_run_dir = Path(src_row["run_dir"])
    final_path = src_run_dir / "final_output.json"
    if not final_path.is_file():
        final_path = Path(src_row.get("final_json_path") or "")
    if not final_path.is_file():
        raise HTTPException(400, "源 final JSON 不可用")
    final = json.loads(final_path.read_text(encoding="utf-8"))

    plans = _flatten_step2_plans(final)

    step2 = final.get("step2_改款方案") or {}
    design_mode = step2.get("design_mode") or "MULTI_TOPIC"

    resp = {
        "source_run_id": source_run_id,
        "trend_name": src_row["trend_name"],
        "style_no": src_row["style_no"],
        "ref_image_url": f"/static/styles/{src_row['style_no']}.jpg",   # 由 main.py 挂载
        "design_mode": design_mode,
        "plans": plans,
        "meta": {
            "款式分析": step2.get("款式分析"),
            "趋势说明": step2.get("趋势说明"),
        },
    }

    # v5 CONVERGE：组合 style_no（如 "YK...服+YK...裤"）拼不出静态 URL——
    # 按 final meta 里的 role/款号/文件名 直接给每个方案下发款图 & 色号图 URL。
    if design_mode != "MULTI_TOPIC":
        styles_meta = step2.get("styles") or []
        resp["styles"] = [
            {
                "role": s.get("role"),
                "style_no": s.get("款号"),
                "ref_image_url": f"/static/styles/{s.get('款号')}.jpg",
            }
            for s in styles_meta
        ]
        color_meta = (final.get("meta") or {}).get("色号图列表") or []
        url_map = {
            (c.get("款号"), c.get("色号代码"), c.get("营销色名")):
                f"/static/styles/{c.get('款号')}/{c.get('文件名')}"
            for c in color_meta
        }
        for p in plans:
            sn = p.get("_款号")
            key = (sn, p.get("_色号代码") or p.get("色号代码"), p.get("_营销色名") or p.get("营销色名"))
            if key in url_map:
                p["_color_image_url"] = url_map[key]
            if sn:
                p["_ref_image_url"] = f"/static/styles/{sn}.jpg"

    return resp


# ============================================================================
# 内部：启动线程跑 generator
# ============================================================================

def _kick_off(
    *,
    task_id: str,
    ref_image_path: Path,
    color_folder: Path,
    plans: list[dict],
    image_root_dir: Path,
    concurrency: int,
    role_assets: dict | None = None,
    merge_with_existing: bool = False,
    keep_original: bool = False,
) -> None:
    """把生图任务丢到线程池。"""
    from backend.db import get_openai_api_key
    api_key = get_openai_api_key()
    if not api_key:
        raise HTTPException(400, "OPENAI_API_KEY 未设置")

    # 解析色号文件夹的 (code, name) → 文件名 映射
    color_filename_map = _build_color_filename_map(color_folder)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None,
        _run_generation_in_thread,
        task_id, api_key, ref_image_path, color_folder, plans,
        image_root_dir, color_filename_map, concurrency, merge_with_existing,
        keep_original, role_assets or {},
    )


def _run_generation_in_thread(
    task_id: str,
    api_key: str,
    ref_image_path: Path,
    color_folder: Path,
    plans: list[dict],
    image_root_dir: Path,
    color_filename_map: dict,
    concurrency: int,
    merge_with_existing: bool,
    keep_original: bool = False,
    role_assets: dict | None = None,
) -> None:
    """实际跑在线程里。"""
    from generator import GenerationRunner

    # 进度 callback
    progress = {"done": 0}
    existing_results: list[dict] = []
    if merge_with_existing:
        row = get_generation_task(task_id)
        if row and row.get("results"):
            existing_results = json.loads(row["results"])

    def event_cb(ev: dict):
        # 推到 SSE
        event_bus.publish(task_id, ev)
        # 单图完成时更新 DB 进度
        if ev.get("event") in ("plan_succeeded", "plan_failed"):
            progress["done"] += 1
            update_generation_task_progress(
                task_id=task_id,
                progress_done=progress["done"],
                status="running",
            )

    update_generation_task_progress(task_id=task_id, status="running")

    try:
        # 生图模型从 .env 读：用户在「设置」页改 DEFAULT_IMAGE_MODEL 立即生效
        image_model = os.environ.get("DEFAULT_IMAGE_MODEL", "gpt-image-2")
        runner = GenerationRunner(
            task_id=task_id,
            api_key=api_key,
            ref_image_path=ref_image_path,
            color_folder=color_folder,
            plans=plans,
            image_root_dir=image_root_dir,
            color_filename_map=color_filename_map,
            concurrency=concurrency,
            event_callback=event_cb,
            keep_original=keep_original,
            model=image_model,
            role_assets=role_assets or {},
        )
        result = runner.run()

        # 合并 results 与 existing（重生模式）
        if merge_with_existing:
            by_id = {r["plan_id"]: r for r in existing_results}
            for new_r in result["results"]:
                by_id[new_r["plan_id"]] = new_r
            merged = list(by_id.values())
            final_results = merged
        else:
            final_results = result["results"]

        succeeded = sum(1 for r in final_results if r.get("status") == "succeeded")
        failed = sum(1 for r in final_results if r.get("status") == "failed")
        status = "completed" if failed == 0 else ("partial" if succeeded > 0 else "failed")

        update_generation_task_progress(
            task_id=task_id,
            status=status,
            results=final_results,
            total_elapsed_ms=result["total_elapsed_ms"],
            total_cost_usd=result["total_cost_usd"],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )

        # v5 成套模式：任务完成后尝试自动组套入 Fitting Room。
        # 每个 look 的 top/bottom 成员都已有成功图 → 自动创建 Fitting Room look
        # （通常上装任务先完成时凑不齐，下装任务完成后这里会把整套配齐）。
        try:
            n = _auto_sync_collection_looks(task_id)
            if n:
                print(f"[task {task_id}] auto-synced {n} collection look(s) to fitting room", flush=True)
        except Exception as _sync_err:
            print(f"[task {task_id}] collection look auto-sync failed: {_sync_err}", flush=True)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        update_generation_task_progress(
            task_id=task_id,
            status="failed",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        event_bus.publish(task_id, {
            "event": "task_finished",
            "status": "failed",
            "error": str(exc),
        })


# ============================================================================
# v5 成套模式：生图完成后自动组套入 Fitting Room
# ============================================================================

@router.post("/sync-collection-looks/{source_run_id}")
async def sync_collection_looks_endpoint(source_run_id: str):
    """手动触发：把某成套 run 已生成的整套 look 同步到 Fitting Room。
    覆盖「任务在自动同步上线前就已跑完」或想立即补同步的场景。幂等（已入库的套不重复建）。"""
    rows = [t for t in list_generation_tasks(limit=300) if t.get("source_step2_run_id") == source_run_id]
    if not rows:
        raise HTTPException(404, f"该 run 没有生图任务：{source_run_id}")
    try:
        created = _auto_sync_collection_looks(rows[0]["id"])
    except Exception as e:
        raise HTTPException(500, f"同步失败：{type(e).__name__}: {e}")
    return {"ok": True, "created": created}


def _auto_sync_collection_looks(task_id: str) -> int:
    """
    某生图任务完成后调用：若源 run 是 COLLECTION_2SKU，
    对每个预选 look 检查 top/bottom 成员是否都已有成功图 →
    是且 Fitting Room 里还没有这套 → 自动创建（tags 带 run_id + look_id 防重复）。

    返回本次新建的 look 数。任何异常由调用方吞掉，不阻断任务收尾。
    """
    import secrets
    import time as _time

    from backend.db import get_generation_task, list_generation_tasks, list_looks, insert_look
    from backend.db import get_run as _get_run

    task_row = get_generation_task(task_id)
    if not task_row:
        return 0
    source_run_id = task_row["source_step2_run_id"]
    src_row = _get_run(source_run_id)
    if not src_row:
        return 0

    # 读 final JSON
    final_path = Path(src_row["run_dir"]) / "final_output.json"
    if not final_path.is_file():
        final_path = Path(src_row.get("final_json_path") or "")
    if not final_path.is_file():
        return 0
    final = json.loads(final_path.read_text(encoding="utf-8"))
    step2 = final.get("step2_改款方案") or {}
    if (step2.get("design_mode") or "MULTI_TOPIC") != "COLLECTION_2SKU":
        return 0

    meta_looks = (final.get("meta") or {}).get("looks") or []
    topic = ((step2.get("主题选择") or {}).get("选用主题列表") or [{}])[0]
    topic_name = topic.get("子主题名称") or ""

    # look_id → {role: [方案编号...]}（按 2.5L 输出顺序，取第一张成功图）
    plans_by_look: dict[str, dict[str, list[str]]] = {}
    for lk in step2.get("look方案列表") or []:
        m: dict[str, list[str]] = {}
        for mem in lk.get("成员方案") or []:
            m[mem.get("role")] = [p.get("方案编号") for p in (mem.get("设计方案") or []) if p.get("方案编号")]
        plans_by_look[lk.get("look_id")] = m

    # 本 run 全部任务的成功图：plan_id → "{task_id}:{plan_id}"（gallery image_id 格式）
    img_by_plan: dict[str, str] = {}
    for t in list_generation_tasks(limit=300):
        if t.get("source_step2_run_id") != source_run_id:
            continue
        try:
            results = json.loads(t.get("results") or "[]")
        except Exception:
            results = []
        for res in results:
            if res.get("status") == "succeeded" and res.get("image_url") and res.get("plan_id"):
                img_by_plan.setdefault(res["plan_id"], f"{t['id']}:{res['plan_id']}")

    # 已入库的 (run, look_id) 防重复——自动同步和 Collection View 手动确认共用这套判重
    existing_look_ids: set[str] = set()
    for lk in list_looks(limit=1000):
        try:
            tags = json.loads(lk.get("tags") or "[]")
        except Exception:
            tags = []
        if source_run_id in tags:
            existing_look_ids.update(tg for tg in tags if isinstance(tg, str) and tg.startswith("LK-"))

    created = 0
    for ml in meta_looks:
        lid = ml.get("look_id")
        if not lid or lid in existing_look_ids:
            continue
        roles = plans_by_look.get(lid) or {}
        top_img = next((img_by_plan[p] for p in roles.get("top") or [] if p in img_by_plan), None)
        bottom_img = next((img_by_plan[p] for p in roles.get("bottom") or [] if p in img_by_plan), None)
        if not top_img or not bottom_img:
            continue    # 有一侧还没生成成功——等另一侧任务完成时再同步
        insert_look(
            id=f"look-{int(_time.time() * 1000)}-{secrets.token_hex(3)}",
            name=f"{ml.get('name') or lid}·{topic_name}".rstrip("·"),
            top_kind="image",
            top_image_id=top_img,
            bottom_kind="image",
            bottom_image_id=bottom_img,
            tags=["collection", "auto-sync", source_run_id, lid],
        )
        created += 1
    return created


# ============================================================================
# 工具
# ============================================================================

def _flatten_step2_plans(final: dict) -> list[dict]:
    """从 step2 final JSON 抽全部方案为 flat list。"""
    step2_data = final.get("step2_改款方案") or final
    out = []
    for color in step2_data.get("色号方案列表", []):
        code = color.get("色号代码", "")
        name = color.get("营销色名", "")
        gender = color.get("性别定向", "")
        needs = color.get("是否需要变色", False)
        for plan in color.get("设计方案", []):
            p = dict(plan)
            p.setdefault("_色号代码", code)
            p.setdefault("_营销色名", name)
            p.setdefault("_性别定向", gender)
            p.setdefault("是否需要变色", needs)
            # v5 CONVERGE：透传 role / look_id / 款号（Mode C 生图按 role 取各自款图；
            # Collection View 按 look_id 聚合）。老 run 没这些字段，setdefault 不影响。
            if color.get("role"):
                p.setdefault("role", color["role"])
            if color.get("look_id"):
                p.setdefault("look_id", color["look_id"])
            if color.get("款号"):
                p.setdefault("_款号", color["款号"])
            out.append(p)
    return out


def _build_color_filename_map(color_folder: Path) -> dict[tuple[str, str], str]:
    """扫描色号目录，建 (code, name) → 文件名 映射。命名规则：{code}({name}已使用).jpg"""
    out: dict[tuple[str, str], str] = {}
    if not color_folder.is_dir():
        return out
    pattern = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')
    for f in sorted(color_folder.iterdir()):
        if f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        m = pattern.match(f.stem)
        if not m:
            continue
        code = m.group(1).strip()
        raw = m.group(2).strip()
        name = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw).strip() or raw
        out[(code, name)] = f.name
    return out


def _row_to_summary(row: dict) -> TaskSummary:
    # source_prompt_bundle 仅 list_generation_tasks 查询会带（LEFT JOIN runs）；
    # get_generation_task 没 join 所以可能没这个键
    bundle_raw = row.get("source_prompt_bundle")
    bundle_dict = None
    if bundle_raw:
        try:
            bundle_dict = json.loads(bundle_raw) if isinstance(bundle_raw, str) else bundle_raw
        except Exception:
            bundle_dict = None

    return TaskSummary(
        id=row["id"],
        source_step2_run_id=row["source_step2_run_id"],
        trend_name=row["trend_name"],
        style_no=row["style_no"],
        status=row["status"],
        progress_done=row["progress_done"] or 0,
        progress_total=row["progress_total"],
        total_elapsed_ms=row.get("total_elapsed_ms"),
        total_cost_usd=row.get("total_cost_usd"),
        created_at=row["created_at"],
        completed_at=row.get("completed_at"),
        source_prompt_bundle=bundle_dict,
    )


def _row_to_detail(row: dict) -> TaskDetail:
    summary = _row_to_summary(row)
    return TaskDetail(
        **summary.model_dump(),
        selected_plan_ids=json.loads(row["selected_plan_ids"]),
        snapshot_step1=json.loads(row["snapshot_step1"]) if row.get("snapshot_step1") else None,
        snapshot_step2_meta=json.loads(row["snapshot_step2_meta"]) if row.get("snapshot_step2_meta") else None,
        snapshot_plans=json.loads(row["snapshot_plans"]),
        results=json.loads(row["results"]) if row.get("results") else None,
        image_root_dir=row["image_root_dir"],
    )
