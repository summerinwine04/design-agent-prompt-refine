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
    billing,
    fixtures,
    looks,
    pattern_library as pattern_library_router,
    prompts,
    runs,
    selection,
    settings,
    styles,
    tasks,
    templates as templates_router,
    trends,
    waves,
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

# 公网访问守卫：本地请求零影响；隧道请求（ngrok/cloudflared）需密码 + 只读白名单
from backend.public_access import PublicAccessMiddleware  # noqa: E402
app.add_middleware(PublicAccessMiddleware)


# ----- 路由 ----- #
app.include_router(runs.router,     prefix="/api/v1/runs",     tags=["runs"])
app.include_router(prompts.router,  prefix="/api/v1/prompts",  tags=["prompts"])
app.include_router(fixtures.router, prefix="/api/v1/fixtures", tags=["fixtures"])
app.include_router(trends.router,   prefix="/api/v1/trends",   tags=["trends"])
app.include_router(styles.router,   prefix="/api/v1/styles",   tags=["styles"])
app.include_router(settings.router, prefix="/api/v1/settings", tags=["settings"])
app.include_router(tasks.router,    prefix="/api/v1/tasks",    tags=["tasks"])    # M6/M7 生图任务
app.include_router(looks.router,        prefix="/api/v1/looks",             tags=["looks"])                # Fitting Room look 组套
app.include_router(looks.img_cat_router, prefix="/api/v1/image-categories", tags=["image-categories"])   # 上/下装归类持久化
app.include_router(templates_router.router, prefix="/api/v1/templates", tags=["templates"])              # 视觉模板库读取
app.include_router(pattern_library_router.router, prefix="/api/v1/pattern-library", tags=["pattern-library"])  # 印花图案库读取（Collection 图库输入源）
app.include_router(billing.router,  prefix="/api/v1/billing",  tags=["billing"])                          # 账单：token/成本统计
app.include_router(waves.router,    prefix="/api/v1/waves",    tags=["waves"])                            # 波段上新管理（guest 只读）
app.include_router(selection.router, prefix="/api/v1/selection", tags=["selection"])                      # 选款中心（guest 只读）
app.include_router(templates_router.look_slot_router, prefix="/api/v1/looks/{look_id}/shooting-slot", tags=["shooting-slot"])  # look 拍摄槽位


# ----- 静态资源挂载 ----- #
# 趋势报告页图、款图、生成图——后续按需指向 ai-supply 目录或本地 runs/
app.mount(
    "/static/runs",
    StaticFiles(directory=str(ROOT / "runs"), check_dir=False),
    name="runs-static",
)

# 选款中心：用户上传的款式图
(ROOT / "data" / "selection_uploads").mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/selection-uploads",
    StaticFiles(directory=str(ROOT / "data" / "selection_uploads"), check_dir=False),
    name="selection-uploads-static",
)

# M6 静态资源：生图任务的图片 + 款图 + 趋势报告页图
# 生图结果图片
(ROOT / "data" / "task_images").mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/tasks",
    StaticFiles(directory=str(ROOT / "data" / "task_images"), check_dir=False),
    name="tasks-static",
)
# 款图（来自 sibling ai-supply）
_styles_dir = ROOT.parent / "ai-supply" / "款图"
if _styles_dir.is_dir():
    app.mount(
        "/static/styles",
        StaticFiles(directory=str(_styles_dir), check_dir=False),
        name="styles-static",
    )
# 趋势报告页图
_trends_dir = ROOT.parent / "ai-supply" / "趋势报告"
if _trends_dir.is_dir():
    app.mount(
        "/static/trends",
        StaticFiles(directory=str(_trends_dir), check_dir=False),
        name="trends-static",
    )

# 视觉模板库图（Fitting Room Try-on 集成）—— TEMPLATES_ROOT 环境变量优先，兜底 ../视觉模板库
import os as _os
_tpl_env = _os.environ.get("TEMPLATES_ROOT")
_tpl_dir = Path(_tpl_env).resolve() / "templates" if _tpl_env else (ROOT.parent / "视觉模板库" / "templates")
if _tpl_dir.is_dir():
    # 顺手确保 user_uploads 子目录存在（用户上传的拍摄参考图落这里）
    (_tpl_dir / "user_uploads").mkdir(parents=True, exist_ok=True)
    app.mount(
        "/static/templates",
        StaticFiles(directory=str(_tpl_dir), check_dir=False),
        name="templates-static",
    )
    print(f"[startup] 视觉模板库 mounted at /static/templates → {_tpl_dir}", flush=True)
else:
    print(
        f"[startup] ⚠ 视觉模板库 not found at {_tpl_dir}. "
        f"Set TEMPLATES_ROOT in .env to enable Fitting Room shooting slot.",
        flush=True,
    )

# 印花图案库图（Collection 图库输入源）—— PATTERN_LIBRARY_ROOT 环境变量优先，兜底 ../ai-supply/印花图案库
_pl_env = _os.environ.get("PATTERN_LIBRARY_ROOT")
_pl_dir = Path(_pl_env).resolve() if _pl_env else (ROOT.parent / "ai-supply" / "印花图案库")
if _pl_dir.is_dir():
    app.mount(
        "/static/pattern-library",
        StaticFiles(directory=str(_pl_dir), check_dir=False),
        name="pattern-library-static",
    )
    print(f"[startup] 印花图案库 mounted at /static/pattern-library → {_pl_dir}", flush=True)
else:
    print(
        f"[startup] ⚠ 印花图案库 not found at {_pl_dir}. "
        f"Set PATTERN_LIBRARY_ROOT in .env to enable Collection 图库输入.",
        flush=True,
    )


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    # 把 main event loop 引用注入 sse.event_bus，
    # 让 orchestrator 工作线程能跨线程安全推事件
    import asyncio
    from backend.sse import event_bus
    event_bus.set_main_loop(asyncio.get_running_loop())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ----- 前端构建产物挂载（单端口公网访问用） ----- #
# 每次请求动态检查 frontend/dist（npm run build 产物）——
# 不依赖启动时序：backend 先启动、后 build 也能立即生效，无需重启。
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

_DIST = ROOT / "frontend" / "dist"


@app.get("/")
async def root_or_spa():
    idx = _DIST / "index.html"
    if idx.is_file():
        return FileResponse(idx)
    return {
        "name": "prompt-refine-agent",
        "version": "0.1.0",
        "docs": "/docs",
        "hint": "前端未构建：运行 `cd frontend && npm run build` 后本页即变为应用首页",
    }


# 注册在所有 API 路由之后：已知路由优先匹配，剩余路径按 SPA 回退
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if ".." not in full_path:
        f = _DIST / full_path
        if full_path and f.is_file():
            return FileResponse(f)
    idx = _DIST / "index.html"
    if idx.is_file():
        return FileResponse(idx)
    return JSONResponse(
        {"detail": "前端未构建：请先运行 `cd frontend && npm run build`"},
        status_code=404,
    )
