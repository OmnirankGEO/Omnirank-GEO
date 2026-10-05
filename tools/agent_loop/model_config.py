"""Model defaults for the Social Studio agent loop.

Step 4 only needs a stable config surface. Step 13 will decide the final
production default after the 50-scenario stress test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentModelConfig:
    name: str
    base_url: str
    api_key_env: tuple[str, ...]
    temperature: float = 0.2
    max_tokens: int = 1200
    thinking_disabled: bool = False


DEFAULT_MODEL = os.getenv("SOCIAL_AGENT_DEFAULT_MODEL", "deepseek-v4-flash")
FALLBACK_MODEL = os.getenv("SOCIAL_AGENT_FALLBACK_MODEL", "kimi-k2.6")

MODEL_CONFIGS: dict[str, AgentModelConfig] = {
    "deepseek-v4-flash": AgentModelConfig(
        name="deepseek-v4-flash",
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        api_key_env=("DEEPSEEK_API_KEY_ORCHESTRATOR", "DEEPSEEK_API_KEY_REALTIME", "DEEPSEEK_API_KEY"),
        temperature=0.2,
        max_tokens=1200,
    ),
    "kimi-k2.6": AgentModelConfig(
        name="kimi-k2.6",
        base_url=os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/"),
        api_key_env=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        # 2026-05-16 实测:thinking=disabled+temperature=0.6 是唯一可用组合
        temperature=0.6,
        max_tokens=1200,
        thinking_disabled=True,
    ),
}


def get_model_config(model: str | None = None) -> AgentModelConfig:
    selected = model or DEFAULT_MODEL
    if selected in MODEL_CONFIGS:
        return MODEL_CONFIGS[selected]
    return AgentModelConfig(
        name=selected,
        base_url=os.getenv("SOCIAL_AGENT_OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        api_key_env=("SOCIAL_AGENT_OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
    )


def resolve_api_key(config: AgentModelConfig) -> str:
    for env_name in config.api_key_env:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""
