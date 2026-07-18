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

    # --- 多样性避重（同款+同趋势最近 N 轮）：由 Step2Config 直接透传 ---
    avoid_topic_ids: list = field(default_factory=list)         # 2.4 主题避重
    avoid_topic_names: list = field(default_factory=list)
    recent_color_designs: dict = field(default_factory=dict)    # 2.5 差异化提示

    # --- v5 CONVERGE 图（强单主题 / Collection）---
    # design_mode: MULTI_TOPIC（现状 DIVERGE 图）| SINGLE_TOPIC_STRONG | COLLECTION_2SKU
    design_mode: str = "MULTI_TOPIC"
    # styles: [{role, style_no, ref_image_path(str), color_items:[{code,name,path}]}]
    #   Mode B（单款强单主题）长度=1 role="main"；Mode C（上下装）长度=2 roles=["top","bottom"]
    styles: list = field(default_factory=list)
    # looks: [{look_id, name, members: {role: color_code}}]
    #   Mode B 由 backend 自动为每个色号包一个单成员 look；Mode C 由用户预选
    looks: list = field(default_factory=list)

    # --- v6 图库输入源（pattern_library）---
    # input_source: "trend_report"（现状）| "pattern_library"（跳过 2.4/2.4s，直接进 2.4.5）
    input_source: str = "trend_report"
    # 图库文件夹名（相对 PATTERN_LIBRARY_ROOT），用作 blueprint 子主题名
    pattern_library_folder: str = ""
    # 用户预筛的核心参考图**绝对路径**列表（1-3 张，2.4.5 会传给 LLM 视觉输入）
    core_ref_image_paths: list = field(default_factory=list)

    # --- 款级持久缓存（2.1/2.2 跨 run / 跨趋势 / 跨模式复用）---
    # style_cache: StyleCache 实例（orchestrator 注入）；use_style_cache=False 时跳过（纯净实验）
    use_style_cache: bool = True
    style_cache: Any = None

    # --- v7 多主题 × 图库：跨文件夹参考图集 [{direction, topic_id, files: [abs paths]}] ---
    # （v6 的 input_source / pattern_library_folder / core_ref_image_paths 在上方已定义）
    reference_set: list = field(default_factory=list)

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

async def node_2_1_style_analysis(
    ctx: NodeContext,
    parent_id: str | None = None,
    title_suffix: str = "",
) -> dict:
    """单次 LLM 调用：基于款图 + 趋势 JSON，输出款式分析 + 趋势说明。

    title_suffix：CONVERGE 图多款场景传 "·{style_no}"，避免两款的 cache slug 冲突。
    Mode A 不传，行为与 v4 完全一致。
    """
    node_name_base = f"款式分析{title_suffix}"
    # M4 fork：先查缓存
    cached = ctx.try_cache_lookup(node_name_base, prompt_node_id="2.1")
    if cached is not None:
        # fork 命中的结果同样有效——顺手回填款级缓存，让其他模式/新 run 也能 ♻️ 复用
        if ctx.style_cache is not None and ctx.use_style_cache and not ctx.dry_run:
            try:
                _sys, _tpl = ctx.bundle.load("2.1")
                ctx.style_cache.put_style_analysis(
                    ctx.style_no, ctx.ref_image_path, _sys + "\n" + _tpl, ctx.model, cached,
                )
            except Exception:
                pass
        with ctx.writer.node(
            "2_1", node_name_base + " ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.1"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.1")

    # 款级持久缓存：同款 + 同款图内容 + 同 prompt 内容 + 同模型 → 跨 run/趋势/模式复用
    _prompt_text_21 = system + "\n" + user_tpl
    if ctx.style_cache is not None and ctx.use_style_cache and not ctx.dry_run:
        sc_hit = ctx.style_cache.get_style_analysis(
            ctx.style_no, ctx.ref_image_path, _prompt_text_21, ctx.model,
        )
        if sc_hit is not None:
            with ctx.writer.node(
                "2_1", node_name_base + " ♻️款级缓存",
                node_type="llm",
                parent_id=parent_id,
                prompt_version=ctx.bundle.version_label("2.1"),
            ) as node:
                node.set_input({"_style_cache_hit": True, "款号": ctx.style_no})
                node.set_output(sc_hit)
            return sc_hit

    # v4：2.1 款式分析不再吃趋势 JSON——趋势相关判断已移至 2.4 主题选择节点。
    # 这样 2.1 的输入只取决于款图本身，可跨趋势完全复用 cache。
    user_prompt = render_user_prompt(user_tpl, {
        "款号": ctx.style_no,
    })

    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.1 prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_1", node_name_base,
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
            image_max_side=1536,   # 款式分析需看结构细节，1536 档；生图用原图不受影响
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {"dry_run": True, "rendered_user_prompt_chars": len(user_prompt)}
        else:
            if result.parsed_json is None:
                raise RuntimeError(f"2.1 模型输出无法解析为 JSON，raw 前 500 字：\n{result.raw_text[:500]}")
            output = result.parsed_json
            # 写回款级缓存（失败不阻断）
            if ctx.style_cache is not None and ctx.use_style_cache:
                ctx.style_cache.put_style_analysis(
                    ctx.style_no, ctx.ref_image_path, _prompt_text_21, ctx.model, output,
                )

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
    title_suffix: str = "",
) -> dict:
    """单色识别。title_suffix：CONVERGE 多款场景避免跨款色号 slug 冲突。"""
    node_name_base = f"颜色识别·{color_item['code']}·{color_item['name']}{title_suffix}"
    # M4 fork：先查缓存（用单色 slug）
    cached = ctx.try_cache_lookup(node_name_base, prompt_node_id="2.2")
    if cached is not None:
        # fork 命中同样回填款级缓存
        if ctx.style_cache is not None and ctx.use_style_cache and not ctx.dry_run:
            try:
                _sys, _tpl = ctx.bundle.load("2.2")
                ctx.style_cache.put_color_recognition(
                    ctx.style_no, Path(color_item["path"]), _sys + "\n" + _tpl,
                    ctx.model, style_base_color, cached,
                )
            except Exception:
                pass
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

    # 款级持久缓存：色号图内容 + prompt 内容 + 模型 + 款图底色字段 联合成 key。
    # 2.1 重跑导致底色变化 → key 变 → 自动级联失效；底色没变则继续命中。
    _prompt_text_22 = system + "\n" + user_tpl
    if ctx.style_cache is not None and ctx.use_style_cache and not ctx.dry_run:
        sc_hit = ctx.style_cache.get_color_recognition(
            ctx.style_no, Path(color_item["path"]), _prompt_text_22, ctx.model, style_base_color,
        )
        if sc_hit is not None:
            with ctx.writer.node(
                "2_2", node_name_base + " ♻️款级缓存",
                node_type="llm",
                parent_id=parent_id,
                prompt_version=ctx.bundle.version_label("2.2"),
            ) as node:
                node.set_input({"_style_cache_hit": True, "色号代码": color_item["code"]})
                node.set_output(sc_hit)
            return sc_hit

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
            # 颜色识别只需看底色，原片(4000px+)按 768 长边压缩后上传：
            # 图片 token ~11k → <1k。仅影响 LLM 识别输入；生图环节始终用原图。
            image_max_side=768,
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
            # 写回款级缓存
            if ctx.style_cache is not None and ctx.use_style_cache:
                ctx.style_cache.put_color_recognition(
                    ctx.style_no, Path(color_item["path"]), _prompt_text_22,
                    ctx.model, style_base_color, output,
                )

        node.set_output(output)
    return output


