"""
GEO AgentScope 模型配置
基于 LM Arena 2025年12月排行榜，仅使用2025年9月后发布的最新模型
"""

import os
from dotenv import load_dotenv
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

# 加载环境变量
load_dotenv()


def _deepseek_api_key(role: str = "realtime") -> str | None:
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key

        return pick_deepseek_api_key(role) or os.environ.get("DEEPSEEK_API_KEY")
    except Exception:
        return os.environ.get("DEEPSEEK_API_KEY")

# ============================================
# DashScope 配置（阿里云）
# ============================================
DASHSCOPE_CONFIG = {
    "api_key": os.environ.get("DASHSCOPE_API_KEY"),
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "models": {
        # [CTO-15.5 2026-04-20] 模型策略: qwen3-max 保留为主(便宜快),qwen3.6-max-preview opt-in 用于 autofill + deep_analyze
        # 实测对比(scripts/compare_qwen_max_versions.py):
        #   扩词场景 qwen3-max 远快于 qwen3.6-max-preview(且便宜得多)
        #   原因:preview 默认开思维链,隐藏 3500 token 计费
        #   质量差异小,不值那个成本 → 只在需要深度推理的场景用 preview
        "qwen3-max": {
            "description": "旗舰模型,复杂 Agent / 深度推理 / 带搜索 · 无思维链默认模式",
            "context_length": 1000000,
            "input_price": 0.001,   # 元/千 Token(示例值)
            "output_price": 0.002,  # 示例值
            "features": ["search_agent", "tool_calling", "vision"],
            "elo": 1434,
        },
        # [2026-06-06 老板] qwen3.7-max GA 旗舰(替代 qwen3-max 用于内部生成:写作/选题/评分/顾问/员工/社媒)
        #   ⚠️ 非 qwen3.6-max-preview(那个慢且贵)· 这是 GA 旗舰 · 1M context · RPM 30000
        #   单价见下方(示例值)
        #   监测/诊断【不用】此模型 → 见 tools/ai_visibility/ai_tester.py 搜索请求体里钉死的
        #   qwen3-max(文本端点·支持全套 DashScope 原生联网;qwen3.7-plus 多模态在 Generation 端点 400)
        "qwen3.7-max": {
            "description": "Qwen3.7 Max GA 旗舰 · 复杂 Agent/深度推理/带搜索 · 内部生成主力(写作/选题/评分/顾问/员工/社媒)",
            "context_length": 1000000,
            "input_price": 0.001,        # 示例值
            "output_price": 0.002,       # 示例值
            "list_input_price": 0.002,   # 示例值
            "list_output_price": 0.004,  # 示例值
            "cache_hit_price": 0.0005,   # 示例值
            "promo_note": "限时 5 折 · 限时结束后翻倍",
            "features": ["search_agent", "tool_calling", "function_calling", "structured_output", "vision"],
            "elo": 1460,
            "rpm": 30000,
            "tpm": 1000000,
        },
        "qwen3.6-max-preview": {
            "description": "Qwen3.6 Max Preview · 默认开思维链(128K) · 仅用于 autofill 品牌真实性验证 + deep_analyze 深度行业",
            "context_length": 256000,
            "input_price": 0.001,   # 示例值
            "output_price": 0.002,  # 示例值 · 注意思维链会膨胀输出
            "cache_hit_price": 0.0005,
            "features": ["search_agent", "tool_calling", "function_calling", "structured_output", "thinking_chain"],
            "elo": 1450,
            "rpm": 600,
            "tpm": 1000000,
            "usage_scenarios": ["autofill_brand_verification", "deep_analyze_v36"],
        },
        "qwen3.6-plus": {
            "description": "原生多模态旗舰 · 文本+图片+视频 · 视觉理解顶级 · OCR + 多模态万物识别 + 物体定位",
            "context_length": 256000,
            # 单价为示例值
            "input_price": 0.001,        # 示例值
            "output_price": 0.002,       # 示例值
            "list_input_price": 0.002,   # 示例值
            "list_output_price": 0.004,  # 示例值
            "cache_hit_price": 0.0005,
            "promo_note": "限时 5 折 · 限时结束后比 qwen3.6-flash 贵 3.3 倍",
            "features": ["vision", "tool_calling", "native_multimodal", "structured_output"],
            "elo": 1450,
        },
        # [census ⑥c 2026-08-23] 这里原本登记着 "qwen3.5-plus",并自称「监测/诊断引擎实际用此」。
        #   **全仓零活调用**:它只出现在本目录、价目表和三处注释里,没有任何一行代码会发这个模型名。
        #   真正在跑的是 qwen3-max(tools/ai_visibility/ai_tester.py 的搜索请求体
        #   + services/monitoring_lineage.py 的 _PLATFORM_CONTRACT)。
        #   幽灵登记比缺登记更贵:它让读代码的人以为自己知道线上在跑什么。已删。
        "qwen3.7-plus": {
            "description": "Qwen3.7 中高性价比 Plus · 文本/视觉/深度思考 · 多模态(须 MultiModalConversation·联网仅 Responses API)· 监测/诊断走 qwen3-max",
            "context_length": 1000000,
            "input_price": 0.001,   # 示例值
            "output_price": 0.002,   # 示例值
            "list_input_price": 0.002,
            "list_output_price": 0.004,
            "cache_hit_price": 0.0005,
            "promo_note": "限时8折",
            "features": ["vision", "tool_calling", "function_calling", "structured_output", "search_agent", "native_multimodal"],
            "elo": 1455,
            "rpm": 30000,
            "tpm": 5000000,
        },
        # 2026-05-18 老板截图证实 qwen3.6-flash 存在 · 注册进系统(虽然 flash 反而比 plus 贵)
        # 反直觉:Qwen3.6 系列 flash 带"深度思考"·output token 膨胀 · 实际成本反超 plus
        # 主打 agentic coding + 空间智能 + 物体定位 · 对纯 PDF OCR overkill
        # 默认保留 plus(便宜+稳)· admin 可手动翻 vision_model='qwen3.6-flash' 实测
        "qwen3.6-flash": {
            "description": "Qwen3.6 原生视觉语言 Flash · 深度思考+视觉空间智能+物体定位 · 适合复杂视觉推理",
            "context_length": 256000,
            "input_price": 0.001,
            "output_price": 0.002,
            "features": ["vision", "tool_calling", "native_multimodal", "thinking_chain"],
            "elo": 1430,
            "rpm": 1200,
            "tpm": 2000000,
            "usage_scenarios": ["complex_visual_reasoning", "agentic_coding"],
        },
        "qwen-turbo": {
            "description": "轻量模型，批量处理、简单任务",
            "context_length": 1000000,
            "input_price": 0.001,
            "output_price": 0.002,
            "features": [],
            "elo": 1350,
        },
    },
    "asr_models": {
        "qwen3-asr-flash": {
            "description": "短视频同步转写，≤5分钟",
            "max_duration_seconds": 300,
            "sync": True,
        },
        "qwen3-asr-flash-filetrans": {
            "description": "长视频异步转写，≤12小时",
            "max_duration_seconds": 43200,
            "sync": False,
        },
    },
}

