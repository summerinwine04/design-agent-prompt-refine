"""
/api/v1/styles — 款图库（浏览 + 管理，见 docs/款图库管理_产品方案PRD.md v1.3）

指向 ai-supply/款图/ 或 STYLES_ROOT 环境变量。文件夹是图片事实来源；
品类/上传时间存 SQLite style_meta（backend/db.py）。

目录约定：
  - 款级双视角图：{款号}.jpg —— 一张图含正面+背面视图（step 2.1 必需输入）
  - 色号正面图  ：{款号}/{色号}（{色名}）.{ext}
  - 色号背面图  ：{款号}/{色号}（{色名}）_back.{ext}   （扫描色号时过滤）

URL 字段约定（由 main.py 把 /static/styles 静态挂载到款图根目录实现）。

接口：
  GET    /styles                    列表（扫描 + JOIN style_meta；含 folder-only 款）
  GET    /styles/{style_no}         详情（colors 含 back_url）
  POST   /styles/upload             新款入库 / 追加色号（multipart，正反面必填）
  PATCH  /styles/{style_no}/meta    编辑/补录品类
  POST   /styles/{style_no}/cover   更换款级双视角图（选色号重拼 或 直传现成图）
  POST   /styles/{style_no}/back    按色号补背面图
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.db import (
    get_style_meta,
    list_style_meta,
    upsert_style_meta,
)

router = APIRouter()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_UPLOAD = 20 * 1024 * 1024   # 20MB，对齐选款中心上传限制

CATEGORY_MAIN = {"童装", "男装", "女装", "运动", "男睡衣", "女睡衣"}
CATEGORY_SUB = {"top", "bottom", "onepiece"}

_BACK_SUFFIX = "_back"
_COLOR_PATTERN = re.compile(r'^([^(（]+)[（(]([^)）]+)[)）]')


def _styles_root() -> Path:
    env = os.environ.get("STYLES_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).parent.parent.parent.resolve()
    return here.parent / "ai-supply" / "款图"


# ============================================================================
# 扫描辅助
# ============================================================================

def _is_image(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS


def _is_back(p: Path) -> bool:
    return p.stem.endswith(_BACK_SUFFIX)


def _parse_color(stem: str) -> tuple[str, str]:
    """从文件名 stem 解析（色号, 色名）——与历史括号规则一致。"""
    m = _COLOR_PATTERN.match(stem)
    if m:
        code = m.group(1).strip()
        raw_label = m.group(2).strip()
        label = re.sub(r'(已使用|不用|备用|未用|待定).*$', '', raw_label).strip() or raw_label
        return code, label
    return stem, stem


def _scan_colors(color_dir: Path) -> list[dict]:
    """扫描色号文件夹：正面图为主体，_back 文件挂到对应色号的 back_url。"""
    if not color_dir.is_dir():
        return []
    fronts: list[Path] = []
    backs: dict[str, Path] = {}   # 去掉 _back 后的 stem → 背面文件
    for f in sorted(color_dir.iterdir()):
        if not _is_image(f):
            continue
        if _is_back(f):
            backs[f.stem[: -len(_BACK_SUFFIX)]] = f
        else:
            fronts.append(f)
    style_no = color_dir.name
    colors = []
    for f in fronts:
        code, label = _parse_color(f.stem)
        back = backs.get(f.stem)
        colors.append({
            "filename": f.name,
            "code": code,
            "name": label,
            "path": str(f),
            "url": f"/static/styles/{style_no}/{f.name}",
            "back_filename": back.name if back else None,
            "back_url": f"/static/styles/{style_no}/{back.name}" if back else None,
        })
    return colors


def _list_style_nos(root: Path) -> list[str]:
    """全部款号 = 根目录图片 stem ∪ 子文件夹名（folder-only 款 = 缺封面）。"""
    nos: set[str] = set()
    for f in root.iterdir():
        if _is_image(f):
            nos.add(f.stem)
        elif f.is_dir():
            nos.add(f.name)
    return sorted(nos)


def _ref_file(root: Path, style_no: str) -> Path | None:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        f = root / f"{style_no}{ext}"
        if f.is_file():
            return f
    return None


# ============================================================================
# 只读接口
# ============================================================================

@router.get("")
async def list_styles():
    root = _styles_root()
    if not root.is_dir():
        return {"root": str(root), "styles": [], "error": "STYLES_ROOT not found"}
    meta_map = list_style_meta()
    styles = []
    for style_no in _list_style_nos(root):
        ref = _ref_file(root, style_no)
        colors = _scan_colors(root / style_no)
        missing_back = sum(1 for c in colors if not c["back_url"])
        meta = meta_map.get(style_no)
        styles.append({
            "style_no": style_no,
            "ref_image": str(ref) if ref else None,
            "ref_image_url": f"/static/styles/{ref.name}" if ref else None,
            "color_folder": str(root / style_no) if (root / style_no).is_dir() else None,
            "color_count": len(colors),
            "cover_color_url": colors[0]["url"] if colors else None,
            "missing_back_count": missing_back,
            "category_main": meta["category_main"] if meta else None,
            "category_sub": meta["category_sub"] if meta else None,
            "created_at": meta["created_at"] if meta else None,
            "meta_missing": meta is None,
        })
    return {"root": str(root), "styles": styles}


@router.get("/{style_no}")
async def get_style(style_no: str):
    root = _styles_root()
    ref_file = _ref_file(root, style_no)
    color_dir = root / style_no
    if not ref_file and not color_dir.is_dir():
        raise HTTPException(404, f"style {style_no} not found")
    meta = get_style_meta(style_no)
    return {
        "style_no": style_no,
        "ref_image": str(ref_file) if ref_file else None,
        "ref_image_url": f"/static/styles/{ref_file.name}" if ref_file else None,
        "color_folder": str(color_dir) if color_dir.is_dir() else None,
        "colors": _scan_colors(color_dir),
        "category_main": meta["category_main"] if meta else None,
        "category_sub": meta["category_sub"] if meta else None,
        "created_at": meta["created_at"] if meta else None,
        "meta_missing": meta is None,
    }


# ============================================================================
# 写接口辅助：ID 生成 / 双视角图拼接 / 校验
# ============================================================================

def _next_style_no(root: Path) -> str:
    """IT{YYMMDD}-{2位序号}，当日已有款序号 +1（同时看文件夹与 DB）。"""
    prefix = f"IT{time.strftime('%y%m%d')}-"
    seqs = []
    existing = set(_list_style_nos(root)) | set(list_style_meta().keys())
    for no in existing:
        if no.startswith(prefix):
            tail = no[len(prefix):]
            if tail.isdigit():
                seqs.append(int(tail))
    return f"{prefix}{(max(seqs) + 1) if seqs else 1:02d}"


def _next_color_code(existing_codes: set[str]) -> str:
    """C{2位序号}，款内递增。"""
    seqs = [int(c[1:]) for c in existing_codes if re.fullmatch(r"C\d+", c)]
    n = (max(seqs) + 1) if seqs else 1
    while f"C{n:02d}" in existing_codes:
        n += 1
    return f"C{n:02d}"


async def _read_upload(file: UploadFile, label: str) -> tuple[bytes, str]:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise HTTPException(400, f"{label}：不支持的图片格式 {ext}（支持 jpg/png/webp）")
    content = await file.read()
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(400, f"{label}：图片超过 20MB 上限")
    if not content:
        raise HTTPException(400, f"{label}：空文件")
    return content, ext


def _stitch_dual_view(front_bytes: bytes, back_bytes: bytes) -> bytes:
    """正+反左右拼接为一张白底双视角图（正左反右，等高缩放），输出 JPEG。"""
    from PIL import Image

    def _open(b: bytes) -> "Image.Image":
        img = Image.open(io.BytesIO(b))
        img.load()
        return img.convert("RGB")

    front, back = _open(front_bytes), _open(back_bytes)
    h = min(front.height, back.height)
    def _scale(img):
        w = round(img.width * h / img.height)
        return img.resize((w, h))
    front, back = _scale(front), _scale(back)
    gap = max(8, h // 50)
    canvas = Image.new("RGB", (front.width + back.width + gap, h), "white")
    canvas.paste(front, (0, 0))
    canvas.paste(back, (front.width + gap, 0))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    return out.getvalue()


def _validate_categories(category_main: str, category_sub: str) -> None:
    if category_main not in CATEGORY_MAIN:
        raise HTTPException(400, f"大类必须是 {'/'.join(sorted(CATEGORY_MAIN))} 之一")
    if category_sub not in CATEGORY_SUB:
        raise HTTPException(400, "小类必须是 top / bottom / onepiece 之一")


_SAFE_NAME = re.compile(r'^[^\\/:*?"<>|]+$')


def _validate_name_component(s: str, label: str) -> str:
    s = (s or "").strip()
    if not s:
        raise HTTPException(400, f"{label}不能为空")
    if not _SAFE_NAME.match(s) or ".." in s:
        raise HTTPException(400, f"{label}含非法字符：{s}")
    return s


# ============================================================================
# 写接口
# ============================================================================

@router.post("/upload")
async def upload_style(
    category_main: str = Form(...),
    category_sub: str = Form(...),
    sku_meta: str = Form(...),           # JSON: [{"code": "C01"|"", "name": "浅天蓝"}, ...]
    style_no: str = Form(""),            # 空 = 自动生成 IT{YYMMDD}-NN
    fronts: list[UploadFile] = File(...),
    backs: list[UploadFile] = File(...),
):
    """新款入库 / 追加色号。每条 SKU = 色号+色名+正面图+背面图，正反面必填。

    fronts / backs 与 sku_meta 按下标一一对应。
    新款：自动用第一条 SKU 的正+反拼接生成款级双视角图 {款号}.jpg。
    已有款：进入追加色号模式，不动已有双视角图。
    """
    root = _styles_root()
    if not root.is_dir():
        raise HTTPException(500, f"款图根目录不存在：{root}")
    _validate_categories(category_main, category_sub)

    try:
        skus = json.loads(sku_meta)
        assert isinstance(skus, list)
    except (ValueError, AssertionError):
        raise HTTPException(400, "sku_meta 必须是 JSON 数组")
    if not skus:
        raise HTTPException(400, "至少需要 1 条 SKU")
    if not (len(skus) == len(fronts) == len(backs)):
        raise HTTPException(400, f"SKU 条数不一致：meta={len(skus)} fronts={len(fronts)} backs={len(backs)}")

    style_no = (style_no or "").strip() or _next_style_no(root)
    _validate_name_component(style_no, "款号")
    color_dir = root / style_no
    is_new_style = not (_ref_file(root, style_no) or color_dir.is_dir())

    # 已有色号（查重 + 自动 code 起点）
    existing_colors = _scan_colors(color_dir)
    existing_codes = {c["code"] for c in existing_colors}

    # ---- 先把所有文件内容读进内存并校验，全部通过才落盘（保证原子性）----
    to_write: list[tuple[Path, bytes]] = []   # (路径, 内容)
    first_front_bytes: bytes | None = None
    first_back_bytes: bytes | None = None
    seen_codes: set[str] = set()

    for i, sku in enumerate(skus):
        name = _validate_name_component(str(sku.get("name") or ""), f"SKU#{i+1} 色名")
        code = (str(sku.get("code") or "")).strip()
        if not code:
            code = _next_color_code(existing_codes | seen_codes)
        _validate_name_component(code, f"SKU#{i+1} 色号")
        if code in existing_codes or code in seen_codes:
            raise HTTPException(400, f"色号重复：{code}（该款下已存在）")
        seen_codes.add(code)

        f_bytes, f_ext = await _read_upload(fronts[i], f"SKU {code} 正面图")
        b_bytes, b_ext = await _read_upload(backs[i], f"SKU {code} 背面图")
        stem = f"{code}（{name}）"
        to_write.append((color_dir / f"{stem}{f_ext}", f_bytes))
        to_write.append((color_dir / f"{stem}{_BACK_SUFFIX}{b_ext}", b_bytes))
        if first_front_bytes is None:
            first_front_bytes, first_back_bytes = f_bytes, b_bytes

    # 新款：拼接款级双视角图（拼接失败 = 整体失败，不落盘）
    if is_new_style:
        try:
            dual = _stitch_dual_view(first_front_bytes, first_back_bytes)
        except Exception as e:
            raise HTTPException(400, f"双视角图拼接失败（请检查图片是否损坏）：{e}")
        to_write.append((root / f"{style_no}.jpg", dual))

    # ---- 落盘（失败回滚已写文件）----
    written: list[Path] = []
    try:
        color_dir.mkdir(parents=True, exist_ok=True)
        for path, content in to_write:
            path.write_bytes(content)
            written.append(path)
    except OSError as e:
        for p in written:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(500, f"写入文件失败已回滚：{e}")

    # ---- 写元数据（失败回滚文件）----
    try:
        upsert_style_meta(
            style_no=style_no,
            category_main=category_main,
            category_sub=category_sub,
            source="upload",
        )
    except Exception as e:
        for p in written:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(500, f"写入元数据失败已回滚：{e}")

    return await get_style(style_no)


class StyleMetaPatch(BaseModel):
    category_main: str
    category_sub: str


@router.patch("/{style_no}/meta")
async def patch_style_meta(style_no: str, req: StyleMetaPatch):
    """编辑/补录品类。存量款（无 meta 记录）补录时 source=backfill 并带文件 mtime。"""
    root = _styles_root()
    ref = _ref_file(root, style_no)
    color_dir = root / style_no
    if not ref and not color_dir.is_dir():
        raise HTTPException(404, f"style {style_no} not found")
    _validate_categories(req.category_main, req.category_sub)

    existing = get_style_meta(style_no)
    file_mtime = None
    if existing is None:
        anchor = ref or color_dir
        try:
            file_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(anchor.stat().st_mtime))
        except OSError:
            pass
    upsert_style_meta(
        style_no=style_no,
        category_main=req.category_main,
        category_sub=req.category_sub,
        source=existing["source"] if existing else "backfill",
        file_mtime=existing["file_mtime"] if existing else file_mtime,
    )
    return await get_style(style_no)


@router.post("/{style_no}/cover")
async def update_cover(
    style_no: str,
    color_code: str = Form(""),
    file: UploadFile | None = File(None),
):
    """更换款级双视角图：传 color_code（服务端取该色号正+反重拼）或直传现成双视角图。"""
    root = _styles_root()
    color_dir = root / style_no
    if not color_dir.is_dir() and not _ref_file(root, style_no):
        raise HTTPException(404, f"style {style_no} not found")

    if file is not None and file.filename:
        content, _ = await _read_upload(file, "双视角图")
        # 统一规格化为 JPEG（顺带校验图片可读）
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(content))
            img.load()
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=92)
            content = out.getvalue()
        except Exception as e:
            raise HTTPException(400, f"图片不可读：{e}")
    elif (color_code or "").strip():
        code = color_code.strip()
        colors = {c["code"]: c for c in _scan_colors(color_dir)}
        c = colors.get(code)
        if not c:
            raise HTTPException(404, f"色号 {code} 不存在")
        if not c["back_filename"]:
            raise HTTPException(400, f"色号 {code} 缺背面图，无法拼接双视角图")
        try:
            content = _stitch_dual_view(
                (color_dir / c["filename"]).read_bytes(),
                (color_dir / c["back_filename"]).read_bytes(),
            )
        except Exception as e:
            raise HTTPException(400, f"双视角图拼接失败：{e}")
    else:
        raise HTTPException(400, "需提供 color_code 或直传文件")

    # 旧封面若是 .png/.webp，先删掉避免 jpg/png 并存时语义含糊
    old = _ref_file(root, style_no)
    (root / f"{style_no}.jpg").write_bytes(content)
    if old and old.name != f"{style_no}.jpg":
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
    return await get_style(style_no)


@router.post("/{style_no}/back")
async def upload_back(
    style_no: str,
    color_code: str = Form(...),
    file: UploadFile = File(...),
):
    """按色号补背面图（存量款补齐用）。"""
    root = _styles_root()
    color_dir = root / style_no
    if not color_dir.is_dir():
        raise HTTPException(404, f"style {style_no} 无色号文件夹")
    colors = {c["code"]: c for c in _scan_colors(color_dir)}
    c = colors.get((color_code or "").strip())
    if not c:
        raise HTTPException(404, f"色号 {color_code} 不存在")
    if c["back_filename"]:
        raise HTTPException(409, f"色号 {color_code} 已有背面图：{c['back_filename']}")
    content, ext = await _read_upload(file, f"色号 {color_code} 背面图")
    stem = Path(c["filename"]).stem
    (color_dir / f"{stem}{_BACK_SUFFIX}{ext}").write_bytes(content)
    return await get_style(style_no)
