r"""
2.4.5 母图案 Blueprint prompt spike —— 独立可执行

从 inputs/ 读:
  - style_analysis.json         (从真实 run 抽取)
  - color_recognition.json      (从真实 run 抽取，精简)
  - selected_pattern_refs.txt   (用户预筛选的 1-3 张图路径)

调 gpt-5.5 with 图片 → 出 blueprint JSON → 落到 outputs/

用法：
  cd prompt-refine-agent
  .\.venv\Scripts\Activate.ps1
  python spikes\blueprint_prompt_v1\run_spike.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# 让脚本能找到 orchestrator 包（借用现成的 LLMClient）
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tool"))

SPIKE_DIR = Path(__file__).resolve().parent
INPUTS = SPIKE_DIR / "inputs"
OUTPUTS = SPIKE_DIR / "outputs"
OUTPUTS.mkdir(exist_ok=True)


def _load_env():
    """跟 backend 一样简单 .env 加载"""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        print(f"[!] .env not found at {env_file}", flush=True)
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _render_prompt(tpl: str, vars: dict) -> str:
    out = tpl
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _split_system_user(md_text: str) -> tuple[str, str]:
    """从 blueprint_prompt.md 里切出 SYSTEM 段和 USER TEMPLATE 段。"""
    sys_marker = "═══ SYSTEM PROMPT ═══"
    usr_marker = "═══ USER PROMPT TEMPLATE ═══"
    if sys_marker not in md_text or usr_marker not in md_text:
        raise ValueError("prompt.md 缺少 SYSTEM/USER 分隔符")
    sys_start = md_text.index(sys_marker) + len(sys_marker)
    usr_start = md_text.index(usr_marker) + len(usr_marker)
    sys_part = md_text[sys_start:md_text.index(usr_marker)].strip()
    usr_part = md_text[usr_start:].strip()
    return sys_part, usr_part


def _load_selected_refs() -> list[Path]:
    """读 selected_pattern_refs.txt，返回图路径列表"""
    p = INPUTS / "selected_pattern_refs.txt"
    if not p.is_file():
        raise FileNotFoundError(p)
    refs: list[Path] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rp = Path(line)
        if not rp.is_file():
            print(f"[!] 参考图文件不存在: {rp}", flush=True)
            continue
        refs.append(rp)
    if not refs:
        raise RuntimeError("selected_pattern_refs.txt 里没有可用的图路径")
    if len(refs) > 3:
        print(f"[!] 参考图 {len(refs)} 张，截断到前 3 张（Phase 1 硬上限）", flush=True)
        refs = refs[:3]
    return refs


def _extract_json_after_marker(raw: str, marker_regex: str) -> dict | None:
    """从 raw text 里找 marker，把后面的裸 JSON 解析出来"""
    m = re.search(marker_regex, raw)
    if not m:
        return None
    tail = raw[m.end():]
    # 找第一个 { 开始
    brace_start = tail.find("{")
    if brace_start < 0:
        return None
    # 用 counter 找匹配的 }
    depth = 0
    end = None
    for i, ch in enumerate(tail[brace_start:], start=brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(tail[brace_start:end])
    except Exception as e:
        print(f"[!] JSON parse failed: {e}", flush=True)
        return None


def main():
    _load_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[FATAL] OPENAI_API_KEY 未设置。检查 .env", file=sys.stderr)
        sys.exit(1)

    # 1. 读 prompt
    prompt_md = (SPIKE_DIR / "blueprint_prompt.md").read_text(encoding="utf-8")
    system_prompt, user_tpl = _split_system_user(prompt_md)

    # 2. 读输入
    style_analysis = json.loads((INPUTS / "style_analysis.json").read_text(encoding="utf-8"))
    color_recognition = json.loads((INPUTS / "color_recognition.json").read_text(encoding="utf-8"))
    ref_paths = _load_selected_refs()

    # 3. 图库上下文（从第一张图的父目录名解析）
    folder_name = ref_paths[0].parent.name           # 如 "山系户外童装花型TOP热榜-植物脉络方向"
    if "-" in folder_name:
        parent_topic, sub_topic = folder_name.split("-", 1)
    else:
        parent_topic, sub_topic = folder_name, folder_name

    # 4. render user prompt
    user_prompt = _render_prompt(user_tpl, {
        "母主题": parent_topic,
        "子主题": sub_topic,
        "核心参考图张数": str(len(ref_paths)),
        "款式分析JSON": json.dumps(style_analysis, ensure_ascii=False, indent=2),
        "色号数": str(len(color_recognition)),
        "色号识别汇总JSON": json.dumps(color_recognition, ensure_ascii=False, indent=2),
    })

    # 校验没有未解析变量
    unresolved = re.findall(r"\{\{[^{}]+\}\}", user_prompt)
    if unresolved:
        print(f"[FATAL] user prompt 有未解析变量: {unresolved}", file=sys.stderr)
        sys.exit(1)

    # 5. 起 LLMClient 调用
    from orchestrator.llm import LLMClient
    model = os.environ.get("DEFAULT_MODEL", "gpt-5.5")
    max_tokens = int(os.environ.get("DEFAULT_MAX_TOKENS", "500000"))
    llm = LLMClient(api_key=api_key, default_model=model, default_max_tokens=max_tokens)

    print("=" * 60)
    print(f"model:           {model}")
    print(f"folder:          {folder_name}")
    print(f"reference imgs:  {len(ref_paths)}")
    for p in ref_paths:
        print(f"    - {p.name}")
    print(f"style keys:      {list(style_analysis.keys())}")
    print(f"colors:          {len(color_recognition)}")
    print(f"prompt chars:    system={len(system_prompt)}  user={len(user_prompt)}")
    print("=" * 60)
    print("→ calling OpenAI ...", flush=True)

    t0 = time.time()
    def _cb(delta):
        # 流式简易打印，只显示进度感
        print(".", end="", flush=True)

    result = llm.call_with_images(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        image_paths=[str(p) for p in ref_paths],
        output_marker_regex=r"##\s*STEP\s*2\.4\.5\s*OUTPUT",
        model=model,
        max_tokens=max_tokens,
        stream_callback=_cb,
        dry_run=False,
    )
    elapsed = time.time() - t0
    print(f"\n← done in {elapsed:.1f}s\n")

    # 6. 尝试解析 blueprint
    blueprint = _extract_json_after_marker(result.raw_text, r"##\s*STEP\s*2\.4\.5\s*OUTPUT")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUTS / f"run_{ts}.json"
    summary = {
        "spike_id": f"run_{ts}",
        "elapsed_sec": round(elapsed, 1),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "model": model,
        "folder": folder_name,
        "reference_imgs": [p.name for p in ref_paths],
        "blueprint_parsed_ok": blueprint is not None,
        "blueprint": blueprint,
        "raw_text": result.raw_text,
    }
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[✓] output → {out_path}")

    # 7. 快速人肉判定提示
    print("\n--- 快速判定 ---")
    if blueprint is None:
        print("[✗] blueprint 未成功解析。看 raw_text 找 JSON 语法错。")
        return
    print("[✓] blueprint JSON 解析成功")
    concept = blueprint.get("母图案概念", "")
    anchors_fixed = (blueprint.get("核心元素锚点") or {}).get("固定不变的元素", []) or []
    anchors_var = (blueprint.get("核心元素锚点") or {}).get("允许变体的元素", []) or []
    colors = blueprint.get("母配色关系") or {}
    placements = (blueprint.get("placement候选池") or {}).get("main", []) or []
    print(f"  母图案概念: {concept[:80]}{'...' if len(concept) > 80 else ''}")
    print(f"  固定不变元素: {len(anchors_fixed)} 项 → {anchors_fixed[:3]}")
    print(f"  允许变体元素: {len(anchors_var)} 项 → {anchors_var[:3]}")
    print(f"  母配色 roles: {list(colors.keys())}")
    print(f"  placement 池: {len(placements)} 项")
    print("\n判定通过标准：")
    print(f"  [{'✓' if 10 <= len(concept) <= 120 else '✗'}] 母图案概念 10-120 字")
    print(f"  [{'✓' if 2 <= len(anchors_fixed) <= 4 else '✗'}] 固定不变元素 2-4 项")
    print(f"  [{'✓' if 2 <= len(anchors_var) <= 4 else '✗'}] 允许变体元素 2-4 项")
    print(f"  [{'✓' if len(colors) >= 3 else '✗'}] 母配色 ≥ 3 roles")
    print(f"  [{'✓' if len(placements) >= 4 else '✗'}] placement 候选池 ≥ 4 项")


if __name__ == "__main__":
    main()
