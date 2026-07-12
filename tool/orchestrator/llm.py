"""
OpenAI Responses API 薄包装 — 懒初始化版

dry-run 模式不会触碰 OpenAI 客户端构造，避免环境层 SOCKS 代理污染。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from openai import OpenAI
except ImportError as e:
    raise ImportError("请先安装依赖: pip install openai") from e


@dataclass
class LLMResult:
    raw_text: str
    parsed_json: object
    tokens_in: int
    tokens_out: int
    elapsed_sec: float
    file_ids: list
    output_marker_found: bool


def extract_output_json(raw: str, output_marker_regex: str):
    m = re.search(output_marker_regex, raw, re.IGNORECASE)
    if not m:
        m = re.search(r"##\s*STEP[^\n]*OUTPUT", raw, re.IGNORECASE)
    if not m:
        return False, None
    after = raw[m.end():]
    cb = re.search(r"```(?:json)?\s*([\s\S]*?)```", after)
    if cb:
        try:
            return True, json.loads(cb.group(1).strip())
        except json.JSONDecodeError:
            pass
    bare = re.search(r"([{\[][\s\S]*)", after)
    if bare:
        try:
            return True, json.loads(bare.group(1).strip())
        except json.JSONDecodeError:
            pass
    return True, None


def _maybe_downscale(img_path, max_side: int):
    """
    上传前压缩：长边 > max_side 时等比缩小并 re-encode JPEG，返回 (filename, BytesIO)。
    仅作用于「给 LLM 看」的视觉输入——生图（images.edit）走 generator/runner.py
    直接读磁盘原文件，与本函数无关，永远用原图。
    PIL 缺失 / 解码失败 / 图本来就小 → 返回 None（调用方回退原文件）。
    """
    try:
        from PIL import Image
        import io
        with Image.open(img_path) as im:
            w, h = im.size
            if max(w, h) <= max_side:
                return None
            ratio = max_side / max(w, h)
            im = im.convert("RGB").resize(
                (max(1, int(w * ratio)), max(1, int(h * ratio))),
                Image.LANCZOS,
            )
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            buf.seek(0)
            return (Path(img_path).stem + "_small.jpg", buf)
    except Exception:
        return None


class LLMClient:
    def __init__(self, *, api_key, default_model="gpt-5.5", default_max_tokens=500000):
        self.api_key = api_key
        self._client = None
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def call_with_images(
        self,
        *,
        system_prompt,
        user_prompt,
        image_paths,
        output_marker_regex,
        model=None,
        max_tokens=None,
        stream_callback=None,
        dry_run=False,
        image_max_side=None,   # 上传前压缩长边上限（None = 原图直传）。仅影响 LLM 视觉输入
    ):
        model = model or self.default_model
        max_tokens = max_tokens or self.default_max_tokens

        if dry_run:
            return LLMResult(
                raw_text="[DRY-RUN] (skipped actual API call)",
                parsed_json=None,
                tokens_in=0,
                tokens_out=0,
                elapsed_sec=0.0,
                file_ids=[],
                output_marker_found=False,
            )

        uploaded_file_ids = []
        try:
            for img_path in image_paths:
                small = _maybe_downscale(img_path, image_max_side) if image_max_side else None
                if small is not None:
                    file_obj = self.client.files.create(file=small, purpose="vision")
                else:
                    with open(img_path, "rb") as fh:
                        file_obj = self.client.files.create(file=fh, purpose="vision")
                uploaded_file_ids.append(file_obj.id)

            user_content = [{"type": "input_text", "text": user_prompt}]
            for fid in uploaded_file_ids:
                user_content.append({"type": "input_image", "file_id": fid})

            raw_text = ""
            t0 = datetime.now()
            final_response = None
            with self.client.responses.stream(
                model=model,
                instructions=system_prompt,
                input=[{"role": "user", "content": user_content}],
                max_output_tokens=max_tokens,
            ) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        raw_text += delta
                        if stream_callback and delta:
                            try:
                                stream_callback(delta)
                            except Exception:
                                pass
                try:
                    final_response = stream.get_final_response()
                except RuntimeError:
                    final_response = None

            if not raw_text and final_response is not None:
                raw_text = getattr(final_response, "output_text", "") or ""

            elapsed = (datetime.now() - t0).total_seconds()
            if not raw_text:
                raise RuntimeError("未收到任何模型输出，请重试")

            marker_found, parsed = extract_output_json(raw_text, output_marker_regex)

            usage = final_response.usage if final_response else None
            tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
            tokens_out = getattr(usage, "output_tokens", 0) if usage else 0

            return LLMResult(
                raw_text=raw_text,
                parsed_json=parsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                elapsed_sec=elapsed,
                file_ids=list(uploaded_file_ids),
                output_marker_found=marker_found,
            )
        finally:
            for fid in uploaded_file_ids:
                try:
                    self.client.files.delete(fid)
                except Exception:
                    pass
