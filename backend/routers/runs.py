"""
/api/v1/runs — Step2 run 编排接口

  POST   /runs                              创建 + 启动一次 run（后台 task）
  GET    /runs                              列表
  GET    /runs/{run_id}                     完整详情（含 trace tree）
  GET    /runs/{run_id}/stream              SSE 事件流
  GET    /runs/{run_id}/nodes/{node_id}     单节点 input/output 缓存
  POST   /runs/{run_id}/fork                fork-from-node（M4 才实装，先 501）
  POST   /runs/{run_id}/nodes/{node_id}/retry   仅重跑此节点（M4）
  GET    /runs/{run_id}/logs                原始 trace.jsonl
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.db import (
    get_conn,
    get_run as db_get_run,
    insert_run as db_insert_run,
    list_runs as db_list_runs,
    mark_run_finished as db_mark_run_finished,
)
from backend.schemas import (
    NodeDetail,
    NodeRetryRequest,
    RunCreateRequest,
    RunDetail,
    RunForkRequest,
    RunSummary,
)
from backend.sse import event_bus, make_trace_callback, sse_stream

router = APIRouter()

# orchestrator 在 tool/ 下
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "tool"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# ============================================================================
# 列表 & 详情
# ============================================================================

@router.get("", response_model=list[RunSummary])
async def list_runs_endpoint(limit: int = 100, fixture_id: Optional[str] = None):
    rows = db_list_runs(limit=limit, fixture_id=fixture_id)
    return [_row_to_summary(r) for r in rows]


@router.get("/{run_id}", response_model=RunDetail)
async def get_run_endpoint(run_id: str):
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, f"run {run_id} not found")
    summary = _row_to_summary(row)
    trace = _load_trace_jsonl(Path(row["run_dir"]))
    return RunDetail(
        **summary.model_dump(),
        prompt_bundle=json.loads(row["prompt_bundle"]),
        run_dir=row["run_dir"],
        final_json_path=row["final_json_path"],
        trace=trace,
    )


@router.get("/{run_id}/nodes/{node_id}", response_model=NodeDetail)
async def get_node_endpoint(run_id: str, node_id: str):
    """读节点 IO 缓存 JSON（fork 前查看上下文用）。"""
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    run_dir = Path(row["run_dir"])

    matches = list(run_dir.glob(f"{node_id}_*.json"))
    if not matches:
        raise HTTPException(404, f"node {node_id} cache not found in {run_dir}")
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    return NodeDetail(
        node_id=node_id,
        name=payload.get("name", ""),
        node_type=payload.get("node_type", "llm"),
        prompt_version=payload.get("prompt_version"),
        status=payload.get("status", "succeeded"),
        input=payload.get("input"),
        output=payload.get("output"),
        tokens=payload.get("tokens"),
        elapsed_ms=payload.get("elapsed_ms"),
        error=payload.get("error"),
    )


# ============================================================================
# 创建 run —— 核心实现
# ============================================================================

@router.post("", response_model=RunSummary)
async def create_run(req: RunCreateRequest):
    """触发一次 step2 run。立即返回 run_id；后台异步执行；前端订阅 /stream 看进度。"""
    # 1. 解析 fixture
    fixture = _resolve_fixture(req)

    # 2. 装配 orchestrator 所需的对象
    from orchestrator.llm import LLMClient
    from orchestrator.orchestrator import Step2Config, Step2Fixture, Step2Run
    from orchestrator.prompts import PromptBundle

    color_items = _parse_color_images(Path(fixture["color_folder"]))
    if not color_items:
        raise HTTPException(400, f"no valid color images in {fixture['color_folder']}")

    # 趋势 JSON
    trend_json = json.loads(Path(fixture["trend_json_path"]).read_text(encoding="utf-8"))
    trend_name = Path(fixture["trend_json_path"]).stem

    step2_fixture = Step2Fixture(
        trend_name=trend_name,
        style_no=Path(fixture["ref_image_path"]).stem,
        ref_image_path=Path(fixture["ref_image_path"]),
        color_items=color_items,
        trend_json=trend_json,
        gender_ratio=fixture["gender_ratio"],
        num_designs_k=fixture["num_designs_k"],
    )

    # API key
    api_key = os.environ.get("OPENAI_API_KEY", "dryrun-placeholder")
    if not req.dry_run and api_key == "dryrun-placeholder":
        raise HTTPException(400, "OPENAI_API_KEY 未设置且非 dry-run 模式")

    llm = LLMClient(
        api_key=api_key,
        default_model=req.model,
        default_max_tokens=req.max_tokens,
    )
    bundle = PromptBundle(
        prompt_root=ROOT / "prompts" / "step2",
        versions=req.prompt_bundle.versions or {},
    )
    config = Step2Config(
        model=req.model,
        max_tokens=req.max_tokens,
        color_recognition_concurrency=req.color_concurrency,
        max_audit_rounds=req.max_audit_rounds,
        a_tier_quota=req.a_tier_quota,
        dry_run=req.dry_run,
    )

    output_root = ROOT / "runs"
    output_root.mkdir(parents=True, exist_ok=True)

    # 3. 构造 Step2Run（它内部会生成 run_id 并初始化 TraceWriter，但还没开始跑事件）
    run = Step2Run(
        fixture=step2_fixture,
        bundle=bundle,
        llm=llm,
        output_root=output_root,
        config=config,
        verbose=False,                # 后端不打 stdout，事件全走 SSE
    )

    # 4. 拿到真实 run_id 后，把 trace callback 接到 SSE event_bus
    # 注意：run.writer.callback 是 TraceWriter 内部存的 external_callback 引用
    run.writer.callback = make_trace_callback(run.run_id)

    # 5. DB 写入（status=running）
    db_insert_run(
        run_id=run.run_id,
        trend_name=trend_name,
        style_no=step2_fixture.style_no,
        gender_ratio=fixture["gender_ratio"],
        num_designs_k=fixture["num_designs_k"],
        prompt_bundle=bundle.to_meta_dict(),
        run_dir=str(run.run_dir),
        fixture_id=req.fixture_id,
    )

    # 6. 后台异步执行——orchestrator 内部用 sync OpenAI SDK 会阻塞 event loop，
    #    所以把整个 run.run() 跑到线程池里独立 event loop 上，让 FastAPI 主 loop 保持响应
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_in_thread, run)

    # 7. 立即返回
    row = db_get_run(run.run_id)
    return _row_to_summary(row)


def _run_in_thread(run) -> None:
    """在独立线程 + 独立 event loop 上跑 orchestrator。
    这样 FastAPI 主 event loop 不会被 OpenAI SDK 的 sync 调用阻塞，
    POST /runs 能立即返回，GET /stream 能持续推送 SSE 事件。"""
    t0 = time.time()
    try:
        # 在这个线程内创建独立 event loop 跑 run.run() (async)
        result = asyncio.run(run.run())
        elapsed_ms = int((time.time() - t0) * 1000)
        totals = run.writer._totals
        audit_passed = result.get("audit_passed", False)
        audit_rounds = len(result.get("audit_history", []))

        db_mark_run_finished(
            run_id=run.run_id,
            status="succeeded" if audit_passed else "succeeded_with_audit_warnings",
            audit_passed=audit_passed,
            audit_rounds=audit_rounds,
            total_tokens_in=totals.get("input_tokens", 0),
            total_tokens_out=totals.get("output_tokens", 0),
            elapsed_ms=elapsed_ms,
            final_json_path=result.get("final_output_path"),
        )
    except Exception as exc:
        elapsed_ms = int((time.time() - t0) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
        print(f"[run {run.run_id}] FAILED: {err}\n{tb}", file=sys.stderr)
        db_mark_run_finished(
            run_id=run.run_id,
            status="failed",
            elapsed_ms=elapsed_ms,
        )
        event_bus.publish(run.run_id, {
            "event": "run_finished",
            "status": "failed",
            "error": err,
        })


# ============================================================================
# Fork
# ============================================================================

@router.post("/{run_id}/fork", response_model=RunSummary)
async def fork_run(run_id: str, req: RunForkRequest):
    """
    从某 run fork：复用源 run 的节点 IO 缓存，仅对 prompt 改动的节点（及下游）重跑。

    工作流：
      1. 拿源 run 的 fixture（trend/style/...）
      2. 拿源 run 的 prompt_bundle 作为 baseline
      3. 用 req.prompt_overrides 覆盖 baseline → 新 bundle
      4. 构造 Step2Run with replay_from_dir = 源 run_dir
      5. orchestrator 跑节点时自动判断 cache hit/miss
    """
    src_row = db_get_run(run_id)
    if not src_row:
        raise HTTPException(404, f"源 run {run_id} 不存在")

    src_run_dir = Path(src_row["run_dir"])
    if not src_run_dir.is_dir():
        raise HTTPException(400, f"源 run 目录已不存在：{src_run_dir}")

    # 重建源 fixture（trend/style 信息存在 trace.jsonl 的 run_started 里）
    src_trace = src_run_dir / "trace.jsonl"
    if not src_trace.is_file():
        raise HTTPException(400, f"源 run 缺少 trace.jsonl，无法 fork")
    first_line = src_trace.read_text(encoding="utf-8").splitlines()[0]
    src_run_started = json.loads(first_line)
    src_fixture_dict = src_run_started.get("fixture") or {}
    src_bundle = src_run_started.get("prompt_bundle") or {}

    ref_image_path = Path(_resolve_path_for_fork(src_fixture_dict, "ref_image"))
    # trend_json_path：优先 meta；缺失则按 ai-supply 默认布局兜底
    if "trend_json_path" in src_fixture_dict:
        trend_json_path = Path(src_fixture_dict["trend_json_path"])
    else:
        trend_name = src_fixture_dict.get("trend_name")
        if not trend_name:
            raise HTTPException(400, "源 fixture 缺少 trend_name，无法反推趋势路径")
        # 默认布局：项目根 sibling 的 ai-supply/趋势报告/{name}/{name}.json
        trend_json_path = (
            ROOT.parent / "ai-supply" / "趋势报告" / trend_name / f"{trend_name}.json"
        )
        if not trend_json_path.is_file():
            raise HTTPException(
                400,
                f"兜底路径推断不到趋势 JSON：{trend_json_path}\n"
                f"  请在源 run 重新生成时手动确保 fixture meta 含 trend_json_path",
            )

    color_codes = src_fixture_dict.get("color_codes") or []
    gender_ratio = src_fixture_dict.get("gender_ratio") or "男女比接近1:1"
    num_designs_k = int(src_fixture_dict.get("K") or src_fixture_dict.get("num_designs_k") or 1)
    # color_folder 优先 meta，缺失就从 ref_image 推
    color_folder = Path(src_fixture_dict.get("color_folder") or (ref_image_path.parent / ref_image_path.stem))
    if not color_folder.is_dir():
        raise HTTPException(400, f"色号文件夹不存在：{color_folder}")

    # 合并 bundle：source @v 给底；req.prompt_overrides 覆盖
    # src_bundle 格式 = "01_style_analysis.md@current"，需要还原成 versions dict
    new_versions: dict[str, str] = {}
    for node_id_key, label in src_bundle.items():
        # label 形如 "01_style_analysis.md@v2"
        if "@" in label:
            new_versions[node_id_key] = label.rsplit("@", 1)[1]
    # 用户 fork 时指定的覆盖
    for k, v in (req.prompt_overrides or {}).items():
        new_versions[k] = v

    # 装配
    from orchestrator.llm import LLMClient
    from orchestrator.orchestrator import Step2Config, Step2Fixture, Step2Run
    from orchestrator.prompts import PromptBundle

    color_items = _parse_color_images(color_folder)
    if not color_items:
        raise HTTPException(400, f"色号文件夹无有效图片：{color_folder}")

    trend_json = json.loads(trend_json_path.read_text(encoding="utf-8"))
    trend_name = trend_json_path.stem
    style_no = ref_image_path.stem

    step2_fixture = Step2Fixture(
        trend_name=trend_name,
        style_no=style_no,
        ref_image_path=ref_image_path,
        color_items=color_items,
        trend_json=trend_json,
        gender_ratio=gender_ratio,
        num_designs_k=num_designs_k,
    )

    api_key = os.environ.get("OPENAI_API_KEY", "dryrun-placeholder")
    if api_key == "dryrun-placeholder":
        raise HTTPException(400, "fork 需要 OPENAI_API_KEY")

    llm = LLMClient(api_key=api_key, default_model="gpt-5.5", default_max_tokens=500000)
    bundle = PromptBundle(prompt_root=ROOT / "prompts" / "step2", versions=new_versions)
    config = Step2Config(model="gpt-5.5", dry_run=False)

    output_root = ROOT / "runs"
    output_root.mkdir(parents=True, exist_ok=True)

    new_run = Step2Run(
        fixture=step2_fixture,
        bundle=bundle,
        llm=llm,
        output_root=output_root,
        config=config,
        verbose=False,
        replay_from_dir=src_run_dir,         # ← 关键：复用缓存
    )
    new_run.writer.callback = make_trace_callback(new_run.run_id)

    db_insert_run(
        run_id=new_run.run_id,
        trend_name=trend_name,
        style_no=style_no,
        gender_ratio=gender_ratio,
        num_designs_k=num_designs_k,
        prompt_bundle=bundle.to_meta_dict(),
        run_dir=str(new_run.run_dir),
        parent_run_id=run_id,
        fork_from_node=req.from_node_id,
    )

    # 后台跑（线程池）
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_in_thread, new_run)

    return _row_to_summary(db_get_run(new_run.run_id))


def _resolve_path_for_fork(fixture_dict: dict, key: str) -> str:
    """从源 fixture meta 取路径字段（容错命名差异）。"""
    if key in fixture_dict:
        return fixture_dict[key]
    # ref_image vs ref_image_path 兼容
    alt = {"ref_image": "ref_image_path", "ref_image_path": "ref_image"}.get(key)
    if alt and alt in fixture_dict:
        return fixture_dict[alt]
    raise HTTPException(400, f"源 fixture 缺少 {key} 字段")


@router.post("/{run_id}/nodes/{node_id}/retry", response_model=RunSummary)
async def retry_node(run_id: str, node_id: str, req: NodeRetryRequest):
    raise HTTPException(501, "单节点 retry 在 M4.2+ 实装；目前用 fork 替代")


# ============================================================================
# SSE & 日志
# ============================================================================

@router.get("/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE 实时事件流。订阅时若 run 已开始，会先回放缓冲事件。"""
    return StreamingResponse(
        sse_stream(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ============================================================================
# 横向对比（M5）
# ============================================================================

@router.get("/compare/multi")
async def compare_runs_metrics(ids: str):
    """
    对多个 run 计算 eval 指标向量，返回扁平表供前端横向对比。

    Args:
        ids: 逗号分隔的 run_id 列表，至少 2 个

    Returns:
        {
          "rows": [
            {
              "run_id": "...",
              "label": "...",                  # 人类可读标签
              "trend_name": "...",
              "style_no": "...",
              "prompt_bundle": {...},
              "created_at": "...",
              "metrics": { schema_version, A_..., B_..., C_..., D_..., E_..., F_... }
            }
          ]
        }
    """
    run_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if len(run_ids) < 2:
        raise HTTPException(400, "至少需要 2 个 run_id 才能对比")

    # 加载 eval/metrics.py（项目根 sibling 的 eval 包）
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        from eval.metrics import all_metrics
    except ImportError as e:
        raise HTTPException(500, f"加载 eval/metrics.py 失败：{e}")

    rows = []
    errors = []
    for rid in run_ids:
        row = db_get_run(rid)
        if not row:
            errors.append({"run_id": rid, "error": "DB 找不到该 run"})
            continue
        # M5 bug 修复：优先用 run_dir/final_output.json（每个 run 独立、不会被覆盖）
        # 兜底兼容 DB 里旧的 final_json_path（可能已被后续 run 覆盖，不可靠）
        run_dir = Path(row["run_dir"])
        candidate_paths = [
            run_dir / "final_output.json",                # 新路径，每 run 独立
            Path(row["final_json_path"]) if row.get("final_json_path") else None,
        ]
        final_path = next((p for p in candidate_paths if p and p.is_file()), None)
        if final_path is None:
            errors.append({
                "run_id": rid,
                "error": f"final JSON 文件不存在（run_dir={run_dir}，可能没跑完或被后续 run 覆盖）",
            })
            continue

        try:
            final = json.loads(final_path.read_text(encoding="utf-8"))
            metrics = all_metrics(final)
        except Exception as e:
            errors.append({"run_id": rid, "error": f"解析/算指标失败：{e}"})
            continue

        # 短 label：trend 前 6 字 + style 后 6 字 + run_id 后 6
        trend_short = (row["trend_name"] or "")[:8]
        rid_short = rid.rsplit("-", 1)[-1] if "-" in rid else rid[-6:]
        label = f"{trend_short}·{row['style_no']}·{rid_short}"

        rows.append({
            "run_id": rid,
            "label": label,
            "trend_name": row["trend_name"],
            "style_no": row["style_no"],
            "prompt_bundle": json.loads(row["prompt_bundle"]) if row["prompt_bundle"] else {},
            "created_at": row["created_at"],
            "status": row["status"],
            "parent_run_id": row.get("parent_run_id"),
            "fork_from_node": row.get("fork_from_node"),
            "metrics": metrics,
        })

    return {"rows": rows, "errors": errors}


@router.get("/{run_id}/logs")
async def get_logs(run_id: str):
    """返回原始 trace.jsonl 全文。"""
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    trace_file = Path(row["run_dir"]) / "trace.jsonl"
    if not trace_file.is_file():
        return {"lines": []}
    return {
        "lines": [
            json.loads(l)
            for l in trace_file.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    }


# ============================================================================
# 工具
# ============================================================================

def _resolve_fixture(req: RunCreateRequest) -> dict:
    """从 fixture_id 或 inline 参数解析出 fixture dict。"""
    if req.fixture_id:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM fixtures WHERE id = ?", (req.fixture_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f"fixture {req.fixture_id} not found")
            return {
                "trend_json_path": row["trend_json_path"],
                "ref_image_path": row["ref_image_path"],
                "color_folder": row["color_folder"],
                "gender_ratio": row["gender_ratio"],
                "num_designs_k": row["num_designs_k"],
                "selected_colors": json.loads(row["selected_colors"]) if row["selected_colors"] else None,
            }
        finally:
            conn.close()
    # inline 模式
    if not (req.trend_json_path and req.ref_image_path and req.color_folder):
        raise HTTPException(
            400,
            "需要提供 fixture_id 或 (trend_json_path + ref_image_path + color_folder)",
        )
    return {
        "trend_json_path": req.trend_json_path,
        "ref_image_path": req.ref_image_path,
        "color_folder": req.color_folder,
        "gender_ratio": req.gender_ratio,
        "num_designs_k": req.num_designs_k,
        "selected_colors": req.selected_colors,
    }


def _parse_color_images(folder: Path) -> list[dict]:
    """与 run_agent.py 一致的色号图解析。"""
    results = []
    pattern = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        m = pattern.match(f.stem)
        if not m:
            results.append({"code": f.stem, "name": f.stem, "path": f})
            continue
        code = m.group(1).strip()
        raw_label = m.group(2).strip()
        label = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw_label).strip() or raw_label
        results.append({"code": code, "name": label, "path": f})
    return results


def _row_to_summary(row: dict) -> RunSummary:
    return RunSummary(
        id=row["id"],
        fixture_id=row.get("fixture_id"),
        trend_name=row["trend_name"],
        style_no=row["style_no"],
        gender_ratio=row["gender_ratio"],
        num_designs_k=row["num_designs_k"],
        status=row["status"],
        audit_passed=bool(row["audit_passed"]) if row.get("audit_passed") is not None else None,
        audit_rounds=row.get("audit_rounds"),
        total_tokens_in=row.get("total_tokens_in"),
        total_tokens_out=row.get("total_tokens_out"),
        elapsed_ms=row.get("elapsed_ms"),
        parent_run_id=row.get("parent_run_id"),
        fork_from_node=row.get("fork_from_node"),
        created_at=row["created_at"],
    )


def _load_trace_jsonl(run_dir: Path) -> list[dict]:
    trace_file = run_dir / "trace.jsonl"
    if not trace_file.is_file():
        return []
    return [
        json.loads(l)
        for l in trace_file.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
