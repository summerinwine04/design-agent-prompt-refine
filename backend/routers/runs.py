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
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.db import (
    get_conn,
    get_openai_api_key,
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

    # v6/v7 图库输入源支持：无 trend_json_path 时兜底空 JSON
    input_source = fixture.get("input_source") or "trend_report"
    pattern_library_folder = fixture.get("pattern_library_path") or ""
    pattern_library_selected_files = fixture.get("pattern_library_selected_files") or []
    # v7 多主题：跨文件夹分组选择 [{folder, files:[...]}, ...]
    pattern_library_selections = fixture.get("pattern_library_selections") or []

    design_mode = fixture.get("design_mode") or "MULTI_TOPIC"

    if input_source == "pattern_library":
        # 图库输入源：trend_json 用 {} 兜底；trend_name：
        #   收敛模式 = 文件夹名（v6 行为）；多主题 = 常量（历史避重按 (trend, style) 配对）
        trend_json = {}
        if design_mode == "MULTI_TOPIC":
            trend_name = "印花图案库"
        else:
            trend_name = pattern_library_folder or "pattern_library"
    else:
        # 趋势报告输入源：正常读取
        if not fixture.get("trend_json_path"):
            raise HTTPException(400, "trend_report 输入源要求 trend_json_path 非空")
        trend_json = json.loads(Path(fixture["trend_json_path"]).read_text(encoding="utf-8"))
        trend_name = Path(fixture["trend_json_path"]).stem

    # 组装图库参考图（v6 收敛：单文件夹 core_ref；v7 多主题：跨文件夹 reference_set）
    core_ref_image_paths: list[str] = []
    reference_set: list[dict] = []
    if input_source == "pattern_library":
        from backend.routers.pattern_library import get_pattern_library_root
        pl_root = get_pattern_library_root()

        if design_mode == "MULTI_TOPIC":
            # v7：每个来源文件夹 = 一个方向（主题），数量不设限（已拍板）
            if not pattern_library_selections:
                raise HTTPException(400, "多主题 × 图库输入要求 pattern_library_selections（按文件夹分组的选图）非空")
            import hashlib as _hashlib
            for sel in pattern_library_selections:
                folder = (sel.get("folder") or "").strip()
                files = sel.get("files") or []
                if not folder or not files:
                    continue
                abs_files = []
                for fn in files:
                    p = pl_root / folder / fn
                    if not p.is_file():
                        raise HTTPException(400, f"图库文件不存在：{p}")
                    abs_files.append(str(p.resolve()))
                topic_id = "PL-" + _hashlib.md5(folder.encode("utf-8")).hexdigest()[:4].upper()
                reference_set.append({
                    "direction": folder,
                    "topic_id": topic_id,
                    "files": abs_files,
                })
            if not reference_set:
                raise HTTPException(400, "图库输入源要求至少选 1 个方向且每方向至少 1 张图")
        else:
            # v6 收敛：单文件夹；张数上限已解除（原 3 张硬上限按拍板取消）
            for fn in pattern_library_selected_files:
                p = pl_root / pattern_library_folder / fn
                if not p.is_file():
                    raise HTTPException(400, f"图库文件不存在：{p}")
                core_ref_image_paths.append(str(p.resolve()))
            if not core_ref_image_paths:
                raise HTTPException(400, "图库输入源要求至少 1 张核心参考图")

    if design_mode in ("SINGLE_TOPIC_STRONG", "COLLECTION_2SKU"):
        # v5 CONVERGE：装配 styles + looks（Mode B 自动包装；Mode C 用户预选）
        styles, looks, primary_style_no, combined_style_no = _assemble_converge_inputs(fixture)
        primary = styles[0]
        step2_fixture = Step2Fixture(
            trend_name=trend_name,
            style_no=combined_style_no,
            ref_image_path=Path(primary["ref_image_path"]),
            color_items=primary["color_items"],
            trend_json=trend_json,
            gender_ratio=fixture["gender_ratio"],
            num_designs_k=fixture["num_designs_k"],
            design_mode=design_mode,
            styles=styles,
            looks=looks,
        )
    else:
        color_items = _parse_color_images(Path(fixture["color_folder"]))
        if fixture.get("selected_colors"):
            # 兼容两种引用：文件名（新前端，唯一）或色号 code（老夹具；同码多色会一起匹配）
            wanted = set(fixture["selected_colors"])
            color_items = [
                c for c in color_items
                if c["code"] in wanted or Path(c["path"]).name in wanted
            ] or color_items
        if not color_items:
            raise HTTPException(400, f"no valid color images in {fixture['color_folder']}")

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
    api_key = get_openai_api_key() or "dryrun-placeholder"
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
        reuse_style_analysis=req.reuse_style_analysis,
        # v6/v7 图库输入源透传到 orchestrator
        input_source=input_source,
        pattern_library_folder=pattern_library_folder,
        core_ref_image_paths=core_ref_image_paths,
        reference_set=reference_set,
    )

    # 历史避重：查同款+同趋势最近 5 轮 succeeded 的 final JSON，抽出选过的主题 + 同色号的方案摘要
    if req.diversity_avoid_history:
        try:
            from backend.db import list_recent_successful_runs
            hist_rows = list_recent_successful_runs(
                trend_name=trend_name,
                style_no=step2_fixture.style_no,
                limit=5,
            )
            _seen_topic_ids: set[str] = set()
            _seen_topic_names: set[str] = set()
            recent_color_designs: dict[str, list[dict]] = {}
            for row in hist_rows:
                fj_path = row.get("final_json_path")
                if not fj_path:
                    continue
                try:
                    fj = json.loads(Path(fj_path).read_text(encoding="utf-8"))
                except Exception:
                    continue
                top = fj.get("step2_改款方案") or {}
                # 2.4 主题选择
                topic_sel = top.get("主题选择") or {}
                for t in topic_sel.get("选用主题列表") or []:
                    tid = (t.get("子主题编号") or "").strip()
                    tname = (t.get("子主题名称") or "").strip()
                    # 用 (id, name) pair 组合去重，避免仅编号相同名字不同（历史 prompt 迭代）
                    key = (tid, tname)
                    if key in _seen_topic_ids:
                        continue
                    _seen_topic_ids.add(key)
                    if tid:
                        config.avoid_topic_ids.append(tid)
                    else:
                        config.avoid_topic_ids.append("")
                    config.avoid_topic_names.append(tname)
                # 2.5 单色方案（按色号代码归组）
                for color_row in top.get("色号方案列表") or []:
                    code = (color_row.get("色号代码") or "").strip()
                    if not code:
                        continue
                    for dp in color_row.get("设计方案") or []:
                        recent_color_designs.setdefault(code, []).append({
                            "子主题名称": dp.get("选用子主题名称") or dp.get("子主题名称"),
                            "图案关键词": dp.get("图案关键词"),
                            "方案说明": dp.get("方案说明") or "",
                        })
            config.recent_color_designs = recent_color_designs
        except Exception as _e:
            # 查历史失败不阻断真跑；打日志由 uvicorn console 看
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "diversity avoid_history lookup failed: %s", _e,
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
        design_mode=design_mode,
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

    # v5：CONVERGE run 的 fork —— 从 meta 里的 styles/looks 重建 fixture。
    # look_id 由成员 uid 哈希生成（确定性），重建后与源 run 的 cache slug 完全对齐，
    # 未被 force_miss 的节点全部走缓存。
    src_mode = src_fixture_dict.get("design_mode") or "MULTI_TOPIC"

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

    trend_json = json.loads(trend_json_path.read_text(encoding="utf-8"))
    trend_name = trend_json_path.stem

    if src_mode != "MULTI_TOPIC":
        # ---- CONVERGE fork：从 meta 的 styles/looks 重建 ----
        meta_styles = src_fixture_dict.get("styles") or []
        meta_looks = src_fixture_dict.get("looks") or []
        if not meta_styles or not meta_looks:
            raise HTTPException(400, "源 run 的 fixture meta 缺少 styles/looks（旧版本 run），无法 fork")
        styles_spec = []
        for s in meta_styles:
            rp = Path(s["ref_image_path"])
            if not rp.is_file():
                raise HTTPException(400, f"款位 {s.get('role')} 的款图不存在：{rp}")
            styles_spec.append({
                "role": s["role"],
                "ref_image_path": str(rp),
                "color_folder": str(rp.parent / rp.stem),
                "selected_colors": None,
            })
        conv_fixture = {
            "design_mode": src_mode,
            "styles": styles_spec,
            # meta looks 的 members 已是 uid（文件名），_resolve_member 直接命中；
            # look_id 由 members 哈希重新生成，与源 run 一致 → cache slug 对齐
            "looks": [{"name": lk.get("name"), "members": lk.get("members")} for lk in meta_looks],
        }
        styles, looks, primary_style_no, combined_style_no = _assemble_converge_inputs(conv_fixture)
        primary = styles[0]
        style_no = combined_style_no
        step2_fixture = Step2Fixture(
            trend_name=trend_name,
            style_no=combined_style_no,
            ref_image_path=Path(primary["ref_image_path"]),
            color_items=primary["color_items"],
            trend_json=trend_json,
            gender_ratio=gender_ratio,
            num_designs_k=num_designs_k,
            design_mode=src_mode,
            styles=styles,
            looks=looks,
        )
    else:
        # ---- DIVERGE fork（原逻辑） ----
        # color_folder 优先 meta，缺失就从 ref_image 推
        color_folder = Path(src_fixture_dict.get("color_folder") or (ref_image_path.parent / ref_image_path.stem))
        if not color_folder.is_dir():
            raise HTTPException(400, f"色号文件夹不存在：{color_folder}")
        color_items = _parse_color_images(color_folder)
        if not color_items:
            raise HTTPException(400, f"色号文件夹无有效图片：{color_folder}")
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

    api_key = get_openai_api_key() or "dryrun-placeholder"
    if api_key == "dryrun-placeholder":
        raise HTTPException(400, "fork 需要 OPENAI_API_KEY")

    llm = LLMClient(api_key=api_key, default_model="gpt-5.5", default_max_tokens=500000)
    bundle = PromptBundle(prompt_root=ROOT / "prompts" / "step2", versions=new_versions)
    config = Step2Config(model="gpt-5.5", dry_run=False)

    output_root = ROOT / "runs"
    output_root.mkdir(parents=True, exist_ok=True)

    # 解析 from_node_id → 业务编号集合，加入 explicit_force_miss
    # 支持格式："2.2" / "node_2.2" / "2_2_005" / "2_2_loop_002"
    #           v5："2_4s_018" → 2.4s / "2_4_5_019" → 2.4.5 / "2_5L_loop_020" → 2.5L
    _VALID_BIZ_IDS = {"2.1", "2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"}
    explicit_force_miss = set()
    fnid = (req.from_node_id or "").strip()
    if fnid.startswith("node_"):
        fnid = fnid[len("node_"):]
    if fnid:
        # 如果包含 "_" 形如 "2_2_005"，去掉 counter 段，剩下的拼成 "2.2"
        if "_" in fnid:
            parts = fnid.split("_")
            # 找第一个 ≥2 位纯数字段（counter），前面所有段为业务部分
            biz_parts: list[str] = []
            for p in parts:
                if p.isdigit() and len(p) >= 2:
                    break
                biz_parts.append(p)
            if biz_parts:
                biz_id = ".".join(biz_parts)
                # 去掉 loop 后缀（"2.2.loop" → "2.2"、"2.5L.loop" → "2.5L"）
                if biz_id.endswith(".loop"):
                    biz_id = biz_id[: -len(".loop")]
                # 兼容旧规则的多段截断，但 2.4.5 是合法三段 id 不能截
                if biz_id not in _VALID_BIZ_IDS and "." in biz_id:
                    biz_id = ".".join(biz_id.split(".")[:2])
                if biz_id in _VALID_BIZ_IDS:
                    explicit_force_miss.add(biz_id)
        elif fnid in _VALID_BIZ_IDS:
            explicit_force_miss.add(fnid)

    new_run = Step2Run(
        fixture=step2_fixture,
        bundle=bundle,
        llm=llm,
        output_root=output_root,
        config=config,
        verbose=False,
        replay_from_dir=src_run_dir,         # ← 关键：复用缓存
        explicit_force_miss=explicit_force_miss,
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
        design_mode=src_mode,
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
# 单色重识别（针对 2.2 失败的色号原地修复）
# ============================================================================

from pydantic import BaseModel as _BM

class RecognizeColorRequest(_BM):
    color_code: str
    color_name: str


@router.post("/{run_id}/recognize-color")
async def recognize_color(run_id: str, req: RecognizeColorRequest):
    """
    对某 run 的某个色号原地重新跑一次 2.2 识别，结果覆盖该节点的 cache JSON。

    适用场景：原 run 跑的时候 2.2 单色识别失败（被 2.2 容错跳过留下 _recognition_failed），
    用户在工作台节点详情里点「↻ 重新识别」修复。

    工作流：
      1. 从 DB 拿 run 的 run_dir、prompt_bundle、fixture meta
      2. 找该色号对应的 cache JSON 文件（用 slug 模糊匹配）
      3. 拿原 input.user_prompt 当 prompt 再调一次 LLM
      4. 解析新结果，覆盖 cache 文件的 output 字段
      5. 返回新结果给前端

    注意：这只修 cache，不重新跑下游 2.3/2.4。要重跑下游用 fork。
    """
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, f"run {run_id} not found")
    run_dir = Path(row["run_dir"])
    if not run_dir.is_dir():
        raise HTTPException(400, f"run_dir 不存在：{run_dir}")

    # 找该色号对应的 cache JSON——slug 形如 颜色识别_DSC09253_中灰
    target_slug_fragment = f"颜色识别_{req.color_code}_{req.color_name}"
    candidates = []
    for f in run_dir.glob("*.json"):
        if f.name == "trace.jsonl" or f.name == "final_output.json":
            continue
        if target_slug_fragment in f.stem:
            candidates.append(f)
    if not candidates:
        raise HTTPException(404, f"找不到该色号的 cache 文件：slug 含 '{target_slug_fragment}'")
    cache_path = candidates[0]

    cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_input = cached_payload.get("input") or {}
    user_prompt = cached_input.get("user_prompt")
    if not user_prompt:
        raise HTTPException(400, "原节点 input 中没有 user_prompt 字段，无法复用")

    # 找色号样图——从源 fixture 推
    src_trace = run_dir / "trace.jsonl"
    color_folder_path = None
    if src_trace.is_file():
        try:
            first_line = src_trace.read_text(encoding="utf-8").splitlines()[0]
            fixture = json.loads(first_line).get("fixture") or {}
            if "color_folder" in fixture:
                color_folder_path = Path(fixture["color_folder"])
        except Exception:
            pass
    if color_folder_path is None:
        # 兜底：从 ai-supply 默认路径
        color_folder_path = ROOT.parent / "ai-supply" / "款图" / row["style_no"]
    if not color_folder_path.is_dir():
        raise HTTPException(400, f"色号文件夹不存在：{color_folder_path}")

    # 找色号样图文件名
    color_filename_map = _parse_color_filename_map(color_folder_path)
    color_img = color_filename_map.get((req.color_code, req.color_name))
    if not color_img:
        raise HTTPException(404, f"色号样图找不到：({req.color_code}, {req.color_name})")
    color_img_path = color_folder_path / color_img

    # 拿 prompt bundle 中 2.2 的 system prompt
    from orchestrator.llm import LLMClient
    from orchestrator.prompts import PromptBundle
    bundle_dict = json.loads(row["prompt_bundle"]) if row.get("prompt_bundle") else {}
    versions = {}
    for k, v in bundle_dict.items():
        if "@" in v:
            versions[k] = v.rsplit("@", 1)[1]
    bundle = PromptBundle(prompt_root=ROOT / "prompts" / "step2", versions=versions)
    system_prompt, _ = bundle.load("2.2")

    api_key = get_openai_api_key()
    if not api_key:
        raise HTTPException(400, "OPENAI_API_KEY 未设置")
    llm = LLMClient(api_key=api_key)

    # 调 LLM（sync，在线程池跑）
    loop = asyncio.get_running_loop()

    def call_llm():
        return llm.call_with_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=[color_img_path],
            output_marker_regex=r"##\s*STEP\s*2\.2\s*OUTPUT",
        )

    try:
        result = await loop.run_in_executor(None, call_llm)
    except Exception as e:
        raise HTTPException(500, f"重新识别失败：{type(e).__name__}: {e}")

    if result.parsed_json is None:
        raise HTTPException(500, "模型输出无法解析为 JSON")

    new_output = dict(result.parsed_json)
    new_output.setdefault("色号代码", req.color_code)
    new_output.setdefault("营销色名", req.color_name)
    # 清除原失败标记（如果有）
    new_output.pop("_recognition_failed", None)
    new_output.pop("_error", None)

    # 覆盖 cache 文件
    cached_payload["output"] = new_output
    cached_payload["_re_recognized_at"] = datetime.utcnow().isoformat() + "Z"
    cache_path.write_text(
        json.dumps(cached_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 同步失效款级缓存：该色号图的旧识别结果不能再被后续 run 复用
    # （不直接写新结果——key 需要底色/模型上下文，让下个 run 重新识别一次并回填更稳）
    try:
        from orchestrator.style_cache import StyleCache
        StyleCache(ROOT / "runs" / "_style_cache").invalidate_color(
            row["style_no"], color_img,
        )
    except Exception:
        pass

    # 翻盘 trace.jsonl 节点状态：追加一条 node_completed 事件，覆盖原 node_failed
    # 前端 TraceTreePanel 按顺序处理事件，最后的 status 以最新的为准
    node_id = _extract_node_id_from_cache_filename(cache_path.stem)
    if node_id:
        trace_path = run_dir / "trace.jsonl"
        if trace_path.is_file():
            child_event = {
                "event": "node_completed",
                "timestamp": time.time(),
                "node_id": node_id,
                "output_summary": {
                    "_re_recognized": True,
                    "色号代码": req.color_code,
                    "营销色名": req.color_name,
                    "是否需要变色": new_output.get("是否需要变色"),
                },
                "tokens": {"input": result.tokens_in, "output": result.tokens_out},
                "elapsed_ms": int((result.elapsed_sec or 0) * 1000),
            }
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(child_event, ensure_ascii=False) + "\n")
                f.flush()

            # 看父 loop 节点是否要一起翻盘
            # 老 run 在 2.2 容错修复之前跑的，一个色号失败会让 gather 抛异常，
            # 导致 loop 父节点也写 node_failed。这里重算父 loop 应有状态。
            parent_id = _find_parent_node_id(trace_path, node_id)
            if parent_id:
                child_status_map = _compute_child_status_map(trace_path, parent_id)
                if child_status_map and all(s == "succeeded" for s in child_status_map.values()):
                    parent_event = {
                        "event": "node_completed",
                        "timestamp": time.time(),
                        "node_id": parent_id,
                        "output_summary": {
                            "_re_recognized_triggered": True,
                            "子节点数": len(child_status_map),
                            "全部成功": True,
                        },
                    }
                    with open(trace_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(parent_event, ensure_ascii=False) + "\n")
                        f.flush()

            # 再看整个 run 是否已经没有任何 failed 节点
            # 是 → DB runs 表的 status 从 failed 翻成 succeeded_with_audit_warnings
            # （保守做法：不知道审计实际是否通过，所以不动 audit_passed）
            # 注意：直接 SQL UPDATE 单字段，不用 mark_run_finished —— 后者会把
            #       audit/tokens/elapsed 全清空，破坏历史 run 信息
            if row.get("status") == "failed" and not _has_any_failed_node(trace_path):
                from backend.db import get_conn
                conn = get_conn()
                try:
                    conn.execute(
                        "UPDATE runs SET status = ? WHERE id = ?",
                        ("succeeded_with_audit_warnings", run_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

    return {
        "ok": True,
        "color_code": req.color_code,
        "color_name": req.color_name,
        "new_output": new_output,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "elapsed_sec": result.elapsed_sec,
        "cache_path": str(cache_path),
        "node_id": node_id,
    }


def _extract_node_id_from_cache_filename(stem: str) -> str | None:
    """从 cache 文件名（如 '2_2_005_颜色识别_DSC09253_中灰'）提取 node_id（'2_2_005'）。

    规则：找到第一个 ≥2 位纯数字段，该段及之前所有段拼起来就是 node_id。
    """
    parts = stem.split("_")
    for i, p in enumerate(parts):
        if p.isdigit() and len(p) >= 2:
            return "_".join(parts[: i + 1])
    return None


def _find_parent_node_id(trace_path: Path, node_id: str) -> str | None:
    """从 trace.jsonl 找该 node_id 的 node_started 事件，提取 parent_id。"""
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") == "node_started" and ev.get("node_id") == node_id:
                return ev.get("parent_id")
    except Exception:
        pass
    return None


def _compute_child_status_map(trace_path: Path, parent_id: str) -> dict[str, str]:
    """扫 trace.jsonl，返回 parent_id 下所有子节点的最终状态。

    事件按时间顺序，后出现的状态覆盖前出现的（跟前端 TraceTreePanel 渲染逻辑一致）。
    """
    children = set()
    status: dict[str, str] = {}
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            event_type = ev.get("event")
            nid = ev.get("node_id")
            if not nid:
                continue
            if event_type == "node_started" and ev.get("parent_id") == parent_id:
                children.add(nid)
                status[nid] = "running"
            elif event_type == "node_completed" and nid in children:
                status[nid] = "succeeded"
            elif event_type == "node_failed" and nid in children:
                status[nid] = "failed"
    except Exception:
        pass
    return status


def _has_any_failed_node(trace_path: Path) -> bool:
    """扫 trace.jsonl 看是否有任何节点最终状态是 failed。

    按事件顺序处理，最后状态以最新事件为准——重识别追加的 node_completed
    会自动覆盖原 node_failed。
    """
    status: dict[str, str] = {}
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            event_type = ev.get("event")
            nid = ev.get("node_id")
            if not nid:
                continue
            if event_type == "node_started":
                status[nid] = "running"
            elif event_type == "node_completed":
                status[nid] = "succeeded"
            elif event_type == "node_failed":
                status[nid] = "failed"
    except Exception:
        pass
    return any(s == "failed" for s in status.values())


class RedesignSingleColorRequest(_BM):
    color_code: str
    color_name: str


@router.post("/{run_id}/redesign-single-color")
async def redesign_single_color(run_id: str, req: RedesignSingleColorRequest):
    """
    对某 run 的某个色号原地重跑 2.4 单色设计——其他 8 色不动。

    适用场景：单色重识别后，希望"让这一个色号的方案也跟着新识别色重新出一遍"。
    （Fork 是整组 9 色都重跑，太重；这是单色精确级联）

    工作流：
      1. 找该色号的 2.4 cache JSON
      2. 复用原 input.user_prompt（含累积状态，其他色号未变所以仍适用）
      3. 加载 prompt_bundle 里 2.4 的 system_prompt
      4. 调一次 LLM（款图 + 该色号样图）
      5. 覆盖 cache JSON 的 output 字段
      6. 追加 trace.jsonl 的 node_completed 事件

    注意：不重跑 2.5 审计 —— 单色变化对全局审计影响小，若想重新审计请走 Fork。
    """
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, f"run {run_id} not found")
    run_dir = Path(row["run_dir"])
    if not run_dir.is_dir():
        raise HTTPException(400, f"run_dir 不存在：{run_dir}")

    # 找该色号的 2.4 cache（slug 含 单色设计_{code}_{name}）
    target_slug_fragment = f"单色设计_{req.color_code}_{req.color_name}"
    candidates = []
    for f in run_dir.glob("*.json"):
        if f.name in {"trace.jsonl", "final_output.json"}:
            continue
        if target_slug_fragment in f.stem and "2_4" in f.stem:
            candidates.append(f)
    if not candidates:
        raise HTTPException(404, f"找不到该色号的 2.4 cache 文件：slug 含 '{target_slug_fragment}'")
    # 多个候选时取最近修改的（如果之前有重设计版本）
    cache_path = max(candidates, key=lambda p: p.stat().st_mtime)

    cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_input = cached_payload.get("input") or {}
    original_user_prompt = cached_input.get("user_prompt")
    if not original_user_prompt:
        raise HTTPException(400, "原 2.4 节点 input 中没有 user_prompt 字段")

    # 关键：把 2.2 重识别后的新识别色文字描述 注入 user_prompt
    # （否则 LLM 拿到的还是旧识别色，重设计就没意义）
    new_recognition = _load_latest_color_recognition(run_dir, req.color_code, req.color_name)
    user_prompt = original_user_prompt
    if new_recognition:
        user_prompt = _replace_recognition_in_prompt(
            original_user_prompt, new_recognition,
        )

    # 找款图 + 色号样图
    src_trace = run_dir / "trace.jsonl"
    ref_image_path = None
    color_folder_path = None
    if src_trace.is_file():
        try:
            first_line = src_trace.read_text(encoding="utf-8").splitlines()[0]
            fixture = json.loads(first_line).get("fixture") or {}
            if "ref_image" in fixture or "ref_image_path" in fixture:
                ref_image_path = Path(fixture.get("ref_image") or fixture.get("ref_image_path"))
            if "color_folder" in fixture:
                color_folder_path = Path(fixture["color_folder"])
        except Exception:
            pass
    if ref_image_path is None or not ref_image_path.is_file():
        ref_image_path = ROOT.parent / "ai-supply" / "款图" / f"{row['style_no']}.jpg"
    if color_folder_path is None or not color_folder_path.is_dir():
        color_folder_path = ROOT.parent / "ai-supply" / "款图" / row["style_no"]
    if not ref_image_path.is_file():
        raise HTTPException(400, f"款图找不到：{ref_image_path}")

    color_filename_map = _parse_color_filename_map(color_folder_path)
    color_img = color_filename_map.get((req.color_code, req.color_name))
    if not color_img:
        raise HTTPException(404, f"色号样图找不到：({req.color_code}, {req.color_name})")
    color_img_path = color_folder_path / color_img

    # 加载 2.4 system prompt（按 run 的 bundle 版本）
    from orchestrator.llm import LLMClient
    from orchestrator.prompts import PromptBundle
    bundle_dict = json.loads(row["prompt_bundle"]) if row.get("prompt_bundle") else {}
    versions = {}
    for k, v in bundle_dict.items():
        if "@" in v:
            versions[k] = v.rsplit("@", 1)[1]
    bundle = PromptBundle(prompt_root=ROOT / "prompts" / "step2", versions=versions)
    system_prompt, _ = bundle.load("2.4")

    api_key = get_openai_api_key()
    if not api_key:
        raise HTTPException(400, "OPENAI_API_KEY 未设置")
    llm = LLMClient(api_key=api_key)

    loop = asyncio.get_running_loop()

    def call_llm():
        return llm.call_with_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=[ref_image_path, color_img_path],
            output_marker_regex=r"##\s*STEP\s*2\.4\s*OUTPUT",
        )

    try:
        result = await loop.run_in_executor(None, call_llm)
    except Exception as e:
        raise HTTPException(500, f"重设计失败：{type(e).__name__}: {e}")

    if result.parsed_json is None:
        raise HTTPException(500, "2.4 模型输出无法解析为 JSON")

    new_output = dict(result.parsed_json)
    new_output.setdefault("色号代码", req.color_code)
    new_output.setdefault("营销色名", req.color_name)
    # 清除原跳过标记（如果有）
    new_output.pop("_skipped", None)
    new_output.pop("_skip_reason", None)

    # 覆盖 cache 文件
    cached_payload["output"] = new_output
    cached_payload["_re_designed_at"] = datetime.utcnow().isoformat() + "Z"
    cache_path.write_text(
        json.dumps(cached_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 翻盘 trace 节点状态
    node_id = _extract_node_id_from_cache_filename(cache_path.stem)
    if node_id:
        trace_path = run_dir / "trace.jsonl"
        if trace_path.is_file():
            child_event = {
                "event": "node_completed",
                "timestamp": time.time(),
                "node_id": node_id,
                "output_summary": {
                    "_re_designed": True,
                    "色号代码": req.color_code,
                    "营销色名": req.color_name,
                    "设计方案数": len(new_output.get("设计方案", [])),
                },
                "tokens": {"input": result.tokens_in, "output": result.tokens_out},
                "elapsed_ms": int((result.elapsed_sec or 0) * 1000),
            }
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(child_event, ensure_ascii=False) + "\n")
                f.flush()

            # 看 2.4 loop 父节点是否能翻盘
            parent_id = _find_parent_node_id(trace_path, node_id)
            if parent_id:
                child_status_map = _compute_child_status_map(trace_path, parent_id)
                if child_status_map and all(s == "succeeded" for s in child_status_map.values()):
                    parent_event = {
                        "event": "node_completed",
                        "timestamp": time.time(),
                        "node_id": parent_id,
                        "output_summary": {
                            "_re_designed_triggered": True,
                            "子节点数": len(child_status_map),
                            "全部成功": True,
                        },
                    }
                    with open(trace_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(parent_event, ensure_ascii=False) + "\n")
                        f.flush()

            # 整 run 无 failed 节点 → DB status 翻盘
            if row.get("status") == "failed" and not _has_any_failed_node(trace_path):
                from backend.db import get_conn
                conn = get_conn()
                try:
                    conn.execute(
                        "UPDATE runs SET status = ? WHERE id = ?",
                        ("succeeded_with_audit_warnings", run_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

    return {
        "ok": True,
        "color_code": req.color_code,
        "color_name": req.color_name,
        "new_output": new_output,
        "design_count": len(new_output.get("设计方案", [])),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "elapsed_sec": result.elapsed_sec,
        "cache_path": str(cache_path),
        "node_id": node_id,
    }


def _load_latest_color_recognition(run_dir: Path, color_code: str, color_name: str) -> dict | None:
    """从 2.2 节点 cache 加载该色号最新识别结果。"""
    target = f"颜色识别_{color_code}_{color_name}"
    for f in run_dir.glob("*.json"):
        if f.name in {"trace.jsonl", "final_output.json"}:
            continue
        if target in f.stem and "2_2" in f.stem:
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                return payload.get("output")
            except Exception:
                continue
    return None


def _replace_recognition_in_prompt(prompt: str, new_recog: dict) -> str:
    """在 2.4 user_prompt 中重新替换识别结果相关字段。

    2.4 prompt 模板里这一段（来自 04_single_color_design.md USER PROMPT TEMPLATE）：
      - 识别底色：{{识别底色}}
      - 是否需要变色：{{是否需要变色}}
      - 变色描述：{{变色描述}}

    渲染后是固定文本（如「识别底色：{"文字描述":"中灰...","近似Pantone":"...",...}」）。
    我们用正则替换这几行。
    """
    new_recog_json = json.dumps(new_recog.get("识别底色") or {}, ensure_ascii=False)
    needs_recolor = new_recog.get("是否需要变色", False)
    recolor_desc = new_recog.get("变色描述") or ""

    # 替换"识别底色"这一行
    prompt = re.sub(
        r"(- 识别底色：).*",
        lambda m: m.group(1) + new_recog_json,
        prompt,
        count=1,
    )
    # 替换"是否需要变色"
    prompt = re.sub(
        r"(- 是否需要变色：).*",
        lambda m: m.group(1) + str(needs_recolor),
        prompt,
        count=1,
    )
    # 替换"变色描述"
    prompt = re.sub(
        r"(- 变色描述：).*",
        lambda m: m.group(1) + recolor_desc,
        prompt,
        count=1,
    )
    return prompt


def _parse_color_filename_map(folder: Path) -> dict[tuple[str, str], str]:
    """扫描色号目录建 (code, name) → 文件名 映射。"""
    out: dict[tuple[str, str], str] = {}
    if not folder.is_dir():
        return out
    pattern = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')
    for f in sorted(folder.iterdir()):
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


@router.get("/{run_id}/final")
async def get_final_json(run_id: str):
    """返回 run 的 final_output.json（Collection View / 前端消费）。"""
    row = db_get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    candidates = [
        Path(row["run_dir"]) / "final_output.json",
        Path(row["final_json_path"]) if row.get("final_json_path") else None,
    ]
    final_path = next((p for p in candidates if p and p.is_file()), None)
    if final_path is None:
        raise HTTPException(404, "final JSON 不存在（run 可能未完成）")
    return json.loads(final_path.read_text(encoding="utf-8"))


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
    """从 fixture_id 或 inline 参数解析出 fixture dict（含 v5/v6 字段）。"""
    if req.fixture_id:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM fixtures WHERE id = ?", (req.fixture_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, f"fixture {req.fixture_id} not found")
            keys = row.keys()
            return {
                "trend_json_path": row["trend_json_path"],
                "ref_image_path": row["ref_image_path"],
                "color_folder": row["color_folder"],
                "gender_ratio": row["gender_ratio"],
                "num_designs_k": row["num_designs_k"],
                "selected_colors": json.loads(row["selected_colors"]) if row["selected_colors"] else None,
                # v5：请求显式传的优先，其次 fixture 存的
                "design_mode": (
                    req.design_mode if req.design_mode != "MULTI_TOPIC"
                    else (row["design_mode"] if "design_mode" in keys and row["design_mode"] else "MULTI_TOPIC")
                ),
                "styles": (
                    [s.model_dump() for s in req.styles] if req.styles
                    else (json.loads(row["styles"]) if "styles" in keys and row["styles"] else None)
                ),
                "looks": (
                    [l.model_dump() for l in req.looks] if req.looks
                    else (json.loads(row["looks"]) if "looks" in keys and row["looks"] else None)
                ),
                # v6 图库输入源：请求显式传的优先，其次 fixture 存的
                "input_source": (
                    req.input_source if req.input_source and req.input_source != "trend_report"
                    else (row["input_source"] if "input_source" in keys and row["input_source"] else "trend_report")
                ),
                "pattern_library_path": (
                    req.pattern_library_path or (row["pattern_library_path"] if "pattern_library_path" in keys else None)
                ),
                "pattern_library_selected_files": (
                    req.pattern_library_selected_files if req.pattern_library_selected_files
                    else (
                        json.loads(row["pattern_library_selected_files"])
                        if "pattern_library_selected_files" in keys and row["pattern_library_selected_files"]
                        else None
                    )
                ),
                # v7 跨文件夹分组选择
                "pattern_library_selections": (
                    [s.model_dump() for s in req.pattern_library_selections]
                    if req.pattern_library_selections
                    else (
                        json.loads(row["pattern_library_selections"])
                        if "pattern_library_selections" in keys and row["pattern_library_selections"]
                        else None
                    )
                ),
            }
        finally:
            conn.close()
    # inline 模式
    input_source = req.input_source or "trend_report"
    # 图库输入源：trend_json_path 可缺；否则必填 3 字段
    if input_source == "pattern_library":
        # v7 多主题走 selections（跨文件夹分组）；v6 收敛走 path+selected_files（单文件夹）
        has_v6 = bool(req.pattern_library_path and req.pattern_library_selected_files)
        has_v7 = bool(req.pattern_library_selections)
        if not (has_v6 or has_v7):
            raise HTTPException(
                400,
                "图库输入源要求 pattern_library_selections（多主题）或 "
                "pattern_library_path + pattern_library_selected_files（收敛模式）",
            )
        if not (req.ref_image_path and req.color_folder):
            raise HTTPException(
                400,
                "图库输入源也需要提供 ref_image_path + color_folder（款图 + 色号池）",
            )
    else:
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
        "design_mode": req.design_mode,
        "styles": [s.model_dump() for s in req.styles] if req.styles else None,
        "looks": [l.model_dump() for l in req.looks] if req.looks else None,
        "input_source": input_source,
        "pattern_library_path": req.pattern_library_path,
        "pattern_library_selected_files": req.pattern_library_selected_files,
        "pattern_library_selections": (
            [s.model_dump() for s in req.pattern_library_selections]
            if req.pattern_library_selections else None
        ),
    }


def _look_id_for(members: dict) -> str:
    """look_id 由成员内容哈希生成——确定性、可作为 2.5L cache key。"""
    import hashlib
    sig = "|".join(f"{r}:{c}" for r, c in sorted(members.items()))
    return "LK-" + hashlib.md5(sig.encode("utf-8")).hexdigest()[:8]


def _assemble_converge_inputs(fixture: dict) -> tuple[list[dict], list[dict], str, str]:
    """
    CONVERGE 模式（Mode B/C）：装配 styles + looks。

    返回 (styles, looks, primary_style_no, combined_style_no)：
      - styles: [{role, style_no, ref_image_path, color_items}]，色号池已过滤为仅 look 用到的
      - looks:  [{look_id, name, members}]
      - combined_style_no: Mode B = 款号本身；Mode C = "top款号+bottom款号"（历史避重按这个 key 匹配）
    """
    design_mode = fixture["design_mode"]
    styles_spec = fixture.get("styles") or []

    # Mode B 未显式传 styles → 用主字段包装单款 main
    if not styles_spec:
        if design_mode == "COLLECTION_2SKU":
            raise HTTPException(400, "COLLECTION_2SKU 模式必须提供 styles（top + bottom 两个款位）")
        styles_spec = [{
            "role": "main",
            "ref_image_path": fixture["ref_image_path"],
            "color_folder": fixture["color_folder"],
            "selected_colors": fixture.get("selected_colors"),
        }]

    if design_mode == "COLLECTION_2SKU":
        roles = [s["role"] for s in styles_spec]
        if sorted(roles) != ["bottom", "top"]:
            raise HTTPException(400, f"COLLECTION_2SKU 模式 styles 的 roles 必须是 top + bottom，实得 {roles}")

    styles: list[dict] = []
    for s in styles_spec:
        folder = Path(s["color_folder"])
        items = _parse_color_images(folder)
        if s.get("selected_colors"):
            # 兼容文件名（唯一）或色号 code 两种引用
            wanted = set(s["selected_colors"])
            items = [
                c for c in items
                if c["code"] in wanted or Path(c["path"]).name in wanted
            ]
        # 唯一标识 uid = 文件名。色号 code 可能重复（同一照片编号对应多个颜色，
        # 如 DSC09174(浅天蓝).jpg + DSC09174(牛油果绿).jpg），code 仅作展示，
        # look 成员引用一律用 uid。按 (code, name) 去掉真重复（同码同名多文件保留第一张）。
        seen_pairs: set[tuple] = set()
        deduped: list[dict] = []
        for c in items:
            pair = (c["code"], c["name"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            c["uid"] = Path(c["path"]).name
            deduped.append(c)
        items = deduped
        if not items:
            raise HTTPException(400, f"款位 {s['role']} 的色号文件夹无有效图片：{folder}")
        styles.append({
            "role": s["role"],
            "style_no": Path(s["ref_image_path"]).stem,
            "ref_image_path": str(s["ref_image_path"]),
            "color_items": items,
        })

    # looks：Mode C 必传；Mode B 未传则每个色号自动包成单成员 look（成员引用用 uid）
    looks_spec = fixture.get("looks") or []
    if not looks_spec:
        if design_mode == "COLLECTION_2SKU":
            raise HTTPException(400, "COLLECTION_2SKU 模式必须提供 looks（用户预选的上下装配对）")
        main = styles[0]
        looks_spec = [{"name": None, "members": {"main": c["uid"]}} for c in main["color_items"]]

    # 成员引用解析：优先按 uid（文件名）匹配；兼容老 payload 按 code 匹配（要求该 code 无歧义）
    items_by_role = {st["role"]: st["color_items"] for st in styles}

    def _resolve_member(role: str, ref: str, idx: int) -> dict:
        pool = items_by_role.get(role)
        if pool is None:
            raise HTTPException(400, f"look #{idx + 1} 引用了不存在的款位 role：{role}")
        hit = next((c for c in pool if c["uid"] == ref), None)
        if hit:
            return hit
        by_code = [c for c in pool if c["code"] == ref]
        if len(by_code) == 1:
            return by_code[0]
        if len(by_code) > 1:
            names = " / ".join(c["name"] for c in by_code)
            raise HTTPException(
                400,
                f"look #{idx + 1} 成员 ({role}, {ref}) 有歧义：该色号代码对应多个颜色（{names}），"
                f"请在前端重新点选色号图（按文件名精确引用）",
            )
        raise HTTPException(400, f"look #{idx + 1} 成员 ({role}, {ref}) 不在该款色号池中")

    looks: list[dict] = []
    seen_ids: set[str] = set()
    for i, lk in enumerate(looks_spec):
        members_in = lk.get("members") or {}
        if not members_in:
            raise HTTPException(400, f"look #{i + 1} 缺少 members")
        members: dict = {}
        members_display: dict = {}
        for role, ref in members_in.items():
            item = _resolve_member(role, ref, i)
            members[role] = item["uid"]
            members_display[role] = f"{item['code']}·{item['name']}"
        look_id = _look_id_for(members)
        if look_id in seen_ids:
            raise HTTPException(400, f"look #{i + 1} 与之前的 look 成员完全重复（{members_display}）")
        seen_ids.add(look_id)
        looks.append({
            "look_id": look_id,
            "name": lk.get("name") or f"look-{i + 1:02d}",
            "members": members,                    # role → uid（文件名，唯一）
            "members_display": members_display,    # role → "code·色名"（展示用）
        })

    # 未被任何 look 选中的色号不设计（省 token / 生图费）
    used_by_role: dict[str, set] = {}
    for lk in looks:
        for role, uid in lk["members"].items():
            used_by_role.setdefault(role, set()).add(uid)
    for st in styles:
        used = used_by_role.get(st["role"], set())
        st["color_items"] = [c for c in st["color_items"] if c["uid"] in used]

    primary_style_no = styles[0]["style_no"]
    if design_mode == "COLLECTION_2SKU":
        by_role = {st["role"]: st["style_no"] for st in styles}
        combined = f"{by_role.get('top')}+{by_role.get('bottom')}"
    else:
        combined = primary_style_no
    return styles, looks, primary_style_no, combined


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
        design_mode=row.get("design_mode") or "MULTI_TOPIC",
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
