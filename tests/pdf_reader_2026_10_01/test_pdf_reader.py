# -*- coding: utf-8 -*-
"""OSS_25 第 4 条 · 去 AGPL:PyMuPDF(fitz,AGPL-3.0)→ services.pdf_reader(pypdfium2,Apache-2.0 / BSD-3-Clause)。

原先 8 处直接 `import fitz`。本包对每一处都用真 PDF 走它所在的真函数:
  S1 api/knowledge_api._extract_text_from_bytes            S2 api/knowledge_api._extract_embedded_images
  S3 api/knowledge_api.upload_role_document(角色库上传)    S4 api/knowledge_api.upload_public_document(公共库上传)
  S5 server.upload_file(/api/upload,从 server.py 取出真函数体单独执行,不 import 整个 server)
  S6 services/upload_text._extract_pdf_with_vision(文字层 + 嵌入图识图)
  S7 services/upload_text._pdf_pages_to_images_via_vision(扫描件整页渲染识图)
  S8 scripts/load_teacher_shu_kb.extract_pages
外加 P1–P4 直测 pdf_reader,L1 全仓锁(带牙证)。识图 / 入库这些外部依赖换成记录入参的替身,PDF 解析一律是真的。
夹具由 make_fixtures.py 用 headless Chrome 打印生成,只有测试文字。
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
TEXT_PDF = (HERE / "text_image.pdf").read_bytes()
SCAN_PDF = (HERE / "scan.pdf").read_bytes()

from services import pdf_reader  # noqa: E402

P1 = "引路测试文档"
P1B = "第一页:这是一份用于解析回归的测试文档"
P2 = "第二页:提取文字时页序必须保持"


def _upload(data: bytes, name: str):
    from starlette.datastructures import UploadFile
    return UploadFile(file=io.BytesIO(data), filename=name)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- pdf_reader 本身

def test_p1_page_texts_in_page_order():
    pages = pdf_reader.page_texts(TEXT_PDF)
    assert len(pages) == 2
    assert P1 in pages[0] and P1B in pages[0] and P2 not in pages[0]
    assert P2 in pages[1]
    assert pdf_reader.page_texts(HERE / "text_image.pdf") == pages          # 路径与 bytes 同结果
    assert "\r" not in "".join(pages)


def test_p2_embedded_images_keep_jpeg_and_drop_icons():
    all_imgs = pdf_reader.embedded_images(TEXT_PDF)
    big = pdf_reader.embedded_images(TEXT_PDF, 4096)
    assert [i.page_index for i in all_imgs] == [0, 0]                        # 照片 + 8px 图标,都在第 1 页
    assert len(big) == 1                                                    # 4KB 阈值滤掉图标
    assert big[0].ext == "jpeg" and big[0].data[:2] == b"\xff\xd8"          # JPEG 原样取出,扩展名与 fitz 同名
    assert Image.open(io.BytesIO(big[0].data)).size == (360, 240)
    assert pdf_reader.embedded_images(TEXT_PDF, 0, max_images=1) == all_imgs[:1]   # 收满就停
    c = pdf_reader.read(TEXT_PDF, min_image_bytes=4096, max_images=5)       # 一次读完:文字与图同一份
    assert c.page_texts == pdf_reader.page_texts(TEXT_PDF) and c.images == big
    assert all(isinstance(x, (str,)) for x in c.page_texts) and isinstance(c.images[0].data, bytes)   # 只出纯数据


def test_p3_scan_has_no_text_layer_and_renders_at_2x():
    assert [t.strip() for t in pdf_reader.page_texts(SCAN_PDF)] == [""]
    (idx, png), = pdf_reader.render_pages_png(SCAN_PDF, scale=2)
    (_, png1), = pdf_reader.render_pages_png(SCAN_PDF, scale=1)
    img, img1 = Image.open(io.BytesIO(png)), Image.open(io.BytesIO(png1))
    assert idx == 0 and png[:4] == b"\x89PNG" and img.format == "PNG"
    assert abs(img.size[0] - 2 * img1.size[0]) <= 1 and abs(img.size[1] - 2 * img1.size[1]) <= 1   # 2 倍
    dark = sum(1 for p in img.convert("L").getdata() if p < 100)
    assert dark > 1000                                                      # 真把图里的黑条画出来了,不是白页
    assert len(pdf_reader.render_pages_png(TEXT_PDF, max_pages=1)) == 1     # 限页数


def test_p4_broken_pdf_raises():
    with pytest.raises(pdf_reader.PdfError):
        pdf_reader.page_texts(b"%PDF-1.4 not really a pdf")


# ---------------------------------------------------------------- 8 处调用点

def test_s1_knowledge_extract_text_from_bytes():
    from api import knowledge_api as k
    out = k._extract_text_from_bytes(TEXT_PDF, "资料.pdf")
    assert out.index(P1) < out.index(P2)


def test_s2_knowledge_extract_embedded_images():
    from api import knowledge_api as k
    out = k._extract_embedded_images(TEXT_PDF, "资料.pdf")
    assert [name for _, name in out] == ["资料.pdf_p1_img1.jpeg"]
    assert out[0][0][:2] == b"\xff\xd8"
    assert k._extract_embedded_images(SCAN_PDF, "s.pdf") == []                # 1.5KB 的图低于阈值


def test_s3_upload_role_document(monkeypatch):
    from api import knowledge_api as k
    import services.knowledge_pipeline as kp
    seen = {}

    class _Pipe:
        async def process_role_document(self, content, filename, role_type, role_id, skip_clean):
            seen["content"] = content
            return types.SimpleNamespace(success=True, original_path="p", chunk_count=1, knowledge_point_count=1,
                                         processing_time_ms=1, error=None)

    monkeypatch.setattr(k, "_require_admin", lambda r: None)
    monkeypatch.setattr(kp, "get_knowledge_pipeline", lambda: _Pipe())
    r = _run(k.upload_role_document("advisor", "x", None, _upload(TEXT_PDF, "角色.pdf")))
    assert r["success"] and seen["content"].index(P1) < seen["content"].index(P2)
    with pytest.raises(k.HTTPException) as e:                               # 坏 PDF 仍是 400(原口径)
        _run(k.upload_role_document("advisor", "x", None, _upload(b"%PDF-1.4 junk", "坏.pdf")))
    assert e.value.status_code == 400


def test_s4_upload_public_document(monkeypatch):
    from api import knowledge_api as k
    seen = {}

    class _Rag:
        def add_document(self, kb_type, kb_id, filename, content):
            seen["content"] = content
            return {"success": True, "path": "p", "size": len(content)}

    monkeypatch.setattr(k, "get_unified_rag", lambda: _Rag())
    req = types.SimpleNamespace(state=types.SimpleNamespace(user={"is_admin": True}))
    r = _run(k.upload_public_document(req, _upload(TEXT_PDF, "公共.pdf")))
    assert r["success"] and seen["content"].index(P1) < seen["content"].index(P2)
    with pytest.raises(k.HTTPException) as e:
        _run(k.upload_public_document(req, _upload(b"%PDF-1.4 junk", "坏.pdf")))
    assert e.value.status_code == 400


def _server_upload_file():
    """从 server.py 取出 upload_file 的真函数体(去掉 @app.post),在最小命名空间里执行。"""
    import logging
    from fastapi import HTTPException, UploadFile
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "upload_file")
    fn.decorator_list = []
    ns = {"UploadFile": UploadFile, "HTTPException": HTTPException, "File": lambda *a, **k: None,
          "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "server.py", "exec"), ns)
    return ns["upload_file"], HTTPException


def test_s5_server_upload_file():
    upload_file, HTTPException = _server_upload_file()
    r = _run(upload_file(_upload(TEXT_PDF, "证据.pdf")))
    assert r["filename"] == "证据.pdf" and r["content"].index(P1) < r["content"].index(P2)
    with pytest.raises(HTTPException) as e:                                 # 扫描件:无文字层 ⇒ 422(原口径)
        _run(upload_file(_upload(SCAN_PDF, "扫描.pdf")))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:                                 # 坏 PDF ⇒ 400
        _run(upload_file(_upload(b"%PDF-1.4 junk", "坏.pdf")))
    assert e.value.status_code == 400


@pytest.fixture
def vision(monkeypatch):
    """识图替身:记录入参,回固定文字;管理员设置取默认值(不连库)。"""
    from services import upload_text as ut
    import threading
    calls = []

    def _lock_free() -> bool:
        """识图被调用的这一刻,pdf_reader 的锁有没有被人握着(另起一个线程去拿,1 秒内拿到 = 没人握)。
        Review 10-02:识图要在锁内取完、关掉文档之后再 await —— 持锁 await 会把全进程的 PDF 解析堵在一次网络调用上。"""
        got = []

        def _try():
            ok = pdf_reader._LOCK.acquire(timeout=1)
            if ok:
                pdf_reader._LOCK.release()
            got.append(ok)
        t = threading.Thread(target=_try)
        t.start()
        t.join()
        return bool(got and got[0])

    async def _fake(image_bytes, original_filename="image"):
        calls.append((original_filename, image_bytes, _lock_free()))
        return f"[识图:{original_filename}]"

    monkeypatch.setattr(ut, "_vision_image_to_text", _fake)
    stub = types.ModuleType("db.social_preferences_db")
    stub.get_admin_setting = lambda key, typ, default: default
    monkeypatch.setitem(sys.modules, "db.social_preferences_db", stub)
    return ut, calls


def test_s6_extract_pdf_with_vision(vision):
    ut, calls = vision
    out = _run(ut._extract_pdf_with_vision(TEXT_PDF, "手册.pdf"))
    assert out.index("--- PDF 文字层 ---") < out.index(P1) < out.index(P2)
    assert "### 第 1 页" in out and "### 第 2 页" in out
    assert [c[0] for c in calls] == ["手册.pdf_p1_img1.jpeg"]                 # 只识照片,图标被阈值滤掉
    assert calls[0][1][:2] == b"\xff\xd8"
    assert "[识图:手册.pdf_p1_img1.jpeg]" in out
    assert all(c[2] for c in calls), "识图是在 pdf_reader 锁还握着的时候 await 的"


def test_s7_scan_pdf_rescued_by_full_page_render(vision):
    ut, calls = vision
    out = _run(ut._pdf_pages_to_images_via_vision(SCAN_PDF, "扫描.pdf", max_pages=5, concurrency=2))
    assert out == "### 第 1 页\n[识图:扫描.pdf_page1.png]"
    assert len(calls) == 1 and calls[0][1][:4] == b"\x89PNG" and calls[0][2]
    calls.clear()
    out2 = _run(ut._extract_pdf_with_vision(SCAN_PDF, "扫描.pdf"))       # 文字层空 + 无大图 ⇒ 走整页救援
    assert "--- PDF 整页视觉识别" in out2 and "[识图:扫描.pdf_page1.png]" in out2


def test_s8_teacher_kb_extract_pages():
    import importlib.util
    spec = importlib.util.spec_from_file_location("load_teacher_shu_kb", ROOT / "scripts" / "load_teacher_shu_kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pages = mod.extract_pages(str(HERE / "text_image.pdf"))
    assert len(pages) == 2 and P1 in pages[0] and P2 in pages[1]
    assert pages == [p.strip() for p in pages]


# ---------------------------------------------------------------- 全仓锁

#: 全仓扫描走 `git grep -P`(0.2s;Python 逐个读 3000+ 个文件要 12s,ONESHOT 预算装不下)。牙证用同一个引擎:
#: 在仓库外的临时目录里 `git grep --no-index -P` 同一条正则,判法与真扫描同一条代码路径。
_FITZ = r"^\s*(import\s+fitz\b|from\s+fitz\b)|\bfitz\.(open|Matrix|Document)\b|\bimport\s+pymupdf\b"
_PDFIUM_IMPORT = r"^\s*(import\s+pypdfium2\b|from\s+pypdfium2\b)|importlib\.import_module\(\s*['\"]pypdfium2"
_REQ_AGPL = re.compile(r"(?im)^\s*pymupdf\b")


def _tracked(pattern: str) -> list:
    out = subprocess.run(["git", "ls-files", pattern], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return [f for f in out.stdout.splitlines() if f]


def _git_grep(regex: str, cwd: Path, no_index: bool = False) -> list:
    """命中的文件(正斜杠相对路径)。rc 1 = 没命中;其他非 0 = 判不了 ⇒ 抛错,不当成「0 处」。"""
    args = ["git", "grep"] + (["--no-index"] if no_index else []) + ["-l", "-P", regex, "--", "*.py"]
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git grep 判不了 rc={r.returncode}: {r.stderr[-200:]}")
    return sorted(ln.strip().replace("\\", "/") for ln in r.stdout.splitlines() if ln.strip())


def _tooth(regex: str, tmp_path: Path, bad: list, good: list) -> None:
    """同一个 git grep -P:每条坏写法单独一个文件必须命中,好写法必须不命中。"""
    for i, text in enumerate(bad):
        (tmp_path / f"bad_{i}.py").write_text(text, encoding="utf-8")
    for i, text in enumerate(good):
        (tmp_path / f"good_{i}.py").write_text(text, encoding="utf-8")
    hits = _git_grep(regex, tmp_path, no_index=True)
    assert hits == sorted(f"bad_{i}.py" for i in range(len(bad))), hits


def test_l1_no_pymupdf_anywhere(tmp_path):
    py = _tracked("*.py")
    assert len(py) > 500, "分母:git ls-files 没读到 .py"
    # 分母自证:本包两个文件(被测入口与本锁自身)都在扫描范围里 —— 未跟踪的文件 git ls-files / git grep 都看不见
    assert {"services/pdf_reader.py", "tests/pdf_reader_2026_10_01/test_pdf_reader.py"} <= set(py)
    assert _git_grep(_FITZ, ROOT) == []
    reqs = [f for f in _tracked("requirements*.txt")]
    assert {"requirements.txt", "requirements-docker.txt"} <= set(reqs)
    for f in reqs:
        assert not _REQ_AGPL.search((ROOT / f).read_text(encoding="utf-8")), f
    for f in ("requirements.txt", "requirements-docker.txt"):
        assert re.search(r"(?m)^pypdfium2==", (ROOT / f).read_text(encoding="utf-8")), f
    # 牙证:原先的写法各自命中;对照:注释里提到不算(拼接写,免得本文件自己的源码被本锁扫中)
    F = "fi" + "tz"
    _tooth(_FITZ, tmp_path, [f"        import {F}  # PyMuPDF\n", f"from {F} import open\n", f"doc = {F}.open(stream=b)\n",
                             f"pix = page.get_pixmap(matrix={F}.Matrix(2, 2))\n", "import " + "pymupdf\n"],
           ["# 原先用 " + F + " 解析,已换\nfrom services import pdf_reader\n"])
    assert _REQ_AGPL.search("pymupdf==1.27.1\n") and _REQ_AGPL.search("PyMuPDF>=1\n")


# ---------------------------------------------------------------- 并发(Review 10-02 NO-GO:PDFium 不可跨线程并发)
#: ONESHOT 每班跑轻量档(8 线程 × 10 轮 × 1 次,约 2s);满强度 8 × 40 × 3 次用 PDF_READER_STRESS=1 打开(Review 10-02 定的量)。
STRESS = os.environ.get("PDF_READER_STRESS") == "1"


def _probe(threads: int, iters: int, nolock: bool = False) -> dict:
    """子进程跑 threads_probe.py(原生崩溃只带走子进程);崩了按 errors=-1 记。"""
    args = [sys.executable, str(HERE / "threads_probe.py"), str(threads), str(iters)] + (["nolock"] if nolock else [])
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        return {"errors": -1, "wrong": 0, "sample": [f"rc={r.returncode} {r.stderr[-300:]}"]}
    import json
    return json.loads(lines[-1])


def test_t1_threads_with_the_lock_zero_errors():
    runs, iters = (3, 40) if STRESS else (1, 10)
    for run in range(runs):
        got = _probe(8, iters)
        assert got["errors"] == 0 and got["wrong"] == 0, (run, iters, got)


def test_t2_tooth_without_the_lock_threads_break():
    got = _probe(8, 40 if STRESS else 10, nolock=True)
    assert got["errors"] != 0 or got["wrong"] > 0, got                      # 去锁 ⇒ 必须读到错误或不一致(崩了也算)


def test_t3_control_single_thread_without_the_lock_is_clean():
    got = _probe(1, 40 if STRESS else 10, nolock=True)
    assert got == {"errors": 0, "wrong": 0, "sample": []}, got              # 不是探针本身在报错


def test_l2_only_pdf_reader_imports_pypdfium2(tmp_path):
    assert _git_grep(_PDFIUM_IMPORT, ROOT) == ["services/pdf_reader.py"]
    src = (ROOT / "services" / "pdf_reader.py").read_text(encoding="utf-8")
    assert "PDFium 不可跨线程并发;本模块是唯一入口并自带锁,别处不得直接 import pypdfium2" in src
    M = "pypdf" + "ium2"                                                    # 拼接写,免得本文件被本锁扫中
    _tooth(_PDFIUM_IMPORT, tmp_path, [f"import {M}\n", f"    import {M} as pdfium\n", f"from {M} import PdfDocument\n",
                                      f"import {M}.raw as c\n", f"x = importlib.import_module('{M}')\n"],
           ["from services import pdf_reader  # 底层是 pdfium\n"])
