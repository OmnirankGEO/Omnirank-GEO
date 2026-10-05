"""开源版署名位的公开读取口(WO_329)。

GET /api/public/oss-attribution —— 免登录(落在 auth 中间件已放行的 /api/public/ 前缀下),只读,不碰库。
    关(主仓 / 线上):{"enabled": false}
    开(开源版):    {"enabled": true, "text": "…", "text_en": "…", "href": "https://omnirank.cn/opensource/"}
应用界面页脚与客户门户页脚(token 页面免登录)都从这里取同一份字;开关与文字的唯一来源是 config/oss_attribution.py。
锁:tests/oss_attribution_2026_10_02
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from config import oss_attribution

router = APIRouter(prefix="/api/public", tags=["开源署名"])


@router.get("/oss-attribution")
def get_oss_attribution(response: Response) -> dict:
    # 开关只随发版变(常量),可以缓存;关着时回的东西里没有任何品牌字
    response.headers["Cache-Control"] = "public, max-age=300"
    return oss_attribution.payload()
