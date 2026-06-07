"""
Step 2 各子节点的实际执行函数

每个 node_* 函数接收：上下文 ctx + 必要的输入 → 返回该节点的输出 dict
所有 trace 事件由 ctx.writer 发射、所有 LLM 调用由 ctx.llm 处理。

节点之间通过共享的 RunState 传递数据。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import (
    AuditResult,
    decide_redesign_targets,
    run_audits,
)
from .llm import LLMClient, LLMResult
from .prompts import (
    PromptBundle,
    list_unresolved_placeholders,
    render_user_prompt,
)
from .trace import TraceWriter


# --------------------------------------------------------------------------- #
# 累积状态（2.4 串行注入）
# --------------------------------------------------------------------------- #

@dataclass
class AccumulatedState:
    used_face_combos: Counter = field(default_factory=Counter)
    used_positions: Counter = field(default_factory=Counter)
    used_tier_combos: Counter = field(default_factory=Counter)
    a_tier_used: int = 0
    a_tier_quota: int = 1

    def absorb(self, plans_for_one_color: list[dict]) -> None:
        for plan in plans_for_one_color:
            combo = plan.get("面组合", "")
            if combo:
                self.used_face_combos[combo] += 1
            tier_combo = plan.get("档组合", "")
            if tier_combo:
                self.used_tier_combos[tier_combo] += 1
            for anchor in plan.get("锚点列表", []):
                pos = anchor.get("位置中文", "").strip()
                if pos:
                    self.used_positions[pos] += 1
                if anchor.get("档", "").strip().upper() == "A":
                    self.a_tier_used += 1

    def suggest_for_next_color(self) -> str:
        """给 prompt 注入"建议本色号优先选择" 文本。"""
        all_face_combos = ["前+后", "前+侧", "后+侧", "前+后+侧"]
        unused = [c for c in all_face_combos if self.used_face_combos[c] == 0]
        if unused:
            return f"优先选择以下未使用的跨面组合之一：{', '.join(unused)}"
        # 全部用过 → 选用次最少的
        least_used = min(all_face_combos, key=lambda c: self.used_face_combos[c])
        return f"全部跨面组合已使用过，建议选用次最少的：{least_used}（已用 {self.used_face_combos[least_used]} 次）"


# --------------------------------------------------------------------------- #
# Slug 工具（与 trace.py NodeContext._slug 保持完全一致）
# --------------------------------------------------------------------------- #

def _slugify(name: str) -> str:
    """与 trace.NodeContext._slug 完全一致的规则，确保 fork 时 slug 对得上。"""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)


# --------------------------------------------------------------------------- #
# 共享上下文
# --------------------------------------------------------------------------- #

@dataclass
class NodeContext:
    """每个 node 函数共享的执行上下文。"""
    writer: TraceWriter
    llm: LLMClient
    bundle: PromptBundle
    style_no: str
    trend_name: str
    ref_image_path: Path
    color_items: list[dict]                   # [{code, name, path}]
    trend_json: dict                          # step1 完整 JSON（含 step1_趋势报告解析 字段）
    num_designs_k: int
    gender_ratio: str
    dry_run: bool = False
    model: str | None = None
    max_tokens: int | None = None

    # M4 fork-from-node 支持
    # replay_cache: slug → cached node IO payload
    # 由 Step2Run 在 fork 模式下从源 run_dir 预扫填充
    replay_cache: dict[str, dict] = field(default_factory=dict)
    # 用户改了上游 prompt → 下游必须 cache miss（避免用旧上游输出的缓存）
    # Step2Run 根据 prompt 版本差异计算
    force_miss_node_ids: set = field(default_factory=set)

    def try_cache_lookup(self, name: str, prompt_node_id: str | None = None) -> dict | None:
        """
        按 name 查 replay_cache。命中条件：
          1. replay_cache 不为空
          2. 该 prompt_node_id 不在 force_miss_node_ids 中（即上游 prompt 没改）
          3. slug 能匹配上某条缓存
          4. 缓存的 prompt_version 跟当前 bundle.version_label 一致
        """
        if not self.replay_cache:
            return None
        if prompt_node_id and prompt_node_id in self.force_miss_node_ids:
            return None
        slug = _slugify(name)
        cached = self.replay_cache.get(slug)
        if cached is None:
            return None
        if prompt_node_id is not None:
            current_version = self.bundle.version_label(prompt_node_id)
            cached_version = cached.get("prompt_version")
            if cached_version != current_version:
                # prompt 版本变了 → cache miss
                return None
        return cached.get("output")

    def step1_data(self) -> dict:
        return self.trend_json.get("step1_趋势报告解析") or self.trend_json

    def subtopics_only(self) -> dict:
        """剔除趋势 JSON 中 raw_response / meta，只留解析结构。"""
        s1 = self.step1_data()
        if isinstance(s1, dict):
            return s1
        return {"step1_趋势报告解析": s1}


# --------------------------------------------------------------------------- #
# 2.1 款式分析
# --------------------------------------------------------------------------- #

async def node_2_1_style_analysis(ctx: NodeContext, parent_id: str | None = None) -> dict:
    """单次 LLM 调用：基于款图 + 趋势 JSON，输出款式分析 + 趋势说明。"""
    # M4 fork：先查缓存
    cached = ctx.try_cache_lookup("款式分析", prompt_node_id="2.1")
    if cached is not None:
        with ctx.writer.node(
            "2_1", "款式分析 ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.1"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.1")

    trend_json_str = json.dumps(ctx.subtopics_only(), ensure_ascii=False, indent=2)
    user_prompt = render_user_prompt(user_tpl, {
        "趋势报告JSON": trend_json_str,
        "款号": ctx.style_no,
    })

    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.1 prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_1", "款式分析",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.1"),
    ) as node:
        node.set_input({
            "system_prompt_chars": len(system),
            "user_prompt_chars": len(user_prompt),
            "image_count": 1,
            "user_prompt": user_prompt,
        })

        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=[ctx.ref_image_path],
            output_marker_regex=r"##\s*STEP\s*2\.1\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {"dry_run": True, "rendered_user_prompt_chars": len(user_prompt)}
        else:
            if result.parsed_json is None:
                raise RuntimeError(f"2.1 模型输出无法解析为 JSON，raw 前 500 字：\n{result.raw_text[:500]}")
            output = result.parsed_json

        node.set_output(output if not ctx.dry_run else {"summary": "dry-run skipped"})

    return output


# --------------------------------------------------------------------------- #
# 2.2 颜色识别（每色一次，并发）
# --------------------------------------------------------------------------- #

async def node_2_2_color_recognition_one(
    ctx: NodeContext,
    color_item: dict,
    style_base_color: dict,
    parent_id: str | None,
) -> dict:
    """单色识别。"""
    node_name_base = f"颜色识别·{color_item['code']}·{color_item['name']}"
    # M4 fork：先查缓存（用单色 slug）
    cached = ctx.try_cache_lookup(node_name_base, prompt_node_id="2.2")
    if cached is not None:
        with ctx.writer.node(
            "2_2", node_name_base + " ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.2"),
        ) as node:
            node.set_input({"_cache_hit": True, "色号代码": color_item["code"]})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.2")

    user_prompt = render_user_prompt(user_tpl, {
        "色号代码": color_item["code"],
        "营销色名": color_item["name"],
        "款图底色hex": style_base_color.get("估计hex", ""),
        "款图底色Pantone": style_base_color.get("估计Pantone", ""),
        "款图底色描述": style_base_color.get("底色文字描述", ""),
    })

    with ctx.writer.node(
        "2_2", node_name_base,
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.2"),
    ) as node:
        node.set_input({
            "色号代码": color_item["code"],
            "营销色名": color_item["name"],
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=[color_item["path"]],
            output_marker_regex=r"##\s*STEP\s*2\.2\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {
                "色号代码": color_item["code"],
                "营销色名": color_item["name"],
                "_dry_run": True,
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError(
                    f"2.2 [{color_item['code']}] 模型输出无法解析"
                )
            output = result.parsed_json
            output.setdefault("色号代码", color_item["code"])
            output.setdefault("营销色名", color_item["name"])

        node.set_output(output)
    return output


async def node_2_2_color_recognition_all(
    ctx: NodeContext,
    style_analysis: dict,
    parent_id: str | None = None,
    concurrency: int = 3,
) -> list[dict]:
    """对全部色号做颜色识别，最多 concurrency 个并发。"""
    base_color = style_analysis.get("款式分析", {}).get("款图底色", {})

    with ctx.writer.node(
        "2_2_loop", "颜色识别（全部色号）",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        sem = asyncio.Semaphore(concurrency)

        async def _run_one(item):
            async with sem:
                return await node_2_2_color_recognition_one(
                    ctx, item, base_color, parent_id=parent.node_id
                )

        results = await asyncio.gather(*[_run_one(c) for c in ctx.color_items])
        parent.set_output({
            "色号数": len(results),
            "需变色数": sum(1 for r in results if r.get("是否需要变色")),
        })
    return results


# --------------------------------------------------------------------------- #
# 2.3 性别比规划
# --------------------------------------------------------------------------- #

async def node_2_3_gender_planning(
    ctx: NodeContext,
    color_results: list[dict],
    parent_id: str | None = None,
) -> dict:
    # M4 fork：先查缓存
    cached = ctx.try_cache_lookup("性别比规划", prompt_node_id="2.3")
    if cached is not None:
        with ctx.writer.node(
            "2_3", "性别比规划 ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.3"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.3")

    color_results_str = json.dumps(color_results, ensure_ascii=False, indent=2)
    user_prompt = render_user_prompt(user_tpl, {
        "性别比例要求": ctx.gender_ratio,
        "色号识别结果JSON": color_results_str,
    })

    with ctx.writer.node(
        "2_3", "性别比规划",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.3"),
    ) as node:
        node.set_input({
            "性别比例要求": ctx.gender_ratio,
            "色号数": len(color_results),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=[],
            output_marker_regex=r"##\s*STEP\s*2\.3\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {
                "_dry_run": True,
                "分配结果": [
                    {"色号代码": c["code"], "营销色名": c["name"], "性别定向": "中性"}
                    for c in ctx.color_items
                ],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError("2.3 模型输出无法解析")
            output = result.parsed_json
        node.set_output(output)
    return output


# --------------------------------------------------------------------------- #
# 2.4 单色设计（串行，累积状态）
# --------------------------------------------------------------------------- #

async def node_2_4_single_color_design(
    ctx: NodeContext,
    *,
    color_item: dict,
    color_recognition: dict,
    gender: str,
    accumulated: AccumulatedState,
    style_analysis: dict,
    parent_id: str | None,
    prompt_node_id: str = "2.4",
    extra_user_vars: dict | None = None,
    title_suffix: str = "",
) -> dict:
    """对单色出 K 个方案。本函数会把 accumulated 状态以文本形式注入 prompt。"""
    node_name_base = f"单色设计·{color_item['code']}·{color_item['name']}{title_suffix}"

    # M4 fork：先查缓存（仅 2.4 主 prompt 时；2.7 重设计不缓存）
    if prompt_node_id == "2.4" and not title_suffix:
        cached = ctx.try_cache_lookup(node_name_base, prompt_node_id="2.4")
        if cached is not None:
            with ctx.writer.node(
                "2_4", node_name_base + " ⚡缓存",
                node_type="llm",
                parent_id=parent_id,
                prompt_version=ctx.bundle.version_label("2.4"),
            ) as node:
                node.set_input({"_cache_hit": True, "色号代码": color_item["code"]})
                # 落盘完整 cached output，方便前端展示 + 下游 absorb 累积状态
                node.set_output(cached)
            return cached

    system, user_tpl = ctx.bundle.load(prompt_node_id)

    style_data = style_analysis.get("款式分析", {})
    used_face_combos_str = json.dumps(dict(accumulated.used_face_combos), ensure_ascii=False)
    used_positions_str = json.dumps(dict(accumulated.used_positions), ensure_ascii=False)
    used_tier_combos_str = json.dumps(dict(accumulated.used_tier_combos), ensure_ascii=False)

    base_vars = {
        "趋势子主题JSON": json.dumps(ctx.subtopics_only(), ensure_ascii=False, indent=2),
        "款式分析JSON": json.dumps(style_data, ensure_ascii=False, indent=2),
        "色号代码": color_item["code"],
        "营销色名": color_item["name"],
        "识别底色": json.dumps(color_recognition.get("识别底色", {}), ensure_ascii=False),
        "是否需要变色": color_recognition.get("是否需要变色", False),
        "变色描述": color_recognition.get("变色描述") or "",
        "性别定向": gender,
        "已用面组合": used_face_combos_str,
        "已用具体位置": used_positions_str,
        "已用档组合": used_tier_combos_str,
        "A档已用次数": accumulated.a_tier_used,
        "A档配额上限": accumulated.a_tier_quota,
        "建议面档组合": accumulated.suggest_for_next_color(),
        "用户设定方案数": ctx.num_designs_k,
        "款号": ctx.style_no,
    }
    if extra_user_vars:
        base_vars.update(extra_user_vars)

    user_prompt = render_user_prompt(user_tpl, base_vars)
    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.4 prompt 渲染后仍含未解析占位符：{unresolved}")

    # 上传顺序：款图 + 本色号图（若需变色）；不需变色仍上传以让模型可参考
    image_paths = [ctx.ref_image_path, color_item["path"]]

    node_name = f"单色设计·{color_item['code']}·{color_item['name']}{title_suffix}"
    with ctx.writer.node(
        "2_4", node_name,
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label(prompt_node_id),
    ) as node:
        node.set_input({
            "色号代码": color_item["code"],
            "性别定向": gender,
            "accumulated_a_tier_used": accumulated.a_tier_used,
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })

        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=image_paths,
            output_marker_regex=r"##\s*STEP\s*2\.4\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {
                "色号代码": color_item["code"],
                "营销色名": color_item["name"],
                "_dry_run": True,
                "设计方案": [],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError(f"2.4 [{color_item['code']}] 模型输出无法解析")
            output = result.parsed_json
            output.setdefault("色号代码", color_item["code"])
            output.setdefault("营销色名", color_item["name"])
            output.setdefault("性别定向", gender)

        # 落盘完整 output 到 cache JSON——前端 NodeDetail 才能看到设计方案数组、锚点等
        # 注意：trace.py 的 _summarize_for_event 会自动给 SSE 事件做摘要，
        # cache 不要在这里手摘要（之前的 bug 就是这里只存了 2 个字段）
        node.set_output(output)
    return output


async def node_2_4_all_colors(
    ctx: NodeContext,
    *,
    color_results: list[dict],
    gender_plan: dict,
    style_analysis: dict,
    parent_id: str | None = None,
    a_tier_quota: int | None = None,
) -> tuple[list[dict], AccumulatedState]:
    """对所有色号串行出方案，每色累积状态注入下一色 prompt。"""
    if a_tier_quota is None:
        a_tier_quota = max(1, len(ctx.color_items) // 9)
    accumulated = AccumulatedState(a_tier_quota=a_tier_quota)

    gender_lookup = {
        item["色号代码"]: item.get("性别定向", "中性")
        for item in gender_plan.get("分配结果", [])
    }
    recog_lookup = {r["色号代码"]: r for r in color_results}

    with ctx.writer.node(
        "2_4_loop", "单色设计（全部色号·串行）",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        all_color_outputs: list[dict] = []
        for color_item in ctx.color_items:
            code = color_item["code"]
            gender = gender_lookup.get(code, "中性")
            recog = recog_lookup.get(code, {"色号代码": code, "营销色名": color_item["name"]})
            out = await node_2_4_single_color_design(
                ctx,
                color_item=color_item,
                color_recognition=recog,
                gender=gender,
                accumulated=accumulated,
                style_analysis=style_analysis,
                parent_id=parent.node_id,
            )
            all_color_outputs.append(out)
            # 吸收该色方案到累积状态
            accumulated.absorb(out.get("设计方案", []))

        parent.set_output({
            "色号数": len(all_color_outputs),
            "A档已用次数": accumulated.a_tier_used,
            "已用面组合": dict(accumulated.used_face_combos),
        })
    return all_color_outputs, accumulated


# --------------------------------------------------------------------------- #
# 2.5 分布审计（纯 Python tool）
# --------------------------------------------------------------------------- #

async def node_2_5_audit(
    ctx: NodeContext,
    color_outputs: list[dict],
    a_tier_quota: int,
    parent_id: str | None = None,
) -> AuditResult:
    with ctx.writer.node(
        "2_5", "分布审计（4 项）",
        node_type="tool",
        parent_id=parent_id,
    ) as node:
        node.set_input({
            "色号数": len(color_outputs),
            "A档配额": a_tier_quota,
        })
        audit = run_audits(color_outputs, a_tier_quota=a_tier_quota)
        summary = {
            "通过": audit.passed,
            "审计结果": [
                {"项目": c.name, "通过": c.passed, "结论": c.summary}
                for c in audit.checks
            ],
        }
        node.set_output(audit.to_json())
        return audit


# --------------------------------------------------------------------------- #
# 2.6 决策（基于审计结果选出要重设计的色号）
# --------------------------------------------------------------------------- #

async def node_2_6_decision(
    ctx: NodeContext,
    audit: AuditResult,
    color_outputs: list[dict],
    parent_id: str | None = None,
    max_redesign: int = 3,
) -> list[dict]:
    targets = decide_redesign_targets(audit, color_outputs, max_redesign=max_redesign)
    ctx.writer.emit_decision(
        node_id=ctx.writer._new_node_id("2_6"),
        parent_id=parent_id,
        condition=f"审计通过={audit.passed}",
        branch="finalize" if audit.passed else f"redesign_{len(targets)}_colors",
        reasoning=(
            f"4 项审计 {sum(1 for c in audit.checks if c.passed)}/4 通过；"
            f"挑选 {len(targets)} 个色号进入 2.7 重设计："
            + ", ".join(t["色号代码"] for t in targets)
        ),
    )
    return targets


# --------------------------------------------------------------------------- #
# 2.7 局部重设计
# --------------------------------------------------------------------------- #

async def node_2_7_redesign(
    ctx: NodeContext,
    *,
    targets: list[dict],
    color_outputs: list[dict],
    color_results: list[dict],
    gender_plan: dict,
    style_analysis: dict,
    accumulated_before: AccumulatedState,
    parent_id: str | None = None,
) -> list[dict]:
    """对 targets 中的色号重新出方案，更新 color_outputs 中对应条目。"""
    if not targets:
        return color_outputs

    color_outputs_by_code = {c["色号代码"]: c for c in color_outputs}
    recog_lookup = {r["色号代码"]: r for r in color_results}
    gender_lookup = {
        item["色号代码"]: item.get("性别定向", "中性")
        for item in gender_plan.get("分配结果", [])
    }

    # 重设计前先从累积状态中"减去"被重做色号原方案的贡献
    new_acc = AccumulatedState(a_tier_quota=accumulated_before.a_tier_quota)
    # 重新从头吸收（不含被重做的色号）
    targets_codes = {t["色号代码"] for t in targets}
    for c in color_outputs:
        if c["色号代码"] in targets_codes:
            continue
        new_acc.absorb(c.get("设计方案", []))

    with ctx.writer.node(
        "2_7_loop", f"局部重设计 x {len(targets)}",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        for target in targets:
            code = target["色号代码"]
            color_item = next((c for c in ctx.color_items if c["code"] == code), None)
            if color_item is None:
                continue

            recog = recog_lookup.get(code, {})
            gender = gender_lookup.get(code, "中性")

            extra_vars = {
                "重设计原因": target["重设计原因"],
                "已尝试面组合": json.dumps(target["已尝试面组合"], ensure_ascii=False),
                "已尝试具体位置": json.dumps(target["已尝试具体位置"], ensure_ascii=False),
                "已尝试档组合": json.dumps(target["已尝试档组合"], ensure_ascii=False),
            }
            new_out = await node_2_4_single_color_design(
                ctx,
                color_item=color_item,
                color_recognition=recog,
                gender=gender,
                accumulated=new_acc,
                style_analysis=style_analysis,
                parent_id=parent.node_id,
                prompt_node_id="2.7",       # 使用 04b prompt
                title_suffix=" [重设计]",
            )
            color_outputs_by_code[code] = new_out
            new_acc.absorb(new_out.get("设计方案", []))

        parent.set_output({"重设计完成色号": [t["色号代码"] for t in targets]})

    # 按原顺序返回更新后的 color_outputs
    return [color_outputs_by_code[c["色号代码"]] for c in color_outputs]
