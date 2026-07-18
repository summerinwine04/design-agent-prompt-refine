"""Pydantic models —— 用于 API 请求/响应序列化。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


# ============================================================================
# Prompt versions
# ============================================================================

class PromptVersion(BaseModel):
    node_id: str           # v4: "2.1", "2.2", "2.3", "2.4", "2.5", "2.8"
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

# v5 设计模式：
#   MULTI_TOPIC          —— Mode A 现状（多子主题发散，DIVERGE 图）
#   SINGLE_TOPIC_STRONG  —— Mode B 单款强单主题（母图案收敛，CONVERGE 图）
#   COLLECTION_2SKU      —— Mode C 上下装成套（look 预选 + 联合设计，CONVERGE 图）
DesignMode = Literal["MULTI_TOPIC", "SINGLE_TOPIC_STRONG", "COLLECTION_2SKU"]


class StyleSlotSpec(BaseModel):
    """CONVERGE 模式的一个款位。Mode B 一个（role=main）；Mode C 两个（top/bottom）。"""
    role: str                                  # "main" | "top" | "bottom"
    ref_image_path: str
    color_folder: str
    selected_colors: Optional[list[str]] = None


class LookSpec(BaseModel):
    """用户预选的一套 look。members: role → color_code。"""
    name: Optional[str] = None
    members: dict[str, str]


class PatternBlueprint(BaseModel):
    """2.4.5 母图案 Blueprint 输出的强类型（在 final JSON 里落盘；可被 API 返回）。"""
    blueprint_id: Optional[str] = None
    input_source: str = "trend_report"                     # trend_report | pattern_library
    所属主题: str
    母图案概念: str
    核心元素锚点: dict                                     # {"固定不变的元素": [...], "允许变体的元素": [...]}
    母配色关系: dict                                       # {"主色_role": ..., "辅色_role": ..., "点缀色_role": ...}
    核心参考图集: list[str] = Field(default_factory=list)   # 图库输入时 = 用户预筛的 1-3 张图文件名
    placement候选池: dict                                  # {"main": [...], "top": [...], "bottom": [...]}


class Fixture(BaseModel):
    id: str
    name: str
    # 老输入源单款场景仍必填（老 fixture 兼容）；图库输入或多款场景可为空
    trend_json_path: Optional[str] = None
    ref_image_path: Optional[str] = None
    color_folder: Optional[str] = None
    selected_colors: Optional[list[str]] = None
    gender_ratio: str
    num_designs_k: int
    description: Optional[str] = None
    created_at: str
    # v5 collection
    design_mode: DesignMode = "MULTI_TOPIC"
    styles: Optional[list[StyleSlotSpec]] = None
    looks: Optional[list[LookSpec]] = None
    color_strategy: Optional[str] = None
    # v6 图库输入
    input_source: str = "trend_report"                     # trend_report | pattern_library
    pattern_library_path: Optional[str] = None             # 相对 PATTERN_LIBRARY_ROOT 的文件夹名
    pattern_library_selected_files: Optional[list[str]] = None  # 用户预筛 1-3 张
    # v7 多主题 × 图库：跨文件夹分组选择
    pattern_library_selections: Optional[list["PatternLibrarySelection"]] = None


class PatternLibrarySelection(BaseModel):
    """v7 多主题 × 图库：一个方向（来源文件夹）内的选图。"""
    folder: str                # 图库根目录下的文件夹名（= 方向名 = 主题名）
    files: list[str]           # 该文件夹内选中的文件名列表（数量不设限）


class FixtureCreateRequest(BaseModel):
    id: str = Field(..., description="人类可读 slug，如 shanxi_yk250609")
    name: str
    trend_json_path: Optional[str] = None
    ref_image_path: Optional[str] = None
    color_folder: Optional[str] = None
    selected_colors: Optional[list[str]] = None
    gender_ratio: str = "男女比接近1:1"
    num_designs_k: int = 1
    description: Optional[str] = None
    # v5
    design_mode: DesignMode = "MULTI_TOPIC"
    styles: Optional[list[StyleSlotSpec]] = None
    looks: Optional[list[LookSpec]] = None
    color_strategy: Optional[str] = None
    # v6
    input_source: str = "trend_report"
    pattern_library_path: Optional[str] = None
    pattern_library_selected_files: Optional[list[str]] = None
    # v7
    pattern_library_selections: Optional[list[PatternLibrarySelection]] = None


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

    # v5 / v6：inline fixture 场景下补齐 design mode / input source
    design_mode: DesignMode = "MULTI_TOPIC"
    input_source: str = "trend_report"
    pattern_library_path: Optional[str] = None
    pattern_library_selected_files: Optional[list[str]] = None
    # v7 多主题 × 图库：跨文件夹分组选择
    pattern_library_selections: Optional[list[PatternLibrarySelection]] = None

    prompt_bundle: PromptBundleSpec = PromptBundleSpec()
    model: str = "gpt-5.5"
    max_tokens: int = 500000
    color_concurrency: int = 3
    max_audit_rounds: int = 2
    a_tier_quota: Optional[int] = None
    dry_run: bool = False
    # 历史避重：查同款+同趋势最近 5 轮 succeeded run，注入 2.4/2.5 prompt 让 LLM 避免雷同
    diversity_avoid_history: bool = True
    # 款级缓存：2.1 款式分析 / 2.2 颜色识别结果跨 run/趋势/模式复用（同款+同图+同 prompt+同模型）
    reuse_style_analysis: bool = True

    # v5 设计模式（默认 Mode A，老前端/老调用完全兼容）
    design_mode: DesignMode = "MULTI_TOPIC"
    # CONVERGE 模式：款位定义。Mode B 可不传（自动用 ref_image_path/color_folder 包装成 main）
    styles: Optional[list[StyleSlotSpec]] = None
    # CONVERGE 模式：look 预选清单。Mode B 可不传（每个色号自动包成单成员 look）；Mode C 必传
    looks: Optional[list[LookSpec]] = None


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
    design_mode: Optional[str] = "MULTI_TOPIC"
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
    default_model: str                            # 推理（文本 LLM）默认模型
    default_image_model: str                      # 生图默认模型（gpt-image-2 / gpt-image-1）
    default_max_tokens: int
    default_color_concurrency: int
    runs_dir: str
    prompts_dir: str
    # 账单：LLM 单价（$/1M tokens），用于成本折算展示（默认 30/30）
    llm_price_input_per_1m: float = 30.0
    llm_price_output_per_1m: float = 30.0
    # 公网访问：两组密码是否已配置（不回显明文）
    has_public_admin_password: bool = False
    has_public_guest_password: bool = False


class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None          # 写入不回显
    default_model: Optional[str] = None
    default_image_model: Optional[str] = None
    default_max_tokens: Optional[int] = None
    default_color_concurrency: Optional[int] = None
    llm_price_input_per_1m: Optional[float] = None
    llm_price_output_per_1m: Optional[float] = None
    # 公网访问密码（写入 .env；传空字符串 = 清除禁用）
    public_admin_password: Optional[str] = None
    public_guest_password: Optional[str] = None


# ============================================================================
# Fitting Room
# ============================================================================

class Look(BaseModel):
    id: str
    name: str
    top_kind: Optional[str] = None            # "image" | "text" | None
    top_image_id: Optional[str] = None
    top_text: Optional[str] = None
    bottom_kind: Optional[str] = None
    bottom_image_id: Optional[str] = None
    bottom_text: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    # 拍摄配置槽位（Try-on 集成）
    shooting_slot_kind: Optional[str] = None  # "upload" | "template" | None
    shooting_slot_url: Optional[str] = None
    shooting_slot_meta: Optional[dict] = None
    # 波段上新管理：至多属一个波段；None = 未分波段池
    wave_id: Optional[str] = None
    created_at: str
    updated_at: str


class LookCreateItem(BaseModel):
    name: Optional[str] = None                # 空则自动 "look-NN"
    top_kind: Optional[str] = None
    top_image_id: Optional[str] = None
    top_text: Optional[str] = None
    bottom_kind: Optional[str] = None
    bottom_image_id: Optional[str] = None
    bottom_text: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class LookBulkCreateRequest(BaseModel):
    """一次批量提交（从 Gallery 组套完成后 push 多条）"""
    looks: list[LookCreateItem]


class LookUpdate(BaseModel):
    """PATCH：所有字段可选，None 表示不动"""
    name: Optional[str] = None
    top_kind: Optional[str] = None
    top_image_id: Optional[str] = None
    top_text: Optional[str] = None
    bottom_kind: Optional[str] = None
    bottom_image_id: Optional[str] = None
    bottom_text: Optional[str] = None
    tags: Optional[list[str]] = None
    shooting_slot_kind: Optional[str] = None
    shooting_slot_url: Optional[str] = None
    shooting_slot_meta: Optional[dict] = None


class ImageCategoryItem(BaseModel):
    image_id: str
    category: str                             # "top" | "bottom"
    source: str = "manual"


class ImageCategoryBulkRequest(BaseModel):
    items: list[ImageCategoryItem]


class LooksExportRequest(BaseModel):
    """POST /looks/export-to-desktop：批量把选中的 look 图片导出到用户桌面。"""
    look_ids: list[str]


# ============================================================================
# 选款中心（已确认上架的 SKU 仓库）
# ============================================================================

class SelectionStyle(BaseModel):
    id: str
    image_id: str                         # generated: task:plan；uploaded: upload:{uuid}
    source_kind: str = "generated"        # generated | uploaded
    category: Optional[str] = None        # top | bottom
    style_no: Optional[str] = None
    color_code: Optional[str] = None
    color_name: Optional[str] = None
    note: Optional[str] = None
    origin: str = "手动选款"               # 手动选款 | 存量迁移 | 成套联动 | 上传
    upload_url: Optional[str] = None      # uploaded 时的静态 URL
    created_at: str


class SelectionAddItem(BaseModel):
    image_id: str
    category: Optional[str] = None        # top | bottom（Gallery 侧已有归类可带上）
    style_no: Optional[str] = None
    color_code: Optional[str] = None
    color_name: Optional[str] = None


class SelectionAddRequest(BaseModel):
    items: list[SelectionAddItem]


# ============================================================================
# 波段上新管理（Wave）
# ============================================================================

class Wave(BaseModel):
    id: str
    name: str
    planned_launch_date: Optional[str] = None   # YYYY-MM-DD
    status: str = "规划中"                       # 规划中 | 已上架（手动）
    created_at: str
    updated_at: str
    # 派生统计（查询时算）
    look_count: int = 0
    style_count: int = 0                        # 波段内款色图去重数


class WaveCreateRequest(BaseModel):
    name: str
    planned_launch_date: Optional[str] = None
    look_ids: list[str] = Field(default_factory=list)   # 创建时可顺带初始归组


class WaveUpdateRequest(BaseModel):
    """PATCH：None 表示不动；planned_launch_date 传 "" 表示清除日期。"""
    name: Optional[str] = None
    planned_launch_date: Optional[str] = None
    status: Optional[str] = None


class WaveAssignRequest(BaseModel):
    look_ids: list[str]
