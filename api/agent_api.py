"""Agent 对话 SSE 端点"""
import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException


logger = logging.getLogger("GEO-AgentAPI")
router = APIRouter(prefix="/api/agent", tags=["AI助手"])


# ========== 顾问映射 ==========

PLATFORM_ADVISOR = {
    "douyin": "huang-douyin",
    "xiaohongshu": "teacher_shu",
    "shipinhao": "sawadika",
}


# ========== 请求模型 ==========

# ========== 工具调用摘要记录 ==========

# ========== AI 幻觉检测：说"卡片已发"但 tool_calls 为空 ==========
# CTO-13.0 2026-04-19 · D6 方案（CTO-14.0 对齐：不改超时逻辑，在 stream 正常结束后做后处理）
# 背景：cdacb20 重写社媒 agent(已随开源 E3 删除)的 prompt 加了禁忌"禁说卡片已发"，但模型有时仍幻觉
# 检测原则：文本命中"卡片已发"类动词短语 + cards 为空 → 双条件触发纠错提示

# [CTO-13.0 2026-04-19 P0] Detect-and-Retry：跨轮幻觉标记
# 当前轮检测到幻觉 → 记 session_id + 时间戳；下一轮 agent_prompt 构造时消费此标记
# 注入强引导 hint 让 AI 必须真调工具。5 分钟 TTL 防脏数据；单进程内存（WORKERS 级生效，
# 配合 nginx ip_hash 同 session 稳定命中同进程），即使跨进程失效也只是回退到老行为。
# ========== 工具结果 → card 事件 ==========

# ========== SSE 端点 ==========

# ========== ABCD 确认回执端点(P0-D2 真修)==========
#
# 修前(P0-D2 audit 找到):FE handleConfirmationSelect 走 onPrompt(`[标题] 选项\n选择值:X`)
# 走 /api/agent/chat 当成新用户输入 · 后果:
#   1. 结构丢失:LLM 只看到 free-text · 可能误解为新问题
#   2. 重复工具调用:LLM 不知道这是 callback · 可能重跑同一组工具(实际扣费风险)
#   3. classify_intent 二次跑(意图分类重复)
#
# 修后(本端点):
#   1. 接 structured payload(request_type/option_value/option_label)
#   2. 转成 `[__agent_confirm__] request_type=X value=Y label=Z` 结构化 prefix 文本
#   3. 直接 forward 给 /chat 处理 · /chat 内部检测 prefix 跳 classify_intent + 注入 callback 系统提示
#   4. SSE 协议复用 · FE 不需重写 stream parser

# ========== 草稿区 API ==========

@router.get("/draft-workspace")
async def get_draft_workspace(brand_id: int, request: Request):
    """获取品牌的草稿区数据（互动记录）"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "未登录")
    user_id = user.get("user_id")

    from agents.session_manager import get_drafts
    drafts = await asyncio.to_thread(get_drafts, user_id, brand_id)

    return {
        "success": True,
        "drafts": [
            {
                "id": d.get("id"),
                "item_type": d.get("item_type"),
                "item_key": d.get("item_key"),
                "item_data": d.get("item_data", {}),
                "source": d.get("source", "ai_chat"),
                "created_at": str(d.get("created_at", "")),
                "expires_at": str(d.get("expires_at", "")) if d.get("expires_at") else None,
            }
            for d in drafts
        ],
        "count": len(drafts),
    }
