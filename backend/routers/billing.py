"""
/api/v1/billing — 账单：交付任务级 token / 成本统计（PRD: docs/账单Tab_*.md）

统计口径（已拍板）：
  - 交付任务 = 主 run（parent_run_id 为空）+ fork 链 + 所有生图任务
  - 节点 token 从各 run 的 trace.jsonl 逐条聚合（DB 汇总列在 failed run 上不可靠）
  - 缓存命中（⚡fork / ♻️款级）计 0，缓存节省按全局历史均值估算
  - 失败 run / 失败节点已烧 token 照常计入；dry-run 计 0（自然为 0）
  - 生图按张计费（沿用任务表 total_cost_usd 估算）；失败张数展示、计 $0
  - LLM 单价：设置页全局 input/output $/1M（默认 30/30）

方案 A：读取时聚合 + run 级内存缓存（run 跑完后不可变，缓存永久有效；
running 状态的 run 每次实时读）。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.db import get_conn, list_generation_tasks

router = APIRouter()

ROOT = Path(__file__).parent.parent.parent

# 单价默认值（$/1M tokens），设置页可改（.env: LLM_PRICE_INPUT_PER_1M / LLM_PRICE_OUTPUT_PER_1M）
DEFAULT_PRICE_IN = 30.0
DEFAULT_PRICE_OUT = 30.0

# run_id → 解析结果 缓存（run 完成后不可变）
_RUN_CACHE: dict[str, list[dict]] = {}

# 有 LLM 消耗语义的业务节点（展示顺序）；2.6/2.7 纯工具 0 token，保留保证链路完整
_BIZ_ORDER = ["2.1", "2.2", "2.3", "2.4", "2.4s", "2.4.5", "2.5", "2.5L", "2.6", "2.7", "2.8", "修复调用"]


def _prices() -> tuple[float, float]:
    try:
        p_in = float(os.environ.get("LLM_PRICE_INPUT_PER_1M", DEFAULT_PRICE_IN))
    except ValueError:
        p_in = DEFAULT_PRICE_IN
    try:
        p_out = float(os.environ.get("LLM_PRICE_OUTPUT_PER_1M", DEFAULT_PRICE_OUT))
    except ValueError:
        p_out = DEFAULT_PRICE_OUT
    return p_in, p_out


def _llm_cost(tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = _prices()
    return tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out


def _biz_id_of(node_id: str) -> str | None:
    """'2_4_5_019' → '2.4.5'；'2_5L_021' → '2.5L'；loop/run 节点 → None（无消耗语义）。"""
    parts = node_id.split("_")
    biz_parts: list[str] = []
    for p in parts:
        if p.isdigit() and len(p) >= 2:
            break
        biz_parts.append(p)
    if not biz_parts:
        return None
    biz = ".".join(biz_parts)
    if biz.endswith(".loop") or biz == "run":
        return None
    return biz


def _parse_run_calls(run_row: dict) -> list[dict]:
    """
    解析一个 run 的 trace.jsonl → 调用明细列表。
    [{node_id, biz_id, name, tokens_in, tokens_out, elapsed_ms, cached, repair}]
      cached: None | "fork"(⚡) | "style"(♻️)
      repair: 同一 node_id 的第二次及以后 completion（工作台单色重识别/重设计追加事件）
    """
    run_id = run_row["id"]
    is_final = run_row.get("status") in ("succeeded", "succeeded_with_audit_warnings", "failed")
    if is_final and run_id in _RUN_CACHE:
        return _RUN_CACHE[run_id]

    trace_path = Path(run_row["run_dir"]) / "trace.jsonl"
    calls: list[dict] = []
    started: dict[str, dict] = {}
    completed_count: dict[str, int] = defaultdict(int)

    if trace_path.is_file():
        try:
            lines = trace_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            etype = ev.get("event")
            nid = ev.get("node_id") or ""
            if etype == "node_started":
                started[nid] = {"name": ev.get("name") or "", "node_type": ev.get("node_type")}
            elif etype == "node_completed":
                biz = _biz_id_of(nid)
                if biz is None:
                    continue
                info = started.get(nid, {})
                name = info.get("name", "")
                cached = None
                if "♻️款级缓存" in name:
                    cached = "style"
                elif "⚡缓存" in name:
                    cached = "fork"
                tokens = ev.get("tokens") or {}
                repair = completed_count[nid] >= 1
                completed_count[nid] += 1
                calls.append({
                    "node_id": nid,
                    "biz_id": "修复调用" if repair else biz,
                    "orig_biz_id": biz,
                    "name": name or nid,
                    "tokens_in": int(tokens.get("input") or 0),
                    "tokens_out": int(tokens.get("output") or 0),
                    "elapsed_ms": int(ev.get("elapsed_ms") or 0),
                    "cached": cached,
                    "repair": repair,
                })
            elif etype == "node_failed":
                # 失败节点若已计 tokens 也计入（当前实现失败时通常无 tokens 字段 → 0）
                biz = _biz_id_of(nid)
                if biz is None:
                    continue
                tokens = ev.get("tokens") or {}
                calls.append({
                    "node_id": nid,
                    "biz_id": biz,
                    "orig_biz_id": biz,
                    "name": (started.get(nid) or {}).get("name") or nid,
                    "tokens_in": int(tokens.get("input") or 0),
                    "tokens_out": int(tokens.get("output") or 0),
                    "elapsed_ms": int(ev.get("elapsed_ms") or 0),
                    "cached": None,
                    "repair": False,
                    "failed": True,
                })
            elif etype == "decision_made":
                biz = _biz_id_of(nid)
                if biz == "2.7":
                    calls.append({
                        "node_id": nid, "biz_id": "2.7", "orig_biz_id": "2.7",
                        "name": "重设计决策", "tokens_in": 0, "tokens_out": 0,
                        "elapsed_ms": 0, "cached": None, "repair": False,
                    })

    if is_final:
        _RUN_CACHE[run_id] = calls
    return calls


def _load_all_runs() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _group_deliveries(runs: list[dict]) -> list[dict]:
    """按 fork 链归并：主 run（无 parent）为锚点，返回 [{main, runs:[...]}]（时间倒序）。"""
    by_id = {r["id"]: r for r in runs}
    children = defaultdict(list)
    for r in runs:
        pid = r.get("parent_run_id")
        if pid and pid in by_id:
            children[pid].append(r)

    def _root_of(r: dict) -> dict:
        cur = r
        seen = set()
        while cur.get("parent_run_id") and cur["parent_run_id"] in by_id and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = by_id[cur["parent_run_id"]]
        return cur

    groups: dict[str, dict] = {}
    for r in runs:
        root = _root_of(r)
        g = groups.setdefault(root["id"], {"main": root, "runs": []})
        g["runs"].append(r)
    # 每组内按创建时间排（主 run 在前）
    out = list(groups.values())
    for g in out:
        g["runs"].sort(key=lambda x: x["created_at"])
    out.sort(key=lambda g: g["main"]["created_at"], reverse=True)
    return out


def _global_avg_by_biz(runs: list[dict]) -> dict[str, float]:
    """全局历史均值：各 biz 节点非缓存、有消耗调用的 (in+out) 均值 → 缓存节省估算。"""
    sums: dict[str, list[int]] = defaultdict(list)
    for r in runs:
        for c in _parse_run_calls(r):
            if c.get("cached") is None and (c["tokens_in"] + c["tokens_out"]) > 0:
                sums[c["orig_biz_id"]].append(c["tokens_in"] + c["tokens_out"])
    return {k: (sum(v) / len(v)) for k, v in sums.items() if v}


def _gen_tasks_by_run() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for t in list_generation_tasks(limit=1000):
        out[t.get("source_step2_run_id") or ""].append(t)
    return out


def _task_stats(t: dict) -> dict:
    try:
        results = json.loads(t.get("results") or "[]")
    except Exception:
        results = []
    ok = sum(1 for r in results if r.get("status") == "succeeded")
    failed = sum(1 for r in results if r.get("status") == "failed")
    return {
        "task_id": t["id"],
        "status": t.get("status"),
        "images_ok": ok,
        "images_failed": failed,          # 展示但计 $0（已拍板）
        "cost_usd": float(t.get("total_cost_usd") or 0),
        # 生图 usage token（新任务起记录；老任务结果里没有该字段 → 0）
        "tokens_in": sum(int(r.get("tokens_in") or 0) for r in results),
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in results),
        "elapsed_ms": t.get("total_elapsed_ms"),
        "created_at": t.get("created_at"),
        "results": results,
    }


@router.get("/summary")
async def billing_summary(date_from: Optional[str] = None, date_to: Optional[str] = None):
    """交付任务汇总（视图 1）。date_from/date_to: 'YYYY-MM-DD'（按主 run created_at 过滤）。"""
    runs = _load_all_runs()
    deliveries = _group_deliveries(runs)
    avg_by_biz = _global_avg_by_biz(runs)
    tasks_by_run = _gen_tasks_by_run()
    p_in, p_out = _prices()

    rows = []
    totals = {"tokens_in": 0, "tokens_out": 0, "llm_cost": 0.0, "images_ok": 0,
              "images_failed": 0, "img_cost": 0.0, "cache_hits": 0, "cache_saved_tokens": 0.0,
              "gen_tokens_in": 0, "gen_tokens_out": 0,
              "design_elapsed_ms": 0, "gen_elapsed_ms": 0}

    for g in deliveries:
        main = g["main"]
        created = (main.get("created_at") or "")[:10]
        if date_from and created < date_from:
            continue
        if date_to and created > date_to:
            continue

        tin = tout = hits = 0
        saved = 0.0
        design_ms = 0
        for r in g["runs"]:
            for c in _parse_run_calls(r):
                tin += c["tokens_in"]
                tout += c["tokens_out"]
                design_ms += c.get("elapsed_ms") or 0
                if c.get("cached"):
                    hits += 1
                    saved += avg_by_biz.get(c["orig_biz_id"], 0.0)

        gen_stats = []
        for r in g["runs"]:
            for t in tasks_by_run.get(r["id"], []):
                gen_stats.append(_task_stats(t))
        images_ok = sum(s["images_ok"] for s in gen_stats)
        images_failed = sum(s["images_failed"] for s in gen_stats)
        img_cost = sum(s["cost_usd"] for s in gen_stats)
        gen_tin = sum(s["tokens_in"] for s in gen_stats)
        gen_tout = sum(s["tokens_out"] for s in gen_stats)
        gen_ms = sum(int(s["elapsed_ms"] or 0) for s in gen_stats)

        llm_cost = _llm_cost(tin, tout)
        rows.append({
            "main_run_id": main["id"],
            "created_at": main.get("created_at"),
            "design_mode": main.get("design_mode") or "MULTI_TOPIC",
            "trend_name": main.get("trend_name"),
            "style_no": main.get("style_no"),
            "status": main.get("status"),
            "fork_count": len(g["runs"]) - 1,
            "tokens_in": tin,
            "tokens_out": tout,
            "llm_cost_usd": round(llm_cost, 4),
            "gen_task_count": len(gen_stats),
            "images_ok": images_ok,
            "images_failed": images_failed,
            "gen_tokens_in": gen_tin,
            "gen_tokens_out": gen_tout,
            # 单款 token = 设计(in+out) / 生图成功张数（衡量每出一张成品图摊多少设计消耗）
            "avg_tokens_per_image": round((tin + tout) / images_ok) if images_ok > 0 else None,
            "design_elapsed_ms": design_ms,
            "gen_elapsed_ms": gen_ms,
            "img_cost_usd": round(img_cost, 4),
            "total_cost_usd": round(llm_cost + img_cost, 4),
            "cache_hits": hits,
            "cache_saved_tokens_est": int(saved),
        })
        totals["tokens_in"] += tin
        totals["tokens_out"] += tout
        totals["llm_cost"] += llm_cost
        totals["images_ok"] += images_ok
        totals["images_failed"] += images_failed
        totals["img_cost"] += img_cost
        totals["cache_hits"] += hits
        totals["cache_saved_tokens"] += saved
        totals["gen_tokens_in"] += gen_tin
        totals["gen_tokens_out"] += gen_tout
        totals["design_elapsed_ms"] += design_ms
        totals["gen_elapsed_ms"] += gen_ms

    return {
        "prices": {"input_per_1m_usd": p_in, "output_per_1m_usd": p_out},
        "rows": rows,
        "totals": {
            "deliveries": len(rows),
            "tokens_in": totals["tokens_in"],
            "tokens_out": totals["tokens_out"],
            "llm_cost_usd": round(totals["llm_cost"], 4),
            "images_ok": totals["images_ok"],
            "images_failed": totals["images_failed"],
            "gen_tokens_in": totals["gen_tokens_in"],
            "gen_tokens_out": totals["gen_tokens_out"],
            "avg_tokens_per_image": (
                round((totals["tokens_in"] + totals["tokens_out"]) / totals["images_ok"])
                if totals["images_ok"] > 0 else None
            ),
            "design_elapsed_ms": totals["design_elapsed_ms"],
            "gen_elapsed_ms": totals["gen_elapsed_ms"],
            "img_cost_usd": round(totals["img_cost"], 4),
            "total_cost_usd": round(totals["llm_cost"] + totals["img_cost"], 4),
            "cache_hits": totals["cache_hits"],
            "cache_saved_tokens_est": int(totals["cache_saved_tokens"]),
            "cache_saved_usd_est": round(
                totals["cache_saved_tokens"] / 1e6 * ((p_in + p_out) / 2), 4
            ),
        },
    }


@router.get("/delivery/{main_run_id}")
async def billing_delivery_detail(main_run_id: str):
    """交付任务明细（视图 2）：节点级调用表（按来源分组）+ 生图任务明细。"""
    runs = _load_all_runs()
    by_id = {r["id"]: r for r in runs}
    if main_run_id not in by_id:
        raise HTTPException(404, f"run {main_run_id} 不存在")
    deliveries = _group_deliveries(runs)
    group = next((g for g in deliveries if g["main"]["id"] == main_run_id), None)
    if group is None:
        # 传的是 fork run id → 找它所在组
        group = next((g for g in deliveries if any(r["id"] == main_run_id for r in g["runs"])), None)
    if group is None:
        raise HTTPException(404, "找不到该交付任务")

    avg_by_biz = _global_avg_by_biz(runs)
    tasks_by_run = _gen_tasks_by_run()
    main = group["main"]

    node_rows = []
    for r in group["runs"]:
        source = "主 run" if r["id"] == main["id"] else f"fork·{r['id'][-6:]}"
        for c in _parse_run_calls(r):
            node_rows.append({
                **{k: c[k] for k in ("biz_id", "name", "tokens_in", "tokens_out", "elapsed_ms", "cached")},
                "repair": c.get("repair", False),
                "failed": c.get("failed", False),
                "source": source,
                "run_id": r["id"],
                "cost_usd": round(_llm_cost(c["tokens_in"], c["tokens_out"]), 4),
                "cache_saved_tokens_est": int(avg_by_biz.get(c["orig_biz_id"], 0)) if c.get("cached") else 0,
            })
    # 按管线顺序 + 来源排列
    order = {b: i for i, b in enumerate(_BIZ_ORDER)}
    node_rows.sort(key=lambda x: (x["run_id"], order.get(x["biz_id"], 99)))

    gen_rows = []
    for r in group["runs"]:
        for t in tasks_by_run.get(r["id"], []):
            s = _task_stats(t)
            gen_rows.append({
                "task_id": s["task_id"],
                "status": s["status"],
                "images_ok": s["images_ok"],
                "images_failed": s["images_failed"],
                "tokens_in": s["tokens_in"],
                "tokens_out": s["tokens_out"],
                "cost_usd": round(s["cost_usd"], 4),
                "elapsed_ms": s["elapsed_ms"],
                "created_at": s["created_at"],
                "plans": [
                    {
                        "plan_id": res.get("plan_id"),
                        "status": res.get("status"),
                        "elapsed_ms": res.get("elapsed_ms"),
                        "tokens_in": int(res.get("tokens_in") or 0),
                        "tokens_out": int(res.get("tokens_out") or 0),
                    }
                    for res in s["results"]
                ],
            })

    tin = sum(n["tokens_in"] for n in node_rows)
    tout = sum(n["tokens_out"] for n in node_rows)
    img_cost = sum(gr["cost_usd"] for gr in gen_rows)
    gen_tin = sum(gr["tokens_in"] for gr in gen_rows)
    gen_tout = sum(gr["tokens_out"] for gr in gen_rows)
    llm_cost = _llm_cost(tin, tout)

    return {
        "main_run_id": main["id"],
        "design_mode": main.get("design_mode") or "MULTI_TOPIC",
        "trend_name": main.get("trend_name"),
        "style_no": main.get("style_no"),
        "runs": [{"id": r["id"], "status": r.get("status"), "created_at": r.get("created_at"),
                  "is_main": r["id"] == main["id"]} for r in group["runs"]],
        "nodes": node_rows,
        "gen_tasks": gen_rows,
        "totals": {
            "tokens_in": tin,
            "tokens_out": tout,
            "gen_tokens_in": gen_tin,
            "gen_tokens_out": gen_tout,
            "design_elapsed_ms": sum(n.get("elapsed_ms") or 0 for n in node_rows),
            "gen_elapsed_ms": sum(int(gr.get("elapsed_ms") or 0) for gr in gen_rows),
            "llm_cost_usd": round(llm_cost, 4),
            "img_cost_usd": round(img_cost, 4),
            "total_cost_usd": round(llm_cost + img_cost, 4),
        },
    }
