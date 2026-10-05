"""
LLM配置工具 - 从settings.json读取模型配置
"""

import os
import json
from pathlib import Path
from typing import Tuple, Optional

from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

# 默认API URLs
API_URLS = {
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    "kimi": "https://api.moonshot.cn/v1/chat/completions",
}

# 环境变量名映射
ENV_KEY_NAMES = {
    "dashscope": "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "doubao": "DOUBAO_API_KEY",
    "kimi": "KIMI_API_KEY",
}


def get_llm_config(task_type: str, module: str = "writing") -> Tuple[str, str, str, str]:
    """
    从settings.json读取LLM配置
    
    Args:
        task_type: 任务类型，如 'geo_article', 'topic_planning', 'title_generation', 'geo_scoring' 等
        module: 模块类型，'writing' 或 'diagnosis'
    
    Returns:
        (api_url, api_key, model, provider)
    """
    settings_file = Path(__file__).parent.parent / "settings.json"

    # 默认值 · [2026-07-26] 按模块分默认,不再共用一个 dashscope 默认。
    # 起因:settings.json 是 gitignored 运行时配置,迁"release 目录 + 打标镜像"部署模型后
    # 没有任何挂载把它送进容器 → 本函数 settings_file.exists() 恒 False → 写作链**静默**
    # 落回 dashscope/qwen3.6-plus,而既定写作模型是官方线 Flash 档。漂移无告警、隐身数月。
    # 所以默认值本身必须是"既定模型"而非"随便一个能跑的模型":配置再丢也只会退回正确答案。
    # (部署层另有 compose 挂载修复,两层缺一不可 —— 只补挂载则下次丢文件重蹈覆辙。)
    if module == "writing":
        default_provider = "deepseek"
        # 🔴 [WO_206 c1b] 取常量不写字面量:官方 2026-09-13 把 v4-flash 改名 flash,
        #    而「既定写作模型」这件事在本仓有四个角色(发/认/比/计价)。
        #    写字面量就会有一处改了另一处没改,而那种漂移**不报错**。
        default_model = DEEPSEEK_OFFICIAL_FLASH
    else:  # diagnosis 保持原状,本次不动诊断链
        default_provider = "dashscope"
        default_model = "qwen3.6-plus"

    provider = default_provider
    model = default_model
    api_key = None
    
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            
            # 确定任务配置来源
            if module == "writing":
                tasks_config = settings.get("writing_tasks", {})
                fallback_provider = settings.get("writing_provider", default_provider)
                fallback_model = settings.get("writing_model", default_model)
            else:  # diagnosis
                tasks_config = settings.get("diagnosis_tasks", {})
                fallback_provider = settings.get("diagnosis_provider", default_provider)
                fallback_model = settings.get("diagnosis_model", default_model)
            
            # 读取特定任务配置
            task_config = tasks_config.get(task_type, {})
            if task_config:
                provider = task_config.get("provider", fallback_provider)
                model = task_config.get("model", fallback_model)
            else:
                provider = fallback_provider
                model = fallback_model
            
            # 获取API Key · [2026-06-11 写作迁 deepseek] deepseek 走 7 账号轮询池解决并发(回落 settings/单 key)
            if provider == "deepseek":
                try:
                    from services.llm.deepseek_key_pool import pick_deepseek_api_key
                    api_key = pick_deepseek_api_key() or settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY")
                except Exception:
                    api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY")
            else:
                key_field = f"{provider}_api_key"
                api_key = settings.get(key_field) or os.getenv(ENV_KEY_NAMES.get(provider, ""))
            
        except Exception as e:
            print(f"    ⚠️ 读取settings.json失败: {e}，使用默认配置")
    
    # 如果仍无API Key，从环境变量获取(deepseek 走 7 账号池)
    if not api_key:
        if provider == "deepseek":
            try:
                from services.llm.deepseek_key_pool import pick_deepseek_api_key
                api_key = pick_deepseek_api_key() or os.getenv("DEEPSEEK_API_KEY", "")
            except Exception:
                api_key = os.getenv("DEEPSEEK_API_KEY", "")
        else:
            api_key = os.getenv(ENV_KEY_NAMES.get(provider, "DASHSCOPE_API_KEY"), "")
    
    api_url = API_URLS.get(provider, API_URLS["dashscope"])
    
    return (api_url, api_key, model, provider)


def get_fallback_llm_config() -> Tuple[str, str, str, str]:
    """
    获取兜底 LLM 配置(2026-05-22 V3.2→V4 全切)
    当主模型失败时自动切换到 DeepSeek 直连 V4-flash

    Returns:
        (api_url, api_key, model, provider)
    """
    provider = "deepseek"
    # 🔴 [WO_206 c1b] 原来写的是 deepseek-chat(注释自称「= v4-flash alias」)。
    #    官方定价页上 deepseek-chat 已经不出现了,它现在指向哪一代官方没说 ——
    #    所以它只配当「认」(归一时认得),不配当「发」。
    model = DEEPSEEK_OFFICIAL_FLASH  # 官方线直连
    api_key = os.getenv("DEEPSEEK_API_KEY", "")

    # 也尝试从 settings.json 读取
    settings_file = Path(__file__).parent.parent / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            api_key = settings.get("deepseek_api_key") or api_key
        except Exception:
            pass

    api_url = API_URLS.get(provider, API_URLS["deepseek"])
    return (api_url, api_key, model, provider)


def get_thinking_disabled_params(api_url: str, model: str) -> dict:
    """返回关闭思考模式的 API body 参数 · 按 provider 区分语法

    GEO 文章生成是结构化生成任务(非推理任务)· 关思考模式省成本/提速
    经 35 篇实测(scripts/deepseek_v4_compare_report.md):
    - v4-flash thinking=ON 比 OFF 慢 17% + 贵 35% · 质量几无差异
    - v4-pro 同理

    - DeepSeek 直连 V4 / reasoner : {"thinking": {"type": "disabled"}}
    - DashScope Qwen 系列           : {"enable_thinking": False}
    - 其他(deepseek-chat/v3 等)     : {} (模型本身就无思考模式)
    """
    is_deepseek_direct = "api.deepseek.com" in (api_url or "")
    is_dashscope = "dashscope" in (api_url or "") or "aliyun" in (api_url or "")
    m = model or ""

    if is_deepseek_direct:
        # V4 默认开思考 · 必须显式关 · deepseek-chat/v3 无此字段传了也无害
        # 🔴 [WO_206] `deepseek-flash` 是官方 2026-09 的新名(V4.1-Flash),
        #    **不以 `deepseek-v4` 开头** —— 只改"发"不改这一行,新名就不匹配,
        #    于是默认开思考:慢 ~17% 贵 ~35%,而且**全程不报错**。
        #    这里是**加**一个前缀,不动旧名判定:旧名还在跑的那些点行为逐字不变。
        if (m.startswith("deepseek-v4") or m.startswith("deepseek-flash")
                or m == "deepseek-reasoner"):
            return {"thinking": {"type": "disabled"}}
        return {}
    if is_dashscope:
        # DashScope Qwen 系列 + 集成 deepseek-v4 均接受 enable_thinking
        return {"enable_thinking": False}
    return {}


def get_api_key_for_provider(provider: str, settings: Optional[dict] = None) -> str:
    """
    根据provider获取API Key
    
    Args:
        provider: 服务商名称
        settings: 可选的settings字典，避免重复读取文件
    """
    if settings:
        key_field = f"{provider}_api_key"
        api_key = settings.get(key_field)
        if api_key:
            return api_key
    
    return os.getenv(ENV_KEY_NAMES.get(provider, ""), "")