async def node_2_2_color_recognition_all(
    ctx: NodeContext,
    style_analysis: dict,
    parent_id: str | None = None,
    concurrency: int = 3,
    title_suffix: str = "",
) -> list[dict]:
    """对全部色号做颜色识别，最多 concurrency 个并发。
    任一色号失败不影响其他色号——失败的返回带 _recognition_failed 占位。"""
    base_color = style_analysis.get("款式分析", {}).get("款图底色", {})

    with ctx.writer.node(
        "2_2_loop", f"颜色识别（全部色号）{title_suffix}",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        sem = asyncio.Semaphore(concurrency)

        async def _run_one(item):
            async with sem:
                try:
                    return await node_2_2_color_recognition_one(
                        ctx, item, base_color, parent_id=parent.node_id,
                        title_suffix=title_suffix,
                    )
                except Exception as e:
                    # 单色失败不影响其他色号
                    err = f"{type(e).__name__}: {e}"
                    return {
                        "色号代码": item["code"],
                        "营销色名": item["name"],
                        "_recognition_failed": True,
                        "_error": err,
                        "是否需要变色": False,
                        "识别底色": None,
                        "变色描述": None,
                    }

        results = await asyncio.gather(*[_run_one(c) for c in ctx.color_items])
        failed = [r for r in results if r.get("_recognition_failed")]
        parent.set_output({
            "色号数": len(results),
            "需变色数": sum(1 for r in results if r.get("是否需要变色")),
            "识别失败数": len(failed),
            "识别失败色号": [r["色号代码"] + "·" + r["营销色名"] for r in failed],
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
# 2.4 趋势主题选择（v4 新增）
# --------------------------------------------------------------------------- #

async def node_2_4_topic_selection(
    ctx: NodeContext,
    style_analysis: dict,
    color_results: list[dict],
    gender_plan: dict,
    parent_id: str | None = None,
) -> dict:
    """
    v4 核心新增节点：基于款式分析 + 性别规划 + 色号识别汇总 + 趋势 JSON，
    从趋势报告中精选 2-3 个子主题，并把每个色号映射到其中一个主题。

    输出会被下游 2.5 单色设计直接消费：每色号只需要看自己分配的主题，
    不再吃完整趋势 JSON——这是 v4 token 节省的关键。
    """
    cached = ctx.try_cache_lookup("趋势主题选择", prompt_node_id="2.4")
    if cached is not None:
        with ctx.writer.node(
            "2_4", "趋势主题选择 ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.4"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.4")

    # 组装避重主题列表：如果本次配了历史避重，把最近选过的主题拉出来（有编号显编号+名字，无编号只显名字）
    if ctx.avoid_topic_ids or ctx.avoid_topic_names:
        _lines = []
        n = max(len(ctx.avoid_topic_ids), len(ctx.avoid_topic_names))
        for i in range(n):
            tid = ctx.avoid_topic_ids[i] if i < len(ctx.avoid_topic_ids) else ""
            tname = ctx.avoid_topic_names[i] if i < len(ctx.avoid_topic_names) else ""
            if tid and tname:
                _lines.append(f"- {tname}（编号 {tid}）")
            elif tname:
                _lines.append(f"- {tname}")
        avoid_topics_text = "\n".join(_lines) if _lines else "本次无历史避重要求"
    else:
        avoid_topics_text = "本次无历史避重要求（历史无同款+同趋势的成功 run，或用户已关闭历史避重）"

    style_data = style_analysis.get("款式分析", {})
    user_prompt = render_user_prompt(user_tpl, {
        "款式分析JSON": json.dumps(style_data, ensure_ascii=False, indent=2),
        "性别比例要求": ctx.gender_ratio,
        "性别分配JSON": json.dumps(gender_plan.get("分配结果", []), ensure_ascii=False, indent=2),
        "色号识别汇总JSON": json.dumps(color_results, ensure_ascii=False, indent=2),
        "趋势报告JSON": json.dumps(ctx.subtopics_only(), ensure_ascii=False, indent=2),
        "避重主题列表": avoid_topics_text,
    })

    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.4 prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_4", "趋势主题选择",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.4"),
    ) as node:
        node.set_input({
            "色号数": len(color_results),
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })

        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=[],
            output_marker_regex=r"##\s*STEP\s*2\.4\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            # dry-run 模拟：用前 2 个色号作主题，所有色号轮流分配
            codes = [c.get("色号代码", "") for c in color_results]
            output = {
                "_dry_run": True,
                "整波主题策略": "dry-run 模拟",
                "选用主题列表": [
                    {"子主题编号": "ST01", "子主题名称": "dry-run-A", "档位推荐": "B",
                     "性别气质": "中性", "整波定位": "主推", "入选理由": "dry-run"},
                    {"子主题编号": "ST02", "子主题名称": "dry-run-B", "档位推荐": "B",
                     "性别气质": "中性", "整波定位": "对比", "入选理由": "dry-run"},
                ],
                "色号到主题映射": [
                    {"色号代码": c, "营销色名": "", "选用主题编号": "ST01" if i % 2 else "ST02",
                     "选用主题名称": "dry-run-A" if i % 2 else "dry-run-B", "分配理由": "dry-run"}
                    for i, c in enumerate(codes)
                ],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError("2.4 模型输出无法解析为 JSON")
            output = result.parsed_json

            # 硬约束校验（选项 A：违规直接报错让节点 failed）
            selected = output.get("选用主题列表") or []
            n_selected = len(selected)
            if n_selected not in (2, 3):
                raise RuntimeError(
                    f"2.4 主题选择违反硬约束：选用主题数 = {n_selected}（必须 2 或 3）"
                )
            mappings = output.get("色号到主题映射") or []
            n_mapped = len(mappings)
            n_expected = len(color_results)
            if n_mapped != n_expected:
                raise RuntimeError(
                    f"2.4 色号映射数 = {n_mapped}（必须 = 色号总数 {n_expected}）"
                )
            valid_topic_ids = {t.get("子主题编号") for t in selected}
            for m in mappings:
                if m.get("选用主题编号") not in valid_topic_ids:
                    raise RuntimeError(
                        f"2.4 映射的主题编号 '{m.get('选用主题编号')}' "
                        f"不在选用主题列表内（合法集 = {valid_topic_ids}）"
                    )

        node.set_output(output)
    return output


def _build_topic_lookup(topic_selection: dict) -> dict[str, dict]:
    """从 2.4 输出建 色号代码 → 分配主题完整描述 的查询表。
    主题完整描述会从 ctx.subtopics_only() 里反查原始趋势 JSON 拿到。"""
    mappings = topic_selection.get("色号到主题映射") or []
    selected = topic_selection.get("选用主题列表") or []
    topic_by_id = {t.get("子主题编号"): t for t in selected}
    out: dict[str, dict] = {}
    for m in mappings:
        code = m.get("色号代码")
        tid = m.get("选用主题编号")
        if code and tid:
            out[code] = {
                "分配主题编号": tid,
                "分配主题名称": m.get("选用主题名称") or topic_by_id.get(tid, {}).get("子主题名称", ""),
                "分配理由": m.get("分配理由") or "",
                "主题元信息": topic_by_id.get(tid, {}),
            }
    return out


def _enrich_assigned_topic(ctx: NodeContext, assigned: dict) -> dict:
    """把 2.4 给的主题元信息 + 从原始趋势 JSON 反查出来的完整描述合并。
    下游 2.5 prompt 需要这个完整描述（图案 / 配色 / 构图细节等）。"""
    tid = assigned.get("分配主题编号")
    # 从 ctx.subtopics_only() 找完整主题描述
    s1 = ctx.subtopics_only()
    subtopics_list = []
    if isinstance(s1, dict):
        # 通常 s1["step1_趋势报告解析"]["子主题列表"] 或者直接 s1["子主题列表"]
        for k in ("子主题列表", "step1_趋势报告解析"):
            v = s1.get(k)
            if isinstance(v, list):
                subtopics_list = v
                break
            if isinstance(v, dict):
                subtopics_list = v.get("子主题列表") or []
                if subtopics_list:
                    break
    full_desc = next((s for s in subtopics_list if s.get("编号") == tid), {})
    if not full_desc and assigned.get("主题元信息"):
        # v7 图库输入源：趋势 JSON 为空反查不到——2.4p 输出的主题元信息
        # （含「图案视觉描述」「english_pattern_essence」）就是完整描述，直接用它。
        full_desc = assigned["主题元信息"]
    return {
        **assigned,
        "完整主题描述": full_desc,
    }


# --------------------------------------------------------------------------- #
# 2.5 单色设计（串行，累积状态；v4：吃分配主题而非完整趋势）
# --------------------------------------------------------------------------- #

async def node_2_5_single_color_design(
    ctx: NodeContext,
    *,
    color_item: dict,
    color_recognition: dict,
    gender: str,
    assigned_topic: dict,                   # 包含 完整主题描述 / 分配主题编号 / 分配主题名称
    accumulated: AccumulatedState,
    style_analysis: dict,
    parent_id: str | None,
    prompt_node_id: str = "2.5",
    extra_user_vars: dict | None = None,
    title_suffix: str = "",
) -> dict:
    """对单色出 K 个方案。v4 起，只用上游 2.4 分配的单个主题，不再吃完整趋势 JSON。"""
    node_name_base = f"单色设计·{color_item['code']}·{color_item['name']}{title_suffix}"

    if prompt_node_id == "2.5" and not title_suffix:
        cached = ctx.try_cache_lookup(node_name_base, prompt_node_id="2.5")
        if cached is not None:
            with ctx.writer.node(
                "2_5", node_name_base + " ⚡缓存",
                node_type="llm",
                parent_id=parent_id,
                prompt_version=ctx.bundle.version_label("2.5"),
            ) as node:
                node.set_input({"_cache_hit": True, "色号代码": color_item["code"]})
                node.set_output(cached)
            return cached

    system, user_tpl = ctx.bundle.load(prompt_node_id)

    style_data = style_analysis.get("款式分析", {})
    used_face_combos_str = json.dumps(dict(accumulated.used_face_combos), ensure_ascii=False)
    used_positions_str = json.dumps(dict(accumulated.used_positions), ensure_ascii=False)
    used_tier_combos_str = json.dumps(dict(accumulated.used_tier_combos), ensure_ascii=False)

    # 组装该色号"近期相似方案"（最近同款+同趋势+同色的历史方案摘要，供 LLM 避免雷同）
    recent_for_this_color = ctx.recent_color_designs.get(color_item["code"], []) or []
    if recent_for_this_color:
        _lines = []
        for d in recent_for_this_color[:5]:  # 最多列 5 条，防止 prompt 爆炸
            tn = d.get("子主题名称") or "?"
            pk = d.get("图案关键词") or "?"
            desc = (d.get("方案说明") or "").strip().replace("\n", " ")[:120]
            _lines.append(f"- 主题「{tn}」· 图案「{pk}」· 摘要：{desc}")
        recent_similar_text = "\n".join(_lines)
    else:
        recent_similar_text = "无历史（本色号首次设计，或用户已关闭历史避重）"

    base_vars = {
        # v4 关键改动：用单个分配主题的完整描述，不再注入完整趋势 JSON
        "分配主题完整描述JSON": json.dumps(assigned_topic, ensure_ascii=False, indent=2),
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
        "近期相似方案": recent_similar_text,
    }
    if extra_user_vars:
        base_vars.update(extra_user_vars)

    user_prompt = render_user_prompt(user_tpl, base_vars)
    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.5 prompt 渲染后仍含未解析占位符：{unresolved}")

    image_paths = [ctx.ref_image_path, color_item["path"]]

    node_name = f"单色设计·{color_item['code']}·{color_item['name']}{title_suffix}"
    with ctx.writer.node(
        "2_5", node_name,
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label(prompt_node_id),
    ) as node:
        node.set_input({
            "色号代码": color_item["code"],
            "营销色名": color_item["name"],
            "性别定向": gender,
            "分配主题编号": assigned_topic.get("分配主题编号"),
            "分配主题名称": assigned_topic.get("分配主题名称"),
            "accumulated_a_tier_used": accumulated.a_tier_used,
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })

        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=image_paths,
            output_marker_regex=r"##\s*STEP\s*2\.5\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
            # 设计节点看版型/可印区域，1536 长边足够；原片色号图 ~11k → ~2k token。
            # 仅影响 LLM 视觉输入，生图（step3）始终用磁盘原图。
            image_max_side=1536,
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
                raise RuntimeError(f"2.5 [{color_item['code']}] 模型输出无法解析")
            output = result.parsed_json
            output.setdefault("色号代码", color_item["code"])
            output.setdefault("营销色名", color_item["name"])
            output.setdefault("性别定向", gender)
            output.setdefault("分配主题编号", assigned_topic.get("分配主题编号"))
            output.setdefault("分配主题名称", assigned_topic.get("分配主题名称"))

        node.set_output(output)
    return output


async def node_2_5_all_colors(
    ctx: NodeContext,
    *,
    color_results: list[dict],
    gender_plan: dict,
    style_analysis: dict,
    topic_selection: dict,
    parent_id: str | None = None,
    a_tier_quota: int | None = None,
) -> tuple[list[dict], AccumulatedState]:
    """对所有色号串行出方案，每色累积状态注入下一色 prompt。v4：用 2.4 分配的主题。"""
    if a_tier_quota is None:
        a_tier_quota = max(1, len(ctx.color_items) // 9)
    accumulated = AccumulatedState(a_tier_quota=a_tier_quota)

    gender_lookup = {
        item["色号代码"]: item.get("性别定向", "中性")
        for item in gender_plan.get("分配结果", [])
    }
    recog_lookup = {r["色号代码"]: r for r in color_results}
    topic_lookup = _build_topic_lookup(topic_selection)

    with ctx.writer.node(
        "2_5_loop", "单色设计（全部色号·串行）",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        all_color_outputs: list[dict] = []
        skipped_count = 0
        design_failed_count = 0
        design_failed_codes: list[str] = []
        for color_item in ctx.color_items:
            code = color_item["code"]
            gender = gender_lookup.get(code, "中性")
            recog = recog_lookup.get(code, {"色号代码": code, "营销色名": color_item["name"]})

            if recog.get("_recognition_failed"):
                skipped_count += 1
                all_color_outputs.append({
                    "色号代码": code,
                    "营销色名": color_item["name"],
                    "性别定向": gender,
                    "_skipped": True,
                    "_skip_reason": f"2.2 识别失败：{recog.get('_error', '未知')}；可手动重识别后再设计",
                    "设计方案": [],
                    "无方案时的跳过原因": "上游 2.2 颜色识别失败",
                })
                continue

            # 拿本色号被分配的主题
            assigned = topic_lookup.get(code)
            if not assigned:
                skipped_count += 1
                all_color_outputs.append({
                    "色号代码": code,
                    "营销色名": color_item["name"],
                    "性别定向": gender,
                    "_skipped": True,
                    "_skip_reason": "上游 2.4 没给本色号分配主题",
                    "设计方案": [],
                    "无方案时的跳过原因": "上游 2.4 主题映射缺该色号",
                })
                continue

            assigned_enriched = _enrich_assigned_topic(ctx, assigned)

            try:
                out = await node_2_5_single_color_design(
                    ctx,
                    color_item=color_item,
                    color_recognition=recog,
                    gender=gender,
                    assigned_topic=assigned_enriched,
                    accumulated=accumulated,
                    style_analysis=style_analysis,
                    parent_id=parent.node_id,
                )
                all_color_outputs.append(out)
                accumulated.absorb(out.get("设计方案", []))
            except Exception as e:
                design_failed_count += 1
                design_failed_codes.append(code)
                err = f"{type(e).__name__}: {e}"
                all_color_outputs.append({
                    "色号代码": code,
                    "营销色名": color_item["name"],
                    "性别定向": gender,
                    "_design_failed": True,
                    "_error": err,
                    "设计方案": [],
                    "无方案时的跳过原因": f"2.5 LLM 调用失败：{err}；可手动重新生成该色号方案",
                })

        parent.set_output({
            "色号数": len(all_color_outputs),
            "跳过数（2.2 失败 + 无主题映射）": skipped_count,
            "设计失败数（2.5 失败）": design_failed_count,
            "设计失败色号": design_failed_codes,
            "A档已用次数": accumulated.a_tier_used,
            "已用面组合": dict(accumulated.used_face_combos),
        })
    return all_color_outputs, accumulated


# --------------------------------------------------------------------------- #
# 2.6 分布审计（纯 Python tool）
# --------------------------------------------------------------------------- #

async def node_2_6_audit(
    ctx: NodeContext,
    color_outputs: list[dict],
    a_tier_quota: int,
    parent_id: str | None = None,
) -> AuditResult:
    with ctx.writer.node(
        "2_6", "分布审计（4 项）",
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
# 2.7 决策（基于审计结果选出要重设计的色号）
# --------------------------------------------------------------------------- #

async def node_2_7_decision(
    ctx: NodeContext,
    audit: AuditResult,
    color_outputs: list[dict],
    parent_id: str | None = None,
    max_redesign: int = 3,
) -> list[dict]:
    targets = decide_redesign_targets(audit, color_outputs, max_redesign=max_redesign)
    ctx.writer.emit_decision(
        node_id=ctx.writer._new_node_id("2_7"),
        parent_id=parent_id,
        condition=f"审计通过={audit.passed}",
        branch="finalize" if audit.passed else f"redesign_{len(targets)}_colors",
        reasoning=(
            f"4 项审计 {sum(1 for c in audit.checks if c.passed)}/4 通过；"
            f"挑选 {len(targets)} 个色号进入 2.8 重设计："
            + ", ".join(t["色号代码"] for t in targets)
        ),
    )
    return targets


# --------------------------------------------------------------------------- #
# 2.8 局部重设计（v4：复用 2.4 分配的主题，不重新选主题）
# --------------------------------------------------------------------------- #

async def node_2_8_redesign(
    ctx: NodeContext,
    *,
    targets: list[dict],
    color_outputs: list[dict],
    color_results: list[dict],
    gender_plan: dict,
    style_analysis: dict,
    topic_selection: dict,
    accumulated_before: AccumulatedState,
    parent_id: str | None = None,
) -> list[dict]:
    """对 targets 中的色号重新出方案。v4：复用上游 2.4 的主题分配，不重新选主题。"""
    if not targets:
        return color_outputs

    color_outputs_by_code = {c["色号代码"]: c for c in color_outputs}
    recog_lookup = {r["色号代码"]: r for r in color_results}
    gender_lookup = {
        item["色号代码"]: item.get("性别定向", "中性")
        for item in gender_plan.get("分配结果", [])
    }
    topic_lookup = _build_topic_lookup(topic_selection)

    new_acc = AccumulatedState(a_tier_quota=accumulated_before.a_tier_quota)
    targets_codes = {t["色号代码"] for t in targets}
    for c in color_outputs:
        if c["色号代码"] in targets_codes:
            continue
        new_acc.absorb(c.get("设计方案", []))

    with ctx.writer.node(
        "2_8_loop", f"局部重设计 x {len(targets)}",
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
            assigned = topic_lookup.get(code)
            if not assigned:
                # 上游 2.4 没给本色号分配主题——跳过重设计
                continue
            assigned_enriched = _enrich_assigned_topic(ctx, assigned)

            extra_vars = {
                "重设计原因": target["重设计原因"],
                "已尝试面组合": json.dumps(target["已尝试面组合"], ensure_ascii=False),
                "已尝试具体位置": json.dumps(target["已尝试具体位置"], ensure_ascii=False),
                "已尝试档组合": json.dumps(target["已尝试档组合"], ensure_ascii=False),
            }
            new_out = await node_2_5_single_color_design(
                ctx,
                color_item=color_item,
                color_recognition=recog,
                gender=gender,
                assigned_topic=assigned_enriched,
                accumulated=new_acc,
                style_analysis=style_analysis,
                parent_id=parent.node_id,
                prompt_node_id="2.8",         # v4：使用 05b prompt（局部重设计约束）
                title_suffix=" [重设计]",
                extra_user_vars=extra_vars,
            )
            color_outputs_by_code[code] = new_out
            new_acc.absorb(new_out.get("设计方案", []))

        parent.set_output({"重设计完成色号": [t["色号代码"] for t in targets]})

    return [color_outputs_by_code[c["色号代码"]] for c in color_outputs]


# =========================================================================== #
# v5 CONVERGE 图专用节点（SINGLE_TOPIC_STRONG / COLLECTION_2SKU）
#
#   2.4s   单主题选择        —— 选且仅选 1 个子主题（收敛模式）
#   2.4.5  母图案 Blueprint  —— 出母图案 DNA + 按 role 分组的 placement 候选池
#   2.5L   Look 联合变体设计 —— 一次调用出整个 look（1..2 成员）的设计方案
#
# DIVERGE 图（MULTI_TOPIC）完全不经过这些函数。
# =========================================================================== #

def _styles_summary_json(ctx: NodeContext, style_analyses: dict[str, dict]) -> str:
    """把 per-role 款式分析组装成一个 JSON 字符串（款号 + 款式分析）。"""
    out = {}
    for slot in ctx.styles:
        role = slot["role"]
        analysis = style_analyses.get(role) or {}
        out[role] = {
            "款号": slot["style_no"],
            "款式名": analysis.get("款式名", ""),
            "款式分析": analysis.get("款式分析", {}),
        }
    return json.dumps(out, ensure_ascii=False, indent=2)


def _flat_color_results_json(ctx: NodeContext, color_results: dict[str, list[dict]]) -> str:
    """把 per-role 色号识别结果拍平（每条带 role + 款号），供 2.4s / 2.4.5 消费。"""
    flat = []
    for slot in ctx.styles:
        role = slot["role"]
        for r in color_results.get(role) or []:
            flat.append({"role": role, "款号": slot["style_no"], **r})
    return json.dumps(flat, ensure_ascii=False, indent=2)


def _looks_manifest_json(ctx: NodeContext) -> str:
    """look 清单（含成员色号），供 2.4s / 2.4.5 了解整波结构。"""
    return json.dumps(ctx.looks, ensure_ascii=False, indent=2)


async def node_2_4s_single_topic(
    ctx: NodeContext,
    style_analyses: dict[str, dict],
    color_results: dict[str, list[dict]],
    gender_plan: dict,
    parent_id: str | None = None,
) -> dict:
    """
    v5 收敛模式主题选择：从趋势报告中选**且仅选 1 个**子主题，作为整个
    collection 母图案的载体。输出 schema 与 2.4 对齐（选用主题列表长度=1，
    色号到主题映射覆盖全部成员色号），下游历史避重 / final JSON 消费完全兼容。
    """
    cached = ctx.try_cache_lookup("单主题选择", prompt_node_id="2.4s")
    if cached is not None:
        with ctx.writer.node(
            "2_4s", "单主题选择 ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.4s"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.4s")

    # 避重清单（与 2.4 同一套组装逻辑）
    if ctx.avoid_topic_ids or ctx.avoid_topic_names:
        _lines = []
        n = max(len(ctx.avoid_topic_ids), len(ctx.avoid_topic_names))
        for i in range(n):
            tid = ctx.avoid_topic_ids[i] if i < len(ctx.avoid_topic_ids) else ""
            tname = ctx.avoid_topic_names[i] if i < len(ctx.avoid_topic_names) else ""
            if tid and tname:
                _lines.append(f"- {tname}（编号 {tid}）")
            elif tname:
                _lines.append(f"- {tname}")
        avoid_topics_text = "\n".join(_lines) if _lines else "本次无历史避重要求"
    else:
        avoid_topics_text = "本次无历史避重要求"

    mode_label = "上下装成套（COLLECTION_2SKU）" if ctx.design_mode == "COLLECTION_2SKU" else "单款强单主题（SINGLE_TOPIC_STRONG）"

    user_prompt = render_user_prompt(user_tpl, {
        "设计模式": mode_label,
        "款式分析汇总JSON": _styles_summary_json(ctx, style_analyses),
        "性别比例要求": ctx.gender_ratio,
        "性别分配JSON": json.dumps(gender_plan.get("分配结果", []), ensure_ascii=False, indent=2),
        "色号识别汇总JSON": _flat_color_results_json(ctx, color_results),
        "look清单JSON": _looks_manifest_json(ctx),
        "趋势报告JSON": json.dumps(ctx.subtopics_only(), ensure_ascii=False, indent=2),
        "避重主题列表": avoid_topics_text,
    })

    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.4s prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_4s", "单主题选择",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.4s"),
    ) as node:
        node.set_input({
            "设计模式": ctx.design_mode,
            "look数": len(ctx.looks),
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=[],
            output_marker_regex=r"##\s*STEP\s*2\.4[sS]\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            all_codes = []
            for slot in ctx.styles:
                for r in color_results.get(slot["role"]) or []:
                    all_codes.append((slot["style_no"], r.get("色号代码", "")))
            output = {
                "_dry_run": True,
                "整波主题策略": "dry-run 模拟（单主题收敛）",
                "选用主题列表": [
                    {"子主题编号": "ST01", "子主题名称": "dry-run-single", "档位推荐": "B",
                     "性别气质": "中性", "整波定位": "单主题收敛", "入选理由": "dry-run",
                     "母图案延展性评估": "dry-run"},
                ],
                "色号到主题映射": [
                    {"款号": sn, "色号代码": c, "营销色名": "", "选用主题编号": "ST01",
                     "选用主题名称": "dry-run-single", "分配理由": "dry-run"}
                    for sn, c in all_codes
                ],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError("2.4s 模型输出无法解析为 JSON")
            output = result.parsed_json
            selected = output.get("选用主题列表") or []
            if len(selected) != 1:
                raise RuntimeError(
                    f"2.4s 单主题选择违反硬约束：选用主题数 = {len(selected)}（收敛模式必须恰好 1 个）"
                )

        node.set_output(output)
    return output


async def node_2_4_5_blueprint(
    ctx: NodeContext,
    single_topic_enriched: dict,
    style_analyses: dict[str, dict],
    color_results: dict[str, list[dict]],
    parent_id: str | None = None,
) -> dict:
    """
    v5 新增：母图案 Blueprint 设计。

    输入：选定的单主题完整描述 + 全部 style 的款式分析（可印区域）+ 色号识别汇总 + look 清单。
    输出：PatternBlueprint —— 母图案概念 / 固定锚点 / 允许变体维度 / 母配色关系 /
          英文母图案 canonical 描述（供 2.5L 各成员的图生图 prompt 原样复用，保证图像层一致性）/
          按 role 分组的 placement 候选池。
    """
    cached = ctx.try_cache_lookup("母图案Blueprint", prompt_node_id="2.4.5")
    if cached is not None:
        with ctx.writer.node(
            "2_4_5", "母图案Blueprint ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.4.5"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.4.5")

    # 图片顺序：先款图（每个 style 一张），再核心参考图（图库输入时才有 1-3 张）
    image_paths = [Path(slot["ref_image_path"]) for slot in ctx.styles]
    img_lines = [
        f"- Image {i + 1}：{slot['role']} 款图（款号 {slot['style_no']}）"
        for i, slot in enumerate(ctx.styles)
    ]

    # v6 图库输入源：追加用户预筛的核心参考图
    core_ref_lines: list[str] = []
    if ctx.input_source == "pattern_library" and ctx.core_ref_image_paths:
        base_idx = len(image_paths)
        for j, p in enumerate(ctx.core_ref_image_paths):
            image_paths.append(Path(p))
            core_ref_lines.append(
                f"- Image {base_idx + j + 1}：核心参考图（图库文件夹「{ctx.pattern_library_folder}」中 {Path(p).name}）"
            )

    if ctx.input_source == "pattern_library":
        input_source_label = "pattern_library（图库输入）"
        input_source_desc = (
            f"用户在图库中选定文件夹「{ctx.pattern_library_folder}」，并预筛 "
            f"{len(ctx.core_ref_image_paths)} 张核心参考图。请**优先看图**抽象共性特质，"
            "选定主题描述仅作辅助。英文母图案描述必须紧扣图片能看到的元素。"
        )
        core_ref_note = (
            "\n**核心参考图集**（共享给全部下游变体的视觉锚）：\n" + "\n".join(core_ref_lines)
            if core_ref_lines else "\n**核心参考图集**：无（图库文件夹为空或未预筛）"
        )
    else:
        input_source_label = "trend_report（趋势报告）"
        input_source_desc = "本次无图库参考图。请从「选定主题完整描述JSON」中抽象母图案 DNA。"
        core_ref_note = ""

    roles = [slot["role"] for slot in ctx.styles]
    mode_label = "上下装成套（COLLECTION_2SKU）" if ctx.design_mode == "COLLECTION_2SKU" else "单款强单主题（SINGLE_TOPIC_STRONG）"

    user_prompt = render_user_prompt(user_tpl, {
        "设计模式": mode_label,
        "输入源类型": input_source_label,
        "输入源说明": input_source_desc,
        "role列表": json.dumps(roles, ensure_ascii=False),
        "选定主题完整描述JSON": json.dumps(single_topic_enriched, ensure_ascii=False, indent=2),
        "款式分析汇总JSON": _styles_summary_json(ctx, style_analyses),
        "色号识别汇总JSON": _flat_color_results_json(ctx, color_results),
        "look清单JSON": _looks_manifest_json(ctx),
        "款图清单说明": "\n".join(img_lines),
        "核心参考图集说明": core_ref_note,
    })

    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.4.5 prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_4_5", "母图案Blueprint",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.4.5"),
    ) as node:
        node.set_input({
            "主题": single_topic_enriched.get("分配主题名称"),
            "roles": roles,
            "look数": len(ctx.looks),
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=image_paths,
            output_marker_regex=r"##\s*STEP\s*2\.4\.5\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
            image_max_side=1536,   # blueprint 看两款可印区域，1536 档
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {
                "_dry_run": True,
                "blueprint_id": "PB-dryrun-001",
                "所属主题编号": single_topic_enriched.get("分配主题编号", "ST01"),
                "所属主题名称": single_topic_enriched.get("分配主题名称", "dry-run"),
                "母图案概念": "dry-run 模拟母图案",
                "核心元素锚点": {"固定不变的元素": ["dry-run"], "允许变体的元素": ["dry-run"]},
                "母配色关系": {},
                "英文母图案描述": "dry-run canonical pattern description",
                "placement候选池": {r: [] for r in roles},
                "系列一致性规则": [],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError("2.4.5 模型输出无法解析为 JSON")
            output = result.parsed_json
            # 硬校验：placement 候选池必须覆盖所有 role
            pool = output.get("placement候选池") or {}
            missing = [r for r in roles if not pool.get(r)]
            if missing:
                raise RuntimeError(
                    f"2.4.5 blueprint 违反硬约束：placement候选池 缺少 role {missing} 的候选"
                )
            if not (output.get("英文母图案描述") or "").strip():
                raise RuntimeError("2.4.5 blueprint 缺少 '英文母图案描述'（图像层一致性锚点，必填）")

        # v6 追溯字段：input_source + 核心参考图集 落地到 blueprint 里（供 final JSON 追溯）
        output.setdefault("input_source", ctx.input_source)
        if ctx.input_source == "pattern_library":
            output.setdefault("pattern_library_folder", ctx.pattern_library_folder)
            output.setdefault("核心参考图集", [Path(p).name for p in ctx.core_ref_image_paths])

        node.set_output(output)
    return output


async def node_2_5_look_design(
    ctx: NodeContext,
    *,
    look: dict,
    blueprint: dict,
    single_topic_enriched: dict,
    style_analyses: dict[str, dict],
    recog_by_role: dict[str, dict],
    look_gender: str,
    accumulated_by_role: dict[str, AccumulatedState],
    recent_similar_text: str,
    prior_looks_summary: str,
    parent_id: str | None,
) -> dict:
    """
    v5 核心节点：对一个 look（1..2 成员）做一次联合设计调用。

    - 所有成员共用 blueprint 的母图案 DNA（固定元素原样保留，仅在允许变体维度上变化）
    - 成员间 placement 互补（上装大图 → 下装小标之类），配色呼应
    - 每个成员输出与 2.5 完全同 schema 的 设计方案[]，含独立的图生图 prompt
    """
    look_id = look["look_id"]
    node_name = f"Look设计·{look_id}"

    cached = ctx.try_cache_lookup(node_name, prompt_node_id="2.5L")
    if cached is not None:
        with ctx.writer.node(
            "2_5L", node_name + " ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.5L"),
        ) as node:
            node.set_input({"_cache_hit": True, "look_id": look_id})
            node.set_output(cached)
        return cached

    system, user_tpl = ctx.bundle.load("2.5L")

    slot_by_role = {s["role"]: s for s in ctx.styles}
    members_info = []
    image_paths: list = []
    img_lines: list[str] = []
    img_idx = 1
    for role, member_ref in look["members"].items():
        slot = slot_by_role[role]
        # member_ref 优先是 uid（文件名，唯一）；兼容老数据按 code 匹配
        color_item = next(
            (c for c in slot["color_items"] if c.get("uid") == member_ref or c["code"] == member_ref),
            None,
        )
        if color_item is None:
            raise RuntimeError(f"look {look_id} 成员 ({role}, {member_ref}) 在色号池中不存在")
        recog = recog_by_role.get(role, {})
        acc = accumulated_by_role[role]
        members_info.append({
            "role": role,
            "款号": slot["style_no"],
            "色号代码": color_item["code"],
            "营销色名": color_item["name"],
            "识别底色": recog.get("识别底色", {}),
            "是否需要变色": recog.get("是否需要变色", False),
            "变色描述": recog.get("变色描述") or "",
            "本role已用面组合": dict(acc.used_face_combos),
            "本role已用具体位置": dict(acc.used_positions),
            "本role已用档组合": dict(acc.used_tier_combos),
            "本role_A档已用": acc.a_tier_used,
            "本role_A档配额": acc.a_tier_quota,
            "参考图编号": {"款图": f"Image {img_idx}", "色号图": f"Image {img_idx + 1}"},
        })
        image_paths.append(Path(slot["ref_image_path"]))
        image_paths.append(color_item["path"])
        img_lines.append(f"- Image {img_idx}：{role} 款图（款号 {slot['style_no']}）")
        img_lines.append(f"- Image {img_idx + 1}：{role} 色号图（{color_item['code']}·{color_item['name']}）")
        img_idx += 2

    user_prompt = render_user_prompt(user_tpl, {
        "look_id": look_id,
        "look名称": look.get("name") or look_id,
        "look性别定向": look_gender,
        "母图案蓝图JSON": json.dumps(blueprint, ensure_ascii=False, indent=2),
        "选定主题完整描述JSON": json.dumps(single_topic_enriched, ensure_ascii=False, indent=2),
        "款式分析汇总JSON": _styles_summary_json(ctx, style_analyses),
        "成员清单JSON": json.dumps(members_info, ensure_ascii=False, indent=2),
        "图片顺序说明": "\n".join(img_lines),
        "用户设定方案数": ctx.num_designs_k,
        "近期相似方案": recent_similar_text,
        "已出look摘要": prior_looks_summary,
    })
    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.5L prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_5L", node_name,
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.5L"),
    ) as node:
        node.set_input({
            "look_id": look_id,
            "成员数": len(members_info),
            "成员": [f"{m['role']}:{m['色号代码']}" for m in members_info],
            "look性别定向": look_gender,
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=image_paths,
            output_marker_regex=r"##\s*STEP\s*2\.5[lL]\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
            image_max_side=1536,   # 同 2.5：LLM 视觉输入压缩，生图用原图
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            output = {
                "look_id": look_id,
                "_dry_run": True,
                "成员方案": [
                    {"role": m["role"], "款号": m["款号"], "色号代码": m["色号代码"],
                     "营销色名": m["营销色名"], "设计方案": []}
                    for m in members_info
                ],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError(f"2.5L [{look_id}] 模型输出无法解析")
            output = result.parsed_json
            output.setdefault("look_id", look_id)
            members_out = output.get("成员方案") or []
            expected_roles = set(look["members"].keys())
            got_roles = {m.get("role") for m in members_out}
            if got_roles != expected_roles:
                raise RuntimeError(
                    f"2.5L [{look_id}] 成员方案 roles 不匹配：期望 {expected_roles}，实得 {got_roles}"
                )
            # 回填成员元信息（保证下游拍平后字段齐全）
            info_by_role = {m["role"]: m for m in members_info}
            for m in members_out:
                info = info_by_role.get(m.get("role"), {})
                m.setdefault("款号", info.get("款号"))
                m.setdefault("色号代码", info.get("色号代码"))
                m.setdefault("营销色名", info.get("营销色名"))
                m.setdefault("性别定向", look_gender)
                m.setdefault("分配主题编号", single_topic_enriched.get("分配主题编号"))
                m.setdefault("分配主题名称", single_topic_enriched.get("分配主题名称"))
                m["look_id"] = look_id

        node.set_output(output)
    return output


async def node_2_5_all_looks(
    ctx: NodeContext,
    *,
    blueprint: dict,
    single_topic_enriched: dict,
    style_analyses: dict[str, dict],
    color_results: dict[str, list[dict]],
    gender_plan: dict,
    parent_id: str | None = None,
    a_tier_quota: int | None = None,
) -> tuple[list[dict], dict[str, AccumulatedState]]:
    """
    对全部 look 串行联合设计。每个 role 各自维护 AccumulatedState
    （上装的位置分布与下装独立累积），look 间注入已出摘要促差异化。
    """
    if a_tier_quota is None:
        a_tier_quota = max(1, len(ctx.looks) // 9)
    accumulated_by_role: dict[str, AccumulatedState] = {
        s["role"]: AccumulatedState(a_tier_quota=a_tier_quota) for s in ctx.styles
    }
    # 性别 / 识别结果索引：用 (色号代码, 营销色名) 复合键——同一 code 可能对应多个颜色
    # （如 DSC09174(浅天蓝) 与 DSC09174(牛油果绿)），单 code 索引会互相覆盖。
    gender_by_pair: dict[tuple, str] = {}
    gender_by_code: dict[str, str] = {}
    for item in gender_plan.get("分配结果", []):
        g = item.get("性别定向", "中性")
        gender_by_pair[(item.get("色号代码"), item.get("营销色名"))] = g
        gender_by_code.setdefault(item.get("色号代码"), g)
    recog_lookup: dict[str, dict[tuple, dict]] = {}
    for slot in ctx.styles:
        role = slot["role"]
        recog_lookup[role] = {
            (r.get("色号代码"), r.get("营销色名")): r
            for r in (color_results.get(role) or [])
        }

    slot_by_role = {s["role"]: s for s in ctx.styles}

    def _find_item(role: str, ref: str) -> dict | None:
        """成员引用 → 色号 item。优先 uid（文件名），兼容 code。"""
        pool = (slot_by_role.get(role) or {}).get("color_items") or []
        return next((c for c in pool if c.get("uid") == ref or c["code"] == ref), None)

    with ctx.writer.node(
        "2_5L_loop", "Look 联合设计（全部 look·串行）",
        node_type="loop",
        parent_id=parent_id,
    ) as parent:
        look_outputs: list[dict] = []
        prior_summaries: list[str] = []
        skipped = 0
        failed = 0
        for look in ctx.looks:
            look_id = look["look_id"]
            # 成员识别检查：任一成员识别失败 → 整个 look 跳过（保留占位）
            member_recogs: dict[str, dict] = {}
            member_items: dict[str, dict] = {}
            skip_reason = None
            for role, ref in look["members"].items():
                item = _find_item(role, ref)
                if item is None:
                    skip_reason = f"成员 ({role},{ref}) 不在色号池中"
                    break
                member_items[role] = item
                recog = recog_lookup.get(role, {}).get((item["code"], item["name"]))
                if recog is None:
                    skip_reason = f"成员 ({role},{item['code']}·{item['name']}) 无识别结果"
                    break
                if recog.get("_recognition_failed"):
                    skip_reason = f"成员 ({role},{item['code']}·{item['name']}) 2.2 识别失败：{recog.get('_error', '未知')}"
                    break
                member_recogs[role] = recog
            if skip_reason:
                skipped += 1
                look_outputs.append({
                    "look_id": look_id, "_skipped": True, "_skip_reason": skip_reason,
                    "成员方案": [],
                })
                continue

            # look 性别 = 成员色号在 2.3 分配中的性别（top 优先，其次首个成员）
            ordered_roles = ["top", "bottom", "main"]
            look_gender = "中性"
            for role in ordered_roles + list(look["members"].keys()):
                if role in member_items:
                    it = member_items[role]
                    g = gender_by_pair.get((it["code"], it["name"])) or gender_by_code.get(it["code"])
                    if g:
                        look_gender = g
                        break

            # 近期相似方案：聚合本 look 全部成员色号的历史摘要
            recent_lines: list[str] = []
            for role, it in member_items.items():
                code = it["code"]
                for d in (ctx.recent_color_designs.get(code) or [])[:3]:
                    tn = d.get("子主题名称") or "?"
                    pk = d.get("图案关键词") or "?"
                    desc = (d.get("方案说明") or "").strip().replace("\n", " ")[:100]
                    recent_lines.append(f"- [{role}·{code}] 主题「{tn}」· 图案「{pk}」· {desc}")
            recent_similar_text = "\n".join(recent_lines) if recent_lines else "无历史（首次设计或已关闭历史避重）"
            prior_looks_summary = "\n".join(prior_summaries) if prior_summaries else "本 look 是第一个，无已出方案"

            try:
                out = await node_2_5_look_design(
                    ctx,
                    look=look,
                    blueprint=blueprint,
                    single_topic_enriched=single_topic_enriched,
                    style_analyses=style_analyses,
                    recog_by_role=member_recogs,
                    look_gender=look_gender,
                    accumulated_by_role=accumulated_by_role,
                    recent_similar_text=recent_similar_text,
                    prior_looks_summary=prior_looks_summary,
                    parent_id=parent.node_id,
                )
                look_outputs.append(out)
                # 吸收累积状态（按成员 role 分别记）+ 生成已出摘要
                summary_bits = []
                for m in out.get("成员方案") or []:
                    role = m.get("role")
                    plans = m.get("设计方案") or []
                    if role in accumulated_by_role:
                        accumulated_by_role[role].absorb(plans)
                    for p in plans[:1]:
                        summary_bits.append(
                            f"{role}[{m.get('色号代码')}]：{p.get('面组合', '?')}/{p.get('档组合', '?')}"
                            f"·{(p.get('图案内容') or '')[:40]}"
                        )
                if summary_bits:
                    prior_summaries.append(f"- {look_id}（{look_gender}）：" + "；".join(summary_bits))
            except Exception as e:
                failed += 1
                err = f"{type(e).__name__}: {e}"
                look_outputs.append({
                    "look_id": look_id, "_design_failed": True, "_error": err,
                    "成员方案": [],
                })

        parent.set_output({
            "look数": len(look_outputs),
            "跳过数": skipped,
            "失败数": failed,
            "各role_A档已用": {r: a.a_tier_used for r, a in accumulated_by_role.items()},
        })
    return look_outputs, accumulated_by_role


# =========================================================================== #
# v7：2.4p 图库方向映射（多主题 × 图库输入源，替代 2.4 主题选择）
#
# 方向由用户在图库亲手选定（每个来源文件夹 = 一个方向），本节点只做：
#   ① 逐方向看图输出「图案视觉描述」（下游 2.5 不看图，这段文字是视觉信息唯一载体）
#   ② 色号 → 方向映射   ③ 整波策略
# 输出 schema 与 2.4 完全同构，下游零改动。
# =========================================================================== #

async def node_2_4p_direction_mapping(
    ctx: NodeContext,
    style_analysis: dict,
    color_results: list[dict],
    gender_plan: dict,
    parent_id: str | None = None,
) -> dict:
    cached = ctx.try_cache_lookup("图库方向映射", prompt_node_id="2.4p")
    if cached is not None:
        with ctx.writer.node(
            "2_4p", "图库方向映射 ⚡缓存",
            node_type="llm",
            parent_id=parent_id,
            prompt_version=ctx.bundle.version_label("2.4p"),
        ) as node:
            node.set_input({"_cache_hit": True})
            node.set_output(cached)
        return cached

    if not ctx.reference_set:
        raise ValueError("2.4p 要求 reference_set 非空（backend 应已装配）")

    system, user_tpl = ctx.bundle.load("2.4p")

    # 附图（按方向分组顺序）+ 清单说明 + 方向清单
    image_paths: list = []
    img_lines: list[str] = []
    directions_manifest: list[dict] = []
    idx = 1
    for grp in ctx.reference_set:
        directions_manifest.append({
            "子主题编号": grp["topic_id"],
            "子主题名称": grp["direction"],
            "参考图数": len(grp["files"]),
        })
        img_lines.append(f"◆ 方向「{grp['direction']}」（编号 {grp['topic_id']}）：")
        for p in grp["files"]:
            image_paths.append(Path(p))
            img_lines.append(f"  - Image {idx}：{Path(p).name}")
            idx += 1

    style_data = style_analysis.get("款式分析", {})
    user_prompt = render_user_prompt(user_tpl, {
        "参考图清单说明": "\n".join(img_lines),
        "方向清单JSON": json.dumps(directions_manifest, ensure_ascii=False, indent=2),
        "款式分析JSON": json.dumps(style_data, ensure_ascii=False, indent=2),
        "性别比例要求": ctx.gender_ratio,
        "性别分配JSON": json.dumps(gender_plan.get("分配结果", []), ensure_ascii=False, indent=2),
        "色号识别汇总JSON": json.dumps(color_results, ensure_ascii=False, indent=2),
    })
    unresolved = list_unresolved_placeholders(user_prompt)
    if unresolved:
        raise ValueError(f"2.4p prompt 渲染后仍含未解析占位符：{unresolved}")

    with ctx.writer.node(
        "2_4p", "图库方向映射",
        node_type="llm",
        parent_id=parent_id,
        prompt_version=ctx.bundle.version_label("2.4p"),
    ) as node:
        node.set_input({
            "方向数": len(directions_manifest),
            "参考图数": len(image_paths),
            "色号数": len(color_results),
            "user_prompt_chars": len(user_prompt),
            "user_prompt": user_prompt,
        })
        result = ctx.llm.call_with_images(
            system_prompt=system,
            user_prompt=user_prompt,
            image_paths=image_paths,
            output_marker_regex=r"##\s*STEP\s*2\.4[pP]\s*OUTPUT",
            model=ctx.model,
            max_tokens=ctx.max_tokens,
            stream_callback=node.emit_streaming,
            dry_run=ctx.dry_run,
            image_max_side=1536,   # 参考图看构图/线条/色相家族，1536 档足够
        )
        node.set_tokens(input=result.tokens_in, output=result.tokens_out)

        if ctx.dry_run:
            codes = [c.get("色号代码", "") for c in color_results]
            n_dir = max(1, len(directions_manifest))
            output = {
                "_dry_run": True,
                "整波主题策略": "dry-run 模拟（图库方向映射）",
                "选用主题列表": [
                    {**d, "档位推荐": "B", "性别气质": "中性", "整波定位": "主推",
                     "入选理由": "dry-run", "图案视觉描述": "dry-run",
                     "english_pattern_essence": "dry-run"}
                    for d in directions_manifest
                ],
                "色号到主题映射": [
                    {"色号代码": c, "营销色名": "",
                     "选用主题编号": directions_manifest[i % n_dir]["子主题编号"],
                     "选用主题名称": directions_manifest[i % n_dir]["子主题名称"],
                     "分配理由": "dry-run"}
                    for i, c in enumerate(codes)
                ],
            }
        else:
            if result.parsed_json is None:
                raise RuntimeError("2.4p 模型输出无法解析为 JSON")
            output = result.parsed_json
            # 硬校验 ①：方向列表原样进原样出（编号集合一致）
            want_ids = {d["子主题编号"] for d in directions_manifest}
            got = output.get("选用主题列表") or []
            got_ids = {t.get("子主题编号") for t in got}
            if got_ids != want_ids:
                raise RuntimeError(
                    f"2.4p 违反硬约束：方向集合被改动（期望 {want_ids}，实得 {got_ids}）"
                )
            # 名称强制回填为文件夹名（不信 LLM 抄写）
            name_by_id = {d["子主题编号"]: d["子主题名称"] for d in directions_manifest}
            for t in got:
                t["子主题名称"] = name_by_id.get(t.get("子主题编号"), t.get("子主题名称"))
            # 硬校验 ②：全部色号映射且编号合法
            mappings = output.get("色号到主题映射") or []
            if len(mappings) != len(color_results):
                raise RuntimeError(
                    f"2.4p 色号映射数 = {len(mappings)}（必须 = 色号总数 {len(color_results)}）"
                )
            for m in mappings:
                if m.get("选用主题编号") not in want_ids:
                    raise RuntimeError(
                        f"2.4p 映射的方向编号 '{m.get('选用主题编号')}' 不在给定集合内"
                    )
                m["选用主题名称"] = name_by_id.get(m.get("选用主题编号"), m.get("选用主题名称"))

        node.set_output(output)
    return output


def flatten_look_outputs(look_outputs: list[dict]) -> list[dict]:
    """
    把 look 输出拍平成与 2.5 输出兼容的 色号方案列表。

    每条 = 一个成员（含 role / 款号 / look_id 附加字段 + 标准 设计方案[]），
    step3 生图、审计、历史避重、Gallery 全部按老结构消费。
    """
    flat: list[dict] = []
    for lk in look_outputs:
        if lk.get("_skipped") or lk.get("_design_failed"):
            flat.append({
                "look_id": lk.get("look_id"),
                "色号代码": lk.get("look_id", "?"),
                "营销色名": "",
                "_skipped": lk.get("_skipped", False),
                "_design_failed": lk.get("_design_failed", False),
                "_error": lk.get("_error"),
                "设计方案": [],
                "无方案时的跳过原因": lk.get("_skip_reason") or lk.get("_error"),
            })
            continue
        for m in lk.get("成员方案") or []:
            flat.append(m)
    return flat