# ============================================
# DeepSeek 配置
# ============================================
DEEPSEEK_CONFIG = {
    "api_key": _deepseek_api_key("realtime"),
    "base_url": "https://api.deepseek.com/v1",
    "models": {
        "deepseek-chat": {
            "description": "DeepSeek V4 Flash 非思考模式 - 内容生成、对话、快速编程(deepseek-chat alias)",
            "context_length": 1000000,
            "features": ["moe", "fast", "v4_flash"],
        },
        "deepseek-reasoner": {
            "description": "DeepSeek R1 - 复杂推理、GEO评分、数学",
            "context_length": 65536,
            "features": ["chain_of_thought", "transparent_reasoning"],
        },
    },
}

# ============================================
# Kimi (月之暗面) 配置
# [CTO-15.23 2026-05-09 Kimi 月烧 ¥1500 治理]
# - 2.5 → 2.6 升级(老板拍板)
# - 全站限定使用场景:仅诊断/监测的 $web_search 硬性必用
# - 其他场景全部砍掉 · 用 deepseek-v4-flash / deepseek-v4-pro / qwen3-max 替代
# ============================================
KIMI_DEFAULT_MODEL = "kimi-k2.6"  # SSOT · 全仓引用此常量 · 防再有 hardcode

KIMI_CONFIG = {
    "api_key": os.environ.get("KIMI_API_KEY"),
    "base_url": "https://api.moonshot.cn/v1",
    "models": {
        "kimi-k2.6": {
            "description": "Kimi K2.6 - $web_search 联网搜索专用 · 仅诊断/监测场景",
            "context_length": 128000,
        },
    },
    # Kimi K2.6 硬约束(2026-05-16 实测确认):
    #   - thinking={"type":"disabled"} → temperature ONLY 0.6(其他值 400)
    #   - thinking 默认 enabled    → temperature ONLY 1.0(其他值 400)
    #   全仓 tool_calls 流程必须 disabled · 因此 temperature 锁定 0.6
    "constraints": {
        "temperature": 0.6,                  # disabled+0.6 是唯一可用组合
        "thinking_disabled": True,           # tool_calls 流程必须 disabled
        "rpm": 60,                           # 速率限制
    },
}

