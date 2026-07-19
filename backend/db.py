"""
SQLite 元数据存储

只存三类元数据：
  - runs        : 一次 step2 执行的索引（trace 实体仍以文件落地）
  - prompt_versions : 每个子 prompt 的历史版本登记（实际文件在 prompts/）
  - fixtures    : 冻结输入快照，用于 prompt 横向 A/B 对比

文件系统是真理源（trace.jsonl / 节点 IO JSON / 最终 step2 JSON 全在 runs/）；
DB 只用于"列表/筛选/外键关联"等元数据查询的快路径，**不存原始 trace 内容**。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "metadata.db"
_ENV_FILE = Path(__file__).parent.parent / ".env"


def get_openai_api_key() -> str | None:
    """
    每次从 .env 实时读取最新 OPENAI_API_KEY，绕过 os.environ 启动时缓存的问题。

    设计动机：旧实现 runs/tasks 都用 `os.environ.get("OPENAI_API_KEY")`，但
    uvicorn 启动时一次性加载 .env 进 os.environ，之后用户在「设置」tab 改 key
    虽然写到了 .env，进程内 os.environ 不会更新，导致真跑仍用老 key。

    优先级：.env 文件 > os.environ > None
    """
    if _ENV_FILE.is_file():
        try:
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.startswith("OPENAI_API_KEY="):
                    v = line.partition("=")[2].strip()
                    if v:
                        return v
        except Exception:
            pass
    return os.environ.get("OPENAI_API_KEY")


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,         -- run_id（含时间戳）
    fixture_id      TEXT,                     -- 引用 fixtures.id 或 NULL（一次性运行）
    trend_name      TEXT NOT NULL,
    style_no        TEXT NOT NULL,
    gender_ratio    TEXT NOT NULL,
    num_designs_k   INTEGER NOT NULL,
    prompt_bundle   TEXT NOT NULL,            -- JSON：{"2.1": "current", "2.2": "v3", ...}
    status          TEXT NOT NULL,            -- running|succeeded|failed
    audit_passed    INTEGER,                  -- 0/1/NULL（未完成）
    audit_rounds    INTEGER,
    total_tokens_in INTEGER,
    total_tokens_out INTEGER,
    elapsed_ms      INTEGER,
    run_dir         TEXT NOT NULL,            -- 磁盘上的 trace 目录
    final_json_path TEXT,                     -- 最终输出 JSON 路径
    parent_run_id   TEXT,                     -- fork 的来源 run_id
    fork_from_node  TEXT,                     -- fork 时的节点 ID
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (fixture_id) REFERENCES fixtures(id),
    FOREIGN KEY (parent_run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_runs_fixture     ON runs(fixture_id);
CREATE INDEX IF NOT EXISTS idx_runs_created     ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_parent      ON runs(parent_run_id);


CREATE TABLE IF NOT EXISTS prompt_versions (
    node_id         TEXT NOT NULL,            -- 2.1 / 2.2 / ...
    version         TEXT NOT NULL,            -- "current" / "v1" / "v2"
    file_path       TEXT NOT NULL,            -- prompts/step2/01_style_analysis.v3.md
    description     TEXT,                     -- 用户写的 changelog
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (node_id, version)
);


CREATE TABLE IF NOT EXISTS fixtures (
    id              TEXT PRIMARY KEY,         -- 用户给的 slug
    name            TEXT NOT NULL,
    trend_json_path TEXT NOT NULL,
    ref_image_path  TEXT NOT NULL,
    color_folder    TEXT NOT NULL,
    selected_colors TEXT,                     -- JSON 数组或 NULL（全选）
    gender_ratio    TEXT NOT NULL,
    num_designs_k   INTEGER NOT NULL,
    description     TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- M6: 生图任务（GenerationTask）
CREATE TABLE IF NOT EXISTS generation_tasks (
    id                   TEXT PRIMARY KEY,         -- 任务 ID（时间戳格式）
    source_step2_run_id  TEXT NOT NULL,            -- 来源 step2 run
    trend_name           TEXT NOT NULL,
    style_no             TEXT NOT NULL,

    -- 用户选了哪几个方案进生图（JSON array of plan_id）
    selected_plan_ids    TEXT NOT NULL,

    -- 决策⑥ 全量快照（防源数据后续变动）
    snapshot_step1       TEXT,                     -- step1 JSON 完整快照
    snapshot_step2_meta  TEXT,                     -- step2 final JSON 的款式分析 + 性别比规划部分
    snapshot_plans       TEXT NOT NULL,            -- 选中方案的完整内容（含 图生图 prompt）

    -- 状态
    status               TEXT NOT NULL,            -- pending|running|completed|partial|failed
    progress_done        INTEGER DEFAULT 0,        -- 已完成图数
    progress_total       INTEGER NOT NULL,         -- 总图数 = len(selected_plan_ids)

    -- 产出（JSON array of {plan_id, status, image_path, image_url, image_prompt_used, elapsed_ms, error}）
    results              TEXT,

    -- 元信息
    image_root_dir       TEXT NOT NULL,            -- 实际图片所在磁盘目录
    total_elapsed_ms     INTEGER,
    total_cost_usd       REAL,                     -- 估算（gpt-image-2 约 $0.04-0.17/张）

    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at         TEXT,

    FOREIGN KEY (source_step2_run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON generation_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_source  ON generation_tasks(source_step2_run_id);

-- Fitting Room：跨任务的 look 组套管理
CREATE TABLE IF NOT EXISTS looks (
    id              TEXT PRIMARY KEY,       -- look-{timestamp}-{hex6}
    name            TEXT NOT NULL,          -- 用户命名或自动 "look-NN"
    top_kind        TEXT,                   -- "image" | "text" | NULL（空侧）
    top_image_id    TEXT,                   -- gallery item ID：{task_id}:{plan_id}
    top_text        TEXT,                   -- 缺失侧补文本描述
    bottom_kind     TEXT,
    bottom_image_id TEXT,
    bottom_text     TEXT,
    tags            TEXT,                   -- JSON array
    -- 拍摄配置槽位（Fitting Room Try-on 集成，v1.1 新增；旧记录保持 NULL 不受影响）
    shooting_slot_kind TEXT,                -- "upload" | "template" | NULL
    shooting_slot_url  TEXT,                -- 静态 URL（上传或模板库路径）
    shooting_slot_meta TEXT,                -- JSON 快照：类目 / 图组 / 标签 / 备注
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 兼容旧 DB：给已存在的 looks 表补三个字段（SQLite 不支持 IF NOT EXISTS on ADD COLUMN，
-- 用 PRAGMA 检查——放到 init_db 的 Python 代码里做，比 SQL 稳）

CREATE INDEX IF NOT EXISTS idx_looks_updated ON looks(updated_at DESC);

-- 用户对每张 gallery 图的上/下装归类持久化（用户手动切换后不丢）
CREATE TABLE IF NOT EXISTS image_categories (
    image_id     TEXT PRIMARY KEY,          -- {task_id}:{plan_id}
    category     TEXT NOT NULL,             -- "top" | "bottom"
    source       TEXT,                      -- "auto"（款号启发式）| "manual"
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 选款中心：已确认上架的 SKU 仓库（Fitting Room 组套只能取仓内的款）
CREATE TABLE IF NOT EXISTS selected_styles (
    id           TEXT PRIMARY KEY,      -- sel-{timestamp}-{hex6}
    image_id     TEXT NOT NULL UNIQUE,  -- generated: {task_id}:{plan_id}；uploaded: upload:{uuid}
    source_kind  TEXT NOT NULL DEFAULT 'generated',  -- generated | uploaded
    category     TEXT,                  -- top | bottom（组套槽位归类）
    style_no     TEXT,
    color_code   TEXT,
    color_name   TEXT,
    note         TEXT,
    origin       TEXT NOT NULL DEFAULT '手动选款',  -- 手动选款 | 存量迁移 | 成套联动 | 上传
    upload_path  TEXT,                  -- uploaded：data/selection_uploads/ 下的文件名
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 款图库元数据：文件夹（ai-supply/款图）是图片事实来源，这里只存品类/时间
-- （见 docs/款图库管理_产品方案PRD.md v1.3）
CREATE TABLE IF NOT EXISTS style_meta (
    style_no       TEXT PRIMARY KEY,
    category_main  TEXT NOT NULL,      -- 童装/男装/女装/运动/男睡衣/女睡衣
    category_sub   TEXT NOT NULL,      -- top | bottom | onepiece
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source         TEXT NOT NULL DEFAULT 'upload',   -- upload | backfill
    file_mtime     TEXT                -- backfill 时的文件修改时间，仅参考
);

-- 波段上新管理：波段（Wave）= 上新排期容器；look 至多属于一个波段
CREATE TABLE IF NOT EXISTS waves (
    id                   TEXT PRIMARY KEY,  -- wv-{timestamp}-{hex6}
    name                 TEXT NOT NULL,     -- 波段名（自由文本，允许重名）
    planned_launch_date  TEXT,              -- 预备上架时间（YYYY-MM-DD，可空）
    status               TEXT NOT NULL DEFAULT '规划中',  -- 规划中 | 已上架（手动切换）
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn() -> sqlite3.Connection:
    """每次 request 获取一个新连接（FastAPI 单 worker 场景足够）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """启动时建表 + 增量迁移（给旧 DB 补新字段）"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        # Migration: 若旧 DB 的 looks 表没有 shooting_slot_* 列，补上
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(looks)").fetchall()}
        for col_name, col_def in [
            ("shooting_slot_kind", "TEXT"),
            ("shooting_slot_url",  "TEXT"),
            ("shooting_slot_meta", "TEXT"),
            # 波段上新管理：look 至多属一个波段；NULL = 未分波段池
            ("wave_id",            "TEXT"),
        ]:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE looks ADD COLUMN {col_name} {col_def}")

        # Migration v5（激进合并方案）：fixtures 表补 design_mode/styles/looks/color_strategy。
        # 老记录 design_mode 默认 MULTI_TOPIC（Mode A），继续可读可跑。
        # Migration v6：加 input_source 与图库输入相关字段（Phase 1 图库输入落地）。
        fcols = {r["name"] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
        for col_name, col_def in [
            ("design_mode",                    "TEXT DEFAULT 'MULTI_TOPIC'"),
            ("styles",                         "TEXT"),   # JSON: [{role, ref_image_path, color_folder, selected_colors}]
            ("looks",                          "TEXT"),   # JSON: [{name, members:{role:color_code}}]
            ("color_strategy",                 "TEXT"),   # shared_pool | per_style_pool | NULL
            # v6 图库输入
            ("input_source",                   "TEXT DEFAULT 'trend_report'"),  # trend_report | pattern_library
            ("pattern_library_path",           "TEXT"),   # 相对 PATTERN_LIBRARY_ROOT 的文件夹名
            ("pattern_library_selected_files", "TEXT"),   # JSON 数组：用户预筛的 1-3 张图文件名
            ("pattern_library_selections",     "TEXT"),   # v7 JSON：[{folder, files:[...]}]（跨文件夹分组）
        ]:
            if col_name not in fcols:
                conn.execute(f"ALTER TABLE fixtures ADD COLUMN {col_name} {col_def}")

        # Migration v5：runs 表补 design_mode
        # v6：runs 也补 input_source（分析用；blueprint 详情落在 final JSON 里）
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "design_mode" not in rcols:
            conn.execute("ALTER TABLE runs ADD COLUMN design_mode TEXT DEFAULT 'MULTI_TOPIC'")
        if "input_source" not in rcols:
            conn.execute("ALTER TABLE runs ADD COLUMN input_source TEXT DEFAULT 'trend_report'")

        # 选款中心存量回填（幂等）：现有 look 引用的图自动视为已选中，
        # 否则「Fitting Room 只能用仓内款」的硬约束会把历史数据全部卡死。
        import time as _t
        import uuid as _uuid
        rows = conn.execute(
            """SELECT top_kind, top_image_id, bottom_kind, bottom_image_id FROM looks"""
        ).fetchall()
        for r in rows:
            for kind_col, img_col, cat in (
                ("top_kind", "top_image_id", "top"),
                ("bottom_kind", "bottom_image_id", "bottom"),
            ):
                if r[kind_col] == "image" and r[img_col]:
                    conn.execute(
                        """INSERT OR IGNORE INTO selected_styles
                           (id, image_id, source_kind, category, origin)
                           VALUES (?, ?, 'generated', ?, '存量迁移')""",
                        (f"sel-{int(_t.time())}-{_uuid.uuid4().hex[:6]}", r[img_col], cat),
                    )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Runs CRUD —— 把 SQL 封一层，router 用着干净
# ============================================================================

def insert_run(
    *,
    run_id: str,
    trend_name: str,
    style_no: str,
    gender_ratio: str,
    num_designs_k: int,
    prompt_bundle: dict,
    run_dir: str,
    fixture_id: str | None = None,
    parent_run_id: str | None = None,
    fork_from_node: str | None = None,
    design_mode: str = "MULTI_TOPIC",
) -> None:
    """新建 run，status='running'。"""
    import json as _json
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO runs (id, fixture_id, trend_name, style_no, gender_ratio,
                                  num_designs_k, prompt_bundle, status,
                                  run_dir, parent_run_id, fork_from_node, design_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
            (
                run_id, fixture_id, trend_name, style_no, gender_ratio,
                num_designs_k, _json.dumps(prompt_bundle, ensure_ascii=False),
                run_dir, parent_run_id, fork_from_node, design_mode,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_run_finished(
    *,
    run_id: str,
    status: str,
    audit_passed: bool | None = None,
    audit_rounds: int | None = None,
    total_tokens_in: int | None = None,
    total_tokens_out: int | None = None,
    elapsed_ms: int | None = None,
    final_json_path: str | None = None,
) -> None:
    """run 结束时更新结果字段。status: succeeded / failed / succeeded_with_audit_warnings"""
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE runs
               SET status = ?,
                   audit_passed = ?,
                   audit_rounds = ?,
                   total_tokens_in = ?,
                   total_tokens_out = ?,
                   elapsed_ms = ?,
                   final_json_path = ?
               WHERE id = ?""",
            (
                status,
                int(audit_passed) if audit_passed is not None else None,
                audit_rounds, total_tokens_in, total_tokens_out, elapsed_ms,
                final_json_path, run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_runs(*, limit: int = 100, fixture_id: str | None = None) -> list[dict]:
    conn = get_conn()
    try:
        if fixture_id:
            rows = conn.execute(
                "SELECT * FROM runs WHERE fixture_id = ? ORDER BY created_at DESC LIMIT ?",
                (fixture_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_recent_successful_runs(
    *, trend_name: str, style_no: str, limit: int = 5,
) -> list[dict]:
    """
    历史避重专用：按创建时间倒序取同款+同趋势最近 N 轮 succeeded run。
    仅返回 status ∈ (succeeded, succeeded_with_audit_warnings) 且 final_json_path 非空的记录。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM runs
               WHERE trend_name = ?
                 AND style_no = ?
                 AND status IN ('succeeded', 'succeeded_with_audit_warnings')
                 AND final_json_path IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (trend_name, style_no, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================================
# M6: Generation Tasks CRUD
# ============================================================================

def insert_generation_task(
    *,
    task_id: str,
    source_step2_run_id: str,
    trend_name: str,
    style_no: str,
    selected_plan_ids: list[str],
    snapshot_step1: dict | None,
    snapshot_step2_meta: dict | None,
    snapshot_plans: list[dict],
    image_root_dir: str,
) -> None:
    """新建任务，status='pending'。"""
    import json as _json
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO generation_tasks
               (id, source_step2_run_id, trend_name, style_no,
                selected_plan_ids, snapshot_step1, snapshot_step2_meta,
                snapshot_plans, status, progress_done, progress_total,
                image_root_dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                task_id, source_step2_run_id, trend_name, style_no,
                _json.dumps(selected_plan_ids, ensure_ascii=False),
                _json.dumps(snapshot_step1, ensure_ascii=False) if snapshot_step1 else None,
                _json.dumps(snapshot_step2_meta, ensure_ascii=False) if snapshot_step2_meta else None,
                _json.dumps(snapshot_plans, ensure_ascii=False),
                len(selected_plan_ids),
                image_root_dir,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_generation_task(task_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT gt.*, r.prompt_bundle AS source_prompt_bundle
               FROM generation_tasks gt
               LEFT JOIN runs r ON gt.source_step2_run_id = r.id
               WHERE gt.id = ?""",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_generation_tasks(*, limit: int = 100) -> list[dict]:
    """JOIN 一下源 step2 run 的 prompt_bundle，方便前端显示用了哪些 prompt 版本。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT gt.*, r.prompt_bundle AS source_prompt_bundle
               FROM generation_tasks gt
               LEFT JOIN runs r ON gt.source_step2_run_id = r.id
               ORDER BY gt.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_generation_task_progress(
    *,
    task_id: str,
    status: str | None = None,
    progress_done: int | None = None,
    results: list[dict] | None = None,
    total_elapsed_ms: int | None = None,
    total_cost_usd: float | None = None,
    completed_at: str | None = None,
) -> None:
    """部分字段更新——传 None 的字段不动。"""
    import json as _json
    updates = []
    values: list = []
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if progress_done is not None:
        updates.append("progress_done = ?")
        values.append(progress_done)
    if results is not None:
        updates.append("results = ?")
        values.append(_json.dumps(results, ensure_ascii=False))
    if total_elapsed_ms is not None:
        updates.append("total_elapsed_ms = ?")
        values.append(total_elapsed_ms)
    if total_cost_usd is not None:
        updates.append("total_cost_usd = ?")
        values.append(total_cost_usd)
    if completed_at is not None:
        updates.append("completed_at = ?")
        values.append(completed_at)
    if not updates:
        return
    values.append(task_id)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE generation_tasks SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
    finally:
        conn.close()


def delete_generation_task(task_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM generation_tasks WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Fitting Room：looks & image_categories CRUD
# ============================================================================

def list_looks(*, limit: int = 500) -> list[dict]:
    """按创建时间倒序（最近入 Fitting Room 的排最前）。

    注意不要按 updated_at 排：编辑文本/tags 都会 bump updated_at，
    导致卡片在页面上跳来跳去——展示顺序应该稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM looks ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_look(look_id: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM looks WHERE id = ?", (look_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def insert_look(
    *,
    id: str,
    name: str,
    top_kind: str | None = None,
    top_image_id: str | None = None,
    top_text: str | None = None,
    bottom_kind: str | None = None,
    bottom_image_id: str | None = None,
    bottom_text: str | None = None,
    tags: list[str] | None = None,
    shooting_slot_kind: str | None = None,
    shooting_slot_url: str | None = None,
    shooting_slot_meta: dict | str | None = None,
) -> None:
    import json as _json
    # meta 允许 dict 传入，会自动 JSON 化
    if isinstance(shooting_slot_meta, dict):
        shooting_slot_meta = _json.dumps(shooting_slot_meta, ensure_ascii=False)
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO looks
               (id, name, top_kind, top_image_id, top_text,
                bottom_kind, bottom_image_id, bottom_text, tags,
                shooting_slot_kind, shooting_slot_url, shooting_slot_meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id, name, top_kind, top_image_id, top_text,
                bottom_kind, bottom_image_id, bottom_text,
                _json.dumps(tags or [], ensure_ascii=False),
                shooting_slot_kind, shooting_slot_url, shooting_slot_meta,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_look(look_id: str, **fields) -> None:
    """部分字段更新——None 表示不动。tags/shooting_slot_meta 传 list/dict 会自动 JSON 化。"""
    import json as _json
    allowed = {"name", "top_kind", "top_image_id", "top_text",
               "bottom_kind", "bottom_image_id", "bottom_text", "tags",
               "shooting_slot_kind", "shooting_slot_url", "shooting_slot_meta"}
    updates: list[str] = []
    values: list = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "tags" and not isinstance(v, str):
            v = _json.dumps(v, ensure_ascii=False)
        if k == "shooting_slot_meta" and isinstance(v, dict):
            v = _json.dumps(v, ensure_ascii=False)
        updates.append(f"{k} = ?")
        values.append(v)
    if not updates:
        return
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(look_id)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE looks SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
    finally:
        conn.close()


def delete_look(look_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM looks WHERE id = ?", (look_id,))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# 选款中心：selected_styles CRUD
# ============================================================================

def list_selected_styles() -> list[dict]:
    """按入仓时间倒序。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM selected_styles ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_selected_style(sel_id: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM selected_styles WHERE id = ?", (sel_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def get_selected_by_image_id(image_id: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM selected_styles WHERE image_id = ?", (image_id,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def selected_image_ids() -> set[str]:
    """全部已入仓的 image_id（前端角标 / look 校验用）。"""
    conn = get_conn()
    try:
        return {r["image_id"] for r in conn.execute("SELECT image_id FROM selected_styles")}
    finally:
        conn.close()


def insert_selected_style(
    *,
    id: str,
    image_id: str,
    source_kind: str = "generated",
    category: str | None = None,
    style_no: str | None = None,
    color_code: str | None = None,
    color_name: str | None = None,
    note: str | None = None,
    origin: str = "手动选款",
    upload_path: str | None = None,
) -> bool:
    """入仓。image_id 已存在则跳过（幂等），返回是否真的插入。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO selected_styles
               (id, image_id, source_kind, category, style_no, color_code, color_name,
                note, origin, upload_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, image_id, source_kind, category, style_no, color_code, color_name,
             note, origin, upload_path),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_selected_style(sel_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM selected_styles WHERE id = ?", (sel_id,))
        conn.commit()
    finally:
        conn.close()


def looks_referencing_image(image_id: str) -> list[dict]:
    """引用了该图的 look（移除阻断用）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, name FROM looks
               WHERE (top_kind = 'image' AND top_image_id = ?)
                  OR (bottom_kind = 'image' AND bottom_image_id = ?)""",
            (image_id, image_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================================
# 波段上新管理：waves CRUD（look 至多属一个波段；wave_id NULL = 未分波段池）
# ============================================================================

def list_waves() -> list[dict]:
    """全部波段 + 派生统计。

    - look_count：波段内 look 数
    - style_count：波段内款式数 = 成员款色图（image_id）去重后计数
      （同一张款色图在多个 look 里复用只计 1 款；上下装齐 = 2 个计数来源）
    排序：预备上架时间升序，无日期垫底；同日期按创建时间。
    """
    conn = get_conn()
    try:
        waves = [dict(r) for r in conn.execute(
            """SELECT * FROM waves
               ORDER BY (planned_launch_date IS NULL) ASC,
                        planned_launch_date ASC, created_at ASC"""
        ).fetchall()]
        rows = conn.execute(
            """SELECT wave_id, top_kind, top_image_id, bottom_kind, bottom_image_id
               FROM looks WHERE wave_id IS NOT NULL"""
        ).fetchall()
        look_counts: dict[str, int] = {}
        style_sets: dict[str, set] = {}
        for r in rows:
            wid = r["wave_id"]
            look_counts[wid] = look_counts.get(wid, 0) + 1
            s = style_sets.setdefault(wid, set())
            if r["top_kind"] == "image" and r["top_image_id"]:
                s.add(r["top_image_id"])
            if r["bottom_kind"] == "image" and r["bottom_image_id"]:
                s.add(r["bottom_image_id"])
        for w in waves:
            w["look_count"] = look_counts.get(w["id"], 0)
            w["style_count"] = len(style_sets.get(w["id"], set()))
        return waves
    finally:
        conn.close()


def get_wave(wave_id: str) -> dict | None:
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM waves WHERE id = ?", (wave_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def insert_wave(*, id: str, name: str, planned_launch_date: str | None = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO waves (id, name, planned_launch_date) VALUES (?, ?, ?)",
            (id, name, planned_launch_date),
        )
        conn.commit()
    finally:
        conn.close()


def update_wave(wave_id: str, **fields) -> None:
    """name / planned_launch_date / status 部分更新。
    planned_launch_date 传空字符串 "" 表示清除日期（置 NULL）。"""
    allowed = {"name", "planned_launch_date", "status"}
    updates: list[str] = []
    values: list = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "planned_launch_date" and v == "":
            v = None
        updates.append(f"{k} = ?")
        values.append(v)
    if not updates:
        return
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(wave_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE waves SET {', '.join(updates)} WHERE id = ?", tuple(values))
        conn.commit()
    finally:
        conn.close()


def delete_wave(wave_id: str) -> int:
    """删除波段：成员 look 回未分波段池（wave_id 置 NULL），绝不删 look。
    返回被释放的 look 数。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE looks SET wave_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE wave_id = ?",
            (wave_id,),
        )
        conn.execute("DELETE FROM waves WHERE id = ?", (wave_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def assign_looks_to_wave(wave_id: str, look_ids: list[str]) -> int:
    """批量归组：look 已在其他波段则自动移过来（单归属模型）。
    同时 bump 目标波段与被移出波段的 updated_at。"""
    if not look_ids:
        return 0
    conn = get_conn()
    try:
        ph = ",".join("?" for _ in look_ids)
        # 记录受影响的原波段（含目标自己，无害）
        old_wave_ids = {
            r["wave_id"]
            for r in conn.execute(
                f"SELECT DISTINCT wave_id FROM looks WHERE id IN ({ph}) AND wave_id IS NOT NULL",
                tuple(look_ids),
            ).fetchall()
        }
        cur = conn.execute(
            f"UPDATE looks SET wave_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({ph})",
            (wave_id, *look_ids),
        )
        touched = old_wave_ids | {wave_id}
        ph2 = ",".join("?" for _ in touched)
        conn.execute(
            f"UPDATE waves SET updated_at = CURRENT_TIMESTAMP WHERE id IN ({ph2})",
            tuple(touched),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def unassign_looks(look_ids: list[str]) -> int:
    """批量移出波段，回未分波段池。bump 被移出波段的 updated_at。"""
    if not look_ids:
        return 0
    conn = get_conn()
    try:
        ph = ",".join("?" for _ in look_ids)
        old_wave_ids = {
            r["wave_id"]
            for r in conn.execute(
                f"SELECT DISTINCT wave_id FROM looks WHERE id IN ({ph}) AND wave_id IS NOT NULL",
                tuple(look_ids),
            ).fetchall()
        }
        cur = conn.execute(
            f"UPDATE looks SET wave_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id IN ({ph})",
            tuple(look_ids),
        )
        if old_wave_ids:
            ph2 = ",".join("?" for _ in old_wave_ids)
            conn.execute(
                f"UPDATE waves SET updated_at = CURRENT_TIMESTAMP WHERE id IN ({ph2})",
                tuple(old_wave_ids),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_image_categories() -> dict[str, str]:
    """返回 image_id → category 映射，前端一次性拿全批用。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT image_id, category FROM image_categories").fetchall()
        return {r["image_id"]: r["category"] for r in rows}
    finally:
        conn.close()


def upsert_image_category(image_id: str, category: str, source: str = "manual") -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO image_categories (image_id, category, source, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(image_id) DO UPDATE SET
                 category = excluded.category,
                 source = excluded.source,
                 updated_at = CURRENT_TIMESTAMP""",
            (image_id, category, source),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_image_categories_bulk(items: list[dict]) -> int:
    """批量 upsert，items = [{image_id, category, source}, ...]。返回处理条数。"""
    conn = get_conn()
    try:
        n = 0
        for it in items:
            iid = it.get("image_id")
            cat = it.get("category")
            src = it.get("source", "manual")
            if not iid or cat not in ("top", "bottom"):
                continue
            conn.execute(
                """INSERT INTO image_categories (image_id, category, source, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(image_id) DO UPDATE SET
                     category = excluded.category,
                     source = excluded.source,
                     updated_at = CURRENT_TIMESTAMP""",
                (iid, cat, src),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


# ============================================================================
# 款图库元数据（style_meta）—— 文件夹是图片事实来源，这里只存品类/时间
# ============================================================================

def list_style_meta() -> dict[str, dict]:
    """返回 style_no → meta 行的映射，GET /styles 列表 JOIN 用。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM style_meta").fetchall()
        return {r["style_no"]: dict(r) for r in rows}
    finally:
        conn.close()


def get_style_meta(style_no: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM style_meta WHERE style_no = ?", (style_no,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_style_meta(
    *,
    style_no: str,
    category_main: str,
    category_sub: str,
    source: str = "upload",
    file_mtime: str | None = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO style_meta (style_no, category_main, category_sub, source, file_mtime)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(style_no) DO UPDATE SET
                 category_main = excluded.category_main,
                 category_sub = excluded.category_sub""",
            (style_no, category_main, category_sub, source, file_mtime),
        )
        conn.commit()
    finally:
        conn.close()


def delete_style_meta(style_no: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM style_meta WHERE style_no = ?", (style_no,))
        conn.commit()
    finally:
        conn.close()
