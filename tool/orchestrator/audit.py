"""
Step 2.5 — 分布审计 Python Tool

输入：色号方案列表（来自 2.4 全部色号设计完成后的累积输出）+ placement_schema.json
输出：4 项审计结果 + 总体是否通过 + 失败原因明细（供 2.6 决策节点消费）

替代旧 step2_redesign.md 中"步骤四完成后：三维分布自检（强制）"的模型自检——
模型不再负责"数 1/3、数 ≤ 1"这种烂数学，全部由确定性 Python 函数计算。

零 token、毫秒级，可重复运行用于审计回归。
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class CheckResult:
    """单项审计结果。"""
    name: str
    passed: bool
    summary: str                              # 一句话总结
    detail: dict[str, Any] = field(default_factory=dict)
    violations: list[dict[str, Any]] = field(default_factory=list)
    # violation 结构：{ 色号代码, 方案编号, 违规项, 建议 }


@dataclass
class AuditResult:
    """完整审计结果，含 4 项 check 子结果。"""
    passed: bool                              # 4 项全过才算 True
    checks: list[CheckResult]
    plan_total: int
    color_total: int
    face_thresh: int                          # ceil(color_total / 3)，每面最少出现次数
    cross_face_thresh: int                    # ceil(color_total * 2 / 3)，跨面方案最少数
    a_tier_quota: int                         # 全集 A 档配额上限

    def to_json(self) -> dict:
        return {
            "通过": self.passed,
            "色号总数": self.color_total,
            "方案总数": self.plan_total,
            "面分布阈值（每面≥）": self.face_thresh,
            "跨面方案阈值（跨面≥）": self.cross_face_thresh,
            "A档配额上限": self.a_tier_quota,
            "审计项": [
                {
                    "项目": c.name,
                    "通过": c.passed,
                    "结论": c.summary,
                    "详情": c.detail,
                    "违规列表": c.violations,
                }
                for c in self.checks
            ],
        }


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _flatten_plans(color_results: list[dict]) -> list[dict]:
    """
    把 2.4 输出的色号方案列表展平成 [plan, plan, ...]，每个 plan 附带其所属色号代码 / 营销色名。
    """
    flat = []
    for color in color_results:
        code = color.get("色号代码", "")
        name = color.get("营销色名", "")
        for plan in color.get("设计方案", []):
            p = dict(plan)
            p.setdefault("_色号代码", code)
            p.setdefault("_营销色名", name)
            flat.append(p)
    return flat


def _faces_of_plan(plan: dict) -> set[str]:
    """从方案的 锚点列表 提取覆盖的面集合（前身/后身/侧身）。"""
    faces = set()
    for anchor in plan.get("锚点列表", []):
        face = anchor.get("面", "").strip()
        if face:
            faces.add(face)
    if not faces:
        # 退化路径：从面组合字段解析
        combo = plan.get("面组合", "")
        if "前" in combo:
            faces.add("前身")
        if "后" in combo:
            faces.add("后身")
        if "侧" in combo:
            faces.add("侧身")
    return faces


def _is_cross_face(plan: dict) -> bool:
    """方案是否为跨面组合（覆盖 ≥ 2 个面）。"""
    return len(_faces_of_plan(plan)) >= 2


def _tiers_of_plan(plan: dict) -> Counter:
    """统计方案内各档使用次数（按锚点）。"""
    cnt = Counter()
    for anchor in plan.get("锚点列表", []):
        tier = anchor.get("档", "").strip().upper()
        if tier in ("A", "B", "C", "D"):
            cnt[tier] += 1
    return cnt


def _positions_of_plan(plan: dict) -> list[str]:
    """提取方案的具体位置文字（中文版），用于位置多样性审计。"""
    positions = []
    for anchor in plan.get("锚点列表", []):
        pos = anchor.get("位置中文", "").strip()
        if pos:
            positions.append(pos)
    return positions


# --------------------------------------------------------------------------- #
# 4 项审计 Check
# --------------------------------------------------------------------------- #

def check_face_distribution(plans: list[dict], color_total: int) -> CheckResult:
    """
    审计 1：面分布
    硬性要求：每个面至少被 ceil(color_total/3) 个方案使用过。
    """
    thresh = math.ceil(color_total / 3)
    face_counter: Counter = Counter()
    for plan in plans:
        for face in _faces_of_plan(plan):
            face_counter[face] += 1

    missing = [face for face in ("前身", "后身", "侧身") if face_counter[face] < thresh]
    passed = len(missing) == 0
    violations = [
        {
            "违规项": f"面 '{face}' 仅出现 {face_counter[face]} 个方案，未达阈值 {thresh}",
            "建议": f"为接下来的色号优先选择包含 '{face}' 的面组合",
        }
        for face in missing
    ]
    summary = "✓ 三面均衡覆盖" if passed else f"✗ 面 {missing} 未达阈值 {thresh}"
    return CheckResult(
        name="审计1 面分布",
        passed=passed,
        summary=summary,
        detail={
            "阈值": thresh,
            "统计": dict(face_counter),
        },
        violations=violations,
    )


def check_face_combo_distribution(plans: list[dict], color_total: int) -> CheckResult:
    """
    审计 2：面组合分布
    硬性要求：跨面方案数 ≥ ceil(color_total * 2 / 3)；至少出现 2 种不同的跨面组合。
    """
    cross_thresh = math.ceil(color_total * 2 / 3)
    combo_counter: Counter = Counter()
    cross_combo_set: set[str] = set()
    cross_count = 0
    for plan in plans:
        combo = plan.get("面组合", "").strip()
        combo_counter[combo] += 1
        if _is_cross_face(plan):
            cross_count += 1
            cross_combo_set.add(combo)

    violations: list[dict] = []
    if cross_count < cross_thresh:
        violations.append({
            "违规项": f"跨面方案仅 {cross_count} 个，未达阈值 {cross_thresh}",
            "建议": f"把单面方案改为跨面组合，再增加 {cross_thresh - cross_count} 个跨面方案",
            "应改色号_候选": [p["_色号代码"] for p in plans if not _is_cross_face(p)],
        })
    if len(cross_combo_set) < 2 and cross_count > 0:
        violations.append({
            "违规项": f"跨面组合种类仅 {len(cross_combo_set)}，少于 2 种",
            "建议": "至少出现 2 种不同的跨面组合（如 前+后 和 前+侧 各占一部分）",
        })
    passed = len(violations) == 0
    summary = (
        f"✓ 跨面 {cross_count}/{color_total}，含 {len(cross_combo_set)} 种组合"
        if passed
        else f"✗ 跨面 {cross_count} (需 ≥ {cross_thresh})；种类 {len(cross_combo_set)}"
    )
    return CheckResult(
        name="审计2 面组合分布",
        passed=passed,
        summary=summary,
        detail={
            "跨面阈值": cross_thresh,
            "跨面方案数": cross_count,
            "跨面组合种类": list(cross_combo_set),
            "全部面组合统计": dict(combo_counter),
        },
        violations=violations,
    )


def check_a_tier_scarcity(plans: list[dict], a_tier_quota: int) -> CheckResult:
    """
    审计 3：A 档稀缺性
    硬性要求：A 档方案数 ≤ a_tier_quota（默认 1，9 色场景）。
    """
    a_plans = [p for p in plans if "A" in _tiers_of_plan(p)]
    a_count = len(a_plans)
    passed = a_count <= a_tier_quota
    violations: list[dict] = []
    if not passed:
        violations.append({
            "违规项": f"A 档方案 {a_count} 个，超过配额上限 {a_tier_quota}",
            "建议": "保留最值得超大表达的 1 个，其余降为 B 档",
            "应改色号_候选": [p["_色号代码"] for p in a_plans],
        })
    summary = f"✓ A 档 {a_count}/{a_tier_quota}" if passed else f"✗ A 档超额 {a_count}/{a_tier_quota}"
    return CheckResult(
        name="审计3 A档稀缺性",
        passed=passed,
        summary=summary,
        detail={
            "配额上限": a_tier_quota,
            "实际使用": a_count,
            "使用方案": [
                {"色号代码": p["_色号代码"], "方案编号": p.get("方案编号", "")}
                for p in a_plans
            ],
        },
        violations=violations,
    )


def check_position_diversity(plans: list[dict], color_total: int) -> CheckResult:
    """
    审计 4：档组合 + 具体位置分布
    硬性要求：
      - 含 + 的档组合方案数 ≥ ceil(color_total * 2 / 3)
      - 单档独图方案数 ≤ ceil(color_total / 3)
      - 任一具体位置使用次数 ≤ ceil(color_total / 3)
      - 必须出现至少 4 种不同的具体位置
      - 至少出现 2 种不同的档组合
    """
    cross_thresh = math.ceil(color_total * 2 / 3)
    single_thresh = math.ceil(color_total / 3)
    pos_thresh = math.ceil(color_total / 3)

    tier_combo_counter: Counter = Counter()
    cross_tier_combos: set[str] = set()
    pos_counter: Counter = Counter()
    single_tier_count = 0
    cross_tier_count = 0

    for plan in plans:
        combo = plan.get("档组合", "").strip()
        tier_combo_counter[combo] += 1
        if "+" in combo:
            cross_tier_count += 1
            cross_tier_combos.add(combo)
        else:
            single_tier_count += 1
        for pos in _positions_of_plan(plan):
            pos_counter[pos] += 1

    violations: list[dict] = []
    if cross_tier_count < cross_thresh:
        violations.append({
            "违规项": f"组合档（含+号）方案 {cross_tier_count} 个，未达阈值 {cross_thresh}",
            "建议": f"把单档独图方案改为组合档（如 B+C、B+C+D）",
        })
    if single_tier_count > single_thresh:
        violations.append({
            "违规项": f"单档独图方案 {single_tier_count} 个，超过上限 {single_thresh}",
            "建议": "选出 1~2 个降级方案改为组合档",
        })
    if len(cross_tier_combos) < 2 and cross_tier_count > 0:
        violations.append({
            "违规项": f"组合档种类仅 {len(cross_tier_combos)}，少于 2 种",
            "建议": "至少出现 2 种不同的档组合",
        })
    overcrowded_positions = {pos: cnt for pos, cnt in pos_counter.items() if cnt > pos_thresh}
    if overcrowded_positions:
        violations.append({
            "违规项": f"以下具体位置使用次数超过 1/3 阈值 ({pos_thresh}): {overcrowded_positions}",
            "建议": "把超额位置的方案改用其他具体位置",
        })
    if len(pos_counter) < 4:
        violations.append({
            "违规项": f"具体位置种类仅 {len(pos_counter)}，少于 4 种",
            "建议": "在剩余色号中扩展位置选择，至少出现 4 种不同的具体位置",
        })

    passed = len(violations) == 0
    summary = (
        f"✓ 组合档 {cross_tier_count}/{color_total}，位置 {len(pos_counter)} 种"
        if passed
        else f"✗ {len(violations)} 项违规"
    )
    return CheckResult(
        name="审计4 档组合+位置多样性",
        passed=passed,
        summary=summary,
        detail={
            "组合档阈值": cross_thresh,
            "组合档方案数": cross_tier_count,
            "单档独图方案数": single_tier_count,
            "单档独图上限": single_thresh,
            "组合档种类": list(cross_tier_combos),
            "全部档组合统计": dict(tier_combo_counter),
            "位置阈值": pos_thresh,
            "位置使用统计": dict(pos_counter),
            "位置种类": len(pos_counter),
        },
        violations=violations,
    )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def run_audits(
    color_results: list[dict],
    *,
    placement_schema_path: Path | None = None,
    a_tier_quota: int | None = None,
) -> AuditResult:
    """
    对全部色号的设计方案做 4 项审计。

    Args:
        color_results: 来自 2.4 全部输出的色号方案列表（每项含 设计方案[]）
        placement_schema_path: placement_schema.json 路径（可选；用于读取硬约束）
        a_tier_quota: 显式指定 A 档配额上限；None 时按 9 色场景默认 1

    Returns:
        AuditResult: 含 4 项 check 子结果与总体 passed
    """
    # 默认 A 档配额：9 色场景 1，按色号比例可调（这里简化为 max(1, color_total // 9)）
    color_total = len(color_results)
    if a_tier_quota is None:
        a_tier_quota = max(1, color_total // 9)

    plans = _flatten_plans(color_results)
    plan_total = len(plans)

    checks = [
        check_face_distribution(plans, color_total),
        check_face_combo_distribution(plans, color_total),
        check_a_tier_scarcity(plans, a_tier_quota),
        check_position_diversity(plans, color_total),
    ]
    overall_passed = all(c.passed for c in checks)

    return AuditResult(
        passed=overall_passed,
        checks=checks,
        plan_total=plan_total,
        color_total=color_total,
        face_thresh=math.ceil(color_total / 3),
        cross_face_thresh=math.ceil(color_total * 2 / 3),
        a_tier_quota=a_tier_quota,
    )


# --------------------------------------------------------------------------- #
# 决策辅助：根据审计结果生成"应重设计的色号 + 约束"
# --------------------------------------------------------------------------- #

def decide_redesign_targets(
    audit: AuditResult,
    color_results: list[dict],
    *,
    max_redesign: int = 3,
) -> list[dict]:
    """
    基于审计违规列表，挑选要重做的色号 + 给出每个的具体约束（喂给 2.7 重设计 prompt）。

    策略：
      - 审计 2 失败（跨面不足）→ 把单面方案的色号挑出来，让它们改跨面
      - 审计 3 失败（A 档超额）→ 把超额的 A 档方案降为 B 档
      - 审计 4 失败（具体位置超额）→ 把超额位置的方案换位置
      - 同一色号同时违反多项时合并约束

    Returns:
        [{色号代码, 重设计原因, 已尝试面组合, 已尝试具体位置, 已尝试档组合, 建议方向}]
    """
    # 收集每色号的原方案信息
    color_info: dict[str, dict] = {}
    for c in color_results:
        code = c.get("色号代码", "")
        plans = c.get("设计方案", [])
        if not plans:
            continue
        faces = set()
        positions = set()
        tier_combos = set()
        for p in plans:
            faces.add(p.get("面组合", ""))
            for pos in _positions_of_plan(p):
                positions.add(pos)
            tier_combos.add(p.get("档组合", ""))
        color_info[code] = {
            "色号代码": code,
            "营销色名": c.get("营销色名", ""),
            "已尝试面组合": list(faces),
            "已尝试具体位置": list(positions),
            "已尝试档组合": list(tier_combos),
            "violation_reasons": [],
            "建议方向": [],
        }

    # 标注违规
    for check in audit.checks:
        if check.passed:
            continue
        for v in check.violations:
            candidates = v.get("应改色号_候选", [])
            for code in candidates:
                if code in color_info:
                    color_info[code]["violation_reasons"].append(
                        f"[{check.name}] {v['违规项']}"
                    )
                    color_info[code]["建议方向"].append(v["建议"])

    # 没有 候选 字段的违规（如位置超额）按"占用超额位置的色号"反查
    pos_overcrowded = {}
    for check in audit.checks:
        if check.name == "审计4 档组合+位置多样性" and not check.passed:
            for v in check.violations:
                if "具体位置使用次数超过" in v["违规项"]:
                    # 从 detail 取位置统计
                    stats = check.detail.get("位置使用统计", {})
                    thresh = check.detail.get("位置阈值", 0)
                    for pos, cnt in stats.items():
                        if cnt > thresh:
                            pos_overcrowded[pos] = cnt

    if pos_overcrowded:
        for code, info in color_info.items():
            hit_positions = [p for p in info["已尝试具体位置"] if p in pos_overcrowded]
            if hit_positions:
                info["violation_reasons"].append(
                    f"[审计4] 占用了超额位置 {hit_positions}"
                )
                info["建议方向"].append(
                    f"避开位置 {hit_positions}，选其他位置"
                )

    # 筛出有违规的色号，按违规数排序
    targets = [
        {
            "色号代码": info["色号代码"],
            "营销色名": info["营销色名"],
            "重设计原因": "；".join(info["violation_reasons"]),
            "已尝试面组合": info["已尝试面组合"],
            "已尝试具体位置": info["已尝试具体位置"],
            "已尝试档组合": info["已尝试档组合"],
            "建议方向": "；".join(info["建议方向"]),
        }
        for info in color_info.values()
        if info["violation_reasons"]
    ]
    targets.sort(key=lambda t: -len(t["重设计原因"]))
    return targets[:max_redesign]


# --------------------------------------------------------------------------- #
# CLI 入口（独立测试用）
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="对一份 step2 输出 JSON 跑全部审计")
    parser.add_argument("--step2-json", required=True, help="step2 输出 JSON 路径")
    parser.add_argument("--a-tier-quota", type=int, default=None)
    args = parser.parse_args()

    data = json.loads(Path(args.step2_json).read_text(encoding="utf-8"))
    color_results = (
        data.get("step2_改款方案", {}).get("色号方案列表")
        or data.get("色号方案列表")
        or []
    )
    audit = run_audits(color_results, a_tier_quota=args.a_tier_quota)
    print(json.dumps(audit.to_json(), ensure_ascii=False, indent=2))
    if not audit.passed:
        targets = decide_redesign_targets(audit, color_results)
        print("\n========== 重设计目标 ==========")
        print(json.dumps(targets, ensure_ascii=False, indent=2))
