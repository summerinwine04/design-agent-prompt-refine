"""
/api/v1/settings — 本地配置（.env 读写）

只单机使用，不做加密：API Key 落到项目根的 .env。
GET 时不回显 key 明文（仅返回 has_openai_key 布尔）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.schemas import Settings, SettingsUpdate

router = APIRouter()

ROOT = Path(__file__).parent.parent.parent
ENV_FILE = ROOT / ".env"
sys.path.insert(0, str(ROOT / "tool"))


def _style_cache():
    from orchestrator.style_cache import StyleCache
    return StyleCache(ROOT / "runs" / "_style_cache")


@router.get("", response_model=Settings)
async def get_settings():
    env = _read_env()
    return Settings(
        has_openai_key=bool(env.get("OPENAI_API_KEY")),
        default_model=env.get("DEFAULT_MODEL", "gpt-5.5"),
        default_image_model=env.get("DEFAULT_IMAGE_MODEL", "gpt-image-2"),
        default_max_tokens=int(env.get("DEFAULT_MAX_TOKENS", "500000")),
        default_color_concurrency=int(env.get("DEFAULT_COLOR_CONCURRENCY", "3")),
        runs_dir=str((Path(__file__).parent.parent.parent / "runs").resolve()),
        prompts_dir=str((Path(__file__).parent.parent.parent / "prompts").resolve()),
        llm_price_input_per_1m=float(env.get("LLM_PRICE_INPUT_PER_1M", "30")),
        llm_price_output_per_1m=float(env.get("LLM_PRICE_OUTPUT_PER_1M", "30")),
        has_public_admin_password=bool(env.get("PUBLIC_ADMIN_PASSWORD")),
        has_public_guest_password=bool(env.get("PUBLIC_GUEST_PASSWORD")),
    )


@router.patch("", response_model=Settings)
async def update_settings(req: SettingsUpdate):
    env = _read_env()
    if req.openai_api_key is not None:
        env["OPENAI_API_KEY"] = req.openai_api_key
    if req.default_model is not None:
        env["DEFAULT_MODEL"] = req.default_model
    if req.default_image_model is not None:
        env["DEFAULT_IMAGE_MODEL"] = req.default_image_model
    if req.default_max_tokens is not None:
        env["DEFAULT_MAX_TOKENS"] = str(req.default_max_tokens)
    if req.default_color_concurrency is not None:
        env["DEFAULT_COLOR_CONCURRENCY"] = str(req.default_color_concurrency)
    if req.llm_price_input_per_1m is not None:
        env["LLM_PRICE_INPUT_PER_1M"] = str(req.llm_price_input_per_1m)
    if req.llm_price_output_per_1m is not None:
        env["LLM_PRICE_OUTPUT_PER_1M"] = str(req.llm_price_output_per_1m)
    if req.public_admin_password is not None:
        env["PUBLIC_ADMIN_PASSWORD"] = req.public_admin_password.strip()
    if req.public_guest_password is not None:
        env["PUBLIC_GUEST_PASSWORD"] = req.public_guest_password.strip()
    _write_env(env)

    # 关键：同步更新当前进程的 os.environ
    # 否则 runs/tasks 真跑时仍读 uvicorn 启动时加载的旧值，
    # 用户改 key 后必须重启 backend 才能生效（这才是 bug）
    for k, v in env.items():
        os.environ[k] = v

    return await get_settings()


# ============================================================================
# 款级缓存管理（2.1 款式分析 / 2.2 颜色识别的跨 run 持久缓存）
# ============================================================================

@router.get("/style-cache")
async def list_style_cache():
    """逐款列出缓存条目统计。"""
    sc = _style_cache()
    styles = sc.stats()
    return {
        "root": str(sc.root),
        "styles": styles,
        "total_bytes": sum(s["total_bytes"] for s in styles),
    }


@router.delete("/style-cache/{style_no}")
async def clear_style_cache_one(style_no: str):
    """清某一款的缓存（下次 run 会重跑 2.1/2.2 并回填）。"""
    deleted = _style_cache().clear(style_no)
    if deleted == 0:
        raise HTTPException(404, f"款 {style_no} 没有缓存条目")
    return {"ok": True, "style_no": style_no, "deleted_files": deleted}


@router.delete("/style-cache")
async def clear_style_cache_all():
    """清全部款级缓存。"""
    deleted = _style_cache().clear(None)
    return {"ok": True, "deleted_files": deleted}


def _read_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    out: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _write_env(env: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
