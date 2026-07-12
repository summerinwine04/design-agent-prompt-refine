"""
Step2Run — agentic step2 的主编排器（v5：支持 DIVERGE / CONVERGE 双图）

把 7 个 node 函数串成完整执行流：
  2.1 → 2.2 (并发) → 2.3 → 2.4 (串行+累积) → 2.5 (tool) → 2.6 (decision) →
       通过：finalize；不通过：→ 2.7 (loop) → 2.5 再审 (最多 max_audit_rounds 轮)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .audit import AuditResult, run_audits
from .llm import LLMClient
from .nodes import (
    AccumulatedState,
    NodeContext,
    flatten_look_outputs,
    node_2_1_style_analysis,
    node_2_2_color_recognition_all,
    node_2_3_gender_planning,
    node_2_4_5_blueprint,
    node_2_4_topic_selection,
    node_2_4s_single_topic,
    node_2_5_all_colors,
    node_2_5_all_looks,
    node_2_6_audit,
    node_2_7_decision,
    node_2_8_redesign,
    _enrich_assigned_topic,
)
from .prompts import PromptBundle
from .style_cache import StyleCache
from .trace import TraceEvent, TraceWriter, new_run_id


# --------------------------------------------------------------------------- #
# Fixture & Config
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 设计模式 → 节点图（graph registry）
#
#   DIVERGE  ：2.1 → 2.2 → 2.3 → 2.4(选2-3主题) → 2.5 per-color → 2.6/7/8
#   CONVERGE ：2.1×styles → 2.2×styles → 2.3 → 2.4s(选1主题) → 2.4.5 blueprint
#              → 2.5L per-look 联合设计 → 2.6 审计（信息性，暂不触发 2.7/2.8）
#
# Mode B（SINGLE_TOPIC_STRONG）与 Mode C（COLLECTION_2SKU）共用 CONVERGE 图，
# 差异全部在数据里（styles 长度、look 成员数）。
# --------------------------------------------------------------------------- #
MODE_GRAPHS = {
    "MULTI_TOPIC": "DIVERGE",
    "SINGLE_TOPIC_STRONG": "CONVERGE",
    "COLLECTION_2SKU": "CONVERGE",
}


@dataclass
class Step2Fixture:
    """冻结的运行输入。多次 run 复用同一 fixture 即可做 prompt A/B 对比。"""
    trend_name: str
    style_no: str
    ref_image_path: Path
    color_items: list[dict]                    # [{code, name, path}]
    trend_json: dict                            # step1 完整 JSON
    gender_ratio: str
    num_designs_k: int

    # --- v5 CONVERGE 图（强单主题 / Collection）---
    design_mode: str = "MULTI_TOPIC"
    # styles: [{role, style_no, ref_image_path(str), color_items:[{code,name,path}]}]
    styles: list = field(default_factory=list)
    # looks: [{look_id, name, members: {role: color_code}}]
    looks: list = field(default_factory=list)

    def to_meta_dict(self) -> dict:
        # 注意：trend_json_path / color_folder 必须存进来，否则 M4 fork 无法反推
        # 推断 color_folder = ref_image 所在目录 / ref_image stem
        color_folder = str(self.ref_image_path.parent / self.ref_image_path.stem)
        # trend_json_path 没存独立字段——但 trend_name 是 path stem，可以从约定位置反推
        # 此处尽量从 color_items 第一个 path 推回趋势位置（兜底）
        return {
            "trend_name": self.trend_name,
            "style_no": self.style_no,
            "ref_image": str(self.ref_image_path),
            "ref_image_path": str(self.ref_image_path),    # alias for fork
            "color_folder": color_folder,                    # M4 fork 直接复用
            "color_codes": [c["code"] for c in self.color_items],
            "color_count": len(self.color_items),
            "gender_ratio": self.gender_ratio,
            "K": self.num_designs_k,
            "num_designs_k": self.num_designs_k,             # alias
            # v5
            "design_mode": self.design_mode,
            "styles": [
                {
                    "role": s.get("role"),
                    "style_no": s.get("style_no"),
                    "ref_image_path": str(s.get("ref_image_path", "")),
                    "color_codes": [c["code"] for c in (s.get("color_items") or [])],
                }
                for s in (self.styles or [])
            ],
            "looks": self.looks or [],
        }


@dataclass
class Step2Config:
    """运行参数。"""
    model: str = "gpt-5.5"
    max_tokens: int = 500000
    color_recognition_concurrency: int = 3
    max_audit_rounds: int = 2                  # 含初次审计；2 表示最多 1 轮重设计
    a_tier_quota: int | None = None             # None → 自动按色号数推断
    dry_run: bool = False

    # --- 多样性避重（同款+同趋势最近 N 轮历史 → 注入 2.4/2.5 prompt） ---
    # 由 backend/runs.py 在起 orchestrator 前查数据库注入
    avoid_topic_ids: list = field(default_factory=list)         # 子主题编号列表（如 ["ST01","ST05"]）
    avoid_topic_names: list = field(default_factory=list)       # 子主题名称列表（跟 ids 平行）
    recent_color_designs: dict = field(default_factory=dict)    # color_code → [{子主题名称,图案关键词,方案说明摘要}, ...]

    # --- 款级持久缓存：2.1/2.2 结果跨 run / 跨趋势 / 跨模式复用（默认开） ---
    reuse_style_analysis: bool = True

    # --- v6 图库输入源（pattern_library）：跳过 2.4s 主题选择，直接进 2.4.5 ---
    input_source: str = "trend_report"                            # trend_report | pattern_library
    pattern_library_folder: str = ""                              # 图库文件夹相对名（充当"子主题名"）
    core_ref_image_paths: list = field(default_factory=list)      # 用户预筛的 1-3 张绝对路径


# --------------------------------------------------------------------------- #
# Step2Run
# --------------------------------------------------------------------------- #

class Step2Run:
    """
    一次完整的 step2 agentic 运行。

    用法：
        run = Step2Run(
            fixture=fix,
            bundle=PromptBundle(prompt_root=Path("prompt/step2")),
            llm=LLMClient(api_key=...),
            output_root=Path("trend2design"),
            replay_from_dir=Path("...runs/SOURCE_RUN/"),   # M4 fork：复用源 run 缓存
        )
        result = asyncio.run(run.run())
    """

    # 上游 → 下游级联失效图。若用户改了 key 的 prompt，则 value 中所有节点都强制 cache miss
    # （因为它们的输入依赖于上游 LLM 的输出）
    # v4 节点级联失效图：用户改了某节点的 prompt 版本，本节点 + 所有依赖它输出的下游
    # 节点都强制 cache miss。注意 2.5 prompt 改了会让 2.8 失效（2.8 复用 2.5 system prompt）。
    _DOWNSTREAM = {
        "2.1": {"2.1", "2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"},
        "2.2": {"2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"},
        "2.3": {"2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"},
        "2.4": {"2.4", "2.5", "2.8"},
        "2.5": {"2.5", "2.8"},
        "2.8": {"2.8"},
        # v5 CONVERGE 图
        "2.4s": {"2.4s", "2.4.5", "2.5L"},
        "2.4.5": {"2.4.5", "2.5L"},
        "2.5L": {"2.5L"},
    }

    def __init__(
        self,
        *,
        fixture: Step2Fixture,
        bundle: PromptBundle,
        llm: LLMClient,
        output_root: Path,
        config: Step2Config | None = None,
        event_callback: Callable[[TraceEvent], None] | None = None,
        verbose: bool = True,
        replay_from_dir: Path | None = None,    # M4 fork
        explicit_force_miss: set | None = None,  # 用户显式指定从该业务节点开始 force_miss
                                                  # 如 {"2.4"} → 2.4 + 下游全部重跑，不管 prompt 改没改
    ):
        self.fixture = fixture
        self.bundle = bundle
        self.llm = llm
        self.config = config or Step2Config()
        self.output_root = Path(output_root)
        self.event_callback = event_callback
        self.replay_from_dir = Path(replay_from_dir) if replay_from_dir else None
        self.explicit_force_miss = set(explicit_force_miss) if explicit_force_miss else set()

        self.base_name = f"{fixture.trend_name}_{fixture.style_no}"
        self.work_dir = self.output_root / self.base_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.work_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)

        self.run_id = new_run_id()
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(exist_ok=True)

        # 款级持久缓存（2.1/2.2 跨 run 复用），目录：{output_root}/_style_cache/
        self.style_cache = StyleCache(self.output_root / "_style_cache")

        self.writer = TraceWriter(
            self.run_dir,
            external_callback=event_callback,
            verbose=verbose,
        )

        # 预扫源 run_dir 的缓存 + 计算 force_miss 集合
        self.replay_cache, self.force_miss_node_ids = self._build_replay_state()
        # 用户显式指定的"从此节点开始重跑"也加入 force_miss（含下游级联）
        for explicit_node in self.explicit_force_miss:
            self.force_miss_node_ids |= self._DOWNSTREAM.get(explicit_node, {explicit_node})
        # 调试日志：把 cache 装载状态打印出来（fork 时排查用）
        if self.replay_from_dir:
            print(
                f"[Fork] source={self.replay_from_dir.name}  "
                f"cache_entries={len(self.replay_cache)}  "
                f"force_miss={sorted(self.force_miss_node_ids)}  "
                f"cache_keys={sorted(list(self.replay_cache.keys()))[:10]}",
                flush=True,
            )

    def _build_replay_state(self) -> tuple[dict[str, dict], set[str]]:
        """
        从 replay_from_dir 读取所有节点 IO JSON，按 slug 索引。
        同时根据本次 bundle vs 源 bundle 的差异，计算 force_miss 集合。
        """
        if not self.replay_from_dir or not self.replay_from_dir.is_dir():
            return {}, set()

        # 1. 索引所有 {node_id}_{slug}.json
        cache: dict[str, dict] = {}
        import json as _json
        for f in self.replay_from_dir.glob("*.json"):
            if f.name == "trace.jsonl" or f.name == "final_output.json":
                continue
            # 文件名格式：{node_id_prefix}_{counter}_{slug}.json
            # slug 可以含下划线，所以从右往左拆：去掉 .json 后切两个 _
            stem = f.stem
            parts = stem.split("_")
            # 至少需要 prefix + counter + slug_part 三段，slug 可以多段
            if len(parts) < 3:
                continue
            # 假设 counter 是 3 位数字，所以从右数倒数第二段是 counter
            # 找第一个纯数字段（counter 通常是 003 / 013 这种 3 位 0 填充）
            counter_idx = -1
            for i, p in enumerate(parts):
                if p.isdigit() and len(p) >= 2:
                    counter_idx = i
                    break
            if counter_idx < 0:
                continue
            slug_parts = parts[counter_idx + 1:]
            slug = "_".join(slug_parts)
            try:
                payload = _json.loads(f.read_text(encoding="utf-8"))
                cache[slug] = payload
            except Exception:
                pass

        # 2. 计算 force_miss：源 trace.jsonl 的 prompt_bundle 跟当前 bundle 哪些节点版本不一样
        force_miss: set[str] = set()
        source_trace = self.replay_from_dir / "trace.jsonl"
        if source_trace.is_file():
            try:
                first_line = source_trace.read_text(encoding="utf-8").splitlines()[0]
                run_started = _json.loads(first_line)
                src_bundle = run_started.get("prompt_bundle", {}) or {}
                new_bundle = self.bundle.to_meta_dict()
                for node_id in ("2.1", "2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"):
                    # 老 run 的 bundle 里没有 v5 节点 key —— 双方都缺时视为一致，
                    # 避免 fork 老 Mode A run 时误报 v5 节点 force_miss（虽无实际影响）
                    if src_bundle.get(node_id) != new_bundle.get(node_id):
                        # 版本变了 → 自身 + 下游全部失效
                        force_miss |= self._DOWNSTREAM.get(node_id, {node_id})
            except Exception:
                # 无法解析就保守：全部 force_miss
                force_miss = {"2.1", "2.2", "2.3", "2.4", "2.5", "2.8", "2.4s", "2.4.5", "2.5L"}

        return cache, force_miss

    # ------------------- 主流程 ------------------- #

    async def run(self) -> dict:
        """按 design_mode 路由到对应节点图。"""
        graph = MODE_GRAPHS.get(self.fixture.design_mode, "DIVERGE")
        if graph == "CONVERGE":
            return await self._run_converge()
        return await self._run_diverge()

    async def _run_diverge(self) -> dict:
        t0 = time.time()
        self.writer.emit_run_started(
            run_id=self.run_id,
            fixture=self.fixture.to_meta_dict(),
            prompt_bundle=self.bundle.to_meta_dict(),
        )

        a_tier_quota = self.config.a_tier_quota
        if a_tier_quota is None:
            a_tier_quota = max(1, len(self.fixture.color_items) // 9)

        # v5：DIVERGE 也走统一的 ctx 构造（含款级缓存注入）
        ctx = self._build_ctx()

        # 2.1 款式分析
        style_analysis = await node_2_1_style_analysis(ctx)

        # 2.2 颜色识别（并发）
        color_results = await node_2_2_color_recognition_all(
            ctx, style_analysis,
            concurrency=self.config.color_recognition_concurrency,
        )

        # 2.3 性别比规划
        gender_plan = await node_2_3_gender_planning(ctx, color_results)

        # 2.4 趋势主题选择（v4 新增节点）：选 2-3 个主题 + 每色号映射 1 个
        topic_selection = await node_2_4_topic_selection(
            ctx, style_analysis, color_results, gender_plan,
        )

        # 2.5 单色设计（串行，吃 2.4 分配的主题，不再吃完整趋势 JSON）
        color_outputs, accumulated = await node_2_5_all_colors(
            ctx,
            color_results=color_results,
            gender_plan=gender_plan,
            style_analysis=style_analysis,
            topic_selection=topic_selection,
            a_tier_quota=a_tier_quota,
        )

        # 2.6 + 2.7 + 2.8 审计 → 决策 → 重设计 循环
        audit_history: list[AuditResult] = []
        for round_idx in range(self.config.max_audit_rounds):
            audit = await node_2_6_audit(ctx, color_outputs, a_tier_quota)
            audit_history.append(audit)
            if audit.passed:
                break
            if round_idx >= self.config.max_audit_rounds - 1:
                break
            targets = await node_2_7_decision(ctx, audit, color_outputs)
            if not targets:
                break
            color_outputs = await node_2_8_redesign(
                ctx,
                targets=targets,
                color_outputs=color_outputs,
                color_results=color_results,
                gender_plan=gender_plan,
                style_analysis=style_analysis,
                topic_selection=topic_selection,
                accumulated_before=accumulated,
            )

        # 组装最终 step2 JSON（与旧 step2_design.py 输出 schema 对齐，便于 step3 直接消费）
        final_audit = audit_history[-1] if audit_history else None
        final_output = self._assemble_final_output(
            style_analysis=style_analysis,
            gender_plan=gender_plan,
            topic_selection=topic_selection,
            color_outputs=color_outputs,
            final_audit=final_audit,
        )

        # 落盘最终输出
        # 主路径：放到 run_dir 下保证每个 run 独立、不被后续 run 覆盖（M5 修复）
        final_path = self.run_dir / "final_output.json"
        final_path.write_text(
            json.dumps(final_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 镜像写到 work_dir 下保持向后兼容（外部脚本如 step3_generate.py 可能用这个名）
        legacy_path = self.work_dir / f"{self.base_name}.json"
        legacy_path.write_text(
            json.dumps(final_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        total_elapsed_ms = int((time.time() - t0) * 1000)
        self.writer.emit_run_finished(
            status="succeeded" if (final_audit and final_audit.passed) else "succeeded_with_audit_warnings",
            total_elapsed_ms=total_elapsed_ms,
        )

        return {
            "run_id": self.run_id,
            "final_output_path": str(final_path),
            "run_dir": str(self.run_dir),
            "audit_passed": bool(final_audit and final_audit.passed),
            "audit_history": [a.to_json() for a in audit_history],
            "total_elapsed_ms": total_elapsed_ms,
        }

    # ------------------- v5 CONVERGE 主流程 ------------------- #

    def _build_ctx(self) -> NodeContext:
        """构造共享 NodeContext（DIVERGE/CONVERGE 通用字段 + v5 字段）。"""
        return NodeContext(
            writer=self.writer,
            llm=self.llm,
            replay_cache=self.replay_cache,
            force_miss_node_ids=self.force_miss_node_ids,
            bundle=self.bundle,
            style_no=self.fixture.style_no,
            trend_name=self.fixture.trend_name,
            ref_image_path=self.fixture.ref_image_path,
            color_items=self.fixture.color_items,
            trend_json=self.fixture.trend_json,
            num_designs_k=self.fixture.num_designs_k,
            gender_ratio=self.fixture.gender_ratio,
            dry_run=self.config.dry_run,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            avoid_topic_ids=list(self.config.avoid_topic_ids),
            avoid_topic_names=list(self.config.avoid_topic_names),
            recent_color_designs=dict(self.config.recent_color_designs),
            design_mode=self.fixture.design_mode,
            styles=list(self.fixture.styles or []),
            looks=list(self.fixture.looks or []),
            use_style_cache=self.config.reuse_style_analysis,
            style_cache=self.style_cache,
            # v6 图库输入源透传
            input_source=self.config.input_source,
            pattern_library_folder=self.config.pattern_library_folder,
            core_ref_image_paths=list(self.config.core_ref_image_paths),
        )

    async def _run_converge(self) -> dict:
        """
        CONVERGE 图（SINGLE_TOPIC_STRONG / COLLECTION_2SKU）：

        2.1×styles → 2.2×styles → 2.3 → 2.4s 单主题 → 2.4.5 blueprint
        → 2.5L per-look 联合设计 → 2.6 审计（信息性）→ finalize

        Phase 1 说明：审计只做信息性报告，不触发 2.7/2.8 重设计循环
        （look 级重设计约束需要独立的 prompt，列为后续迭代）。
        """
        t0 = time.time()
        self.writer.emit_run_started(
            run_id=self.run_id,
            fixture=self.fixture.to_meta_dict(),
            prompt_bundle=self.bundle.to_meta_dict(),
        )

        if not self.fixture.styles:
            raise ValueError("CONVERGE 模式要求 fixture.styles 非空（backend 应已装配）")
        if not self.fixture.looks:
            raise ValueError("CONVERGE 模式要求 fixture.looks 非空（backend 应已装配）")

        a_tier_quota = self.config.a_tier_quota
        if a_tier_quota is None:
            a_tier_quota = max(1, len(self.fixture.looks) // 9)

        ctx = self._build_ctx()

        # --- 2.1 / 2.2：逐 style（Mode B 单款退化为 1 轮循环） ---
        style_analyses: dict[str, dict] = {}
        color_results: dict[str, list[dict]] = {}
        for slot in ctx.styles:
            role = slot["role"]
            style_ctx = replace(
                ctx,
                style_no=slot["style_no"],
                ref_image_path=Path(slot["ref_image_path"]),
                color_items=slot["color_items"],
            )
            suffix = f"·{slot['style_no']}" if len(ctx.styles) > 1 else ""
            analysis = await node_2_1_style_analysis(style_ctx, title_suffix=suffix)
            style_analyses[role] = analysis
            color_results[role] = await node_2_2_color_recognition_all(
                style_ctx, analysis,
                concurrency=self.config.color_recognition_concurrency,
                title_suffix=suffix,
            )

        # --- 2.3 性别比规划（全部成员色号拍平后一次调用） ---
        flat_colors: list[dict] = []
        for slot in ctx.styles:
            for r in color_results.get(slot["role"]) or []:
                flat_colors.append({"款号": slot["style_no"], **r})
        gender_plan = await node_2_3_gender_planning(ctx, flat_colors)

        # --- 2.4s 单主题选择 ---
        # v6 图库输入源：SKIP 2.4s，用文件夹名当子主题，直接构造 fake topic_selection
        # 语义："用户已经用手指了 → 主题已锁定 → 不需要 LLM 再选一遍"
        if ctx.input_source == "pattern_library":
            folder = ctx.pattern_library_folder or "pattern_library_folder"
            # 命名约定：<母主题>-<子主题方向>，取 `-` 后半为子主题名，前半为编号占位
            if "-" in folder:
                parent_topic, sub_topic_name = folder.split("-", 1)
            else:
                parent_topic, sub_topic_name = folder, folder
            fake_topic = {
                "子主题编号": f"PL-{sub_topic_name[:12]}",     # PL 前缀标识图库输入
                "子主题名称": sub_topic_name,
                "档位推荐": "B",
                "性别气质": "中性",
                "整波定位": "图库主题（跳过 2.4s）",
                "入选理由": f"用户直接在图库中选择了「{folder}」文件夹",
                "母图案延展性评估": "由图库图承载视觉证据",
            }
            all_members = []
            for slot in ctx.styles:
                for r in color_results.get(slot["role"]) or []:
                    all_members.append({
                        "款号": slot["style_no"],
                        "色号代码": r.get("色号代码", ""),
                        "营销色名": r.get("营销色名", ""),
                        "选用主题编号": fake_topic["子主题编号"],
                        "选用主题名称": fake_topic["子主题名称"],
                        "分配理由": f"图库输入源，全部色号统一映射到「{sub_topic_name}」",
                    })
            topic_selection = {
                "整波主题策略": f"图库输入源：所有色号收敛到「{sub_topic_name}」",
                "选用主题列表": [fake_topic],
                "色号到主题映射": all_members,
                "_pattern_library_skipped_2_4s": True,
                "_pattern_library_folder": folder,
            }
        else:
            topic_selection = await node_2_4s_single_topic(
                ctx, style_analyses, color_results, gender_plan,
            )
        sel = (topic_selection.get("选用主题列表") or [{}])[0]
        assigned = {
            "分配主题编号": sel.get("子主题编号"),
            "分配主题名称": sel.get("子主题名称"),
            "分配理由": sel.get("入选理由") or "",
            "主题元信息": sel,
        }
        single_topic_enriched = _enrich_assigned_topic(ctx, assigned)

        # --- 2.4.5 母图案 Blueprint ---
        blueprint = await node_2_4_5_blueprint(
            ctx, single_topic_enriched, style_analyses, color_results,
        )

        # --- 2.5L 全部 look 联合设计 ---
        look_outputs, accumulated_by_role = await node_2_5_all_looks(
            ctx,
            blueprint=blueprint,
            single_topic_enriched=single_topic_enriched,
            style_analyses=style_analyses,
            color_results=color_results,
            gender_plan=gender_plan,
            a_tier_quota=a_tier_quota,
        )
        flat_outputs = flatten_look_outputs(look_outputs)

        # --- 2.6 分布审计（信息性；Phase 1 不触发重设计循环） ---
        audit_history: list[AuditResult] = []
        try:
            audit = await node_2_6_audit(ctx, flat_outputs, a_tier_quota)
            audit_history.append(audit)
        except Exception:
            pass

        final_audit = audit_history[-1] if audit_history else None
        final_output = self._assemble_final_output_converge(
            style_analyses=style_analyses,
            gender_plan=gender_plan,
            topic_selection=topic_selection,
            blueprint=blueprint,
            look_outputs=look_outputs,
            flat_outputs=flat_outputs,
            final_audit=final_audit,
        )

        final_path = self.run_dir / "final_output.json"
        final_path.write_text(
            json.dumps(final_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        legacy_path = self.work_dir / f"{self.base_name}.json"
        legacy_path.write_text(
            json.dumps(final_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        total_elapsed_ms = int((time.time() - t0) * 1000)
        self.writer.emit_run_finished(
            status="succeeded" if (final_audit and final_audit.passed) else "succeeded_with_audit_warnings",
            total_elapsed_ms=total_elapsed_ms,
        )
        return {
            "run_id": self.run_id,
            "final_output_path": str(final_path),
            "run_dir": str(self.run_dir),
            "audit_passed": bool(final_audit and final_audit.passed),
            "audit_history": [a.to_json() for a in audit_history],
            "total_elapsed_ms": total_elapsed_ms,
        }

    def _assemble_final_output_converge(
        self,
        *,
        style_analyses: dict[str, dict],
        gender_plan: dict,
        topic_selection: dict,
        blueprint: dict,
        look_outputs: list[dict],
        flat_outputs: list[dict],
        final_audit: AuditResult | None,
    ) -> dict:
        """
        v5 CONVERGE final JSON。

        兼容性约定（下游全部按老结构消费，不需要感知 v5）：
          - step2_改款方案.主题选择.选用主题列表     —— 长度 1（历史避重照常抽取）
          - step2_改款方案.色号方案列表              —— look 成员拍平（step3 生图 / Gallery / eval 照常）
        v5 新增字段：design_mode / styles / 母图案蓝图 / look方案列表。
        """
        primary_role = self.fixture.styles[0]["role"] if self.fixture.styles else "main"
        primary_analysis = style_analyses.get(primary_role, {})

        step2_data = {
            "schema_version": "v5",
            "design_mode": self.fixture.design_mode,
            "款号": self.fixture.style_no,
            "款式名": primary_analysis.get("款式名", ""),
            "款式分析": primary_analysis.get("款式分析", {}),
            "styles": [
                {
                    "role": s["role"],
                    "款号": s["style_no"],
                    "款式名": style_analyses.get(s["role"], {}).get("款式名", ""),
                    "款式分析": style_analyses.get(s["role"], {}).get("款式分析", {}),
                }
                for s in self.fixture.styles
            ],
            "性别比规划": gender_plan,
            "主题选择": topic_selection,
            "母图案蓝图": blueprint,
            "look方案列表": look_outputs,
            "色号方案列表": flat_outputs,
        }
        if final_audit:
            audit_json = final_audit.to_json()
            step2_data["agentic_audit"] = audit_json
            step2_data["placement分布"] = (
                audit_json["审计项"][-1].get("详情", {}).get("位置使用统计", {})
                if audit_json.get("审计项") else {}
            )

        totals = self.writer._totals if hasattr(self, "writer") else {}
        token_usage = {
            "input": totals.get("input_tokens", 0),
            "output": totals.get("output_tokens", 0),
        }
        elapsed_sec = round((self.writer._t0 and (
            __import__("time").time() - self.writer._t0
        )) or 0, 1) if hasattr(self, "writer") else 0

        # 色号图列表：全部 style 拍平（带 role/款号，索引与 2.5L 图片顺序无关，仅供展示）
        color_list_meta = []
        idx = 2
        for s in self.fixture.styles:
            for item in s.get("color_items") or []:
                color_list_meta.append({
                    "图片索引": idx,
                    "role": s["role"],
                    "款号": s["style_no"],
                    "色号代码": item["code"],
                    "营销色名": item["name"],
                    "文件名": Path(item["path"]).name,
                })
                idx += 1

        return {
            "meta": {
                "趋势名字": self.fixture.trend_name,
                "款号": self.fixture.style_no,
                "性别比例要求": self.fixture.gender_ratio,
                "方案数K": self.fixture.num_designs_k,
                "模型": self.config.model,
                "design_mode": self.fixture.design_mode,
                "looks": self.fixture.looks,
                "色号图列表": color_list_meta,
                "agentic": True,
                "run_id": self.run_id,
                "prompt_bundle": self.bundle.to_meta_dict(),
                "token用量": token_usage,
                "耗时秒": elapsed_sec,
            },
            "step2_改款方案": step2_data,
            "raw_response": None,
        }

    # ------------------- 拼装输出 ------------------- #

    def _assemble_final_output(
        self,
        *,
        style_analysis: dict,
        gender_plan: dict,
        topic_selection: dict,
        color_outputs: list[dict],
        final_audit: AuditResult | None,
    ) -> dict:
        # 与旧 step2_design.py 输出格式保持兼容；v4 起新增「主题选择」字段
        step2_data = {
            "schema_version": "v4",                          # v4 hard break 标识，前端据此区分
            "款号": self.fixture.style_no,
            "款式名": style_analysis.get("款式名", ""),
            "款式分析": style_analysis.get("款式分析", {}),
            "性别比规划": gender_plan,
            "主题选择": topic_selection,                      # v4 新增：2.4 节点输出
            "色号方案列表": color_outputs,
        }
        if final_audit:
            audit_json = final_audit.to_json()
            step2_data["面分布"] = next(
                (c for c in audit_json["审计项"] if c["项目"] == "审计1 面分布"),
                {},
            )
            step2_data["面组合分布"] = next(
                (c for c in audit_json["审计项"] if c["项目"] == "审计2 面组合分布"),
                {},
            )
            step2_data["档组合分布"] = next(
                (c for c in audit_json["审计项"] if c["项目"] == "审计4 档组合+位置多样性"),
                {},
            )
            step2_data["placement分布"] = audit_json["审计项"][-1].get("详情", {}).get("位置使用统计", {})
            step2_data["agentic_audit"] = audit_json

        # 从 TraceWriter 累计的 totals 读取真实 token 用量（修复：之前 meta 没聚合 cost）
        totals = self.writer._totals if hasattr(self, "writer") else {}
        token_usage = {
            "input": totals.get("input_tokens", 0),
            "output": totals.get("output_tokens", 0),
        }
        elapsed_sec = round((self.writer._t0 and (
            __import__("time").time() - self.writer._t0
        )) or 0, 1) if hasattr(self, "writer") else 0

        return {
            "meta": {
                "趋势名字": self.fixture.trend_name,
                "款号": self.fixture.style_no,
                "性别比例要求": self.fixture.gender_ratio,
                "方案数K": self.fixture.num_designs_k,
                "模型": self.config.model,
                "色号图列表": [
                    {
                        "图片索引": i + 2,
                        "色号代码": item["code"],
                        "营销色名": item["name"],
                        "文件名": Path(item["path"]).name,
                    }
                    for i, item in enumerate(self.fixture.color_items)
                ],
                "agentic": True,
                "run_id": self.run_id,
                "prompt_bundle": self.bundle.to_meta_dict(),
                "audit_rounds": len(getattr(self, "_audit_history", [])),
                # 新增：把 token 用量 + 耗时聚合到 meta，eval 直接消费
                "token用量": token_usage,
                "耗时秒": elapsed_sec,
            },
            "step2_改款方案": step2_data,
            "raw_response": None,
        }
