"""
Step 2 输出指标——纯函数实现

每个 metric 函数签名：
    (step2_final_json: dict) -> dict[str, value]

便于：
- 单独 unit test
- 横向加新 metric 不破现有
- 老 step2_design.py 输出 与 新 agentic 输出 共用同一组函数
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ._audit import run_4_audits


# ============================================================================
# 七段式图生图 prompt 必含段落（来自 prompt/step2/04_single_color_design.md）
# ============================================================================
SEVEN_SECTIONS = [
    "[EDIT TASK]", "[RECOLOR]", "[LOCATION]",
    "[PATTERN]", "[COLORS]", "[PRESERVE]", "[FORBID]",
]


# ============================================================================
# JSON 结构归一化辅助（兼容 legacy 与 agentic 两种 schema）
# ============================================================================

def _step2_data(d: dict) -> dict:
    """优先读 step2_改款方案 包装；兼容裸字典。"""
    return d.get("step2_改款方案") or d


def _meta(d: dict) -> dict:
    return d.get("meta") or {}


def _flatten_plans(d: dict) -> list[dict]:
    """色号方案列表 → 全平方案列表，每条带 _色号代码 / _性别定向 反向标注。"""
    flat = []
    for c in _step2_data(d).get("色号方案列表", []):
        for p in c.get("设计方案", []):
            flat.append({
                **p,
                "_色号代码": c.get("色号代码", ""),
                "_营销色名": c.get("营销色名", ""),
                "_性别定向": c.get("性别定向", ""),
            })
    return flat


def _count_chinese_chars(s: str) -> int:
    if not s:
        return 0
    return len(re.findall(r"[一-鿿]", s))


# ============================================================================
# A. 结构完整性
# ============================================================================
# 目的：抓"模型偷懒少出方案"、"schema 字段缺失"——最廉价、最先看的指标
# 手段：纯计数；无任何模型偏见
# ============================================================================

def structural_metrics(d: dict) -> dict[str, Any]:
    s2 = _step2_data(d)
    meta = _meta(d)
    colors = s2.get("色号方案列表", [])
    plans = _flatten_plans(d)
    k = int(meta.get("方案数K") or meta.get("K") or 1)
    color_count = len(colors)
    expected = color_count * k
    return {
        "A_color_count_input": color_count,
        "A_color_count_with_plans": sum(1 for c in colors if c.get("设计方案")),
        "A_total_plans": len(plans),
        "A_expected_total_plans": expected,
        "A_plan_completion_rate": round(len(plans) / expected, 3) if expected > 0 else 0.0,
    }


# ============================================================================
# B. 审计四项（复用 orchestrator/audit.py，保证 eval 和 agent loop 同一份判定逻辑）
# ============================================================================
# 目的：你 prompt 里硬约束的 4 项规则——eval 直接拿审计函数测，不再让模型自检
# 手段：直接 import orchestrator.audit.run_audits
# ============================================================================

def audit_metrics(d: dict) -> dict[str, Any]:
    """复用 eval/_audit.py 本地实现——不依赖 orchestrator 包。"""
    colors = _step2_data(d).get("色号方案列表", [])
    a = run_4_audits(colors)
    return {
        "B_audit_overall_pass": a.get("overall_pass", False),
        "B_color_total": a.get("color_total", 0),
        "B_plan_total": a.get("plan_total", 0),
        "B_face_threshold": a.get("face_threshold", 0),
        "B_cross_face_threshold": a.get("cross_face_threshold", 0),
        "B_a_tier_quota": a.get("a_tier_quota", 0),
        # audit 1 面分布
        "B_audit1_face_dist_pass": a.get("a1_pass", False),
        "B_audit1_face_dist_front": a.get("a1_face_front", 0),
        "B_audit1_face_dist_back": a.get("a1_face_back", 0),
        "B_audit1_face_dist_side": a.get("a1_face_side", 0),
        # audit 2 跨面方案
        "B_audit2_cross_face_pass": a.get("a2_pass", False),
        "B_audit2_cross_face_count": a.get("a2_cross_face_count", 0),
        "B_audit2_cross_face_variety": a.get("a2_cross_face_variety", 0),
        # audit 3 A 档稀缺
        "B_audit3_a_tier_pass": a.get("a3_pass", False),
        "B_audit3_a_tier_used": a.get("a3_a_tier_count", 0),
        # audit 4 档组合+位置多样性
        "B_audit4_diversity_pass": a.get("a4_pass", False),
        "B_audit4_diversity_cross_tier_count": a.get("a4_cross_tier_count", 0),
        "B_audit4_diversity_single_tier_count": a.get("a4_single_tier_count", 0),
        "B_audit4_diversity_unique_positions": a.get("a4_unique_positions", 0),
    }


# ============================================================================
# C. 字段填充质量（细粒度，每方案算一次再聚合）
# ============================================================================
# 目的：抓"字段填了但内容是空话"、"prompt 没遵守 七段式"、"方案说明超 50 字"
# 手段：正则 + 字数；每条方案算一遍，输出占比（rate）—— 占比比绝对数更稳健
# ============================================================================

def field_completion_metrics(d: dict) -> dict[str, Any]:
    plans = _flatten_plans(d)
    n = len(plans)
    if n == 0:
        return {"C_plans_evaluated": 0}

    anchors_filled = 0
    face_tier_complete = 0
    seven_section_complete = 0
    visual_relationship_filled = 0
    plan_desc_within_limit = 0           # ≤ 50 中文字
    image_prompt_min_length = 0          # ≥ 200 字符（防止填一句"todo"）

    plan_desc_chars: list[int] = []
    image_prompt_chars: list[int] = []

    for p in plans:
        anchors = p.get("锚点列表") or []
        if anchors:
            anchors_filled += 1
            if all(a.get("面") and a.get("档") for a in anchors):
                face_tier_complete += 1

        prompt = p.get("图生图prompt") or ""
        image_prompt_chars.append(len(prompt))
        if all(sec in prompt for sec in SEVEN_SECTIONS):
            seven_section_complete += 1
        if len(prompt) >= 200:
            image_prompt_min_length += 1

        if p.get("视觉呼应关系"):
            visual_relationship_filled += 1

        desc = p.get("方案说明") or ""
        cn = _count_chinese_chars(desc)
        plan_desc_chars.append(cn)
        if cn <= 50:
            plan_desc_within_limit += 1

    return {
        "C_plans_evaluated": n,
        "C_anchors_filled_rate": round(anchors_filled / n, 3),
        "C_face_tier_complete_rate": round(face_tier_complete / n, 3),
        "C_seven_section_complete_rate": round(seven_section_complete / n, 3),
        "C_visual_relationship_filled_rate": round(visual_relationship_filled / n, 3),
        "C_plan_desc_within_50_chars_rate": round(plan_desc_within_limit / n, 3),
        "C_image_prompt_above_200_chars_rate": round(image_prompt_min_length / n, 3),
        "C_plan_desc_chars_avg": round(sum(plan_desc_chars) / n, 1),
        "C_image_prompt_chars_avg": round(sum(image_prompt_chars) / n, 1),
    }


# ============================================================================
# D. 比例合规（性别分配 / 变色判断）
# ============================================================================
# 目的：抓"用户要求 1:1 但模型分了 7 男 2 女"这种硬性违规
# 手段：Counter + 比对 meta.性别比例要求
# ============================================================================

def adherence_metrics(d: dict) -> dict[str, Any]:
    s2 = _step2_data(d)
    meta = _meta(d)
    gender_plan = s2.get("性别比规划", {}).get("分配结果", [])
    counter = Counter(item.get("性别定向") for item in gender_plan)
    colors = s2.get("色号方案列表", [])

    return {
        "D_gender_male_count": counter.get("男童", 0),
        "D_gender_female_count": counter.get("女童", 0),
        "D_gender_unisex_count": counter.get("中性", 0),
        "D_gender_total_assigned": sum(counter.values()),
        "D_user_gender_ratio_request": meta.get("性别比例要求") or "",
        "D_recolor_needed_count": sum(1 for c in colors if c.get("是否需要变色")),
        "D_recolor_skipped_count": sum(1 for c in colors if c.get("是否需要变色") is False),
        "D_colors_with_skip_reason": sum(1 for c in colors if c.get("无方案时的跳过原因")),
    }


# ============================================================================
# E. 质量（模型自评 — 仅作参考，权重低）
# ============================================================================
# 目的：抓"全场都打 9 分"这种自评坍缩；不作为版本好坏的主决策
# 手段：直接读 适配度 字段；附带趋势说明字数（≤100 字硬约束）
# 注：这里数字"好"不代表 prompt 真"好"——只表示模型自我认为好
# ============================================================================

def quality_metrics(d: dict) -> dict[str, Any]:
    plans = _flatten_plans(d)
    scores = [p.get("适配度") for p in plans if isinstance(p.get("适配度"), (int, float))]
    s2 = _step2_data(d)
    trend_desc = s2.get("趋势说明") or ""
    trend_chars = _count_chinese_chars(trend_desc)

    return {
        "E_adaptation_score_avg": round(sum(scores) / len(scores), 2) if scores else 0,
        "E_adaptation_score_min": min(scores) if scores else 0,
        "E_adaptation_score_max": max(scores) if scores else 0,
        "E_adaptation_score_stddev": round(_stddev(scores), 2),
        "E_trend_description_chars": trend_chars,
        "E_trend_description_within_100": trend_chars <= 100,
    }


def _stddev(values: list) -> float:
    if not values or len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return var ** 0.5


# ============================================================================
# F. 成本（token + latency + LLM call 次数）
# ============================================================================
# 目的：跟质量指标同表看 Pareto；agentic 拆分会比 legacy 多调用，但单次 token 少
# 手段：从 meta 直接读；llm_call_count 用 fixture 大小估算（agentic = 2 + 2N）
# ============================================================================

def cost_metrics(d: dict) -> dict[str, Any]:
    meta = _meta(d)
    tokens = meta.get("token用量") or meta.get("tokens") or {}
    colors = _step2_data(d).get("色号方案列表", [])
    n = len(colors)
    is_agentic = bool(meta.get("agentic"))

    tin = tokens.get("input") or 0
    tout = tokens.get("output") or 0

    return {
        "F_tokens_input": tin,
        "F_tokens_output": tout,
        "F_tokens_total": tin + tout,
        "F_elapsed_seconds": meta.get("耗时秒") or 0,
        "F_model": meta.get("模型") or "unknown",
        "F_agentic": is_agentic,
        # agentic：2.1(1) + 2.2(N) + 2.3(1) + 2.4(N) + 可能 2.7(≤3) ≈ 2 + 2N
        # legacy：1 次大调用
        "F_llm_call_count_estimate": (2 + 2 * n) if is_agentic else 1,
    }


# ============================================================================
# 主入口：跑全部指标，返回扁平 dict（CSV 友好）
# ============================================================================

def all_metrics(d: dict) -> dict[str, Any]:
    """对单份 step2 final JSON 计算全部指标。返回扁平 dict（一行）。"""
    out: dict[str, Any] = {"schema_version": "1.0"}
    out.update(structural_metrics(d))
    out.update(audit_metrics(d))
    out.update(field_completion_metrics(d))
    out.update(adherence_metrics(d))
    out.update(quality_metrics(d))
    out.update(cost_metrics(d))
    return out