# ============================================
# 豆包 (字节跳动) 配置
# API Key + Endpoint ID 从火山方舟控制台获取
# ============================================
DOUBAO_CONFIG = {
    "api_key": os.environ.get("DOUBAO_API_KEY"),
    "seed_api_key": os.environ.get("DOUBAO_SEED_API_KEY") or os.environ.get("DOUBAO_API_KEY"),  # Seed 2.0 独立key
    "endpoint_id": os.environ.get("DOUBAO_ENDPOINT_ID"),  # ep-xxx 格式（旧模型兼容）
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "models": {
        # Seed 2.0 Pro - 2026年2月最新模型，直接用模型名调用
        "doubao-seed-2-0-pro-260215": {
            "description": "豆包 Seed 2.0 Pro - 最新旗舰模型",
            "context_length": 32768,
        },
    },
}

# ============================================
# OpenRouter 配置
# ============================================
OPENROUTER_CONFIG = {
    "api_key": os.environ.get("OPENROUTER_API_KEY"),
    "base_url": "https://openrouter.ai/api/v1",
    "models": {
        # Google Gemini（当前最强）
        "google/gemini-3-pro": {
            "description": "综合最强，超长上下文分析",
            "context_length": 1000000,
            "elo": 1490,
            "best_for": ["complex_analysis", "long_document"],
        },
        "google/gemini-3-flash": {
            "description": "高效推理，快速响应",
            "context_length": 1000000,
            "elo": 1480,
            "best_for": ["fast_analysis", "summarization"],
        },
        # Anthropic Claude
        "anthropic/claude-opus-4.5": {
            "description": "高端写作，深度分析",
            "context_length": 200000,
            "elo": 1467,
            "best_for": ["premium_writing", "complex_coding"],
        },
        "anthropic/claude-sonnet-4.5": {
            "description": "创意写作最佳，营销文案",
            "context_length": 200000,
            "elo": 1446,
            "best_for": ["creative_writing", "marketing_copy", "emotional_content"],
        },
        # OpenAI GPT
        "openai/gpt-5.1-high": {
            "description": "高质量推理",
            "context_length": 128000,
            "elo": 1458,
            "best_for": ["quality_reasoning"],
        },
        "openai/gpt-5.1": {
            "description": "通用任务",
            "context_length": 128000,
            "elo": 1438,
            "best_for": ["general_tasks"],
        },
        # 国产模型
        "zhipu/glm-4.7": {
            "description": "中文理解，国内部署",
            "context_length": 128000,
            "elo": 1439,
            "best_for": ["chinese_understanding"],
        },
        # DeepSeek（免费）
        "deepseek/deepseek-r1": {
            "description": "推理任务，免费",
            "context_length": 65536,
            "elo": 1430,
            "best_for": ["reasoning", "free_tier"],
        },
    },
}

# ============================================
# 秘塔搜索 配置
# ============================================
METASO_CONFIG = {
    "api_key": os.environ.get("METASO_API_KEY"),
    "base_url": "https://metaso.cn",
    "endpoints": {
        "search": "/api/v1/search",
    },
    "scopes": ["webpage", "scholar"],
}

