"""
LLM Provider 配置 - 支持前端选择不同模型
"""
import os
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH


class ProviderType(str, Enum):
    DEEPSEEK = "deepseek"
    DASHSCOPE = "dashscope"
    SILICONFLOW = "siliconflow"
    OPENROUTER = "openrouter"
    DOUBAO = "doubao"
    KIMI = "kimi"


@dataclass
class ModelConfig:
    """模型配置"""
    model_id: str           # 模型标识
    display_name: str       # 显示名称
    provider: ProviderType  # 提供商
    api_url: str           # API端点
    api_key_env: str       # API Key环境变量名
    max_tokens: int = 12000  # 增加到12000以支持长文章
    supports_streaming: bool = True
    estimated_speed: str = "fast"  # fast/medium/slow
    quality_tier: str = "high"     # high/medium/low


# ========================================
# 所有可用模型配置
# ========================================
LLM_PROVIDERS: Dict[str, ModelConfig] = {
    
    # ===== DeepSeek 官方 =====
    "deepseek-v3": ModelConfig(
        # 🔴 [WO_206 c1b] 原写 deepseek-chat,注释说「官方 API 通常用它指代 V3」——
        #    那句话 2026-09-13 之后不成立了:官方把 Flash 档改名 deepseek-flash,
        #    deepseek-chat 在定价页上已经不出现。取常量,别再让注释替代事实。
        model_id=DEEPSEEK_OFFICIAL_FLASH,
        display_name="DeepSeek V3 (官方)",
        provider=ProviderType.DEEPSEEK,
        api_url="https://api.deepseek.com/v1/chat/completions",
        api_key_env="DEEPSEEK_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="high"
    ),
    
    # ===== DashScope (阿里云) =====
    # 2026-05-09 升级:加 v4-flash / v4-pro · v3.2 保留作历史遗留(老报价单/老 employee 仍引用)
    "dashscope-deepseek-v4-flash": ModelConfig(
        model_id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash (阿里云 · 速度+轻量)",
        provider=ProviderType.DASHSCOPE,
        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key_env="DASHSCOPE_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="high"
    ),
    "dashscope-deepseek-v4-pro": ModelConfig(
        model_id="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro (阿里云 · 重度推理+展示质量)",
        provider=ProviderType.DASHSCOPE,
        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key_env="DASHSCOPE_API_KEY",
        max_tokens=8000,
        estimated_speed="medium",
        quality_tier="high"
    ),
    # 2026-05-22 老板拍板:V3.2 → V4 全切 · dashscope-deepseek-v3.2 配置 key 保留兼容
    # 但实际指向 deepseek-v4-flash(便宜 + 快)· settings.json 历史 row 自动升级
    "dashscope-deepseek-v3.2": ModelConfig(
        model_id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash (阿里云 · V3.2 已淘汰自动升级)",
        provider=ProviderType.DASHSCOPE,
        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key_env="DASHSCOPE_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="high"
    ),
    "dashscope-qwen-max": ModelConfig(
        model_id="qwen3.7-max",   # [2026-06-06 老板] 升级 qwen3-max→qwen3.7-max(内部写作生成旗舰)
        display_name="Qwen3.7 Max (阿里云)",
        provider=ProviderType.DASHSCOPE,
        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        api_key_env="DASHSCOPE_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="medium"
    ),
    
    # ===== SiliconFlow (硅基流动) =====
    "siliconflow-deepseek-v3": ModelConfig(
        model_id="deepseek-ai/DeepSeek-V3",
        display_name="DeepSeek V3 (硅基流动)",
        provider=ProviderType.SILICONFLOW,
        api_url="https://api.siliconflow.cn/v1/chat/completions",
        api_key_env="SILICONFLOW_API_KEY",
        max_tokens=8000,
        estimated_speed="medium",
        quality_tier="high"
    ),
    
    # ===== OpenRouter =====
    "openrouter-claude-opus": ModelConfig(
        model_id="anthropic/claude-opus-4.5",
        display_name="Claude Opus 4.5 (OpenRouter)",
        provider=ProviderType.OPENROUTER,
        api_url="https://openrouter.ai/api/v1/chat/completions",
        api_key_env="OPENROUTER_API_KEY",
        max_tokens=8000,
        estimated_speed="medium",
        quality_tier="high"
    ),
    "openrouter-gemini-flash": ModelConfig(
        model_id="google/gemini-3-flash",
        display_name="Gemini 3 Flash (OpenRouter)",
        provider=ProviderType.OPENROUTER,
        api_url="https://openrouter.ai/api/v1/chat/completions",
        api_key_env="OPENROUTER_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="medium"
    ),
    "openrouter-gemini-pro": ModelConfig(
        model_id="google/gemini-3-pro",
        display_name="Gemini 3 Pro (OpenRouter)",
        provider=ProviderType.OPENROUTER,
        api_url="https://openrouter.ai/api/v1/chat/completions",
        api_key_env="OPENROUTER_API_KEY",
        max_tokens=8000,
        estimated_speed="medium",
        quality_tier="high"
    ),
    
    # ===== Doubao (豆包/火山引擎) =====
    "doubao-seed": ModelConfig(
        model_id="doubao-seed-2-0-pro-260215",
        display_name="豆包 Seed 2.0 Pro (火山引擎)",
        provider=ProviderType.DOUBAO,
        api_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        api_key_env="DOUBAO_API_KEY",
        max_tokens=8000,
        estimated_speed="fast",
        quality_tier="high"
    ),
    
    # ===== Kimi (Moonshot) =====
    "kimi-k2-thinking": ModelConfig(
        model_id="kimi-k2-thinking",
        display_name="Kimi K2 Thinking",
        provider=ProviderType.KIMI,
        api_url="https://api.moonshot.cn/v1/chat/completions",
        api_key_env="KIMI_API_KEY",
        max_tokens=8000,
        estimated_speed="slow",
        quality_tier="high"
    ),
}

