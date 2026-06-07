"""
Compare — 给定多个 eval row，输出横向对比表 + 差异高亮
"""

from __future__ import annotations

from typing import Any


# 指标 → "高就是好" / "低就是好" / "无方向"
# 这决定 compare 时给 ✓/✗ 哪边
METRIC_DIRECTION = {
    # 高就是好
    "A_plan_completion_rate": "↑",
    "B_audit_overall_pass": "↑",
    "B_audit1_face_dist_pass": "↑",
    "B_audit2_cross_face_pass": "↑",
    "B_audit2_cross_face_count": "↑",
    "B_audit2_cross_face_variety": "↑",
    "B_audit3_a_tier_pass": "↑",
    "B_audit4_diversity_pass": "↑",
    "B_audit4_diversity_unique_positions": "↑",
    "B_audit4_diversity_cross_tier_count": "↑",
    "C_anchors_filled_rate": "↑",
    "C_face_tier_complete_rate": "↑",
    "C_seven_section_complete_rate": "↑",
    "C_visual_relationship_filled_rate": "↑",
    "C_plan_desc_within_50_chars_rate": "↑",
    "C_image_prompt_above_200_chars_rate": "↑",
    "E_adaptation_score_avg": "↑",
    "E_adaptation_score_min": "↑",
    "E_trend_description_within_100": "↑",
    # 低就是好
    "F_tokens_input": "↓",
    "F_tokens_output": "↓",
    "F_tokens_total": "↓",
    "F_elapsed_seconds": "↓",
    "F_llm_call_count_estimate": "↓",
    "E_adaptation_score_stddev": "↓",
}


def compare_rows(rows: list[dict], *, show_unchanged: bool = True) -> str:
    """生成 ASCII 表格供命令行/日志查看。

    Args:
        rows: 每行是 eval_step2_json 的输出
        show_unchanged: False 时只显示 v1≠v2 的指标
    """
    if not rows:
        return "(no rows)"
    if len(rows) == 1:
        return _single_row(rows[0])

    # 收集所有 metric 键（排除 _ 前缀的元数据列）
    keys: set[str] = set()
    for r in rows:
        keys.update(k for k in r.keys() if not k.startswith("_") and k != "schema_version")

    # 分组：质量类 (A/B/C/D/E)、成本类 (F)
    grouped = _group_by_prefix(sorted(keys))

    labels = [r.get("_label", f"run{i}") for i, r in enumerate(rows)]
    lines = []
    lines.append("")
    lines.append("=" * 80)
    lines.append(f"Eval Compare —— {len(rows)} runs: " + ", ".join(labels))
    lines.append("=" * 80)

    for group_key, group_metrics in grouped.items():
        lines.append("")
        lines.append(f"── {GROUP_NAMES.get(group_key, group_key)} " + "─" * (60 - len(GROUP_NAMES.get(group_key, group_key))))
        for m in group_metrics:
            values = [r.get(m) for r in rows]
            if not show_unchanged and len(set(map(_norm, values))) == 1:
                continue
            arrow = METRIC_DIRECTION.get(m, " ")
            value_str = " | ".join(_fmt(v) for v in values)
            lines.append(f"  [{arrow}] {m:<45s}  {value_str}")

    lines.append("")
    return "\n".join(lines)


def _single_row(row: dict) -> str:
    """单个 run 的全指标总览。"""
    keys = sorted(k for k in row.keys() if not k.startswith("_") and k != "schema_version")
    grouped = _group_by_prefix(keys)
    lines = []
    lines.append("=" * 80)
    lines.append(f"Eval Report —— {row.get('_label', '(no label)')}")
    lines.append(f"Source: {row.get('_source_file', '?')}")
    lines.append("=" * 80)
    for group_key, group_metrics in grouped.items():
        lines.append("")
        lines.append(f"── {GROUP_NAMES.get(group_key, group_key)} " + "─" * (60 - len(GROUP_NAMES.get(group_key, group_key))))
        for m in group_metrics:
            v = row.get(m)
            lines.append(f"  {m:<45s}  {_fmt(v)}")
    lines.append("")
    return "\n".join(lines)


GROUP_NAMES = {
    "A": "A. 结构完整性",
    "B": "B. 审计四项（硬性规则）",
    "C": "C. 字段填充质量",
    "D": "D. 比例合规",
    "E": "E. 质量（模型自评，仅参考）",
    "F": "F. 成本",
}


def _group_by_prefix(keys: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k in keys:
        prefix = k[0] if k and k[0] in "ABCDEF" else "X"
        out.setdefault(prefix, []).append(k)
    return out


def _norm(v: Any) -> Any:
    """规范化用于 set 比较。"""
    if isinstance(v, float):
        return round(v, 6)
    return v


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "✓" if v else "✗"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)
