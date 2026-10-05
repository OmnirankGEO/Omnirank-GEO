"""
V3.5 W3 · 协议签约 FastAPI dependency gate

用法(装饰 W2 已建 endpoint · 不改函数体):
    @router.put("/pricing/skus/{id}", dependencies=[Depends(require_signed_agreement)])
    async def agent_pricing_sku_save(...): ...

未签 → 403 + 引导签约 URL
"""
from fastapi import Request, HTTPException

from db.connection import get_db
from services.agent_agreement import is_agent_signed, CURRENT_VERSION, PRICING_DISCLAIMER_VERSION
from auth.user_ctx import current_user_id


def require_signed_agreement(request: Request) -> dict:
    """
    要求当前登录用户 · 若是代理 · 必须已签代理合作协议(CURRENT_VERSION)
    admin 豁免 · 非代理(普通用户)豁免(进不来 W2 endpoint 已 _require_agent 兜底)
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    if user.get("is_admin"):
        return {"signed": True, "exempt_reason": "admin"}

    user_id = user.get("user_id") or current_user_id(user)
    with get_db() as conn:
        cur = conn.cursor()
        # 仅 agent_level >= 1 才检查协议(普通用户在 W2 _require_agent 已挡)
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        level = (row.get("agent_level") if row and isinstance(row, dict) else (row[0] if row else 0)) or 0
        if level < 1:
            return {"signed": True, "exempt_reason": "not_agent"}
        if is_agent_signed(cur, user_id, CURRENT_VERSION):
            return {"signed": True, "version": CURRENT_VERSION}
    raise HTTPException(
        status_code=403,
        detail={
            "code": "AGREEMENT_NOT_SIGNED",
            "required_version": CURRENT_VERSION,
            "agreement_url": "/agent/agreement",
            "message": f"请先签署代理合作协议 {CURRENT_VERSION}",
        },
    )


def require_pricing_authority(request: Request) -> dict:
    """报价定价功能(自设报价系数 / 单篇成本)统一闸。

    [2026-06-08 老板拍板 · 实名=X] 不强制实名,签《报价定价免责协议》即可开通:
      - admin → 豁免
      - 服务商(agent_level>=1·已走 v2.3 经营功能协议)→ 放行(不需另签此份)
      - 普通用户(agent_level=0)→ 必须已签 PRICING_DISCLAIMER_VERSION,否则 403 引导签署
    用法:装在自设报价系数 / 单篇成本的 endpoint(不改函数体)。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    if user.get("is_admin"):
        return {"allowed": True, "exempt_reason": "admin"}

    user_id = user.get("user_id") or current_user_id(user)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        level = (row.get("agent_level") if row and isinstance(row, dict) else (row[0] if row else 0)) or 0
        if level >= 1:
            return {"allowed": True, "exempt_reason": "agent"}
        if is_agent_signed(cur, user_id, PRICING_DISCLAIMER_VERSION):
            return {"allowed": True, "version": PRICING_DISCLAIMER_VERSION}
    raise HTTPException(
        status_code=403,
        detail={
            "code": "PRICING_DISCLAIMER_NOT_SIGNED",
            "required_version": PRICING_DISCLAIMER_VERSION,
            "agreement_url": "/pricing",
            "message": "请先阅读并同意《报价定价免责协议》后再设置价格",
        },
    )