# 默认模型
DEFAULT_MODEL = "dashscope-qwen-max"  # 内部model_id已升 qwen3.7-max(2026-06-06)


def get_model_config(model_key: str) -> Optional[ModelConfig]:
    """获取模型配置"""
    return LLM_PROVIDERS.get(model_key)


def get_api_key(model_key: str) -> Optional[str]:
    """获取模型的API Key"""
    config = get_model_config(model_key)
    if config:
        return os.getenv(config.api_key_env)
    return None


def get_available_models() -> List[Dict]:
    """获取所有可用模型列表（供前端使用）"""
    models = []
    for key, config in LLM_PROVIDERS.items():
        api_key = os.getenv(config.api_key_env)
        models.append({
            "key": key,
            "model_id": config.model_id,
            "display_name": config.display_name,
            "provider": config.provider.value,
            "available": bool(api_key),
            "estimated_speed": config.estimated_speed,
            "quality_tier": config.quality_tier
        })
    return models


def get_models_by_provider(provider: ProviderType) -> List[str]:
    """按提供商获取模型列表"""
    return [
        key for key, config in LLM_PROVIDERS.items()
        if config.provider == provider
    ]


# 并行配置
PARALLEL_CONFIG = {
    "max_concurrent": 10,           # 最大并发数
    "timeout_per_request": 300,     # 单次请求超时（秒）
    "retry_count": 2,               # 重试次数
    "batch_size": 10,               # 批量大小
    
    # 按提供商的推荐并发数
    "provider_limits": {
        "deepseek": 10,
        "dashscope": 10,
        "siliconflow": 10,
        "openrouter": 5,   # OpenRouter可能有更严格的限流
        "doubao": 10,
        "kimi": 5,         # Kimi thinking模型较慢
    }
}


def get_recommended_concurrent(provider: ProviderType) -> int:
    """获取推荐并发数"""
    return PARALLEL_CONFIG["provider_limits"].get(provider.value, 5)
