"""
prompt-refine-agent FastAPI 主入口

启动：
  cd prompt-refine-agent
  uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

接口分组（前缀 /api/v1）：
  /runs        — 跑 step2 / 查 trace / fork / 重跑某节点
  /prompts     — prompt 文件 + 版本管理
  /fixtures    — 测试夹具
  /trends      — 趋势报告
  /styles      — 款图库
  /settings    — .env 设置

SSE 流：
  /api/v1/runs/{run_id}/stream
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 让 orchestrator 包可 import
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tool"))

from backend.db import init_db
from backend.routers import (
    fixtures,
    prompts,
    runs,
    settings,
    styles,
    trends,
)


# ============================================================================
# .env 加载（与 run_agent.py 行为一致）
# ============================================================================

def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        print(f"[startup] ⚠ .env not found at {env_file}", flush=True)
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    # print 而非 logging.info：模块加载时 uvicorn 还没配置 logger，info 会被吞
    print(
        f"[startup] .env loaded  OPENAI_API_KEY={'set ✓' if has_key else 'MISSING ✗'}",
        flush=True,
    )


_load_env()


app = FastAPI(
    title="prompt-refine-agent",
    description="童装牛仔 Step 2 prompt 迭代工作台",
    version="0.1.0",
)

# 本地开发：允许前端开发服务器跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----- 路由 ----- #
app.include_router(runs.router,     prefix="/api/v1/runs",     tags=["runs"])
app.include_router(prompts.router,  prefix="/api/v1/prompts",  tags=["prompts"])
app.include_router(fixtures.router, prefix="/api/v1/fixtures", tags=["fixtures"])
app.include_router(trends.router,   prefix="/api/v1/trends",   tags=["trends"])
app.include_router(styles.router,   prefix="/api/v1/styles",   tags=["styles"])
app.include_router(settings.router, prefix="/api/v1/settings", tags=["settings"])


# ----- 静态资源挂载 ----- #
# 趋势报告页图、款图、生成图——后续按需指向 ai-supply 目录或本地 runs/
app.mount(
    "/static/runs",
    StaticFiles(directory=str(ROOT / "runs"), check_dir=False),
    name="runs-static",
)


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    # 把 main event loop 引用注入 sse.event_bus，
    # 让 orchestrator 工作线程能跨线程安全推事件
    import asyncio
    from backend.sse import event_bus
    event_bus.set_main_loop(asyncio.get_running_loop())


@app.get("/")
async def root():
    return {
        "name": "prompt-refine-agent",
        "version": "0.1.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