# ============================================
# TikHub 配置
# ============================================
TIKHUB_CONFIG = {
    "api_key": os.environ.get("TIKHUB_API_KEY"),
    "domains": [
        "https://api.tikhub.io",
        "https://api.tikhub.dev",
    ],
    "endpoints": {
        "douyin_video_search": [
            "/api/v1/douyin/search/fetch_video_search_v2",
            "/api/v1/douyin/search/fetch_video_search_v1",
            "/api/v1/douyin/app/v3/search_video",
        ],
        "douyin_user_search": [
            "/api/v1/douyin/search/fetch_user_search_v2",
            "/api/v1/douyin/search/fetch_user_search_v1",
        ],
        "xiaohongshu_note_search": [
            "/api/v1/xiaohongshu/web/search_notes",
            "/api/v1/xiaohongshu/app/search_notes",
        ],
        "xiaohongshu_user_info": [
            "/api/v1/xiaohongshu/app/user_info",
            "/api/v1/xiaohongshu/web/user_info",
        ],
        "xiaohongshu_note_detail": [
            "/api/v1/xiaohongshu/web/get_note_info_v7",
            "/api/v1/xiaohongshu/app/get_note_info_v2",
            "/api/v1/xiaohongshu/app/get_note_info",
        ],
        "xiaohongshu_video_note_detail": [
            "/api/v1/xiaohongshu/app_v2/get_video_note_detail",
        ],
        "xiaohongshu_extract_share": [
            "/api/v1/xiaohongshu/web/get_note_id_and_xsec_token",
            "/api/v1/xiaohongshu/app/extract_share_info",
        ],
        "xiaohongshu_user_notes": [
            "/api/v1/xiaohongshu/web_v2/fetch_home_notes",
            "/api/v1/xiaohongshu/app/get_user_notes",
        ],
        "wechat_channels_search": [
            "/api/v1/wechat_channels/fetch_default_search",
            "/api/v1/wechat_channels/fetch_home_page",
        ],
    },
    # 熔断配置
    "circuit_breaker": {
        "max_failures": 3,
        "cooldown_seconds": 300,
    },
}

# ============================================
# GEO 业务场景 → 模型映射
# ============================================
GEO_MODEL_ROUTING = {
    # 数据处理
    "data_filtering": ("qwen-turbo", "dashscope"),
    "video_asr_short": ("qwen3-asr-flash", "dashscope"),
    "video_asr_long": ("qwen3-asr-flash-filetrans", "dashscope"),
    
    # GEO诊断
    "geo_diagnostic_deep": ("google/gemini-3-pro", "openrouter"),
    "keyword_optimization": ("qwen3.7-max", "dashscope"),
    # 🔴 [WO_206 c1c] 元组第一位是**发出去的模型名**(第二位才是 provider)。
    #    原写 deepseek-reasoner —— 官方线那一档 2026-09-14 已经不存在了
    #    (Deploy 206-d2:仍返 200 但回显 deepseek-flash)。取常量。
    #    🔴 今天它其实**没有调用方**:GEO_MODEL_ROUTING 只由 get_model_for_task 读,
    #       而那个函数全仓零调用点。改它是为了"接线那天不会捡起一个死名字"。
    "geo_scoring": (DEEPSEEK_OFFICIAL_FLASH, "deepseek"),
    "diagnostic_report": ("qwen3-max", "dashscope"),
    
    # 内容策略
    "competitor_analysis": ("google/gemini-3-flash", "openrouter"),
    "selling_point_extraction": ("qwen3.6-plus", "dashscope"),
    "topic_planning": ("qwen3.7-max", "dashscope"),
    
    # GEO内容创作
    "geo_article_zhihu": ("anthropic/claude-sonnet-4.5", "openrouter"),
    "geo_article_baike": ("anthropic/claude-opus-4.5", "openrouter"),
    "geo_article_baijiahao": ("qwen3.6-plus", "dashscope"),
    "geo_article_seo_batch": ("qwen-turbo", "dashscope"),
    "supporting_evidence": ("qwen3.7-max", "dashscope"),
    
    # 社媒内容
    "xiaohongshu_note": ("anthropic/claude-sonnet-4.5", "openrouter"),
    "douyin_script": ("qwen3.6-plus", "dashscope"),
    "wechat_article": ("anthropic/claude-sonnet-4.5", "openrouter"),
    
    # 辅助
    "title_generation": ("qwen3.6-plus", "dashscope"),
    "json_formatting": ("qwen-turbo", "dashscope"),
    "long_document_analysis": ("google/gemini-3-pro", "openrouter"),

    # 视觉分析
    "image_understanding": ("qwen3.6-plus", "dashscope"),
}


