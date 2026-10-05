"""Excel 导出 helper · CTO-15.18 PM 干预 类 C(C.2/C.3/C.4)

老板红线(2026-04-28):
- 旧版有 Excel 导出 / M3 砍了 → 中坚代理回旧版根本动机之一(根因 #4 工具回归)
- Q4 老板裁决:openpyxl 完整 xlsx · 不用 CSV(中文 BOM 是技术债)

用法:
    from utils.excel_export import build_xlsx_response

    # 监测 / 报告 / 报价单 endpoint 中:
    return build_xlsx_response(
        filename="omnirank_quotes_2026-04-28.xlsx",
        sheet_name="报价单",
        headers=["客户", "月费", ...],
        rows=[[r["brand"], r["monthly_price"], ...] for r in items],
    )
"""
from __future__ import annotations
from typing import Iterable, Sequence, Any
from io import BytesIO
from datetime import datetime
from urllib.parse import quote
from fastapi.responses import StreamingResponse


def build_xlsx_bytes(
    sheet_name: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    column_widths: Sequence[int] | None = None,
) -> bytes:
    """生成 xlsx 文件字节流

    Args:
        sheet_name: 工作表名(中文 OK)
        headers: 列标题
        rows: 行数据(每行 list[Any] · 跟 headers 等长)
        column_widths: 列宽(可选 · 默认 18)

    Returns:
        bytes · xlsx 文件二进制
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel sheet name 上限 31 字符

    # 表头样式
    header_fill = PatternFill(start_color="F0F4F8", end_color="F0F4F8", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=11, bold=True, color="1F2937")
    cell_font = Font(name="Microsoft YaHei", size=10)
    center_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # 写表头
    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=ci, value=h)
        c.fill = header_fill
        c.font = header_font
        c.alignment = center_align

    # 写数据
    for ri, row in enumerate(rows, start=2):
        for ci, val in enumerate(row, start=1):
            # 时间对象自动转 ISO 字符串
            if isinstance(val, datetime):
                val = val.strftime("%Y-%m-%d %H:%M")
            c = ws.cell(row=ri, column=ci, value=val if val is not None else "")
            c.font = cell_font
            c.alignment = center_align

    # 列宽
    widths = list(column_widths) if column_widths else [18] * len(headers)
    for i, w in enumerate(widths, start=1):
        col_letter = ws.cell(row=1, column=i).column_letter
        ws.column_dimensions[col_letter].width = w

    # 冻结首行
    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_xlsx_response(
    filename: str,
    sheet_name: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    column_widths: Sequence[int] | None = None,
) -> StreamingResponse:
    """生成 FastAPI 下载响应(中文文件名兼容)

    用 RFC 5987 编码中文文件名(filename*=UTF-8''...)避免浏览器乱码
    """
    data = build_xlsx_bytes(sheet_name, headers, rows, column_widths)
    encoded = quote(filename)
    headers_resp = {
        "Content-Disposition": f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}",
    }
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers_resp,
    )
