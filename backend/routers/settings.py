"""
/api/v1/settings — 本地配置（.env 读写）

只单机使用，不做加密：API Key 落到项目根的 .env。
GET 时不回显 key 明文（仅返回 has_openai_key 布尔）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from backend.schemas import Settings, SettingsUpdate

router = APIRouter()

ENV_FILE = Path(__file__).parent.parent.parent / ".env"


@router.get("", response_model=Settings)
async def get_settings():
    env = _read_env()
    return Settings(
        has_openai_key=bool(env.get("OPENAI_API_KEY")),
        default_model=env.get("DEFAULT_MODEL", "gpt-5.5"),
        default_max_tokens=int(env.get("DEFAULT_MAX_TOKENS", "500000")),
        default_color_concurrency=int(env.get("DEFAULT_COLOR_CONCURRENCY", "3")),
        runs_dir=str((Path(__file__).parent.parent.parent / "runs").resolve()),
        prompts_dir=str((Path(__file__).parent.parent.parent / "prompts").resolve()),
    )


@router.patch("", response_model=Settings)
async def update_settings(req: SettingsUpdate):
    env = _read_env()
    if req.openai_api_key is not None:
        env["OPENAI_API_KEY"] = req.openai_api_key
    if req.default_model is not None:
        env["DEFAULT_MODEL"] = req.default_model
    if req.default_max_tokens is not None:
        env["DEFAULT_MAX_TOKENS"] = str(req.default_max_tokens)
    if req.default_color_concurrency is not None:
        env["DEFAULT_COLOR_CONCURRENCY"] = str(req.default_color_concurrency)
    _write_env(env)
    return await get_settings()


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
