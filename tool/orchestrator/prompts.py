"""
Prompt 加载与版本管理

支持的 prompt 文件格式（与现有 step1_trend_parse.md / step2_redesign.md 二段式格式一致）：
  ## ═══ SYSTEM PROMPT ═══
  ... system prompt 内容 ...
  ---
  ## ═══ USER PROMPT TEMPLATE ═══
  ... user prompt template，含 {{变量}} 占位符 ...

每个子 prompt 文件独立版本管理：
  prompt/step2/01_style_analysis.md           ← 当前版本
  prompt/step2/01_style_analysis.v1.md        ← 历史版本
  prompt/step2/01_style_analysis.v2.md
  ...

调用方通过 PromptBundle 把一组子 prompt 的"版本组"固定下来，确保一次 run 用同一组 prompt 文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Prompt 加载
# --------------------------------------------------------------------------- #

def load_prompt_file(md_path: Path) -> tuple[str, str]:
    """
    从 .md 文件中提取 (system_prompt, user_prompt_template)。

    与 step1_trend.py / step2_design.py 中的 load_step{1,2}_prompts 行为兼容。
    """
    text = md_path.read_text(encoding="utf-8")

    sys_match = re.search(
        r"═══\s*SYSTEM PROMPT\s*═══\s*\n([\s\S]*?)\n---",
        text,
        re.DOTALL,
    )
    if not sys_match:
        raise ValueError(f"未能在 {md_path} 中找到 SYSTEM PROMPT 块")
    system_prompt = sys_match.group(1).strip()

    user_match = re.search(
        r"═══\s*USER PROMPT TEMPLATE\s*═══\s*\n([\s\S]*?)$",
        text,
        re.DOTALL,
    )
    if not user_match:
        raise ValueError(f"未能在 {md_path} 中找到 USER PROMPT TEMPLATE 块")
    raw_user = user_match.group(1)
    user_lines = [
        line for line in raw_user.splitlines()
        if not line.strip().startswith(">")
    ]
    user_template = "\n".join(user_lines).strip()

    return system_prompt, user_template


def render_user_prompt(template: str, variables: dict) -> str:
    """把 {{变量}} 占位符替换为实际值。未提供的变量保持原样，便于排查。"""
    rendered = template
    for k, v in variables.items():
        placeholder = "{{" + k + "}}"
        rendered = rendered.replace(placeholder, str(v))
    return rendered


def list_unresolved_placeholders(rendered: str) -> list[str]:
    """检查渲染后仍未替换的占位符，用于校验完整性。"""
    return re.findall(r"\{\{[^}]+\}\}", rendered)


# --------------------------------------------------------------------------- #
# PromptBundle
# --------------------------------------------------------------------------- #

# 子节点 → 默认 prompt 文件名（在 prompt/step2/ 下）
DEFAULT_PROMPT_FILES = {
    # v4 节点结构（旧 04_single_color_design / 04b_redesign 保留为历史快照不删，不在此映射）
    "2.1": "01_style_analysis.md",
    "2.2": "02_color_recognition.md",
    "2.3": "03_gender_planning.md",
    "2.4": "04_topic_selection.md",            # v4 新增：趋势主题选择
    "2.5": "05_single_color_design.md",
    "2.8": "05b_redesign_constraint.md",       # 局部重设计，复用 2.5 的 system，单独的 user 模板
    # v5 CONVERGE 图（强单主题 / Collection）专用节点。
    # DIVERGE 图（MULTI_TOPIC 现状）不加载这三个文件，Mode A 完全免疫。
    "2.4s": "04s_single_topic_selection.md",   # v5：单主题选择（收敛模式，选且仅选 1 个）
    "2.4.5": "045_pattern_blueprint.md",       # v5：母图案 Blueprint 设计
    "2.5L": "05L_look_variant_design.md",      # v5：按 look 联合变体设计
}


@dataclass
class PromptBundle:
    """
    一次 step2 run 用的 prompt 版本组。

    显式版本绑定示例：
      bundle = PromptBundle(
          prompt_root=Path("prompt/step2"),
          versions={"2.1": "v3", "2.2": "v1", "2.4": "v5"},  # 未指定的用 current
      )
    """
    prompt_root: Path
    versions: dict[str, str] = field(default_factory=dict)
    _cache: dict[str, tuple[str, str]] = field(default_factory=dict, init=False, repr=False)

    def file_path(self, node_id: str) -> Path:
        """返回某子节点对应的 prompt 文件路径（考虑版本）。"""
        if node_id not in DEFAULT_PROMPT_FILES:
            raise KeyError(f"未知子节点 prompt 标识：{node_id}")
        base = DEFAULT_PROMPT_FILES[node_id]
        version = self.versions.get(node_id, "current")
        if version == "current":
            return self.prompt_root / base
        # 历史版本：插入 .v{N} 后缀
        stem, suffix = base.rsplit(".", 1)
        return self.prompt_root / f"{stem}.{version}.{suffix}"

    def load(self, node_id: str) -> tuple[str, str]:
        """加载并缓存某子节点的 (system, user_template)。"""
        if node_id not in self._cache:
            path = self.file_path(node_id)
            if not path.is_file():
                raise FileNotFoundError(f"Prompt 文件不存在：{path}")
            self._cache[node_id] = load_prompt_file(path)
        return self._cache[node_id]

    def version_label(self, node_id: str) -> str:
        version = self.versions.get(node_id, "current")
        return f"{DEFAULT_PROMPT_FILES.get(node_id, '?')}@{version}"

    def to_meta_dict(self) -> dict[str, str]:
        """供 trace 事件序列化使用。"""
        return {
            node: self.version_label(node)
            for node in DEFAULT_PROMPT_FILES
        }
