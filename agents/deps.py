"""Agent 运行时依赖注入"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
import httpx

logger = logging.getLogger("GEO-AgentDeps")


@dataclass
class OmniRankDeps:
    """每次请求构建，注入到 Agent 和所有工具"""
    user_id: int
    username: str
    is_admin: bool
    token: str                              # JWT，用于内部 HTTP 调用
    paid_points: int = 0
    bonus_points: int = 0
    current_profile_id: Optional[str] = None
    current_brand_id: Optional[int] = None
    current_page: Optional[str] = None      # 前端当前页面（FAB 上下文感知）
    user_mode: str = "agent"                # "c" | "agent" | "c_with_trial"，v3.2 引入
    agent_level: int = 0                    # 0/1/2，用户等级
    http_client: httpx.AsyncClient = field(default=None, repr=False)
    progress_queue: Optional[asyncio.Queue] = field(default=None, repr=False)

    @property
    def total_points(self) -> int:
        return self.paid_points + self.bonus_points

    @property
    def auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }


async def build_deps(request, body: dict) -> OmniRankDeps:
    """从 FastAPI Request + 请求体构建 deps"""
    from fastapi import HTTPException

    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    user_id = user.get("user_id") or user.get("id")

    # 从 Authorization header 提取 token
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""

    # 查钱包余额
    import asyncio
    from db.wallet_db import get_wallet_balance
    wallet = await asyncio.to_thread(get_wallet_balance, user_id)

    profile_id = body.get("profile_id")
    brand_id = body.get("brand_id")

    # 2026-04-17 P1-C: profile_id 必须和 brand_id 属于同一品牌
    # 原问题：用户传 brand_id=185（深圳全屋定制）+ 旧 profile_id=VuRCnFQe（属 brand=168）
    # → context_card 返回错的 profile "企业营销"，AI 基于错画像生成方案
    # 修复：若两者都传，校验 profile.brand_id == 传入 brand_id，不一致则以 brand_id 为准重查
    from db.connection import get_connection
    if brand_id and profile_id:
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT brand_id FROM client_profiles WHERE id = %s",
                    (profile_id,)
                )
                row = cur.fetchone()
                if row and row.get("brand_id") != brand_id:
                    # 不匹配 → 清掉 profile_id，走下面的自动关联
                    logger.info(
                        f"[deps] profile_id 和 brand_id 不匹配: profile {profile_id} "
                        f"属于 brand {row.get('brand_id')} != 请求 brand {brand_id}，重新关联"
                    )
                    profile_id = None
            finally:
                conn.close()
        except Exception:
            pass

    # 自动关联：有 brand_id 但没 profile_id → 查该品牌的 profile
    if brand_id and not profile_id:
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM client_profiles WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL) ORDER BY updated_at DESC LIMIT 1",
                    (brand_id,)
                )
                row = cur.fetchone()
                if row:
                    profile_id = row["id"]
            finally:
                conn.close()
        except Exception:
            pass  # 查不到不阻塞

    return OmniRankDeps(
        user_id=user_id,
        username=user.get("username", ""),
        is_admin=user.get("is_admin", False),
        token=token,
        paid_points=wallet.get("paid_points", 0) if wallet else 0,
        bonus_points=wallet.get("bonus_points", 0) if wallet else 0,
        agent_level=wallet.get("agent_level", 0) if wallet else 0,
        current_profile_id=profile_id,
        current_brand_id=brand_id,
        current_page=body.get("current_page"),
        user_mode=body.get("user_mode", "agent"),   # 默认 agent，C 端端点传 "c"
        # 2026-04-17 P1-9: 120s 对 full-plan 工具（generate_geo_plan / estimate_managed_word_plan）
        # p95 可能超时；nginx SSE block 已 600s，http_client 提到 300s 匹配
        http_client=httpx.AsyncClient(base_url="http://localhost:8000", timeout=300.0),
    )
