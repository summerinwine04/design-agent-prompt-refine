"""
Step 0 — PDF → page_*.png 系列图片

两种用法：
1. CLI（被 backend / 命令行调用）：
   python tool/step0_pdf2images.py --pdf "C:/.../foo.pdf" --out "C:/.../foo/"

2. 作为函数 import（被 backend trend importer 调用）：
   from tool.step0_pdf2images import convert_pdf_to_pages
   convert_pdf_to_pages(pdf_path, out_dir, on_progress=lambda i,n: ...)

进度协议（CLI 模式 stdout）：每转完 1 页打印一行：
   [PROGRESS_LINE] stage=pdf2img current=3 total=12
backend 解析这行转 SSE event。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional

# poppler binary 默认位置（Windows 装的位置），可被 env 覆盖
DEFAULT_POPPLER_PATH = r"E:\base\poppler-26.02.0\Library\bin"


def convert_pdf_to_pages(
    pdf_path: Path | str,
    out_dir: Path | str,
    *,
    poppler_path: Optional[str] = None,
    dpi: int = 300,
    max_size: int = 2048,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """
    PDF → out_dir/page_0.png ... page_{N-1}.png

    参数
        pdf_path        : PDF 文件路径
        out_dir         : 输出目录（不存在会自动创建）
        poppler_path    : poppler bin 路径，None 用默认 + 环境变量 POPPLER_PATH 覆盖
        dpi             : 渲染分辨率（默认 300，越高图越清越大）
        max_size        : 输出图最长边像素（默认 2048，超过会等比缩放）
        on_progress     : 可选 callback (current_index 1-based, total) 每转一页调用一次

    返回
        list[str]       : 生成的图片文件名列表（不含路径）
    """
    from pdf2image import convert_from_path
    from PIL import Image

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # poppler 路径解析优先级：函数参数 > env POPPLER_PATH > 默认硬编码路径 > 系统 PATH
    # 如果硬编码路径不存在，退回到不传 poppler_path（让 pdf2image 在系统 PATH 中找）
    pp_candidate = poppler_path or os.environ.get("POPPLER_PATH") or DEFAULT_POPPLER_PATH
    pp = pp_candidate if pp_candidate and Path(pp_candidate).is_dir() else None
    try:
        pages = convert_from_path(str(pdf_path), dpi=dpi, poppler_path=pp)
    except Exception as exc:
        # 友好诊断：告诉用户去哪装 / 配
        msg = (
            f"PDF 转换失败：{exc}\n"
            f"poppler 二进制找不到。三种解决方案任选其一：\n"
            f"  A. 装 poppler-windows (https://github.com/oschwartz10612/poppler-windows/releases) "
            f"     解压后把 bin 目录加到系统 PATH\n"
            f"  B. 设置环境变量 POPPLER_PATH 指向 poppler bin 目录"
            f"  C. 通过 --poppler 参数指定 poppler bin 目录\n"
            f"  当前尝试的路径：{pp_candidate or '(无)'}（存在？{Path(pp_candidate).is_dir() if pp_candidate else 'N/A'}）"
        )
        raise RuntimeError(msg) from exc
    total = len(pages)

    image_files: list[str] = []
    for i, page in enumerate(pages):
        width, height = page.size
        scale = max_size / max(width, height)
        if scale < 1:
            page = page.resize(
                (int(width * scale), int(height * scale)),
                Image.LANCZOS,  # type: ignore
            )
        file_name = f"page_{i}.png"
        page.save(out_dir / file_name, "PNG")
        image_files.append(file_name)
        if on_progress is not None:
            try:
                on_progress(i + 1, total)
            except Exception:
                pass

    return image_files


def _cli_progress(current: int, total: int) -> None:
    """CLI 模式：把进度打到 stdout 让 backend 能解析。"""
    print(f"[PROGRESS_LINE] stage=pdf2img current={current} total={total}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 0 — PDF 转 page_*.png 系列图片")
    parser.add_argument("--pdf", required=True, help="PDF 文件路径")
    parser.add_argument("--out", required=True, help="输出目录（不存在自动创建）")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--max-size", type=int, default=2048)
    parser.add_argument("--poppler", default=None, help="poppler bin 路径（默认走 env / 内置）")
    args = parser.parse_args()

    try:
        files = convert_pdf_to_pages(
            args.pdf,
            args.out,
            poppler_path=args.poppler,
            dpi=args.dpi,
            max_size=args.max_size,
            on_progress=_cli_progress,
        )
        print(f"[DONE] pages={len(files)} out={args.out}", flush=True)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
