"""
eval/_audit.py — 4 项审计的本地副本

设计原因：eval 模块作为"下游审计员"应当独立于 orchestrator 实现。
否则：(1) 老 legacy 输出会因为没引入 orchestrator 而无法跑 eval；
      (2) 你改 orchestrator/audit 的内部实现，老 eval 数据就不可比了。

约定：本文件的判定逻辑是 eval schema 的一部分。若需要改判定规则，
     必须同时升 eval/__init__.py 的 SCHEMA_VERSION 字段。

来源：tool/orchestrator/audit.py（保持判定语义一致；表达上更简洁）
"""

from __future__ import annotations

import math
from collections import Counter


def _faces(plan: dict) -> set[str]:
    faces = set()
    for a in plan.get("锚点列表", []):
        if a.get("面"):
            faces.add(a["面"])
    if not faces:
        combo = plan.get("面组合", "")
        if "前" in combo: faces.add("前身")
        if "后" in combo: faces.add("后身")
        if "侧" in combo: faces.add("侧身")
    return faces


def _tiers(plan: dict) -> set[str]:
    return {a.get("档", "").upper() for a in plan.get("锚点列表", []) if a.get("档")}


def _positions(plan: dict) -> list[str]:
    return [a.get("位置中文", "") for a in plan.get("锚点列表", []) if a.get("位置中文")]


def run_4_audits(color_results: list[dict]) -> dict:
    """返回 4 项 audit 的扁平结果字典，供 eval/metrics.py 消费。"""
    n = len(color_results)
    if n == 0:
        return {"overall_pass": False, "no_colors": True}

    a_quota = max(1, n // 9)
    face_thresh = math.ceil(n / 3)
    cross_face_thresh = math.ceil(n * 2 / 3)

    plans = []
    for c in color_results:
        for p in c.get("设计方案", []):
            plans.append(p)

    # 审计 1：面分布
    face_counter: Counter = Counter()
    for p in plans:
        for f in _faces(p):
            face_counter[f] += 1
    a1_missing = [f for f in ("前身", "后身", "侧身") if face_counter[f] < face_thresh]
    a1_pass = len(a1_missing) == 0

    # 审计 2：跨面方案分布
    cross_face_count = sum(1 for p in plans if len(_faces(p)) >= 2)
    cross_face_combos = {p.get("面组合", "") for p in plans if len(_faces(p)) >= 2}
    a2_pass = cross_face_count >= cross_face_thresh and len(cross_face_combos) >= 2

    # 审计 3：A 档稀缺性
    a_tier_count = sum(1 for p in plans if "A" in _tiers(p))
    a3_pass = a_tier_count <= a_quota

    # 审计 4：档组合 + 位置多样性
    cross_thresh = math.ceil(n * 2 / 3)
    single_thresh = math.ceil(n / 3)
    pos_thresh = math.ceil(n / 3)
    cross_tier_combos: set[str] = set()
    cross_tier_count = 0
    single_tier_count = 0
    pos_counter: Counter = Counter()
    for p in plans:
        combo = p.get("档组合", "")
        if "+" in combo:
            cross_tier_count += 1
            cross_tier_combos.add(combo)
        else:
            single_tier_count += 1
        for pos in _positions(p):
            pos_counter[pos] += 1
    overcrowded = {pos: cnt for pos, cnt in pos_counter.items() if cnt > pos_thresh}
    a4_pass = (
        cross_tier_count >= cross_thresh
        and single_tier_count <= single_thresh
        and len(cross_tier_combos) >= 2
        and not overcrowded
        and len(pos_counter) >= 4
    )

    return {
        "overall_pass": a1_pass and a2_pass and a3_pass and a4_pass,
        "color_total": n,
        "plan_total": len(plans),
        "face_threshold": face_thresh,
        "cross_face_threshold": cross_face_thresh,
        "a_tier_quota": a_quota,
        # audit 1
        "a1_pass": a1_pass,
        "a1_face_front": face_counter.get("前身", 0),
        "a1_face_back": face_counter.get("后身", 0),
        "a1_face_side": face_counter.get("侧身", 0),
        "a1_missing_faces": a1_missing,
        # audit 2
        "a2_pass": a2_pass,
        "a2_cross_face_count": cross_face_count,
        "a2_cross_face_variety": len(cross_face_combos),
        "a2_cross_face_combos": sorted(cross_face_combos),
        # audit 3
        "a3_pass": a3_pass,
        "a3_a_tier_count": a_tier_count,
        # audit 4
        "a4_pass": a4_pass,
        "a4_cross_tier_count": cross_tier_count,
        "a4_single_tier_count": single_tier_count,
        "a4_cross_tier_variety": len(cross_tier_combos),
        "a4_unique_positions": len(pos_counter),
        "a4_overcrowded_positions": list(overcrowded.keys()),
    }
