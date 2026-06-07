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

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "metadata.db"


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
"""


def get_conn() -> sqlite3.Connection:
    """每次 request 获取一个新连接（FastAPI 单 worker 场景足够）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """启动时建表。"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
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
) -> None:
    """新建 run，status='running'。"""
    import json as _json
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO runs (id, fixture_id, trend_name, style_no, gender_ratio,
                                  num_designs_k, prompt_bundle, status,
                                  run_dir, parent_run_id, fork_from_node)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
            (
                run_id, fixture_id, trend_name, style_no, gender_ratio,
                num_designs_k, _json.dumps(prompt_bundle, ensure_ascii=False),
                run_dir, parent_run_id, fork_from_node,
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
