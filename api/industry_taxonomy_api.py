"""行业大类字典 · 只读端点(WO_267 · 2026-09-23)。

`GET /api/industry-taxonomy` → 大类 key + 中文名 + 细分名。
给前端「我的客户」品牌表单的行业大类下拉、点亮调研弹窗的「改选大类」用 —— 不再在前端硬编码旧值。
只给前端要用的:别名 / 强别名 / 调研行 slug 这些路由内部细节不出后端(`taxonomy_public_payload`)。
鉴权:全局中间件对 /api/* 默认要求登录;内容是产品分类表,不含任何租户数据。
"""
from fastapi import APIRouter

from services.industry_taxonomy import taxonomy_public_payload

router = APIRouter(prefix="/api", tags=["行业大类"])


@router.get("/industry-taxonomy", summary="行业大类字典(只读)")
async def get_industry_taxonomy():
    return taxonomy_public_payload()
