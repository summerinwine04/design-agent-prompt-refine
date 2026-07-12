"""
公网访问守卫（两组账号：管理员全功能 / 访客三页面）

设计：
  - 本地请求（无隧道注入的 X-Forwarded-For / Cf-Ray 头）→ 完全放行，功能不变
  - 隧道请求（ngrok / cloudflared 注入转发头，外部客户端无法剥掉）→ HTTP Basic：
      * 密码 = PUBLIC_ADMIN_PASSWORD → 管理员：全页面全功能（等同本机）
      * 密码 = PUBLIC_GUEST_PASSWORD → 访客：仅 Fitting Room / 生图任务 / 账单
          - 查看：looks / tasks / image-categories / templates / billing 的 GET
          - 操作：Fitting Room 的组套/编辑（looks 与 image-categories 的写）
          - 拦截：发起设计 run、创建/重生/删除生图任务（烧钱/删数据）、
                  导出到桌面（写宿主机磁盘）、prompt/夹具/设置 等一切其他能力
      * 两个密码都未配置 → 一律 403（公网关闭）

角色由密码区分，用户名任意填。
"""

from __future__ import annotations

import base64
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# 访客可读（GET/HEAD）的 API 前缀
_GUEST_GET_PREFIXES = (
    "/api/v1/looks",              # Fitting Room look 列表
    "/api/v1/tasks",              # 生图任务列表 / 详情 / all-images / SSE
    "/api/v1/image-categories",   # 上下装归类
    "/api/v1/templates",          # 视觉模板库
    "/api/v1/billing",            # 账单（只读天然）
)

# 访客可写（POST/PATCH/DELETE）的 API 前缀 —— Fitting Room 的组套/编辑操作
_GUEST_WRITE_PREFIXES = (
    "/api/v1/looks",              # look 增删改 / tags / 文本 / 拍摄槽位
    "/api/v1/image-categories",   # 组套模式的上下装归类
)

# 访客禁写的例外（优先于可写前缀）：写宿主机磁盘的导出
_GUEST_WRITE_DENY = (
    "/api/v1/looks/export-to-desktop",
)

# 访客可访问的静态资源前缀
_STATIC_ALLOW_PREFIXES = (
    "/static/tasks",      # 生成图
    "/static/styles",     # 款图 / 色号图
    "/static/templates",  # 拍摄模板图
)


def _is_tunnel_request(request: Request) -> bool:
    """ngrok / cloudflared 都会注入转发头且外部客户端无法移除；本地直连没有这些头。

    坑：Vite dev proxy（http-proxy 库）默认也会添加 x-forwarded-for，此时真实 client
    仍然是 127.0.0.1，不算隧道请求。CF 头则是 Cloudflare 独有，见到就一定是公网。
    """
    h = request.headers
    if h.get("cf-ray") or h.get("cf-connecting-ip"):
        return True                                    # Cloudflare 隧道
    if h.get("x-forwarded-for"):
        # xff 存在 → 看真实 client 是不是本机（Vite dev proxy 会加 xff 但源是 127.0.0.1）
        client_host = (request.client.host if request.client else "") or ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return False                               # 本地 dev proxy，不算隧道
        return True                                    # 真的公网隧道（ngrok 等）
    return False


def _role_of(request: Request) -> str | None:
    """按 Basic auth 密码判定角色：'admin' / 'guest' / None（未认证或密码错）。"""
    admin_pwd = (os.environ.get("PUBLIC_ADMIN_PASSWORD") or "").strip()
    guest_pwd = (os.environ.get("PUBLIC_GUEST_PASSWORD") or "").strip()
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return None
    _user, _, pwd = decoded.partition(":")
    if admin_pwd and secrets.compare_digest(pwd, admin_pwd):
        return "admin"
    if guest_pwd and secrets.compare_digest(pwd, guest_pwd):
        return "guest"
    return None


class PublicAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _is_tunnel_request(request):
            return await call_next(request)          # 本地：完全放行

        admin_pwd = (os.environ.get("PUBLIC_ADMIN_PASSWORD") or "").strip()
        guest_pwd = (os.environ.get("PUBLIC_GUEST_PASSWORD") or "").strip()
        if not admin_pwd and not guest_pwd:
            return JSONResponse(
                {"detail": "公网访问未启用（未在设置页配置访问密码）"}, status_code=403,
            )

        role = _role_of(request)
        if role is None:
            return Response(
                "需要访问密码", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="prompt-refine-agent"'},
            )

        if role == "admin":
            return await call_next(request)          # 管理员：等同本机

        # ---- 访客 ----
        path = request.url.path
        method = request.method.upper()

        if path.startswith("/api/"):
            if method in ("GET", "HEAD"):
                ok = any(path.startswith(p) for p in _GUEST_GET_PREFIXES)
            else:
                ok = (
                    any(path.startswith(p) for p in _GUEST_WRITE_PREFIXES)
                    and not any(path.startswith(p) for p in _GUEST_WRITE_DENY)
                )
            if not ok:
                return JSONResponse(
                    {"detail": "访客模式：该功能仅限管理员使用"}, status_code=403,
                )
        elif path.startswith("/static/"):
            if not any(path.startswith(p) for p in _STATIC_ALLOW_PREFIXES):
                return JSONResponse({"detail": "访客模式不可访问该资源"}, status_code=403)
        # 其余（前端页面 / assets）放行——页面本身无数据，数据都要过 API 白名单

        return await call_next(request)