def get_model_for_task(task: str) -> tuple[str, str]:
    """根据任务获取推荐的模型和API"""
    if task in GEO_MODEL_ROUTING:
        return GEO_MODEL_ROUTING[task]
    return ("qwen3.6-plus", "dashscope")


def get_agentscope_model_configs() -> list[dict]:
    """获取 AgentScope 模型配置列表"""
    return [
        # ========== DashScope 模型 ==========
        {
            "config_name": "qwen3_max",
            "model_type": "dashscope_chat",
            "model_name": "qwen3.7-max",
            "api_key": DASHSCOPE_CONFIG["api_key"],
        },
        {
            "config_name": "qwen35_plus",
            "model_type": "dashscope_chat",
            "model_name": "qwen3.6-plus",
            "api_key": DASHSCOPE_CONFIG["api_key"],
        },
        {
            "config_name": "qwen_turbo",
            "model_type": "dashscope_chat",
            "model_name": "qwen-turbo",
            "api_key": DASHSCOPE_CONFIG["api_key"],
        },
        {
            "config_name": "qwen35_plus",
            "model_type": "dashscope_chat",
            "model_name": "qwen3.6-plus",
            "api_key": DASHSCOPE_CONFIG["api_key"],
        },
        
        # ========== DeepSeek 模型 ==========
        {
            "config_name": "deepseek_v32",
            "model_type": "openai_chat",
            "model_name": DEEPSEEK_OFFICIAL_FLASH,
            "api_key": DEEPSEEK_CONFIG["api_key"],
            "client_args": {
                "base_url": DEEPSEEK_CONFIG["base_url"],
            },
        },
        {
            # config_name 是**注册表键**,调用方按它取档,不跟着改。
            # 🔴 [WO_206 c1b② · Owner 2026-09-14 拍板「全部改成 deepseek-flash」]
            #    Deploy 206-d2 当天实打:官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro;
            #    `deepseek-reasoner` 仍返 200,但**回显 deepseek-flash** —— 那一档已经没有了,
            #    这一行一直在**静默降级**跑 flash。改成常量不是「换模型」,是把已经发生的事写明。
            "config_name": "deepseek_r1",
            "model_type": "openai_chat",
            "model_name": DEEPSEEK_OFFICIAL_FLASH,
            "api_key": DEEPSEEK_CONFIG["api_key"],
            "client_args": {
                "base_url": DEEPSEEK_CONFIG["base_url"],
            },
        },
        
        # ========== OpenRouter - Gemini ==========
        {
            "config_name": "gemini_3_pro",
            "model_type": "openai_chat",
            "model_name": "google/gemini-3-pro",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
        {
            "config_name": "gemini_3_flash",
            "model_type": "openai_chat",
            "model_name": "google/gemini-3-flash",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
        
        # ========== OpenRouter - Claude ==========
        {
            "config_name": "claude_opus_45",
            "model_type": "openai_chat",
            "model_name": "anthropic/claude-opus-4.5",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
        {
            "config_name": "claude_sonnet_45",
            "model_type": "openai_chat",
            "model_name": "anthropic/claude-sonnet-4.5",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
        
        # ========== OpenRouter - GPT ==========
        {
            "config_name": "gpt_51_high",
            "model_type": "openai_chat",
            "model_name": "openai/gpt-5.1-high",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
        
        # ========== OpenRouter - 国产模型 ==========
        {
            "config_name": "glm_47",
            "model_type": "openai_chat",
            "model_name": "zhipu/glm-4.7",
            "api_key": OPENROUTER_CONFIG["api_key"],
            "client_args": {
                "base_url": OPENROUTER_CONFIG["base_url"],
            },
        },
    ]
