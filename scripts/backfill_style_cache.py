# -*- coding: utf-8 -*-
"""
款级缓存历史回填：把过往 run 里 2.1 款式分析 / 2.2 颜色识别的结果导入 runs/_style_cache/。

用法（项目根目录）：
    python scripts/backfill_style_cache.py           # 实际写入
    python scripts/backfill_style_cache.py --dry     # 只打印将写入什么

回填规则（与在线缓存 key 语义严格一致）：
  - 仅回填节点 prompt_version 为 "@current" 的结果（key 含当前 prompt 文件内容 hash，
    历史版本 prompt 的结果与当前 key 对不上，回填无意义）
  - key 用当前磁盘上的图片 md5——图片换过内容的自动对不上，安全跳过
  - 按 run 时间升序遍历，同 key 后写覆盖 → 最终留最新结果
  - 2.2 的 key 掺该款 2.1 输出的「款图底色」——用同 run 的 2.1 结果配对
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load_module(name: str, path: Path):
    """按文件路径加载模块——绕开 orchestrator 包 __init__（它会连带 import openai）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # dataclass 装饰器需要模块已注册
    spec.loader.exec_module(mod)
    return mod


StyleCache = _load_module("style_cache", ROOT / "tool" / "orchestrator" / "style_cache.py").StyleCache
load_prompt_file = _load_module("prompts_mod", ROOT / "tool" / "orchestrator" / "prompts.py").load_prompt_file

DRY = "--dry" in sys.argv

P21 = ROOT / "prompts" / "step2" / "01_style_analysis.md"
P22 = ROOT / "prompts" / "step2" / "02_color_recognition.md"
_s21, _t21 = load_prompt_file(P21)
PROMPT_21 = _s21 + "\n" + _t21
_s22, _t22 = load_prompt_file(P22)
PROMPT_22 = _s22 + "\n" + _t22

sc = StyleCache(ROOT / "runs" / "_style_cache")

_COLOR_PAT = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')


def _color_file_map(folder: Path) -> dict:
    """(code, name) → path（与 backend 同一套解析/清洗规则）"""
    out = {}
    if not folder.is_dir():
        return out
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            continue
        m = _COLOR_PAT.match(f.stem)
        if m:
            code = m.group(1).strip()
            raw = m.group(2).strip()
            name = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw).strip() or raw
            out[(code, name)] = f
        else:
            out[(f.stem, f.stem)] = f
    return out


def main() -> None:
    run_dirs = sorted(
        (d for d in ROOT.glob("runs/*/runs/*") if (d / "trace.jsonl").is_file()),
        key=lambda d: d.name,   # run_id 以时间戳开头，升序 = 时间序
    )
    n21 = n22 = skipped = 0

    for rd in run_dirs:
        # fixture meta（款图/色号目录/成套 styles）
        try:
            first = json.loads((rd / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
        except Exception:
            continue
        fixture = first.get("fixture") or {}
        model = "gpt-5.5"
        fj = rd / "final_output.json"
        if fj.is_file():
            try:
                model = (json.loads(fj.read_text(encoding="utf-8")).get("meta") or {}).get("模型") or model
            except Exception:
                pass

        # 款位表：suffix（文件名里的款号）→ (style_no, ref_image, color_folder)
        slots = {}
        primary_ref = Path(fixture.get("ref_image") or fixture.get("ref_image_path") or "")
        if primary_ref.name:
            primary_style = primary_ref.stem
            primary_folder = Path(fixture.get("color_folder") or (primary_ref.parent / primary_ref.stem))
            slots[""] = (primary_style, primary_ref, primary_folder)
            slots[primary_style] = slots[""]
        for s in fixture.get("styles") or []:
            rp = Path(s.get("ref_image_path") or "")
            if rp.name:
                slots[rp.stem] = (rp.stem, rp, rp.parent / rp.stem)

        # 该 run 各款的 2.1 输出（2.2 的 base_color 要用）
        base_color_by_style = {}

        for f in sorted(rd.glob("2_1_*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("status") != "succeeded" or not (d.get("prompt_version") or "").endswith("@current"):
                skipped += 1
                continue
            out = d.get("output") or {}
            if not out or out.get("dry_run"):
                continue
            # 文件名后缀定位款位：2_1_001_款式分析_YK250609-牛仔服.json / 2_1_001_款式分析.json
            m = re.search(r"款式分析_?(.*)$", f.stem)
            suffix = (m.group(1) if m else "").strip("_")
            slot = slots.get(suffix) or slots.get("")
            if not slot or not slot[1].is_file():
                skipped += 1
                continue
            style_no, ref_img, _folder = slot
            base_color_by_style[style_no] = (out.get("款式分析") or {}).get("款图底色") or {}
            if not DRY:
                sc.put_style_analysis(style_no, ref_img, PROMPT_21, model, out)
            n21 += 1

        for f in sorted(rd.glob("2_2_0*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("status") != "succeeded" or not (d.get("prompt_version") or "").endswith("@current"):
                skipped += 1
                continue
            out = d.get("output") or {}
            if not out or out.get("_dry_run") or out.get("_recognition_failed"):
                continue
            code = out.get("色号代码") or ""
            name = out.get("营销色名") or ""
            # 文件名尾部若带款号后缀（成套），定位对应款位；否则主款
            suffix = ""
            for k in slots:
                if k and f.stem.endswith(k):
                    suffix = k
                    break
            slot = slots.get(suffix) or slots.get("")
            if not slot:
                skipped += 1
                continue
            style_no, _ref, folder = slot
            img = _color_file_map(folder).get((code, name))
            if img is None or not img.is_file():
                skipped += 1
                continue
            base_color = base_color_by_style.get(style_no) or {}
            if not DRY:
                sc.put_color_recognition(style_no, img, PROMPT_22, model, base_color, out)
            n22 += 1

    mode = "[DRY-RUN 预览] " if DRY else ""
    print(f"{mode}回填完成：款式分析 {n21} 条，颜色识别 {n22} 条，跳过 {skipped} 条"
          f"（非 current prompt / dry-run / 图片缺失）")
    if not DRY:
        stats = sc.stats()
        print(f"款级缓存现有 {len(stats)} 个款：")
        for s in stats:
            print(f"  {s['style_no']}: 款式分析×{s['analysis_count']} 颜色识别×{s['color_count']}")


if __name__ == "__main__":
    main()
