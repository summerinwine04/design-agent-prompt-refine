"""
/api/v1/trends — 趋势报告浏览 + 导入流水线

只读浏览：
  GET   /trends                              扫 ai-supply/趋势报告/ 目录列表
  GET   /trends/{trend_name}                 单份趋势详情

导入流水线（新增 PDF → 自动跑 step0 + step1）：
  POST  /trends/upload                       multipart 上传 PDF + name → 启动后台任务
  GET   /trends/jobs/{job_id}                查询单个 job 状态
  GET   /trends/jobs/{job_id}/stream         SSE 实时进度

prompt-refine-agent 不重复存数据；指向 ai-supply/趋势报告/。
通过环境变量 TRENDS_ROOT 配置数据根；默认 ../ai-supply/趋势报告/。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from backend.sse import event_bus, sse_stream

router = APIRouter()

ROOT = Path(__file__).parent.parent.parent.resolve()


def _trends_root() -> Path:
    """优先环境变量；默认指向 ../ai-supply/趋势报告/。"""
    env = os.environ.get("TRENDS_ROOT")
    if env:
        return Path(env).resolve()
    candidate = ROOT.parent / "ai-supply" / "趋势报告"
    return candidate


# ============================================================================
# 只读浏览
# ============================================================================

@router.get("")
async def list_trends():
    root = _trends_root()
    if not root.is_dir():
        return {"root": str(root), "trends": [], "error": "TRENDS_ROOT not found"}
    items = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        json_file = d / f"{d.name}.json"
        items.append({
            "name": d.name,
            "path": str(d),
            "page_count": len(list(d.glob("page_*.png"))),
            "parsed": json_file.is_file(),
            "json_path": str(json_file) if json_file.is_file() else None,
        })
    return {"root": str(root), "trends": items}


@router.get("/{trend_name}")
async def get_trend(trend_name: str):
    root = _trends_root()
    trend_dir = root / trend_name
    if not trend_dir.is_dir():
        raise HTTPException(404, f"trend {trend_name} not found in {root}")
    json_file = trend_dir / f"{trend_name}.json"
    parsed = None
    if json_file.is_file():
        parsed = json.loads(json_file.read_text(encoding="utf-8"))
    pages = sorted(trend_dir.glob("page_*.png"))
    return {
        "name": trend_name,
        "trend_dir": str(trend_dir),
        "pages": [p.name for p in pages],
        "json_path": str(json_file) if parsed else None,
        "parsed": parsed,
    }


# ============================================================================
# 导入流水线
# ============================================================================

# 内存 job dict：job_id → {name, status, stage, current, total, error, started_at, ...}
# 单进程 in-memory，重启 backend 会清空——这是设计选择（不持久化任务状态）
_jobs: dict[str, dict] = {}


def _safe_name(raw: str) -> str:
    """剔除路径分隔符 / 控制字符；保留中文 + 字母 + 数字 + 下划线连字符。"""
    # 去掉首尾空格、禁止 .. / 前导斜杠等
    cleaned = re.sub(r"[\\/:\x00-\x1f<>|?*\"']+", "_", raw.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:120]  # 最长 120 字符


@router.post("/upload")
async def upload_trend(
    name: str = Form(..., description="趋势报告名（中英文均可，将作为目录名 + json 文件名）"),
    pdf: UploadFile = File(..., description="PDF 文件"),
):
    """
    上传 PDF + 名字 → 后台跑 step0 (PDF→图) → step1 (图+LLM→JSON)。
    立即返回 job_id；前端订阅 /jobs/{job_id}/stream 看进度。
    """
    safe = _safe_name(name)
    if not safe:
        raise HTTPException(400, "name 非法（剔除特殊字符后为空）")

    root = _trends_root()
    if not root.is_dir():
        raise HTTPException(500, f"TRENDS_ROOT 不存在：{root}")

    # 校验 PDF 后缀（防止上传别的）
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 .pdf 文件")

    target_dir = root / safe
    if target_dir.exists():
        # 已存在同名子目录 → 多半是已经解析过 → 拒绝覆盖避免误删
        raise HTTPException(
            409,
            f"已存在同名子目录 '{safe}/'（多半是已解析过的趋势）。请改名，或先手动删除该子目录。",
        )

    # 落 PDF 到 ai-supply/趋势报告/{name}.pdf
    pdf_path = root / f"{safe}.pdf"

    pdf_bytes = await pdf.read()
    if len(pdf_bytes) == 0:
        raise HTTPException(400, "PDF 文件为空")
    if len(pdf_bytes) > 200 * 1024 * 1024:   # 200MB 上限
        raise HTTPException(400, "PDF 超过 200MB 上限")

    # 如果同名 PDF 已经存在（用户之前手动放的或上次失败留下的）→ 覆盖
    # 这样最常见的场景能 work：用户在 ai-supply/ 里有 PDF + 用前端触发自动解析
    try:
        pdf_path.write_bytes(pdf_bytes)
    except PermissionError as e:
        # Windows 常见：PDF 被 reader / 资源管理器预览占用
        raise HTTPException(
            409,
            f"无法写入 PDF（文件被其他程序占用）：{pdf_path.name}。"
            f"请关闭所有可能在打开这个 PDF 的程序（Adobe Reader / Edge / 资源管理器预览窗格），"
            f"或者把名称改一下避开同名文件。原始错误：{e}",
        )
    except OSError as e:
        raise HTTPException(500, f"写入 PDF 失败：{type(e).__name__}: {e}")

    # 注册 job
    job_id = f"trend-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    job = {
        "id": job_id,
        "name": safe,
        "status": "running",
        "stage": "starting",
        "current": 0,
        "total": 0,
        "error": None,
        "started_at": time.time(),
        "pdf_path": str(pdf_path),
        "target_dir": str(target_dir),
    }
    _jobs[job_id] = job

    # 后台线程跑（subprocess 调 step0+step1，解析 stdout 进度）
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_import_job, job_id)

    return {
        "ok": True,
        "job_id": job_id,
        "name": safe,
        "pdf_path": str(pdf_path),
        "target_dir": str(target_dir),
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"job {job_id} not found")
    return job


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, f"job {job_id} not found")
    return StreamingResponse(
        sse_stream(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ============================================================================
# 内部：跑流水线
# ============================================================================

_PROGRESS_RE = re.compile(r"\[PROGRESS_LINE\]\s*(.*)")


def _parse_progress_line(line: str) -> dict | None:
    """解析 step0/step1 输出的 [PROGRESS_LINE] stage=xxx key=val ... 行。"""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    rest = m.group(1).strip()
    out = {}
    for tok in rest.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out[k.strip()] = v.strip()
    return out or None


def _emit(job_id: str, event: str, **extra) -> None:
    """更新 job 内存状态 + 发 SSE 事件。"""
    job = _jobs.get(job_id)
    if not job:
        return
    payload = {"event": event, "job_id": job_id, **extra}
    # 更新 in-memory job 状态
    if "stage" in extra:
        job["stage"] = extra["stage"]
    if "current" in extra:
        job["current"] = extra["current"]
    if "total" in extra:
        job["total"] = extra["total"]
    if event == "trend_failed":
        job["status"] = "failed"
        job["error"] = extra.get("error", "unknown")
    elif event == "trend_succeeded":
        job["status"] = "succeeded"
    event_bus.publish(job_id, payload)


def _run_import_job(job_id: str) -> None:
    """在线程池里跑：先 subprocess 调 step0，成功后调 step1。stdout 行解析进度。"""
    job = _jobs.get(job_id)
    if not job:
        return
    pdf_path = Path(job["pdf_path"])
    target_dir = Path(job["target_dir"])
    name = job["name"]

    _emit(job_id, "trend_started", stage="pdf2img", name=name, total=0, current=0)

    python_exe = sys.executable  # 同一个 uvicorn 跑的 Python，venv 一致
    step0_script = str(ROOT / "tool" / "step0_pdf2images.py")
    step1_script = str(ROOT / "tool" / "step1_trend.py")

    # ----- step 0: PDF → page_*.png ----- #
    try:
        proc = subprocess.Popen(
            [python_exe, step0_script, "--pdf", str(pdf_path), "--out", str(target_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
        )
        for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            prog = _parse_progress_line(line)
            if prog and prog.get("stage") == "pdf2img":
                _emit(
                    job_id, "trend_progress",
                    stage="pdf2img",
                    current=int(prog.get("current", 0)),
                    total=int(prog.get("total", 0)),
                )
        proc.stdout.close()  # type: ignore[union-attr]
        rc = proc.wait()
        err_text = proc.stderr.read() if proc.stderr else ""
        if rc != 0:
            err = f"step0 PDF→图失败 (rc={rc}): {err_text[:500]}"
            _emit(job_id, "trend_failed", error=err)
            return
    except Exception as exc:
        _emit(job_id, "trend_failed", error=f"step0 启动异常: {type(exc).__name__}: {exc}")
        return

    # ----- step 1: 图 + LLM → {name}.json ----- #
    # 用 .env 里的 DEFAULT_MODEL（前端「设置」页可改）
    model = os.environ.get("DEFAULT_MODEL", "gpt-5.5")
    max_tokens = os.environ.get("DEFAULT_MAX_TOKENS", "500000")

    try:
        proc = subprocess.Popen(
            [
                python_exe, step1_script,
                "--trend-folder", str(target_dir),
                "--model", model,
                "--max-tokens", str(max_tokens),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
        )
        for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            prog = _parse_progress_line(line)
            if not prog:
                continue
            stage = prog.get("stage", "")
            if stage == "upload":
                _emit(
                    job_id, "trend_progress",
                    stage="upload",
                    current=int(prog.get("current", 0)),
                    total=int(prog.get("total", 0)),
                )
            elif stage == "upload_done":
                _emit(
                    job_id, "trend_progress",
                    stage="upload_done",
                    total=int(prog.get("total", 0)),
                )
            elif stage == "llm_start":
                _emit(job_id, "trend_progress", stage="llm_start")
            elif stage == "llm_streaming":
                _emit(
                    job_id, "trend_progress",
                    stage="llm_streaming",
                    chars=int(prog.get("chars", 0)),
                )
            elif stage == "done":
                pass  # 真正的 succeeded 由进程 rc 决定
        proc.stdout.close()  # type: ignore[union-attr]
        rc = proc.wait()
        err_text = proc.stderr.read() if proc.stderr else ""
        if rc != 0:
            err = f"step1 LLM 解析失败 (rc={rc}): {err_text[:500]}"
            _emit(job_id, "trend_failed", error=err)
            return
    except Exception as exc:
        _emit(job_id, "trend_failed", error=f"step1 启动异常: {type(exc).__name__}: {exc}")
        return

    # 验证 json 真的写出来了
    json_file = target_dir / f"{name}.json"
    if not json_file.is_file():
        _emit(job_id, "trend_failed", error=f"step1 跑完但 {json_file.name} 没生成")
        return

    elapsed = int(time.time() - job["started_at"])
    _emit(
        job_id, "trend_succeeded",
        stage="done",
        elapsed_sec=elapsed,
        json_path=str(json_file),
    )
