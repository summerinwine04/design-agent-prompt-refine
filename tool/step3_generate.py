#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
童装牛仔 — Step 3 批量生图

根据 Step 2 改款方案 JSON，对每个方案编号调用 GPT Image API 生成改款效果图。

图片预加载策略：
  款图和所有被引用的色号图字节在启动时一次性读入内存，
  并发任务共享同一份字节缓冲（每次调用时包装成新 BytesIO），
  整批完成后统一退出，无需文件 API 上传/删除。

参考图规则（来自 Step 2 JSON 的 "是否需要变色" 字段）：
  变色=false → 仅上传款图（1 张）
  变色=true  → 款图 + 对应色号图（2 张）

色号图匹配：通过 meta.色号图列表 中的 (色号代码 + 营销色名 → 文件名) 精确定位，
             避免多个色号共用同一代码时的歧义（如 DSC09258 对应多种颜色）。

生图尺寸：默认读取款图实际像素，按最近 16 的倍数对齐；需安装 Pillow（pip install Pillow），
          无法读取时回退至 1024x1024。

生图完成后在输出目录写入 {json文件名}_交付说明.json，汇总趋势介绍与各方案说明，用于客户交付。

用法示例:
  python tool/step3_generate.py \
      --step2-json trend2design/山系户外_YK250609-牛仔服.json \
      --ref-image  款图/YK250609-牛仔服.jpg \
      --color-folder 款图/色号图 \
      --concurrency 3

  # 空跑（不实际提交生图，仅验证请求参数）：
  python tool/step3_generate.py ... --dry-run

环境变量:
  OPENAI_API_KEY   — OpenAI API Key（也可用 --api-key 参数覆盖）
