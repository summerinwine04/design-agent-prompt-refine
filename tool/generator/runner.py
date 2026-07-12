"""
GenerationRunner —— 生图任务执行器

复用 ai-supply step3_generate.py 的核心逻辑：
  - 款图 / 色号图字节预读入内存
  - 是否需要变色判断（决定上传 1 张还是 2 张图）
  - 自动从款图比例匹配 gpt-image-2 三档（1024² / 1024×1536 / 1536×1024）
  - 并发提交（ThreadPoolExecutor）

跟原 step3 区别：
  - 不依赖 CLI args，接受 task 配置对象
  - 每张图开始/完成/失败时调用 event_callback（推到 SSE）
  - 单张失败不影响其他图（独立 try/except）
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    from openai import OpenAI
except ImportError:
    raise ImportError("请先安装依赖: pip install openai")


# --------------------------------------------------------------------------- #
# 系统级 layout 兜底
# --------------------------------------------------------------------------- #
#
# 强制追加到每条图生图 prompt 末尾的"输出布局"约束。
# 目的：解决 gpt-image-2 倾向于把正面/背面视图压在一起、袖口重叠的问题。
# 通过 marker 实现幂等：重生（regenerate）时复用旧 image_prompt 不会重复追加。

_LAYOUT_GUARD_MARKER = "[OUTPUT LAYOUT — MANDATORY]"
_LAYOUT_GUARD = (
    f"\n\n{_LAYOUT_GUARD_MARKER} Render the result as the same side-by-side dual-view as Image 1: "
    f"the front-view garment on the LEFT half of the canvas, the back-view garment on the RIGHT half. "
    f"BOTH garments must be shown 100% intact and fully visible — NO cropping, NO truncation, "
    f"NO cutting off of any part. SPECIFICALLY: each garment must display BOTH of its own sleeves "
    f"in full, including both cuffs. Across the entire canvas you must see exactly 4 sleeves total "
    f"(2 sleeves on the front-view garment + 2 sleeves on the back-view garment), all complete from "
    f"shoulder to cuff. If the canvas feels tight, SHRINK each garment uniformly to fit — never crop. "
    f"Keep a clear visible empty gap (at least 5% of canvas width) between the two garments. "
    f"The inner edges (front garment's right side + back garment's left side) must each show their "
    f"OWN complete sleeve — NOT a half garment, NOT a hidden sleeve. "
    f"NO part of either garment may touch, overlap, or share boundary with the other. "
    f"Background between and around both views must remain plain/empty."
)


def _ensure_layout_guard(prompt: str) -> str:
    """幂等地把 _LAYOUT_GUARD append 到 prompt 末尾。已含 marker 则跳过。"""
    if _LAYOUT_GUARD_MARKER in prompt:
        return prompt
    return prompt.rstrip() + _LAYOUT_GUARD


# --------------------------------------------------------------------------- #
# 图像尺寸检测（沿用 step3_generate.py）
# --------------------------------------------------------------------------- #

def _read_image_size_pil(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def _read_image_size_header(path: Path):
    try:
        data = path.read_bytes()
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w = struct.unpack('>I', data[16:20])[0]
            h = struct.unpack('>I', data[20:24])[0]
            return w, h
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
    (1024, 1024),
    (1024, 1536),
    (1536, 1024),
]


def get_aligned_size(path: Path) -> str:
    size = _read_image_size_pil(path) or _read_image_size_header(path)
    if size is None:
        return "1024x1024"
    img_w, img_h = size
    img_ratio = img_w / img_h if img_h else 1.0
    best = min(_SUPPORTED_SIZES, key=lambda s: abs(s[0] / s[1] - img_ratio))
    return f"{best[0]}x{best[1]}"


# --------------------------------------------------------------------------- #
# 单张图生图任务
# --------------------------------------------------------------------------- #

@dataclass
class PlanTask:
    """一张图的执行单元。"""
    plan_id: str
    color_code: str
    color_name: str
    needs_recolor: bool
    image_prompt: str          # 七段式英文 prompt
    plan_desc: str             # 方案说明
    gender: str
    out_path: Path             # 图片落盘路径
    role: str = ""             # v5 成套模式：top/bottom（空 = 用任务级默认款图）

    # 运行时填充
    status: str = "pending"    # pending|running|succeeded|failed
    image_url: str = ""        # 前端可访问的 URL
    elapsed_ms: int = 0
    error: str = ""
    started_at: float = 0.0
    # 生图 API 返回的 usage（gpt-image 系列按 token 计入/出；老任务无此数据为 0）
    tokens_in: int = 0
    tokens_out: int = 0


# --------------------------------------------------------------------------- #
# Cost 估算
# --------------------------------------------------------------------------- #

# gpt-image-2 大致价格（USD per image）。可能因尺寸而变
COST_PER_IMAGE = {
    "1024x1024": 0.04,
    "1024x1536": 0.06,
    "1536x1024": 0.06,
}


def estimate_cost(size: str, n_images: int) -> float:
    return COST_PER_IMAGE.get(size, 0.04) * n_images


# --------------------------------------------------------------------------- #
# GenerationRunner
# --------------------------------------------------------------------------- #

@dataclass
class GenerationRunner:
    """
    一次生图任务。在线程池里跑，通过 event_callback 推事件给 SSE。

    用法（在线程里）：
        runner = GenerationRunner(...)
        runner.run()  # 阻塞直到所有图完成
    """
    task_id: str
    api_key: str
    ref_image_path: Path                       # 款图正反面
    color_folder: Path                          # 色号图所在目录
    plans: list[dict]                           # step2 选中的方案 list[plan dict]
    image_root_dir: Path                        # 图片落盘目录
    color_filename_map: dict[tuple[str, str], str] = field(default_factory=dict)
    concurrency: int = 2
    model: str = "gpt-image-2"
    event_callback: Callable[[dict], None] | None = None
    # 重生时：True = 先把旧图复制到 image_root_dir/_archive/ 再覆盖，False = 直接覆盖
    keep_original: bool = False
    # v5 成套模式：按 role 提供各自的款图/色号资产，同一任务里混装上下装方案。
    # role → {"ref_image_path": Path, "color_folder": Path, "color_filename_map": {(code,name): fname}}
    # 方案 dict 带 "role" 字段时用对应资产；没有或 role 缺失时回落到任务级默认。
    role_assets: dict = field(default_factory=dict)

    # 内部状态
    _tasks: list[PlanTask] = field(default_factory=list, init=False)
    _client: OpenAI = field(init=False, default=None)
    _garment_bytes: bytes = field(init=False, default=b"")
    _garment_bytes_by_role: dict = field(init=False, default_factory=dict)
    _color_bytes_map: dict[tuple, bytes] = field(init=False, default_factory=dict)
    _size: str = field(init=False, default="1024x1024")
    _size_by_role: dict = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.image_root_dir = Path(self.image_root_dir)
        self.image_root_dir.mkdir(parents=True, exist_ok=True)

    # ------------------- 主入口 ------------------- #

    def run(self) -> dict:
        """阻塞执行；返回 results dict。"""
        self._emit("task_started", {"task_id": self.task_id, "total": len(self.plans)})
        t0 = time.time()

        # 1. 预读取所有图字节
        try:
            self._prepare_bytes()
        except Exception as exc:
            err = f"预读图片失败：{exc}"
            self._emit("task_failed", {"error": err})
            return {"status": "failed", "error": err, "tasks": []}

        # 2. 计算尺寸（基于款图比例）；成套模式按 role 各算一档
        self._size = get_aligned_size(self.ref_image_path)
        for role, assets in (self.role_assets or {}).items():
            self._size_by_role[role] = get_aligned_size(Path(assets["ref_image_path"]))
        self._emit("task_size_chosen", {"size": self._size, "size_by_role": self._size_by_role})

        # 3. 构造 PlanTask 列表
        self._tasks = self._build_plan_tasks()

        # 4. 初始化 OpenAI client（懒）
        self._client = OpenAI(api_key=self.api_key)

        # 5. 并发提交
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            future_to_task = {
                pool.submit(self._generate_one, t): t
                for t in self._tasks
            }
            for fut in as_completed(future_to_task):
                task = future_to_task[fut]
                try:
                    fut.result()
                except Exception as exc:
                    task.status = "failed"
                    task.error = str(exc)
                    self._emit("plan_failed", self._task_to_event(task))

        # 6. 汇总
        total_ms = int((time.time() - t0) * 1000)
        succeeded = sum(1 for t in self._tasks if t.status == "succeeded")
        failed = sum(1 for t in self._tasks if t.status == "failed")
        status = "completed" if failed == 0 else ("partial" if succeeded > 0 else "failed")

        results = [
            {
                "plan_id": t.plan_id,
                "color_code": t.color_code,
                "color_name": t.color_name,
                "status": t.status,
                "image_path": str(t.out_path) if t.status == "succeeded" else None,
                "image_url": t.image_url,
                "image_prompt_used": t.image_prompt,
                "elapsed_ms": t.elapsed_ms,
                "error": t.error or None,
                "tokens_in": t.tokens_in,
                "tokens_out": t.tokens_out,
            }
            for t in self._tasks
        ]
        total_cost = estimate_cost(self._size, succeeded)

        self._emit("task_finished", {
            "status": status,
            "succeeded": succeeded,
            "failed": failed,
            "total_elapsed_ms": total_ms,
            "total_cost_usd": total_cost,
        })

        return {
            "status": status,
            "results": results,
            "total_elapsed_ms": total_ms,
            "total_cost_usd": total_cost,
        }

    # ------------------- 内部 ------------------- #

    def _prepare_bytes(self) -> None:
        """预读款图 + 所有色号图的字节到内存（沿用 step3 的零文件 API 策略）。

        成套模式：按 role 各读一份款图；色号图从该 role 自己的文件夹/映射取，
        字节缓存 key 带 role 前缀避免上下装同名色号互相覆盖。
        """
        self._garment_bytes = Path(self.ref_image_path).read_bytes()
        for role, assets in (self.role_assets or {}).items():
            self._garment_bytes_by_role[role] = Path(assets["ref_image_path"]).read_bytes()

        for plan in self.plans:
            needs = plan.get("是否需要变色", False)
            if not needs:
                continue
            code = plan.get("_色号代码") or plan.get("色号代码", "")
            name = plan.get("_营销色名") or plan.get("营销色名", "")
            role = plan.get("role") or ""
            key = (role, code, name)
            if key in self._color_bytes_map:
                continue
            assets = (self.role_assets or {}).get(role)
            if assets:
                fname = assets.get("color_filename_map", {}).get((code, name))
                folder = Path(assets["color_folder"])
            else:
                fname = self.color_filename_map.get((code, name))
                folder = Path(self.color_folder)
            if fname:
                fpath = folder / fname
                if fpath.is_file():
                    self._color_bytes_map[key] = fpath.read_bytes()

    def _build_plan_tasks(self) -> list[PlanTask]:
        out: list[PlanTask] = []
        for plan in self.plans:
            plan_id = plan.get("方案编号", "")
            # 兼容 _色号代码 (来自 step2 _flatten_plans) 或 色号代码
            code = plan.get("_色号代码") or plan.get("色号代码", "")
            name = plan.get("_营销色名") or plan.get("营销色名", "")
            gender = plan.get("_性别定向") or plan.get("性别定向", "中性")
            needs = plan.get("是否需要变色", False)
            image_prompt = plan.get("图生图prompt", "")
            plan_desc = plan.get("方案说明", "")
            out_path = self.image_root_dir / f"{plan_id}.png"
            out.append(PlanTask(
                plan_id=plan_id,
                color_code=code,
                color_name=name,
                needs_recolor=needs,
                image_prompt=image_prompt,
                plan_desc=plan_desc,
                gender=gender,
                out_path=out_path,
                role=plan.get("role") or "",
            ))
        return out

    def _generate_one(self, task: PlanTask) -> None:
        """单张图生成。阻塞调用。"""
        task.started_at = time.time()
        task.status = "running"
        self._emit("plan_started", self._task_to_event(task))

        # 准备图字节（成套模式按 role 取对应款图；否则任务级默认）
        try:
            garment_bytes = self._garment_bytes_by_role.get(task.role, self._garment_bytes)
            if task.needs_recolor:
                key = (task.role, task.color_code, task.color_name)
                color_bytes = self._color_bytes_map.get(key)
                if color_bytes is None:
                    raise FileNotFoundError(
                        f"色号图字节未预加载：{key}（可能没有匹配的文件）"
                    )
                images = [
                    ("garment.jpg", io.BytesIO(garment_bytes), "image/jpeg"),
                    (
                        f"{task.color_code}-{task.color_name}.jpg",
                        io.BytesIO(color_bytes),
                        "image/jpeg",
                    ),
                ]
            else:
                images = [
                    ("garment.jpg", io.BytesIO(garment_bytes), "image/jpeg")
                ]

            # 系统级 layout 兜底：强制 append 双视图分离约束（幂等）
            # 同时更新 task.image_prompt 让 image_prompt_used 落盘记录真实发给 API 的版本
            task.image_prompt = _ensure_layout_guard(task.image_prompt)

            result = self._client.images.edit(
                model=self.model,
                image=images if len(images) > 1 else images[0],
                prompt=task.image_prompt,
                n=1,
                size=self._size_by_role.get(task.role, self._size),
            )

            if not result or not result.data:
                raise RuntimeError("API 返回空 data")

            # 捕获 usage token（gpt-image 系列返回 input_tokens/output_tokens；异常兜 0）
            try:
                usage = getattr(result, "usage", None)
                if usage:
                    task.tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
                    task.tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
            except Exception:
                pass

            img_item = result.data[0]
            b64 = getattr(img_item, "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
            else:
                url = getattr(img_item, "url", None)
                if not url:
                    raise RuntimeError("API 未返回 b64_json 也未返回 url")
                import urllib.request
                with urllib.request.urlopen(url) as resp:
                    img_bytes = resp.read()

            # 重生模式下若开启 keep_original 且原图存在，先归档到 _archive/ 子目录再覆盖
            if self.keep_original and task.out_path.exists():
                try:
                    import shutil
                    archive_dir = self.image_root_dir / "_archive"
                    archive_dir.mkdir(exist_ok=True)
                    ts = int(time.time() * 1000)
                    archive_path = archive_dir / f"{task.plan_id}_{ts}.png"
                    shutil.copy2(task.out_path, archive_path)
                    self._emit("plan_archived", {
                        "plan_id": task.plan_id,
                        "archive_path": str(archive_path),
                    })
                except Exception as arch_exc:
                    # 归档失败不影响主流程，只发警告事件
                    self._emit("plan_archive_failed", {
                        "plan_id": task.plan_id,
                        "error": f"{type(arch_exc).__name__}: {arch_exc}",
                    })

            task.out_path.write_bytes(img_bytes)
            # 前端访问 URL（与 FastAPI 静态挂载对齐）
            task.image_url = f"/static/tasks/{self.task_id}/{task.out_path.name}"
            task.status = "succeeded"
            task.elapsed_ms = int((time.time() - task.started_at) * 1000)
            self._emit("plan_succeeded", self._task_to_event(task))

        except Exception as exc:
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.elapsed_ms = int((time.time() - task.started_at) * 1000)
            self._emit("plan_failed", self._task_to_event(task))

    def _task_to_event(self, task: PlanTask) -> dict:
        return {
            "plan_id": task.plan_id,
            "color_code": task.color_code,
            "color_name": task.color_name,
            "status": task.status,
            "image_url": task.image_url,
            "elapsed_ms": task.elapsed_ms,
            "error": task.error,
        }

    def _emit(self, event: str, payload: dict) -> None:
        if self.event_callback:
            try:
                self.event_callback({"event": event, **payload})
            except Exception:
                # event_callback 异常不能干扰主流程
                pass
