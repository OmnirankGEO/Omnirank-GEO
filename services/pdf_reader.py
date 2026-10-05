"""PDF 读取的唯一入口:文字层 / 内嵌图 / 整页渲染。

底层用 pypdfium2(pdfium,Apache-2.0 / BSD-3-Clause)。原先各处直接 `import fitz`(PyMuPDF,AGPL-3.0),
开源许可证硬门过不了(OSS_25 第 4 条 · WO_319)—— 全仓 PDF 解析收口到这里。

🔴 PDFium 不可跨线程并发;本模块是唯一入口并自带锁,别处不得直接 import pypdfium2。
   pypdfium2 走 ctypes,调 C 函数期间释放 GIL,Python 层也没有锁 ⇒ 两个线程会真的同时跑进 PDFium
   (Review 10-02 实测:8 线程 × 40 轮,完好的 PDF 被报「Data format error」、取出的文字悄悄不一致)。
   旧 PyMuPDF 大多数调用握着 GIL,等于天然串行;换库把这层隐性保护拿掉了,所以这里显式加一把进程级锁。
   规矩:
   · 对外只给整份文档级的函数。每个函数在锁内一次做完「打开 → 取文字 / 取图 / 渲染 → 关闭」,只返回纯 Python 数据
     (str / bytes / int),任何 PDFium 对象都不出本模块。
   · 关闭也在锁内:关文档会先关掉它的页、文字页、图片对象,终结器随之摘除,锁外不会再有 PDFium 调用。
   · 调用方拿到数据之后再去 await 别的东西(识图等);别在持有本模块任何东西的时候 await。
   静态锁见 tests/pdf_reader_2026_10_01(全仓除本模块外 import pypdfium2 = 0)。

打不开(损坏 / 加密 / 不是 PDF)一律抛 PdfError(= pypdfium2.PdfiumError),由调用方按各自原有口径转成 400 / 退化。
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PdfError = pdfium.PdfiumError
PdfSource = Union[bytes, str, Path]

#: 进程级锁:本模块里所有 PDFium 调用都在它里面。可重入,便于本模块内部函数互相调用。
_LOCK = threading.RLock()


@dataclass
class PdfImage:
    """一张内嵌图:页序号(0 起)、编码后的字节、扩展名(与旧 fitz extract_image 的 ext 同名)。"""
    page_index: int
    data: bytes
    ext: str


@dataclass
class PdfContent:
    """一次读完的整份文档:每页文字层(按页序)+ 内嵌图(按页序、限张数)。"""
    page_texts: list[str] = field(default_factory=list)
    images: list[PdfImage] = field(default_factory=list)


# ---------------------------------------------------------------- 只在锁内调用的内部函数

def _open(source: PdfSource) -> pdfium.PdfDocument:
    return pdfium.PdfDocument(str(source) if isinstance(source, Path) else source)


def _page_text(page: pdfium.PdfPage) -> str:
    tp = page.get_textpage()
    try:
        return (tp.get_text_bounded() or "").replace("\r\n", "\n").replace("\r", "\n")
    finally:
        tp.close()


def _image_ext(head: bytes) -> str:
    """扩展名与原 fitz extract_image 的 ext 同名(jpeg / jpx / png / tiff),入库文件名不变。"""
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\x00\x00\x00\x0cjP") or head.startswith(b"\xff\x4f\xff\x51"):
        return "jpx"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return "png"


def _page_images(page: pdfium.PdfPage, min_bytes: int) -> list[tuple[bytes, str]]:
    """一页里的内嵌图。JPEG / JPEG 2000 原样取出,其余重编码为 PNG;小于 min_bytes(图标 / 装饰)跳过;单张取不出跳过。"""
    out = []
    for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)):
        buf = io.BytesIO()
        try:
            obj.extract(buf, fb_format="png")
        except Exception:
            continue
        data = buf.getvalue()
        if data and len(data) >= min_bytes:
            out.append((data, _image_ext(data[:12])))
    return out


def _render_png(page: pdfium.PdfPage, scale: float) -> bytes:
    bitmap = page.render(scale=scale)
    try:
        img = bitmap.to_pil()
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    finally:
        bitmap.close()


# ---------------------------------------------------------------- 对外:整份文档级,锁内打开到关闭,只返回纯数据

def page_texts(source: PdfSource) -> list[str]:
    """每页文字层,按页序;没有文字层(扫描件)的页是空串。"""
    with _LOCK:
        doc = _open(source)
        try:
            return [_page_text(doc[i]) for i in range(len(doc))]
        finally:
            doc.close()


def embedded_images(source: PdfSource, min_bytes: int = 0, max_images: Optional[int] = None) -> list[PdfImage]:
    """内嵌图,按页序;只收 ≥ min_bytes 的,收满 max_images 张就停。"""
    return read(source, min_image_bytes=min_bytes, max_images=max_images, with_text=False).images


def read(source: PdfSource, *, min_image_bytes: int = 0, max_images: Optional[int] = None,
         with_text: bool = True) -> PdfContent:
    """一次读完文字层与内嵌图(识图链路用:先在锁内全部取完、关掉文档,调用方再去 await 识图)。"""
    out = PdfContent()
    with _LOCK:
        doc = _open(source)
        try:
            for i in range(len(doc)):
                page = doc[i]
                if with_text:
                    out.page_texts.append(_page_text(page))
                if max_images is not None and len(out.images) >= max_images:
                    continue
                for data, ext in _page_images(page, min_image_bytes):
                    if max_images is not None and len(out.images) >= max_images:
                        break
                    out.images.append(PdfImage(i, data, ext))
        finally:
            doc.close()
    return out


def render_pages_png(source: PdfSource, max_pages: Optional[int] = None, scale: float = 2.0) -> list[tuple[int, bytes]]:
    """前 max_pages 页各渲染成 PNG(扫描件交给识图用)⇒ [(页序号, png 字节)]。单页渲染失败跳过,不影响别的页。
    scale=2 与改造前的 2 倍渲染矩阵同倍率。"""
    out: list[tuple[int, bytes]] = []
    with _LOCK:
        doc = _open(source)
        try:
            n = len(doc) if max_pages is None else min(len(doc), max(1, max_pages))
            for i in range(n):
                try:
                    out.append((i, _render_png(doc[i], scale)))
                except Exception:
                    continue
        finally:
            doc.close()
    return out
