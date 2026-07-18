#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
童装牛仔 — Step 1 趋势报告解析

用法示例:
  python tool/step1_trend.py --trend-folder 趋势报告/山系户外

输出:
  趋势报告文件夹下同名 .json，如 趋势报告/山系户外/山系户外.json

环境变量:
  OPENAI_API_KEY   — OpenAI API Key（也可用 --api-key 参数覆盖）
"""

import os
import sys
import re
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime

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

_SCRIPT_DIR = Path(__file__).parent
PROMPT_MD_PATH = _SCRIPT_DIR.parent / "prompts" / "step1_trend_parse.md"
DEFAULT_MODEL = "gpt-5.5"


# --------------------------------------------------------------------------- #
# Prompt 加载
# --------------------------------------------------------------------------- #

def load_step1_prompts(md_path: Path) -> tuple[str, str]:
    """
    从 step1_trend_parse.md 分别提取 System Prompt 和 User Prompt Template。
    返回 (system_prompt, user_prompt_template)。
    """
    text = md_path.read_text(encoding="utf-8")

    # System Prompt: SYSTEM PROMPT 标记到下一个 --- 分隔符之间
    sys_match = re.search(
        r"═══\s*SYSTEM PROMPT\s*═══\s*\n([\s\S]*?)\n---",
        text,
        re.DOTALL,
    )
    if not sys_match:
        raise ValueError(f"未能在 {md_path} 中找到 SYSTEM PROMPT 块")
    system_prompt = sys_match.group(1).strip()

    # User Prompt Template: USER PROMPT TEMPLATE 标记到文件末尾（去掉说明注释行）
    user_match = re.search(
        r"═══\s*USER PROMPT TEMPLATE\s*═══\s*\n([\s\S]*?)$",
        text,
        re.DOTALL,
    )
    if not user_match:
        raise ValueError(f"未能在 {md_path} 中找到 USER PROMPT TEMPLATE 块")
    # 过滤掉以 > 开头的注释行
    raw_user = user_match.group(1)
    user_lines = [
        line for line in raw_user.splitlines()
        if not line.strip().startswith(">")
    ]
    user_template = "\n".join(user_lines).strip()

    return system_prompt, user_template


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def setup_logger(log_path: Path, debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("step1_trend")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #

def extract_step1_json(raw: str):
    """从模型输出中提取 ## STEP 1 OUTPUT 后的 JSON。"""
    m = re.search(r"##\s*STEP\s*1\s*OUTPUT", raw, re.IGNORECASE)
    if not m:
        return None
    after = raw[m.end():]
    # 尝试代码块
    cb = re.search(r"```(?:json)?\s*([\s\S]*?)```", after)
    if cb:
        try:
            return json.loads(cb.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 尝试裸 JSON
    bare = re.search(r"([{\[][\s\S]*)", after)
    if bare:
        try:
            return json.loads(bare.group(1).strip())
        except json.JSONDecodeError:
            pass
    return None


# --------------------------------------------------------------------------- #
# 主逻辑
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="童装牛仔趋势报告解析（Step 1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--trend-folder",
        default=r"C:\Users\zgj\Documents\Claude\Projects\summer\ai-supply\趋势报告\山系户外童装花型TOP热榜",
        help="趋势报告图片文件夹（含 page_0.png, page_1.png …），最后一层目录名即趋势名字",
    )
    parser.add_argument(
        "--output-folder",
        default=None,
        help="输出目录，默认与 --trend-folder 相同",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名称（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--max-tokens", type=int, default=500000, help="最大输出 token 数")
    parser.add_argument("--api-key", default=None, help="OpenAI API Key（优先于 .env）")
    parser.add_argument("--prompt-file", default=None, help="自定义 prompt .md 文件路径")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument("--dry-run", action="store_true", help="仅生成 prompt 并输出，不调用 OpenAI API")
    args = parser.parse_args()

    trend_path = Path(args.trend_folder).resolve()
    if not trend_path.is_dir():
        sys.exit(f"错误：趋势文件夹不存在 → {trend_path}")

    trend_name = trend_path.name

    out_dir = Path(args.output_folder).resolve() if args.output_folder else trend_path
    out_dir.mkdir(parents=True, exist_ok=True)

    json_file = out_dir / f"{trend_name}.json"
    log_file  = out_dir / f"{trend_name}.log"

    logger = setup_logger(log_file, debug=args.debug)
    logger.info("=" * 60)
    logger.info("Step 1 趋势报告解析 启动")
    logger.info(f"趋势名字      : {trend_name}")
    logger.info(f"趋势文件夹    : {trend_path}")
    logger.info(f"输出目录      : {out_dir}")

    # 收集趋势图（page_0.png, page_1.png, …），按数字排序
    trend_images: list[Path] = sorted(
        [f for f in trend_path.iterdir()
         if re.match(r"^page_\d+\.png$", f.name, re.IGNORECASE)],
        key=lambda p: int(re.search(r"\d+", p.stem).group()),  # type: ignore
    )
    if not trend_images:
        sys.exit(f"错误：趋势文件夹中未找到 page_N.png 文件 → {trend_path}")

    trend_list_str = ", ".join(f.name for f in trend_images)
    logger.info(f"趋势图数量    : {len(trend_images)}")
    logger.info(f"趋势图列表    : {trend_list_str}")

    # Prompt 加载
    md_path = Path(args.prompt_file).resolve() if args.prompt_file else PROMPT_MD_PATH
    logger.info(f"Prompt 文件   : {md_path}")
    system_prompt, user_template = load_step1_prompts(md_path)
    user_prompt = user_template.replace("{{趋势报告图列表}}", trend_list_str)
    logger.debug(f"System Prompt 完整内容:\n{system_prompt}")
    logger.debug(f"User Prompt 完整内容:\n{user_prompt}")

    if args.dry_run:
        dryrun_path = out_dir / f"{trend_name}.dryrun.txt"
        dryrun_path.write_text(user_prompt, encoding="utf-8")
        logger.info("Dry run 模式，已生成 user prompt，未调用 OpenAI API")
        logger.info(f"Prompt 已保存: {dryrun_path}")
        print()
        print("=" * 55)
        print("Dry run 完成，未实际调用 OpenAI API。")
        print(f"  Prompt 文件: {dryrun_path}")
        print("=" * 55)
        return

    # API 客户端
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("未找到 OpenAI API Key，请设置 OPENAI_API_KEY 环境变量或使用 --api-key 参数")
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    # 上传趋势图到 Files API
    uploaded_file_ids: list[str] = []
    upload_url = "https://api.openai.com/v1/files"
    logger.info(f"上传 URL: {upload_url}")
    logger.info(f"开始上传趋势图（共 {len(trend_images)} 张）…")

    try:
        # 进度行格式：backend trend importer 会监听 stdout 解析
        total = len(trend_images)
        for i, img_path in enumerate(trend_images):
            logger.info(f"上传: {img_path.name} ({img_path.stat().st_size / 1024:.1f} KB)")
            with open(img_path, "rb") as fh:
                file_obj = client.files.create(file=fh, purpose="vision")
            uploaded_file_ids.append(file_obj.id)
            logger.info(f"  → file_id: {file_obj.id}")
            print(f"[PROGRESS_LINE] stage=upload current={i + 1} total={total}", flush=True)

        logger.info(f"全部上传完成，共 {len(uploaded_file_ids)} 个 file_id")
        print(f"[PROGRESS_LINE] stage=upload_done total={total}", flush=True)

        # 组装 Responses API 请求
        responses_url = "https://api.openai.com/v1/responses"
        logger.info(f"推理 URL: {responses_url}")

        user_content: list[dict] = [{"type": "input_text", "text": user_prompt}]
        for fid in uploaded_file_ids:
            user_content.append({"type": "input_image", "file_id": fid})

        request_summary = {
            "url": responses_url,
            "model": args.model,
            "max_output_tokens": args.max_tokens,
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
            "file_ids": uploaded_file_ids,
        }
        logger.info(f"请求摘要:\n{json.dumps(request_summary, ensure_ascii=False, indent=2)}")

        logger.info("正在调用 Responses API（流式）…")
        t0 = datetime.now()

        raw_text = ""
        char_count = 0
        last_log_time = datetime.now()
        final_response = None

        last_progress_chars = 0
        print(f"[PROGRESS_LINE] stage=llm_start", flush=True)
        with client.responses.stream(  # type: ignore[attr-defined]
            model=args.model,
            instructions=system_prompt,
            input=[{"role": "user", "content": user_content}],  # type: ignore[arg-type]
            max_output_tokens=args.max_tokens,
        ) as stream:
            for event in stream:
                if getattr(event, "type", "") == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    raw_text += delta
                    char_count += len(delta)
                now = datetime.now()
                if (now - last_log_time).seconds >= 30:
                    logger.info(f"  推理中… 已接收 {char_count} 字符")
                    last_log_time = now
                # 每接收 ~1000 字符发一次进度行，让 backend SSE 能转推给前端
                if char_count - last_progress_chars >= 1000:
                    print(f"[PROGRESS_LINE] stage=llm_streaming chars={char_count}", flush=True)
                    last_progress_chars = char_count
            try:
                final_response = stream.get_final_response()  # type: ignore[attr-defined]
            except RuntimeError as e:
                logger.warning(f"未收到 completed 事件（{e}），使用已累积的 {char_count} 字符")
                final_response = None

        if not raw_text and final_response is not None:
            raw_text = getattr(final_response, "output_text", "") or ""

        elapsed = (datetime.now() - t0).total_seconds()
        logger.info(f"推理完成，耗时 {elapsed:.1f}s，共 {char_count} 字符")

        if not raw_text:
            raise RuntimeError("未收到任何模型输出，请重试")

        # 记录完整响应
        if final_response is not None:
            logger.debug(f"完整响应:\n{json.dumps(final_response.model_dump(), ensure_ascii=False, indent=2)}")
            usage = final_response.usage
            logger.info(f"token 用量: input={getattr(usage, 'input_tokens', '?')}, "
                        f"output={getattr(usage, 'output_tokens', '?')}")
        else:
            logger.warning("（流被截断，无完整响应元数据）")
            usage = None

        # 解析 Step 1 JSON
        step1_data = extract_step1_json(raw_text)
        if step1_data is None:
            logger.warning("未能解析 Step 1 JSON，raw_response 已保存")

        # 保存输出
        output = {
            "meta": {
                "趋势名字": trend_name,
                "趋势图列表": trend_list_str,
                "模型": args.model,
                "token用量": {
                    "input": getattr(usage, "input_tokens", None) if usage else None,
                    "output": getattr(usage, "output_tokens", None) if usage else None,
                },
                "耗时秒": round(elapsed, 2),
                "请求时间": t0.isoformat(),
                "file_ids": uploaded_file_ids,
            },
            "step1_趋势报告解析": step1_data,
            "raw_response": raw_text,
        }

        json_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"JSON 已保存: {json_file}")
        print(f"[PROGRESS_LINE] stage=done json={json_file}", flush=True)

    except Exception as exc:
        logger.error(f"调用失败: {exc}", exc_info=True)
        raise

    finally:
        if uploaded_file_ids:
            logger.info(f"清理 Files API 临时文件（{len(uploaded_file_ids)} 个）…")
            for fid in uploaded_file_ids:
                try:
                    client.files.delete(fid)
                    logger.debug(f"  已删除 {fid}")
                except Exception as e:
                    logger.warning(f"  删除 {fid} 失败（可忽略）: {e}")

    logger.info("=" * 60)
    print()
    print("=" * 55)
    print("Step 1 完成！")
    print(f"  JSON 输出  : {json_file}")
    print(f"  日志文件   : {log_file}")
    if step1_data is None:  # type: ignore[possibly-undefined]
        print("  ⚠ JSON 块解析失败，请查看 raw_response 字段")
    print("=" * 55)


if __name__ == "__main__":
    main()
