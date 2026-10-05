"""多 LLM Provider 配置 — 使用 OpenAI 兼容模式，不依赖专用 Provider"""
import os
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import logging

logger = logging.getLogger("GEO-AgentProvider")


def get_chat_model(role: str = "default"):
    """根据角色返回模型（带 Fallback）

    全部使用 OpenAI 兼容协议 + 自定义 base_url，
    避免依赖 pydantic_ai.providers.alibaba 等可能不存在的模块。
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.providers.openai import OpenAIProvider

    models = []

    # DashScope (Qwen) — 首选，OpenAI 兼容协议
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if dashscope_key:
        try:
            model_name = "qwen3.6-flash" if role == "router" else "qwen3.6-plus"
            qwen = OpenAIChatModel(model_name, provider=OpenAIProvider(
                api_key=dashscope_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            ))
            models.append(qwen)
            logger.info(f"DashScope 初始化成功: {model_name}")
        except Exception as e:
            logger.warning(f"DashScope 初始化失败: {e}")

    # DeepSeek — 备选
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        try:
            ds = OpenAIChatModel(DEEPSEEK_OFFICIAL_FLASH, provider=OpenAIProvider(
                api_key=deepseek_key,
                base_url="https://api.deepseek.com/v1",
            ))
            models.append(ds)
        except Exception as e:
            logger.warning(f"DeepSeek 初始化失败: {e}")

    # Kimi — 备选
    kimi_key = os.getenv("KIMI_API_KEY")
    if kimi_key:
        try:
            kimi = OpenAIChatModel("moonshot-v1-auto", provider=OpenAIProvider(
                api_key=kimi_key,
                base_url="https://api.moonshot.cn/v1",
            ))
            models.append(kimi)
        except Exception as e:
            logger.warning(f"Kimi 初始化失败: {e}")

    if not models:
        raise RuntimeError("至少需要配置一个 LLM API Key (DASHSCOPE/DEEPSEEK/KIMI)")

    if len(models) == 1:
        return models[0]
    return FallbackModel(*models)


def get_router_model():
    return get_chat_model("router")

def get_social_model():
    return get_chat_model("social")