"""

import io
import os
import sys
import base64
import json
import logging
import argparse
import struct
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from openai import OpenAI
except ImportError:
    print("请先安装依赖: pip install openai")
    sys.exit(1)

# 从项目根目录的 .env 加载环境变量（不覆盖已有环境变量）
_ENV_FILE = Path(__file__).parent.parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DEFAULT_MODEL = "gpt-image-2"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def setup_logger(log_path: Path) -> logging.Logger:
    name = f"step3_{log_path.stem}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --------------------------------------------------------------------------- #
# 图像尺寸检测
# --------------------------------------------------------------------------- #

def _read_image_size_pil(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception:
        return None


def _read_image_size_header(path: Path) -> tuple[int, int] | None:
    """无 PIL 时通过文件头解析 JPEG/PNG 尺寸。"""
    try:
        data = path.read_bytes()
        # PNG: signature 8 bytes + IHDR chunk: 4(len) + 4(type) + 4(w) + 4(h)
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w = struct.unpack('>I', data[16:20])[0]
            h = struct.unpack('>I', data[20:24])[0]
            return w, h
        # JPEG: scan for SOF markers (0xFF 0xC0/C1/C2)
        if data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h = struct.unpack('>H', data[i + 5:i + 7])[0]
                    w = struct.unpack('>H', data[i + 7:i + 9])[0]
                    return w, h
                length = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + length
    except Exception:
        pass
    return None


_SUPPORTED_SIZES = [
    (1024, 1024),   # square,    ratio ≈ 1.00
    (1024, 1536),   # portrait,  ratio ≈ 0.67
    (1536, 1024),   # landscape, ratio ≈ 1.50
]

def get_aligned_size(path: Path, logger: logging.Logger | None = None) -> str:
    """
    读取图片实际像素尺寸，从 gpt-image-1 支持的三档中选最近长宽比的一档。
    支持档位：1024x1024 / 1024x1536 / 1536x1024。
    读取失败时回退至 '1024x1024'。
    """
    size = _read_image_size_pil(path) or _read_image_size_header(path)
    if size is None:
        if logger:
            logger.warning(f"无法读取 {path.name} 尺寸，使用默认 1024x1024")
        return "1024x1024"

    img_w, img_h = size
    img_ratio = img_w / img_h if img_h else 1.0

    best = min(
        _SUPPORTED_SIZES,
        key=lambda s: abs(s[0] / s[1] - img_ratio),
    )
    chosen = f"{best[0]}x{best[1]}"
    if logger:
        logger.info(
            f"款图尺寸: {img_w}×{img_h}（比例 {img_ratio:.2f}）→ 匹配档位: {chosen}"
        )
    return chosen


# --------------------------------------------------------------------------- #
# JSON 解析 & 任务提取
# --------------------------------------------------------------------------- #

def build_filename_map(meta: dict) -> dict[tuple[str, str], str]:
    """
    从 meta.色号图列表 构建 (色号代码, 营销色名) → 文件名 的精确映射。
    避免同一色号代码对应多个颜色时的歧义。
    """
    mapping: dict[tuple[str, str], str] = {}
    for entry in meta.get("色号图列表", []):
        code = entry.get("色号代码", "")
        name = entry.get("营销色名", "")
        fname = entry.get("文件名", "")
        if code and name and fname:
            mapping[(code, name)] = fname
    return mapping


def extract_tasks(step2_json: dict) -> list[dict]:
    """
    从 Step 2 JSON 中提取所有生图任务。

    返回每项：
      {
        "方案编号": str,
        "色号代码": str,
        "营销色名": str,
        "是否需要变色": bool,
        "prompt": str,
        "方案说明": str,
      }
    """
    design_data = step2_json.get("step2_改款方案") or step2_json
    tasks = []
    for color_item in design_data.get("色号方案列表", []):
        code = color_item.get("色号代码", "")
        name = color_item.get("营销色名", "")
        needs_recolor = color_item.get("是否需要变色", False)
        gender = color_item.get("性别定向", "")
        for design in color_item.get("设计方案", []):
            plan_id = design.get("方案编号", "").strip()
            prompt_text = design.get("图生图prompt", "").strip()
            plan_desc = design.get("方案说明", "").strip()
            if plan_id and prompt_text:
                tasks.append({
                    "方案编号": plan_id,
                    "色号代码": code,
                    "营销色名": name,
                    "是否需要变色": needs_recolor,
                    "性别定向": gender,
                    "prompt": prompt_text,
                    "方案说明": plan_desc,
                })
    return tasks


def build_delivery_json(step2_json: dict, results: list[dict], out_dir: Path) -> dict:
    """
    构建交付说明 JSON：趋势介绍 + 款号款式名 + 各方案生图结果摘要。
    """
    design_data = step2_json.get("step2_改款方案") or step2_json
    delivery = {
        "趋势介绍": design_data.get("趋势说明", ""),
        "款号": design_data.get("款号", ""),
        "款式名": design_data.get("款式名", ""),
        "生图结果": [],
    }
    result_map = {r["方案编号"]: r for r in results}
    for color_item in design_data.get("色号方案列表", []):
        gender = color_item.get("性别定向", "")
        for design in color_item.get("设计方案", []):
            plan_id = design.get("方案编号", "")
            r = result_map.get(plan_id, {})
            entry: dict = {
                "方案编号": plan_id,
                "适用性别": gender,
                "方案说明": design.get("方案说明", ""),
                "生图状态": "成功" if r.get("ok") else "失败",
            }
            img_file = out_dir / f"{plan_id}.png"
            if img_file.is_file():
                entry["图片文件"] = img_file.name
            if not r.get("ok") and r.get("error"):
                entry["错误信息"] = r["error"]
            delivery["生图结果"].append(entry)
    return delivery


# --------------------------------------------------------------------------- #
# 单任务生图
# --------------------------------------------------------------------------- #

def generate_one(
    *,
    task: dict,
    garment_bytes: bytes,
    color_bytes_map: dict[tuple[str, str], bytes],
    client: OpenAI,
    model: str,
    size: str,
    out_dir: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> dict:
    plan_id = task["方案编号"]
    code = task["色号代码"]
    name = task["营销色名"]
    needs_recolor = task["是否需要变色"]
    prompt_text = task["prompt"]
    out_path = out_dir / f"{plan_id}.png"

    img_count = 2 if needs_recolor else 1
    logger.info(f"[{plan_id}] 开始生图  变色={needs_recolor}  参考图={img_count}张  尺寸={size}")

    if dry_run:
        color_key = (code, name)
        color_found = (not needs_recolor) or (color_key in color_bytes_map)
        logger.info(
            f"[{plan_id}] [DRY-RUN] model={model} size={size} "
            f"garment_bytes={len(garment_bytes)} "
            f"color_found={color_found} "
            f"prompt_chars={len(prompt_text)}"
        )
        return {
            "方案编号": plan_id,
            "ok": True,
            "dry_run": True,
            "color_image_found": color_found,
            "prompt_chars": len(prompt_text),
        }

    try:
        if needs_recolor:
            color_key = (code, name)
            if color_key not in color_bytes_map:
                raise FileNotFoundError(
                    f"内存中未找到色号图 ({code}, {name})，请检查色号图文件夹"
                )
            images = [
                ("garment.jpg", io.BytesIO(garment_bytes), "image/jpeg"),
                (f"{code}-{name}.jpg", io.BytesIO(color_bytes_map[color_key]), "image/jpeg"),
            ]
        else:
            images = [("garment.jpg", io.BytesIO(garment_bytes), "image/jpeg")]

        result = client.images.edit(
            model=model,
            image=images if len(images) > 1 else images[0],
            prompt=prompt_text,
            n=1,
            size=size,
        )

        # 提取图像数据（b64_json 优先，回退 url）
        if not result or not result.data:
            raise RuntimeError("API 返回结果为空")
        img_item = result.data[0]
        b64 = getattr(img_item, "b64_json", None)
        if b64:
            img_bytes = base64.b64decode(b64)
        else:
            url = getattr(img_item, "url", None)
            if not url:
                raise RuntimeError("API 未返回图像数据（既无 b64_json 也无 url）")
            import urllib.request
            with urllib.request.urlopen(url) as resp:
                img_bytes = resp.read()

        out_path.write_bytes(img_bytes)
        logger.info(f"[{plan_id}] 完成 → {out_path.name}  ({len(img_bytes) / 1024:.1f} KB)")
        return {"方案编号": plan_id, "ok": True, "path": str(out_path)}

    except Exception as exc:
        logger.error(f"[{plan_id}] 失败: {exc}", exc_info=True)
        return {"方案编号": plan_id, "ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# 主逻辑
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="童装牛仔趋势改款批量生图（Step 3）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--step2-json",
        default=r"C:\Users\zgj\Documents\Claude\Projects\summer\ai-supply\trend2design\山系户外童装花型TOP热榜_YK250609-牛仔服.json",
        help="Step 2 输出的改款方案 JSON 文件路径",
    )
    parser.add_argument(
        "--ref-image",
        default=r"C:\Users\zgj\Documents\Claude\Projects\summer\ai-supply\款图\YK250609-牛仔服.jpg",
        help="款式正反面参考图路径（单张）",
    )
    parser.add_argument(
        "--color-folder",
        default=r"C:\Users\zgj\Documents\Claude\Projects\summer\ai-supply\款图\YK250609-牛仔服",
        help="色号图文件夹路径",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发生图任务数 K（默认 1）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"生图模型名称（默认 {DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--size",
        default=None,
        help="生图尺寸，如 '1024x1024'；不指定时自动从款图尺寸对齐 16 倍数",
    )
    parser.add_argument(
        "--dry-run",
        # default=True,
        action="store_true",
        help="空跑模式：验证所有参数和图片匹配，不实际提交生图",
    )
    parser.add_argument("--api-key", default=None, help="OpenAI API Key（优先于 .env）")
    args = parser.parse_args()

    # 路径校验
    step2_json_path = Path(args.step2_json).resolve()
    if not step2_json_path.is_file():
        sys.exit(f"错误：Step 2 JSON 不存在 → {step2_json_path}")

    ref_path = Path(args.ref_image).resolve()
    if not ref_path.is_file():
        sys.exit(f"错误：款式图不存在 → {ref_path}")

    color_folder = Path(args.color_folder).resolve()
    if not color_folder.is_dir():
        sys.exit(f"错误：色号图文件夹不存在 → {color_folder}")

    # 输出目录：JSON 同名子文件夹
    out_dir = step2_json_path.parent / step2_json_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # 日志
    log_path = out_dir / f"{step2_json_path.stem}_step3.log"
    logger = setup_logger(log_path)
    logger.info("=" * 60)
    logger.info(f"Step 3 批量生图 启动{'（DRY-RUN）' if args.dry_run else ''}")
    logger.info(f"Step 2 JSON   : {step2_json_path}")
    logger.info(f"款式图        : {ref_path}")
    logger.info(f"色号图文件夹  : {color_folder}")
    logger.info(f"输出目录      : {out_dir}")
    logger.info(f"并发数 K      : {args.concurrency}")
    logger.info(f"生图模型      : {args.model}")

    # 确定生图尺寸
    size = args.size if args.size else get_aligned_size(ref_path, logger=logger)
    logger.info(f"生图尺寸      : {size}")

    # 解析 Step 2 JSON
    try:
        step2_json = json.loads(step2_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"错误：JSON 解析失败 → {e}")

    meta = step2_json.get("meta", {})
    filename_map = build_filename_map(meta)
    tasks = extract_tasks(step2_json)

    if not tasks:
        sys.exit("错误：JSON 中未找到任何设计方案，请检查 step2_改款方案.色号方案列表")

    logger.info(f"共 {len(tasks)} 个生图任务")

    # 确定需要预加载的色号图
    needed_color_keys: set[tuple[str, str]] = {
        (t["色号代码"], t["营销色名"])
        for t in tasks
        if t["是否需要变色"]
    }

    # 预加载款图
    logger.info(f"预加载款图: {ref_path.name} ({ref_path.stat().st_size / 1024:.1f} KB)")
    garment_bytes = ref_path.read_bytes()

    # 预加载色号图
    color_bytes_map: dict[tuple[str, str], bytes] = {}
    missing_colors: list[str] = []

    for key in needed_color_keys:
        code, name = key
        fname = filename_map.get(key)
        if not fname:
            missing_colors.append(f"({code}, {name}) 未在 meta 中找到")
            continue

        color_path = color_folder / fname
        if not color_path.is_file():
            # 大小写回退匹配
            matches = [
                f for f in color_folder.iterdir()
                if f.suffix.lower() in IMAGE_EXTENSIONS
                and f.name.lower() == fname.lower()
            ]
            color_path = matches[0] if matches else None  # type: ignore[assignment]

        if color_path is None or not color_path.is_file():
            missing_colors.append(f"({code}, {name}) 文件 {fname} 不存在")
            continue

        color_bytes_map[key] = color_path.read_bytes()
        logger.info(
            f"预加载色号图 [{code}·{name}]: {color_path.name} "
            f"({len(color_bytes_map[key]) / 1024:.1f} KB)"
        )

    if missing_colors:
        logger.warning(f"以下色号图未找到，对应任务将报错: {missing_colors}")

    # API 客户端：dry-run 时占位初始化（不实际发起请求）
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key and not args.dry_run:
        sys.exit("错误：未找到 OpenAI API Key（请设置 OPENAI_API_KEY 或使用 --api-key）")
    client = OpenAI(api_key=api_key or "dry-run-placeholder")

    # 并发生图
    t_start = datetime.now()
    results: list[dict] = []

    mode_label = "DRY-RUN 验证" if args.dry_run else "生图"
    logger.info(f"开始并发{mode_label}（K={args.concurrency}）…")

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                generate_one,
                task=task,
                garment_bytes=garment_bytes,
                color_bytes_map=color_bytes_map,
                client=client,
                model=args.model,
                size=size,
                out_dir=out_dir,
                dry_run=args.dry_run,
                logger=logger,
            ): task["方案编号"]
            for task in tasks
        }
        for future in as_completed(futures):
            plan_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.error(f"[{plan_id}] 未捕获异常: {exc}", exc_info=True)
                result = {"方案编号": plan_id, "ok": False, "error": str(exc)}
            results.append(result)

    elapsed = (datetime.now() - t_start).total_seconds()

    # 交付说明 JSON（dry-run 时也生成，方便预览）
    delivery = build_delivery_json(step2_json, results, out_dir)
    delivery_path = out_dir / f"{step2_json_path.stem}_交付说明.json"
    delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"交付说明 JSON 已保存: {delivery_path}")

    # 汇总
    ok_list = [r for r in results if r.get("ok")]
    fail_list = [r for r in results if not r.get("ok")]

    logger.info("=" * 60)
    dry_tag = " [DRY-RUN]" if args.dry_run else ""
    logger.info(f"完毕{dry_tag}  成功 {len(ok_list)} / 失败 {len(fail_list)} / 总耗时 {elapsed:.1f}s")

    print()
    print("=" * 60)
    print(
        f"Step 3 完成{dry_tag}！  "
        f"成功 {len(ok_list)} / 共 {len(tasks)}  耗时 {elapsed:.1f}s"
    )
    for r in sorted(results, key=lambda x: x["方案编号"]):
        if args.dry_run:
            cf = "色号图✓" if r.get("color_image_found", True) else "色号图✗"
            status = f"[DRY-RUN] {cf}  prompt={r.get('prompt_chars', 0)}字"
        else:
            status = "✓" if r.get("ok") else f"✗ {r.get('error', '')[:60]}"
        print(f"  {r['方案编号']:50s} {status}")
    print(f"  输出目录     : {out_dir}")
    print(f"  交付说明 JSON: {delivery_path}")
    print(f"  日志         : {log_path}")
    print("=" * 60)

    if fail_list and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
