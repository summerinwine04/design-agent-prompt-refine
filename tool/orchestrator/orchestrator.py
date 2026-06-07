"""
Step2Run — agentic step2 的主编排器

把 7 个 node 函数串成完整执行流：
  2.1 → 2.2 (并发) → 2.3 → 2.4 (串行+累积) → 2.5 (tool) → 2.6 (decision) →
       通过：finalize；不通过：→ 2.7 (loop) → 2.5 再审 (最多 max_audit_rounds 轮)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audit import AuditResult, run_audits
from .llm import LLMClient
from .nodes import (
    AccumulatedState,
    NodeContext,
    node_2_1_style_analysis,
    node_2_2_color_recognition_all,
    node_2_3_gender_planning,
    node_2_4_all_colors,
    node_2_5_audit,
    node_2_6_decision,
    node_2_7_redesign,
)
from .prompts import PromptBundle
from .trace import TraceEvent, TraceWriter, new_run_id


# --------------------------------------------------------------------------- #
# Fixture & Config
# --------------------------------------------------------------------------- #

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
    _DOWNSTREAM = {
        "2.1": {"2.1", "2.2", "2.3", "2.4", "2.7"},
        "2.2": {"2.2", "2.3", "2.4", "2.7"},
        "2.3": {"2.3", "2.4", "2.7"},
        "2.4": {"2.4", "2.7"},
        "2.7": {"2.7"},
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
    ):
        self.fixture = fixture
        self.bundle = bundle
        self.llm = llm
        self.config = config or Step2Config()
        self.output_root = Path(output_root)
        self.event_callback = event_callback
        self.replay_from_dir = Path(replay_from_dir) if replay_from_dir else None

        self.base_name = f"{fixture.trend_name}_{fixture.style_no}"
        self.work_dir = self.output_root / self.base_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.work_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)

        self.run_id = new_run_id()
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(exist_ok=True)

        self.writer = TraceWriter(
            self.run_dir,
            external_callback=event_callback,
            verbose=verbose,
        )

        # 预扫源 run_dir 的缓存 + 计算 force_miss 集合
        self.replay_cache, self.force_miss_node_ids = self._build_replay_state()
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
                for node_id in ("2.1", "2.2", "2.3", "2.4", "2.7"):
                    if src_bundle.get(node_id) != new_bundle.get(node_id):
                        # 版本变了 → 自身 + 下游全部失效
                        force_miss |= self._DOWNSTREAM.get(node_id, {node_id})
            except Exception:
                # 无法解析就保守：全部 force_miss
                force_miss = {"2.1", "2.2", "2.3", "2.4", "2.7"}

        return cache, force_miss

    # ------------------- 主流程 ------------------- #

    async def run(self) -> dict:
        t0 = time.time()
        self.writer.emit_run_started(
            run_id=self.run_id,
            fixture=self.fixture.to_meta_dict(),
            prompt_bundle=self.bundle.to_meta_dict(),
        )

        a_tier_quota = self.config.a_tier_quota
        if a_tier_quota is None:
            a_tier_quota = max(1, len(self.fixture.color_items) // 9)

        ctx = NodeContext(
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
        )

        # 2.1 款式分析
        style_analysis = await node_2_1_style_analysis(ctx)

        # 2.2 颜色识别（并发）
        color_results = await node_2_2_color_recognition_all(
            ctx, style_analysis,
            concurrency=self.config.color_recognition_concurrency,
        )

        # 2.3 性别比规划
        gender_plan = await node_2_3_gender_planning(ctx, color_results)

        # 2.4 单色设计（串行）
        color_outputs, accumulated = await node_2_4_all_colors(
            ctx,
            color_results=color_results,
            gender_plan=gender_plan,
            style_analysis=style_analysis,
            a_tier_quota=a_tier_quota,
        )

        # 2.5 + 2.6 + 2.7 审计循环
        audit_history: list[AuditResult] = []
        for round_idx in range(self.config.max_audit_rounds):
            audit = await node_2_5_audit(ctx, color_outputs, a_tier_quota)
            audit_history.append(audit)
            if audit.passed:
                break
            if round_idx >= self.config.max_audit_rounds - 1:
                break
            targets = await node_2_6_decision(ctx, audit, color_outputs)
            if not targets:
                break
            color_outputs = await node_2_7_redesign(
                ctx,
                targets=targets,
                color_outputs=color_outputs,
                color_results=color_results,
                gender_plan=gender_plan,
                style_analysis=style_analysis,
                accumulated_before=accumulated,
            )

        # 组装最终 step2 JSON（与旧 step2_design.py 输出 schema 对齐，便于 step3 直接消费）
        final_audit = audit_history[-1] if audit_history else None
        final_output = self._assemble_final_output(
            style_analysis=style_analysis,
            gender_plan=gender_plan,
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

    # ------------------- 拼装输出 ------------------- #

    def _assemble_final_output(
        self,
        *,
        style_analysis: dict,
        gender_plan: dict,
        color_outputs: list[dict],
        final_audit: AuditResult | None,
    ) -> dict:
        # 与旧 step2_design.py 输出格式保持兼容
        step2_data = {
            "款号": self.fixture.style_no,
            "款式名": style_analysis.get("款式名", ""),
            "趋势说明": style_analysis.get("趋势说明", ""),
            "款式分析": style_analysis.get("款式分析", {}),
            "性别比规划": gender_plan,
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
