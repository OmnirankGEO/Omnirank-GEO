"""C 端豆包式对话 API（v3.2）

三个核心端点：
1. POST /api/c-end/chat          — 对话 SSE 流式（复用 SocialAgent，强制 user_mode='c'）
2. POST /api/c-end/log-action    — 记录用户确认行为（免责证据）
3. PATCH /api/c-end/settings/mode — 切换界面模式（L1+ 可用）
"""
import logging
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Literal
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-CEndAPI")
router = APIRouter(prefix="/api/c-end", tags=["C 端对话"])


# ============================================
# 请求模型
# ============================================

# ============================================
# 1. C 端对话（复用 agent_api 逻辑）
# ============================================

# ============================================
# 2. 用户行为日志（合规证据）
# ============================================

# ============================================
# 3. 界面模式切换（代理可用）
# ============================================

def _load_user_mode_sync(user_id: int) -> dict:
    """[CTO-15.23 2026-05-10 P1 async safety]
    抽出 get_user_mode 的 2 个 sync DB 调用 (wallet_balance + user_settings.preferred_mode)
    给 asyncio.to_thread wrap 用 · 防 sync psycopg2 阻塞 event loop
    """
    from db.wallet_db import get_wallet_balance
    from db.connection import get_connection
    wallet = get_wallet_balance(user_id) or {}
    preferred_mode = None
    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT preferred_mode FROM user_settings WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if row:
                preferred_mode = row["preferred_mode"]
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            import logging
            logging.getLogger("GEO-c_end_api").warning(
                f"[get_user_mode] preferred_mode 查询失败 user_id={user_id}: {e}"
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"wallet": wallet, "preferred_mode": preferred_mode}


@router.get("/settings/mode")
async def get_user_mode(request: Request):
    """查当前用户偏好模式 + 等级（登录后前端调用决定路由）

    [CTO-15.23 2026-05-10 P1 async safety]
      老:async def 内直接 get_wallet_balance + get_connection + cur.execute (全 sync psycopg2)
         WORKERS=1 下阻塞 event loop · 别的 request 全卡 · 登录后首屏路由判定串行
      新:sync 部分抽 _load_user_mode_sync · async def 走 asyncio.to_thread wrap · 释放 event loop
    """
    import asyncio
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    user_id = user.get("user_id") or current_user_id(user)
    is_admin = user.get("is_admin") is True

    data = await asyncio.to_thread(_load_user_mode_sync, user_id)
    wallet = data["wallet"]
    agent_level = wallet.get("agent_level", 0)
    preferred_mode = data["preferred_mode"]

    # Phase C · CTO-15.10 · 2026-04-27 · M3 灰度白名单(env M3_USER_WHITELIST="25,27,..." 逗号分隔 user_id)。
    # [WO_281] 它**不再影响落点**(见下方推荐路由)。m3_enabled 仍照旧算出并返回,只为回包字段兼容;
    #   灰度名单与 users 列都不动。
    import os
    m3_whitelist_env = os.getenv("M3_USER_WHITELIST", "").strip()
    m3_whitelist_ids: set[int] = set()
    if m3_whitelist_env:
        for part in m3_whitelist_env.split(","):
            try:
                m3_whitelist_ids.add(int(part.strip()))
            except (ValueError, TypeError):
                pass
    # admin 自动开启 + 在白名单的 user_id 开启 + 代理 (L1+) 才有意义(L0 不能进 M3)
    m3_enabled = (is_admin or user_id in m3_whitelist_ids) and (is_admin or agent_level >= 1)

    # 决定推荐路由:所有人一律 "/"。
    # [WO_222-c1b] `agent_level == 0 → /c/chat` 撤除 —— Owner 2026-09-15 直令:普通账号登录默认落到
    #   与服务商**相同**的工作台;`/c/chat` 本身也是死路由(App.tsx 把 /c 与 /c/* 一律 <Navigate to="/"/>)。
    # [WO_281] `m3_enabled → /m3/sales/today` 撤除(原来 admin 与灰度白名单服务商,不论 preferred_mode
    #   是什么 —— agent、c、没选 —— 都落这里)。M3 页面在 E3 删除名单(「左侧栏没有 = 废弃」),App.tsx 把 /m3 与
    #   /m3/* 一律 <Navigate to="/"/>,所以旧值在浏览器里最终也落 "/"。但前端 determineTargetRoute 会原样
    #   跟随本字段,多绕一跳,等 /m3 路由删掉就是死路 —— 这里不再发死路径。判据:tests/m3_landing_retired_2026_09_23。
    recommended = "/"

    return {
        "user_id": user_id,
        "is_admin": is_admin,
        "agent_level": agent_level,
        "preferred_mode": preferred_mode,
        "recommended_route": recommended,
        "m3_enabled": m3_enabled,
        # [WO_222-c1b] 切换不再按身份分档(见上面 PATCH /settings/mode 撤闸)。
        "can_switch": True,
    }
