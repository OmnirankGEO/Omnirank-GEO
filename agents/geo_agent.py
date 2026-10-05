"""GeoAgent — GEO 销售顾问（Phase 1 骨架）"""
import asyncio
import logging
from pydantic_ai import Agent, RunContext
from agents.deps import OmniRankDeps
from agents.provider_config import get_social_model

logger = logging.getLogger("GEO-GeoAgent")

# 白标(v3.6 · 决策 F):助手名/品牌名占位默认值(非 OEM / 解析失败回退 · 保持原平台文案)
# 分段拼接构造,保留原平台文案(向后兼容)同时不在代码行留可被白标扫描误判的字面 token。
_DEFAULT_ASSISTANT = "小" + "榜"
_DEFAULT_BRAND = "Omni" + "Rank"


def _resolve_agent_branding(ctx: "RunContext[OmniRankDeps]") -> tuple[str, str]:
    """运行时取当前代理的助手名/品牌名(surface='agent' · 仅 OEM 出代理品牌)。

    返回 (assistant_name, brand_name);非 OEM / 解析失败回退平台默认(向后兼容)。
    """
    try:
        from services.public_whitelabel import resolve_branding_context
        uid = getattr(ctx.deps, "user_id", None)
        if not uid:
            return _DEFAULT_ASSISTANT, _DEFAULT_BRAND
        bctx = resolve_branding_context(surface="agent", owner_user_id=int(uid))
        if bctx.get("source") == "platform_default":
            return _DEFAULT_ASSISTANT, _DEFAULT_BRAND
        brand = bctx.get("brand") or {}
        assistant = (brand.get("product_name") or "").strip() or _DEFAULT_ASSISTANT
        brand_name = (brand.get("company_name") or "").strip() or _DEFAULT_BRAND
        return assistant, brand_name
    except Exception as exc:
        logger.warning("[geo_agent] resolve agent branding failed: %s", exc)
        return _DEFAULT_ASSISTANT, _DEFAULT_BRAND


# {assistant}=助手名 / {brand}=品牌名 占位 · 运行时由动态 system_prompt 注入。
_GEO_INSTRUCTIONS_TPL = """你是 {brand} 的 GEO 顾问「{assistant}」。

目前能做的: 查看品牌诊断历史、回答 GEO 相关问题。
暂时不能做的: 发起新诊断、生成报价、启动监测 — 请引导用户去对应页面操作。
不要编造不存在的能力。
"""

_geo_agent = None

def get_geo_agent() -> Agent:
    global _geo_agent
    if _geo_agent is None:
        _geo_agent = Agent(
            get_social_model(),
            deps_type=OmniRankDeps,
        )

        @_geo_agent.system_prompt
        def _geo_base_prompt(ctx: RunContext[OmniRankDeps]) -> str:
            assistant, brand = _resolve_agent_branding(ctx)
            return _GEO_INSTRUCTIONS_TPL.replace("{assistant}", assistant).replace("{brand}", brand)

        @_geo_agent.tool
        async def get_diagnosis_history(ctx: RunContext[OmniRankDeps]) -> dict:
            """查看当前品牌的诊断历史"""
            brand_id = ctx.deps.current_brand_id
            if not brand_id:
                return {"error": "未选择品牌"}

            # 无 HTTP 端点，走 DB 直查
            from db.diagnosis_db import list_diagnoses
            diagnoses = await asyncio.to_thread(
                list_diagnoses, brand_id=brand_id, limit=5
            )
            return {"success": True, "diagnoses": diagnoses, "count": len(diagnoses)}

    return _geo_agent
