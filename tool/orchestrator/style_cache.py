"""
款级持久缓存（style-level cache）

以「款」为主体，跨 run / 跨趋势 / 跨设计模式复用 2.1 款式分析、2.2 颜色识别的结果。

目录布局：
  runs/_style_cache/{style_no}/
    style_analysis.{key}.json           # key = md5(款图) + md5(prompt内容) + 模型
    color.{色号图文件名}.{key}.json      # key = md5(色号图) + md5(prompt内容) + 模型 + md5(款图底色字段)

失效语义（全部编码在 key 里，不需要手动清）：
  - 换款图/色号图（内容变，同名也算）→ 文件 md5 变 → miss
  - 改 2.1/2.2 prompt（含覆盖 current）→ prompt 内容 hash 变 → miss
  - 换模型 → miss
  - 2.1 重跑后款图底色字段变了 → 全部 2.2 级联 miss；底色没变则 2.2 继续命中

所有读写异常吞掉（缓存层永不阻断真跑）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_text(*parts: str) -> str:
    return hashlib.md5("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


class StyleCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, style_no: str) -> Path:
        return self.root / _safe_name(style_no)

    # ------------------- 2.1 款式分析 ------------------- #

    def _analysis_key(self, ref_image_path: Path, prompt_text: str, model: str | None) -> str:
        return _md5_text(_md5_file(Path(ref_image_path)), _md5_text(prompt_text), model or "")[:16]

    def get_style_analysis(
        self, style_no: str, ref_image_path: Path, prompt_text: str, model: str | None,
    ) -> dict | None:
        try:
            key = self._analysis_key(ref_image_path, prompt_text, model)
            f = self._dir(style_no) / f"style_analysis.{key}.json"
            if not f.is_file():
                return None
            return json.loads(f.read_text(encoding="utf-8")).get("output")
        except Exception:
            return None

    def put_style_analysis(
        self, style_no: str, ref_image_path: Path, prompt_text: str, model: str | None, output: dict,
    ) -> None:
        try:
            key = self._analysis_key(ref_image_path, prompt_text, model)
            d = self._dir(style_no)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"style_analysis.{key}.json").write_text(
                json.dumps({
                    "output": output,
                    "meta": {
                        "node": "2.1",
                        "ref_image": str(ref_image_path),
                        "model": model,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------- 2.2 颜色识别 ------------------- #

    def _color_key(
        self, color_image_path: Path, prompt_text: str, model: str | None, base_color: dict,
    ) -> str:
        base_hash = _md5_text(json.dumps(base_color or {}, ensure_ascii=False, sort_keys=True))
        return _md5_text(_md5_file(Path(color_image_path)), _md5_text(prompt_text), model or "", base_hash)[:16]

    def _color_file(self, style_no: str, color_image_path: Path, key: str) -> Path:
        fname = _safe_name(Path(color_image_path).name)
        return self._dir(style_no) / f"color.{fname}.{key}.json"

    def get_color_recognition(
        self, style_no: str, color_image_path: Path, prompt_text: str,
        model: str | None, base_color: dict,
    ) -> dict | None:
        try:
            key = self._color_key(color_image_path, prompt_text, model, base_color)
            f = self._color_file(style_no, color_image_path, key)
            if not f.is_file():
                return None
            return json.loads(f.read_text(encoding="utf-8")).get("output")
        except Exception:
            return None

    def put_color_recognition(
        self, style_no: str, color_image_path: Path, prompt_text: str,
        model: str | None, base_color: dict, output: dict,
    ) -> None:
        try:
            key = self._color_key(color_image_path, prompt_text, model, base_color)
            d = self._dir(style_no)
            d.mkdir(parents=True, exist_ok=True)
            self._color_file(style_no, color_image_path, key).write_text(
                json.dumps({
                    "output": output,
                    "meta": {
                        "node": "2.2",
                        "color_image": str(color_image_path),
                        "model": model,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def invalidate_color(self, style_no: str, color_filename: str) -> int:
        """删掉某色号图的全部缓存条目（单色重识别修复后调用，避免旧识别结果继续被复用）。"""
        n = 0
        try:
            d = self._dir(style_no)
            if not d.is_dir():
                return 0
            fname = _safe_name(color_filename)
            for f in d.glob(f"color.{fname}.*.json"):
                f.unlink()
                n += 1
        except Exception:
            pass
        return n

    # ------------------- 管理（设置页消费） ------------------- #

    def stats(self) -> list[dict]:
        """逐款统计缓存条目，供 GET /settings/style-cache。"""
        out: list[dict] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            analysis_files = list(d.glob("style_analysis.*.json"))
            color_files = list(d.glob("color.*.json"))
            all_files = analysis_files + color_files
            if not all_files:
                continue
            out.append({
                "style_no": d.name,
                "analysis_count": len(analysis_files),
                "color_count": len(color_files),
                "total_bytes": sum(f.stat().st_size for f in all_files),
                "updated_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(max(f.stat().st_mtime for f in all_files)),
                ),
            })
        return out

    def clear(self, style_no: str | None = None) -> int:
        """清缓存。style_no=None 清全部。返回删除文件数。"""
        n = 0
        try:
            targets = [self._dir(style_no)] if style_no else (
                [d for d in self.root.iterdir() if d.is_dir()] if self.root.is_dir() else []
            )
            for d in targets:
                if not d.is_dir():
                    continue
                for f in d.glob("*.json"):
                    f.unlink()
                    n += 1
                try:
                    d.rmdir()
                except OSError:
                    pass
        except Exception:
            pass
        return n
