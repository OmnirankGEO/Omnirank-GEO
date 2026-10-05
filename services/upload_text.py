# -*- coding: utf-8 -*-
"""上传文件 → 文字(小帮「解析文件」的依赖链)。

开源 E3 · B1b-1 · 2026-09-28 从社媒主路径 router 上提(那个 router 已随 B1b-2b 删),**逐字搬运**:
`_decode_uploaded_bytes` · `_extract_upload_text` · PDF 混合提取(`_extract_pdf_with_vision` /
`_pdf_pages_to_images_via_vision` / `_extract_pdf_text_fallback` 与四个 PDF 常量)。
唯一改动:文件分析器改从 `services.file_analyzer` 取(同批从社媒包上提)。
旧位置只留 re-export 兼容垫(标 E3 删),给社媒文件里尚未删除的代码用;删那个文件时垫子随文件一起走。
🔴 本模块不许 import 社媒代码(社媒工具包 / 社媒主路径 router / 社媒 agent)——
   锁 `tests/oss_e3b1_lift_upload_text_2026_09_28` 守着,含函数体内的延迟导入。
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from fastapi import HTTPException, UploadFile

from services.image_to_text import _vision_image_to_text


def _decode_uploaded_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


async def _extract_upload_text(file: UploadFile) -> str:
    raw = await file.read()
    # 2026-05-18 老板拍升 50M(产品手册/客户资料经常很大)· 转写完 tempfile 直接删 · 不留盘
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="资料文件太大，请先压缩到 50MB 以内")
    filename = file.filename or "uploaded.txt"
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".txt", ".md", ".csv", ".json"}:
        return _decode_uploaded_bytes(raw)

    # 2026-05-18 老板反馈 "上传 20+MB PDF 显示产品资料太短" 根因诊断:
    #   PDF 走 file_analyzer 仅提文字层 · 扫描件文字层为空 → raw_text 空 → 后续检查 < 20 字
    # 修法:PDF 路径直接走 _extract_pdf_with_vision hybrid(文字层 + 嵌入图 vision)
    #   pdf_reader 提文字层 + qwen vision 转扫描页 · 文字层为空时仍能从图救文字
    if ext == ".pdf":
        try:
            text = await _extract_pdf_with_vision(raw, filename)
            if text and text.strip():
                return text
        except Exception as e:
            print(f"[_extract_upload_text] PDF hybrid 失败 · 退到纯文字 fallback: {e}", flush=True)
        # hybrid 失败 / 返空 · 走老的 file_analyzer(pypdf/pdfplumber)兜底

    suffix = ext if ext in {".pdf", ".docx", ".doc"} else ".txt"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        from services.file_analyzer import analyze_file

        analyzed = analyze_file(tmp_path)
        if analyzed.get("success") and analyzed.get("raw_text"):
            return str(analyzed.get("raw_text") or "")
        if ext not in {".pdf", ".docx", ".doc"}:
            return _decode_uploaded_bytes(raw)
        raise HTTPException(status_code=400, detail=analyzed.get("error") or "无法读取这个文件的文字")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# 2026-05-12 R14 hotfix C4 · PDF 带图混合方案(老板拍板)
# 现有 _extract_upload_text 只提 PDF 文字层 · 嵌入图全丢
# 混合方案:文字层走 pdf_reader(快+免费)+ 嵌入图限 5 张走 qwen3.6-plus 转写
# 适用范围:仅社媒 · GEO/C 端/M3 不动
PDF_MAX_VISION_IMAGES = 5  # 单 PDF 最多 vision N 张图 · 防 100 页 PDF 爆成本
PDF_MIN_IMAGE_BYTES = 4096  # 嵌入图小于 4KB 当作 icon / decoration 跳过
# 2026-05-18 老板拍 · 整页 render vision 救扫描 PDF
# 实测 qwen3.6-flash 并发 10 + 20 页 = 35.5s wall clock · 全 20/20 成功
# 并发 20 开始失败(服务端 throttle outlier)· 10 是甜点
# 老板补:flash RPM=30000(500RPS) TPM=10M · 我们用 ~0.1RPS · 远低 · 瓶颈是单 call 处理时间
PDF_FULL_PAGE_VISION_MAX_PAGES = 20
PDF_FULL_PAGE_VISION_CONCURRENCY = 10  # 实测 10 是甜点 · 5→10 提速 14% · 10→20 收益 0 + 失败率上升


async def _extract_pdf_with_vision(pdf_bytes: bytes, filename: str) -> str:
    """混合方案:PDF 文字层(pdf_reader)+ 嵌入图 vision 转写(qwen3.6-plus)

    步骤:
    1. pdf_reader 提文字层(每页 page_text)
    2. pdf_reader 提嵌入图(page_images · 限 max_vision_images 张)
    3. 每张图调 _vision_image_to_text
    4. 拼接 文字 + 图描述 入 KB

    Fallback:PDF 解析库 import 失败 / PDF 无嵌入图 → 退化到纯文字
    2026-05-12 R15:max_vision_images 可由 admin 在 settings 改(0-10)
    """
    try:
        from services import pdf_reader
    except Exception as e:
        print(f"[pdf-vision] PDF 解析库未装 · 退化纯文字: {e}", flush=True)
        return await _extract_pdf_text_fallback(pdf_bytes, filename)

    # admin 可改 PDF vision 上限 · fallback hardcoded PDF_MAX_VISION_IMAGES
    try:
        from db.social_preferences_db import get_admin_setting
        max_vision_images = get_admin_setting("pdf_max_vision_images", int, PDF_MAX_VISION_IMAGES)
        if max_vision_images is None or max_vision_images < 0:
            max_vision_images = PDF_MAX_VISION_IMAGES
    except Exception:
        max_vision_images = PDF_MAX_VISION_IMAGES

    # 1. 提文字层
    text_parts: list[str] = []
    image_count = 0
    image_descriptions: list[str] = []

    # 先在 pdf_reader 的锁内一次取完全部文字和嵌入图、关掉文档,再去 await 识图(PDFium 不可跨线程并发,
    # 不许文档还开着就 await;< PDF_MIN_IMAGE_BYTES 的 icon 已滤掉,收满 max_vision_images 张就停)
    try:
        content = pdf_reader.read(pdf_bytes, min_image_bytes=PDF_MIN_IMAGE_BYTES, max_images=max_vision_images)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF 解析失败 · 文件可能损坏: {e}")

    for page_idx, raw_text in enumerate(content.page_texts):
        page_text = raw_text.strip()
        if page_text:
            text_parts.append(f"### 第 {page_idx + 1} 页\n{page_text}")

    # 2. 嵌入图逐张识图
    for img in content.images:
        image_count += 1
        page_idx = img.page_index
        img_filename = f"{filename}_p{page_idx + 1}_img{image_count}.{img.ext}"
        try:
            desc = await _vision_image_to_text(img.data, img_filename)
            image_descriptions.append(
                f"### 第 {page_idx + 1} 页 · 图 {image_count}\n{desc}"
            )
        except Exception as e:
            print(f"[pdf-vision] 第 {page_idx+1} 页图 {image_count} 转写失败: {e}", flush=True)
            image_descriptions.append(f"### 第 {page_idx + 1} 页 · 图 {image_count}\n[转写失败]")

    out_parts = []
    if text_parts:
        out_parts.append("--- PDF 文字层 ---")
        out_parts.extend(text_parts)
    if image_descriptions:
        out_parts.append(f"\n--- PDF 嵌入图识别({image_count} 张 · 上限 {PDF_MAX_VISION_IMAGES})---")
        out_parts.extend(image_descriptions)

    # 2026-05-18 老板拍 · 扫描 PDF / 纯图 PDF 救援 ·
    # 嵌入图为空(get_images 拿不到) + 文字层很短(< 50 字) → 整页 render 走 qwen3.6-plus 视觉识别
    # 老板原话:用 qwen3.6 看 PDF · 不要用其他模型
    merged_so_far = "\n\n".join(out_parts).strip()
    if len(merged_so_far) < 50:
        print(f"[pdf-vision] 文字层+嵌入图共 {len(merged_so_far)} 字 · 触发整页 vision 救援", flush=True)
        try:
            page_vision_text = await _pdf_pages_to_images_via_vision(
                pdf_bytes, filename, max_pages=PDF_FULL_PAGE_VISION_MAX_PAGES,
            )
        except Exception as e:
            print(f"[pdf-vision] 整页 vision 救援失败: {e}", flush=True)
            page_vision_text = ""
        if page_vision_text:
            out_parts.append(f"\n--- PDF 整页视觉识别(qwen3.6-plus · 救扫描件)---")
            out_parts.append(page_vision_text)
            return "\n\n".join(out_parts)

    if not text_parts and not image_descriptions:
        # 极端:整页 vision 也救不到 → 退到 pypdf/pdfplumber 兜底
        return await _extract_pdf_text_fallback(pdf_bytes, filename)
    return "\n\n".join(out_parts)


async def _pdf_pages_to_images_via_vision(
    pdf_bytes: bytes,
    filename: str,
    max_pages: int = PDF_FULL_PAGE_VISION_MAX_PAGES,
    concurrency: int = PDF_FULL_PAGE_VISION_CONCURRENCY,
) -> str:
    """2026-05-18 老板拍 · 纯图 PDF / 扫描 PDF 救援
    pdf_reader 把每一页 render 成 PNG → qwen3.6-flash(_vision_image_to_text)看图转文字
    跟"嵌入图 vision"不同 · 这里是把**整页内容**当成图来识别 · 救文字层空 + 无嵌入图的扫描件
    限页数防 LLM 调爆 + Semaphore 并发限速(防 dashscope QPS · 同时大幅加速)

    2026-05-18 老板反馈"太慢了 · 能不能并发":
    · 顺序 10 页 × 19.2s/页 ≈ 192s
    · 并发 5 · 10 页 ≈ 50-80s(3-4x 加速)
    · qwen3.6-flash rpm=1200 远高于 5 并发 · 安全
    """
    try:
        from services import pdf_reader
    except Exception as e:
        print(f"[pdf-page-vision] PDF 解析库未装 · 跳过 vision · {e}", flush=True)
        return ""

    # 1. 先把所有页 render 成 PNG(CPU 操作 · 同步;在 pdf_reader 的锁内做完并关掉文档,单页失败跳过)
    try:
        rendered: list[tuple[int, bytes]] = pdf_reader.render_pages_png(pdf_bytes, max_pages=max_pages, scale=2)
    except Exception as e:
        print(f"[pdf-page-vision] 打开失败: {e}", flush=True)
        return ""

    if not rendered:
        return ""

    # 2. 并发调 vision · Semaphore 限并发(防 QPS · 同时大幅加速)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(page_idx: int, png_bytes: bytes) -> tuple[int, str]:
        async with sem:
            page_filename = f"{filename}_page{page_idx + 1}.png"
            try:
                text = await _vision_image_to_text(png_bytes, page_filename)
                if text and text.strip():
                    return (page_idx, text.strip())
            except Exception as e:
                print(f"[pdf-page-vision] vision page {page_idx+1} 失败: {e}", flush=True)
        return (page_idx, "")

    results = await asyncio.gather(*[_run_one(i, b) for i, b in rendered], return_exceptions=False)

    # 3. 按 page_idx 排序 · 过滤空 · 拼接
    out_parts = [
        f"### 第 {idx + 1} 页\n{text}"
        for idx, text in sorted(results, key=lambda x: x[0])
        if text
    ]
    return "\n\n".join(out_parts) if out_parts else ""


async def _extract_pdf_text_fallback(pdf_bytes: bytes, filename: str) -> str:
    """pdf_reader 失败兜底:复用 _extract_upload_text(走 pypdf/pdfplumber)"""
    import tempfile
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        # 用 services.file_analyzer 兜底(已经在 _extract_upload_text 里用了)
        try:
            from services.file_analyzer import analyze_file
            result = analyze_file(tmp_path)
            if result.get("success") and result.get("raw_text"):
                return str(result.get("raw_text") or "")
        except Exception as e:
            print(f"[pdf-vision-fallback] file_analyzer 也失败: {e}", flush=True)
        raise HTTPException(status_code=400, detail=f"PDF 无法解析: {filename}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
