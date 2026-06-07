"""Pydantic models —— 用于 API 请求/响应序列化。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


# ============================================================================
# Prompt versions
# ============================================================================

class PromptVersion(BaseModel):
    node_id: str           # "2.1", "2.2", "2.3", "2.4", "2.7"
    version: str           # "current" / "v1" / "v2"
    file_path: str
    description: Optional[str] = None
    created_at: str


class PromptBundleSpec(BaseModel):
    """指定一次 run 用哪些 prompt 版本。未列出的节点走 current。"""
    versions: dict[str, str] = Field(default_factory=dict)


class PromptUpdateRequest(BaseModel):
    """编辑 prompt 内容并存为新版本。
    node_id 由 URL path 提供，不在 body 里重复要求。"""
    new_content: str       # 整个 .md 文件的新内容
    description: Optional[str] = None
    save_as: Optional[str] = None      # 不指定 → 覆盖 current；指定 → 存为 vN


# ============================================================================
# Fixtures
# ============================================================================

class Fixture(BaseModel):
    id: str
    name: str
    trend_json_path: str
    ref_image_path: str
    color_folder: str
    selected_colors: Optional[list[str]] = None
    gender_ratio: str
    num_designs_k: int
    description: Optional[str] = None
    created_at: str


class FixtureCreateRequest(BaseModel):
    id: str = Field(..., description="人类可读 slug，如 shanxi_yk250609")
    name: str
    trend_json_path: str
    ref_image_path: str
    color_folder: str
    selected_colors: Optional[list[str]] = None
    gender_ratio: str = "男女比接近1:1"
    num_designs_k: int = 1
    description: Optional[str] = None


# ============================================================================
# Runs
# ============================================================================

class RunCreateRequest(BaseModel):
    fixture_id: Optional[str] = None
    # 或直接传 inline fixture
    trend_json_path: Optional[str] = None
    ref_image_path: Optional[str] = None
    color_folder: Optional[str] = None
    selected_colors: Optional[list[str]] = None
    gender_ratio: str = "男女比接近1:1"
    num_designs_k: int = 1

    prompt_bundle: PromptBundleSpec = PromptBundleSpec()
    model: str = "gpt-5.5"
    max_tokens: int = 500000
    color_concurrency: int = 3
    max_audit_rounds: int = 2
    a_tier_quota: Optional[int] = None
    dry_run: bool = False


class RunForkRequest(BaseModel):
    from_node_id: str                              # 例如 "2_4_018"
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    # 如果只是改 prompt 内容（未存为新版本），临时传 inline 文本
    prompt_inline_edits: dict[str, str] = Field(default_factory=dict)
    description: Optional[str] = None


class NodeRetryRequest(BaseModel):
    node_id: str                                   # 例 "2_2_005"


RunStatus = Literal[
    "running",
    "succeeded",
    "succeeded_with_audit_warnings",     # 全跑完但 4 项审计未全过；用户可以接受继续，也可以 fork 重做
    "failed",                            # orchestrator 抛异常或被外部 kill
]


class RunSummary(BaseModel):
    id: str
    fixture_id: Optional[str]
    trend_name: str
    style_no: str
    gender_ratio: str
    num_designs_k: int
    status: RunStatus
    audit_passed: Optional[bool] = None
    audit_rounds: Optional[int] = None
    total_tokens_in: Optional[int] = None
    total_tokens_out: Optional[int] = None
    elapsed_ms: Optional[int] = None
    parent_run_id: Optional[str] = None
    fork_from_node: Optional[str] = None
    created_at: str


class RunDetail(RunSummary):
    prompt_bundle: dict[str, str]
    run_dir: str
    final_json_path: Optional[str]
    trace: Optional[list[dict]] = None             # 解析后的 trace.jsonl


class NodeDetail(BaseModel):
    node_id: str
    name: str
    node_type: Literal["llm", "tool", "decision", "loop", "run"]
    parent_id: Optional[str] = None
    prompt_version: Optional[str] = None
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    input: Optional[dict] = None
    output: Optional[dict] = None
    tokens: Optional[dict] = None
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None


# ============================================================================
# Settings
# ============================================================================

class Settings(BaseModel):
    has_openai_key: bool
    default_model: str
    default_max_tokens: int
    default_color_concurrency: int
    runs_dir: str
    prompts_dir: str


class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None          # 写入不回显
    default_model: Optional[str] = None
    default_max_tokens: Optional[int] = None
    default_color_concurrency: Optional[int] = None
