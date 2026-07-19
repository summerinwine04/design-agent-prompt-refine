"""
款图库管理 M1 后端测试（docs/款图库管理_产品方案PRD.md v1.3）

跑法：
  cd prompt-refine-agent
  pytest backend/tests/test_style_library.py -v

隔离：STYLES_ROOT 与 DB_PATH 都指向 tmp_path，不碰真实款图/DB。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _img_bytes(color: str = "red", size=(60, 90), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    styles_root = tmp_path / "款图"
    styles_root.mkdir()

    # 存量款 A123：双视角图 + 一个色号（无背面、无 meta）
    (styles_root / "A123.jpg").write_bytes(_img_bytes("blue", (120, 90)))
    (styles_root / "A123").mkdir()
    (styles_root / "A123" / "01（红色）.jpg").write_bytes(_img_bytes("red"))

    # folder-only 存量款 B456（缺双视角图）
    (styles_root / "B456").mkdir()
    (styles_root / "B456" / "02（蓝色）.jpg").write_bytes(_img_bytes("blue"))

    monkeypatch.setenv("STYLES_ROOT", str(styles_root))

    import backend.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "metadata.db")
    db.init_db()

    from backend.routers import styles as styles_router
    app = FastAPI()
    app.include_router(styles_router.router, prefix="/api/v1/styles")
    return TestClient(app)


def _upload_payload(style_no="", skus=None):
    """构造 multipart：sku_meta JSON + fronts/backs 文件对。"""
    skus = skus or [{"code": "", "name": "浅天蓝"}]
    import json
    data = {
        "style_no": style_no,
        "category_main": "童装",
        "category_sub": "top",
        "sku_meta": json.dumps(skus, ensure_ascii=False),
    }
    files = []
    for i, _ in enumerate(skus):
        files.append(("fronts", (f"f{i}.jpg", _img_bytes("green"), "image/jpeg")))
        files.append(("backs", (f"b{i}.jpg", _img_bytes("gray"), "image/jpeg")))
    return data, files


# ---------------------------------------------------------------- 只读 / 对账

def test_list_join_meta_and_back_scan(client):
    r = client.get("/api/v1/styles")
    assert r.status_code == 200
    by_no = {s["style_no"]: s for s in r.json()["styles"]}

    a = by_no["A123"]
    assert a["meta_missing"] is True            # 存量款：待补录
    assert a["color_count"] == 1
    assert a["missing_back_count"] == 1         # 缺背面
    assert a["ref_image_url"] == "/static/styles/A123.jpg"

    b = by_no["B456"]                           # folder-only：缺双视角图但要出现在列表
    assert b["ref_image"] is None
    assert b["color_count"] == 1


def test_back_files_not_counted_as_colors(client):
    import os
    root = Path(os.environ["STYLES_ROOT"])
    (root / "A123" / "01（红色）_back.jpg").write_bytes(_img_bytes("gray"))
    detail = client.get("/api/v1/styles/A123").json()
    assert len(detail["colors"]) == 1           # _back 不算色号
    assert detail["colors"][0]["back_url"] == "/static/styles/A123/01（红色）_back.jpg"
    lst = {s["style_no"]: s for s in client.get("/api/v1/styles").json()["styles"]}
    assert lst["A123"]["missing_back_count"] == 0


# ---------------------------------------------------------------- 上传

def test_upload_new_style_auto_ids_and_stitch(client):
    import os, time
    data, files = _upload_payload(skus=[{"code": "", "name": "浅天蓝"}, {"code": "C09", "name": "藏青"}])
    r = client.post("/api/v1/styles/upload", data=data, files=files)
    assert r.status_code == 200, r.text
    body = r.json()

    style_no = body["style_no"]
    assert style_no.startswith(f"IT{time.strftime('%y%m%d')}-")   # 自动款号
    codes = {c["code"] for c in body["colors"]}
    assert codes == {"C01", "C09"}                                # 自动色号从 C01 起
    assert all(c["back_url"] for c in body["colors"])             # 正反面齐
    assert body["category_main"] == "童装" and body["meta_missing"] is False
    assert body["created_at"]

    # 双视角图：白底左右拼接，宽 > 高
    root = Path(os.environ["STYLES_ROOT"])
    dual = Image.open(root / f"{style_no}.jpg")
    assert dual.width > dual.height

    # 再传一款：当日序号递增
    d2, f2 = _upload_payload()
    no2 = client.post("/api/v1/styles/upload", data=d2, files=f2).json()["style_no"]
    assert no2 != style_no and no2.startswith(f"IT{time.strftime('%y%m%d')}-")


def test_upload_append_mode_keeps_cover(client):
    import os
    root = Path(os.environ["STYLES_ROOT"])
    cover_before = (root / "A123.jpg").read_bytes()
    data, files = _upload_payload(style_no="A123", skus=[{"code": "03", "name": "米白"}])
    r = client.post("/api/v1/styles/upload", data=data, files=files)
    assert r.status_code == 200, r.text
    assert {c["code"] for c in r.json()["colors"]} == {"01", "03"}
    assert (root / "A123.jpg").read_bytes() == cover_before        # 追加不动双视角图


def test_upload_duplicate_color_rejected_no_partial_write(client):
    import os
    root = Path(os.environ["STYLES_ROOT"])
    before = sorted(p.name for p in (root / "A123").iterdir())
    data, files = _upload_payload(style_no="A123", skus=[{"code": "01", "name": "红色"}])
    r = client.post("/api/v1/styles/upload", data=data, files=files)
    assert r.status_code == 400 and "色号重复" in r.text
    assert sorted(p.name for p in (root / "A123").iterdir()) == before


def test_upload_invalid_category(client):
    data, files = _upload_payload()
    data["category_main"] = "潮牌"
    assert client.post("/api/v1/styles/upload", data=data, files=files).status_code == 400


def test_upload_corrupt_image_rolls_back(client):
    import json, os
    root = Path(os.environ["STYLES_ROOT"])
    data = {
        "style_no": "", "category_main": "童装", "category_sub": "top",
        "sku_meta": json.dumps([{"code": "", "name": "坏图"}], ensure_ascii=False),
    }
    files = [
        ("fronts", ("f.jpg", b"not-an-image-at-all", "image/jpeg")),
        ("backs",  ("b.jpg", _img_bytes("gray"), "image/jpeg")),
    ]
    r = client.post("/api/v1/styles/upload", data=data, files=files)
    assert r.status_code == 400 and "拼接失败" in r.text
    # 没有留下任何新款残余
    nos = {s["style_no"] for s in client.get("/api/v1/styles").json()["styles"]}
    assert nos == {"A123", "B456"}


# ---------------------------------------------------------------- meta / cover / back

def test_patch_meta_backfill(client):
    r = client.patch("/api/v1/styles/A123/meta", json={"category_main": "女装", "category_sub": "bottom"})
    assert r.status_code == 200
    assert r.json()["category_main"] == "女装" and r.json()["meta_missing"] is False

    import backend.db as db
    meta = db.get_style_meta("A123")
    assert meta["source"] == "backfill" and meta["file_mtime"]

    # 再次 PATCH 不应把 source 变回 upload
    client.patch("/api/v1/styles/A123/meta", json={"category_main": "童装", "category_sub": "top"})
    assert db.get_style_meta("A123")["source"] == "backfill"


def test_cover_restitch_from_color(client):
    import os
    root = Path(os.environ["STYLES_ROOT"])
    # 无背面 → 拒绝
    assert client.post("/api/v1/styles/A123/cover", data={"color_code": "01"}).status_code == 400
    (root / "A123" / "01（红色）_back.jpg").write_bytes(_img_bytes("gray"))
    r = client.post("/api/v1/styles/A123/cover", data={"color_code": "01"})
    assert r.status_code == 200
    dual = Image.open(root / "A123.jpg")
    assert dual.width > dual.height                                # 重拼后是横版双视角


def test_cover_direct_upload_for_folder_only_style(client):
    import os
    root = Path(os.environ["STYLES_ROOT"])
    files = {"file": ("dual.png", _img_bytes("blue", (200, 90), "PNG"), "image/png")}
    r = client.post("/api/v1/styles/B456/cover", files=files)
    assert r.status_code == 200
    assert (root / "B456.jpg").is_file()                           # 统一规格化为 jpg
    assert r.json()["ref_image_url"] == "/static/styles/B456.jpg"


def test_upload_back_for_legacy_color(client):
    r = client.post(
        "/api/v1/styles/B456/back",
        data={"color_code": "02"},
        files={"file": ("b.jpg", _img_bytes("gray"), "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json()["colors"][0]["back_url"]
    # 已有背面 → 409
    r2 = client.post(
        "/api/v1/styles/B456/back",
        data={"color_code": "02"},
        files={"file": ("b.jpg", _img_bytes("gray"), "image/jpeg")},
    )
    assert r2.status_code == 409
