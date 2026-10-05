"""
系统设置管理器
处理用户配置的持久化和加载
"""

import os
import json
from pathlib import Path
from typing import Optional, Literal
from pydantic import BaseModel
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

# 配置文件路径
SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


class ConfirmCodeEntry(BaseModel):
    """操作确认码条目"""
    code_hash: str = ""           # SHA256 哈希
    updated_at: str = ""          # 最后修改时间
    updated_by: str = ""          # 修改人
    fail_count: int = 0           # 连续失败次数
    locked_until: str = ""        # 锁定截止时间


class SystemSettings(BaseModel):
    """系统设置模型"""
    # API Keys (6+2个服务商)
    dashscope_api_key: str = ""
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    doubao_api_key: str = ""      # 豆包
    doubao_endpoint_id: str = ""  # 豆包 Endpoint ID (ep-xxx格式)
    kimi_api_key: str = ""        # Kimi
    tikhub_api_key: str = ""
    siliconflow_api_key: str = "" # SiliconFlow (DeepSeek备用通道)
    metaso_api_key: str = ""      # Metaso (诊断搜索引擎)
    # P14-v14 (2026-05-28 老板): Jina Reader key (调研监测 stage_3 抓取原文)
    # 没填 → 走免费版 5 并发 / 填了 → 100 RPM 限 2 并发(可被 JINA_CONCURRENCY env 覆盖)
    jina_api_key: str = ""
    
    # 调研报告LLM - 默认配置
    # [hotfix 2026-06-06] qwen3.7-plus 是多模态(须 MultiModalConversation)且其联网搜索仅 Responses API 支持 →
    # 在 DashScope 原生 Generation + enable_search 会 400「url error」,且丢搜索来源/角标。
    # 改 qwen3-max:文本模型·原生端点支持全套联网(forced_search/enable_source/角标)·监测可稳定带来源(同代·读结果判品牌差距极小)。
    diagnosis_model: str = "qwen3-max"
    diagnosis_provider: str = "dashscope"
    # 调研报告LLM - 子任务配置
    diagnosis_tasks: dict = {
        # 🔴 [WO_206 c1b② · Owner 2026-09-14 拍板「全部改成 deepseek-flash」]
        #    Deploy 206-d2 当天实打:官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro;
        #    `deepseek-reasoner` 仍返 200,但**回显 deepseek-flash** —— 那一档已经没有了,
        #    这一行一直在**静默降级**跑 flash。改成常量不是「换模型」,是把已经发生的事写明。
        "geo_scoring": {"provider": "deepseek", "model": DEEPSEEK_OFFICIAL_FLASH},
        "diagnostic_report": {"provider": "dashscope", "model": "qwen3-max"},
        "keyword_optimization": {"provider": "dashscope", "model": "qwen3.7-max"},
        "competitor_analysis": {"provider": "openrouter", "model": "google/gemini-3-flash"},
    }
    
    # 写作板块LLM - 默认配置
    writing_model: str = "qwen3.6-plus"
    writing_provider: str = "dashscope"
    # 写作板块LLM - 子任务配置
    writing_tasks: dict = {
        "geo_article": {"provider": "dashscope", "model": "qwen3.7-max"},
        "title_generation": {"provider": "dashscope", "model": "qwen3.6-plus"},
        "topic_planning": {"provider": "dashscope", "model": "qwen3.7-max"},
        # [AI 原生化 V3/V4/V5] 飞轮解释层(数字全真实,LLM 只解释)· 默认官方线 Flash 档,分币级
        # 注:get_llm_config 运行时读 settings.json;未注册也回落写作全局(同一档),此处为 checked-in 意图声明
        # 🔴 2026-09-14 官方改名 deepseek-flash(同一档);名字统一由 config/deepseek_models 出 —— 这三行取常量,别再在注释里写死一个会过期的名字
        "outcome_commentary": {"provider": "deepseek", "model": DEEPSEEK_OFFICIAL_FLASH},   # V3 进化历史评语
        "structure_rules": {"provider": "deepseek", "model": DEEPSEEK_OFFICIAL_FLASH},      # V4 结构规则行业化
        "flywheel_insight": {"provider": "deepseek", "model": DEEPSEEK_OFFICIAL_FLASH},     # V5 飞轮总汇总
    }
    
    # 社媒操盘手LLM配置
    social_tasks: dict = {
        "topic_generation": {"provider": "dashscope", "model": "qwen3.7-max"},      # 选题生成
        "script_writing": {"provider": "dashscope", "model": "qwen3.7-max"},         # 脚本写作
        "rewrite_analysis": {"provider": "dashscope", "model": "qwen3.7-max"},       # 仿写分析
        "opening_generation": {"provider": "dashscope", "model": "qwen3.7-max"},     # 开篇生成
    }
    
    # AI员工LLM配置
    employee_tasks: dict = {
        "default": {"provider": "dashscope", "model": "qwen3.6-plus"},             # 默认模型
        "high_quality": {"provider": "dashscope", "model": "qwen3.7-max"},          # 高质量任务
        "content_planning": {"provider": "dashscope", "model": "qwen3.7-max"},      # 内容规划
    }
    
    # [包F ⑦c · 2026-08-24 Owner 已批] `monitoring_tasks` **已删**。
    # 孤儿判定的证据是机械重扫得来的:全仓(api/ services/ tools/ workflows/
    # agents/)零消费点 —— 没有任何一行读 settings.monitoring_tasks,
    # 而它里面那两个 qwen3-max 会让读代码的人以为线上监测按它路由。
    # 幽灵配置比缺配置更贵:它让人以为自己知道线上在跑什么。
    # 判据 tests/defensive_geo_2026_08_21/test_model_pricing_census.py::
    # test_orphan_claim_is_still_true 随本次删除同 commit 转向(见该处说明)。

    # 调研监测板块 LLM 配置(Plan A 2026-05-06)
    research_monitor_tasks: dict = {
        "platforms": {
            "doubao": {
                "endpoint_var": "DOUBAO_ENDPOINT",
                "api_key_var": "VOLC_API_KEY",
                "options": []
            },
            "deepseek": {
                "endpoint_var": "DEEPSEEK_ENDPOINT",
                "api_key_var": "VOLC_API_KEY",
                "options": []
            },
            "qwen": {
                # [包F ⑦b · 2026-08-24 Owner 已批] 调研监测 qwen 默认值
                # qwen3.6-max-preview → qwen-plus-latest。
                # 🔴 这是**调研监测**那条链的默认模型,与包F ⑥ 换的
                #    「被测千问引擎」(monitoring_lineage.QWEN_ENGINE)是
                #    两条不同的链、两个不同的用途:前者是我们自己干活的
                #    工具,后者是被观测对象。两者刻意不统一 ——
                #    统一会让"换干活工具"顺手动到检索保真基准。
                # 🔴 qwen-plus-latest 是**老 qwen-plus 线**的 latest,
                #    不是 qwen3.7-plus 的别名(官方文档两个独立条目)。
                #    「latest」这个名字在骗人 —— 命名即断言。
                "model": "qwen-plus-latest",
                "api_key_var": "DASHSCOPE_API_KEY",
                "options": ["qwen3.7-max", "qwen3.6-plus", "qwen-plus", "qwen-max"]
            },
            "kimi": {
                "model": "kimi/kimi-k2.5",
                "api_key_var": "DASHSCOPE_API_KEY",
                "locked": True,
                "lock_reason": "依赖输出格式 ---REFERENCES---,不可切"
            }
        },
        "cleaner_model": "qwen-turbo",
        "cleaner_options": ["qwen-turbo", "deepseek-v4-flash"],  # 2026-05-22 V3→V4 切换
        "scorer_model": "qwen-plus",
        "scorer_options": ["qwen-plus", "qwen-max"]
    }

    # 内容配置(v2.7.1 GEO 文体改造 · 8 angle key sum=100 · 不含 company_profile)
    content_ratios: dict = {
        "authority":   0,   # 新内容不再生产匿名权威榜单
        "deep_dive":  25,   # 场景化证据对比
        "case_study": 10,
        "pitfall":    15,
        "trend":      15,
        "faq":        10,
        "checklist":  20,
        "expert":      5,
    }
    # 风格配比(v2.7.1 新增 · 11 style key sum=100 · 不含 company_profile · 走 fixed_count=1)
    # [WP9-P0-4] 旧口径:降榜单/对比类、升事实类,3 legacy 维持 0。
    # [WP12 P0-2 · Master SSOT v2.4 ①② / v2.3 ②] **默认档反转**:飞轮实证显示
    # 拐点前后被引文章中榜单/推荐式占比 44.7%→43.2% 几乎不变,引擎淘汰的是编造
    # 评分而非榜单形态;全面退榜单属过度反应并已造成丢客,予以纠正。默认档
    # (本地化 / 供给分散 / B2B)恢复"有依据榜单+推荐+对比"为主力,合计 ≥50%。
    # 高供给纯 B2C(酒店/OTA)维持事实/指南为主 → 见 style_ratio_category_sets。
    # trojan_horse 仍为 0(退役未复活);医疗/法律 industry_overrides 强制 0 不动。
    style_ratios: dict = {
        "ranking_v2":          20,
        "authority_ranking":    0,
        "recommendation_review":14,
        "buying_guide":        14,
        "trojan_horse":         0,
        "qa_recommendation":   14,
        "brand_softarticle":    4,
        "comparison_review":   18,
        "risk_compliance":      8,
        "price_roi":            4,
        "data_report":          4,
    }
    # [WP12 P0-2] 文体按类目分治 · 映射表可配。key = 类目;value = 11 style key 配比。
    # 默认档不在此表内(= 上面的 style_ratios),只登记需要偏离默认的类目。
    style_ratio_category_sets: dict = {
        # 高供给纯 B2C(酒店 / OTA / 机票 / 景区):候选极多且平台自带排序,
        # 榜单边际价值低 → 维持改造前的事实/指南为主配比。
        "high_supply_b2c": {
            "ranking_v2":           0,
            "authority_ranking":    0,
            "recommendation_review":0,
            "buying_guide":        30,
            "trojan_horse":         0,
            "qa_recommendation":   22,
            "brand_softarticle":    8,
            "comparison_review":    8,
            "risk_compliance":     18,
            "price_roi":            8,
            "data_report":          6,
        },
    }
    # 行业 → 类目映射(子串匹配 · 运营可改)。命中即用对应类目档,未命中走默认档。
    industry_style_categories: dict = {
        "high_supply_b2c": [
            "旅游酒店", "酒店", "民宿", "OTA", "在线旅游", "景区", "度假",
            "机票", "航空", "旅行社",
        ],
    }
    # [WP12 P0-1] 真实被引域权重观测窗口(天) · admin 可治理
    citation_domain_window_days: int = 90
    # 行业 override(v2.7.1 新增 · 医疗/法律 完整 8-key content + 11-key style · authority/ranking 强制 0)
    industry_overrides: dict = {
        "医疗健康": {
            "content_ratios": {
                "authority":  0,
                "deep_dive": 10,
                "case_study":10,
                "pitfall":   15,
                "trend":     10,
                "faq":       20,
                "checklist": 25,
                "expert":    10,
            },
            "style_ratios": {
                "ranking_v2":          0,
                "authority_ranking":    0,
                "recommendation_review":4,
                "buying_guide":        20,
                "trojan_horse":         0,
                "qa_recommendation":   14,
                "brand_softarticle":    4,
                "comparison_review":   18,
                "risk_compliance":     30,
                "price_roi":            6,
                "data_report":          4,
            },
        },
        "法律商务": {
            "content_ratios": {
                "authority":  0,
                "deep_dive":  8,
                "case_study":12,
                "pitfall":   15,
                "trend":      8,
                "faq":       20,
                "checklist": 25,
                "expert":    12,
            },
            "style_ratios": {
                "ranking_v2":          0,
                "authority_ranking":    0,
                "recommendation_review":4,
                "buying_guide":        18,
                "trojan_horse":         0,
                "qa_recommendation":   14,
                "brand_softarticle":    4,
                "comparison_review":   18,
                "risk_compliance":     32,
                "price_roi":            6,
                "data_report":          4,
            },
        },
    }
    concurrent_writers: int = 10
    default_article_count: int = 40
    
    # ====== 报价系统配置 ======
    quote_cluster_mode: bool = True   # 是否启用主题包模式（False=旧版平铺关键词模式）
    # 单篇成本（元）
    quote_content_cost: int = 25      # 内容创作（示例值）
    quote_media_cost: int = 40        # 媒体发布费（示例值）
    quote_operation_cost: int = 15    # 运营执行（示例值）
    # 利润配置
    # [§4.5/决策5 2026-06-06] 默认报价倍率 2.0→1.0:回成本价 · 服务商自设利润(operator 自控)
    quote_markup_ratio: float = 1.0   # 售价倍率（默认=成本·服务商往上加利润）
    # 其他
    quote_ai_reference_count: int = 4 # AI每次回答平均引用条数
    quote_default_target_share: float = 0.20  # 默认目标占比(20%)
    quote_batch_concurrency: int = 30  # 批量查询并发数 · [CTO-15.5 2026-04-20 Q1.C 老板批] 10→30 · 秘塔 QPS 200 / qwen3.6-max RPM 600 · 实际 ~12 QPS 远低于天花板 · 多任务并行瞬时 150 仍 3x 缓冲
    
    # ====== 文章生成配置 ======
    article_timeout: int = 180            # 单篇文章生成超时（秒）
    article_retry_count: int = 2          # 失败重试次数
    article_client_position: int = 1      # 客户在排名中的位置
    article_client_coverage: float = 0.5  # 客户内容占比(50%)
    article_max_competitors: int = 4      # 每篇最多展示竞品数
    
    # ====== 诊断系统配置 ======
    diagnosis_ai_test_timeout: int = 180  # AI引擎测试超时（秒）
    diagnosis_step_cache: bool = True     # 是否启用步骤缓存
    diagnosis_metaso_size: int = 100      # 竞品搜索数量
    
    # ====== 社媒操盘手配置 ======
    social_asr_timeout: int = 180         # ASR转录超时（秒）
    social_asr_model: str = "qwen3-asr-flash"  # ASR模型
    social_tikhub_timeout: int = 120      # TikHub API超时（秒）
    
    # ====== 知识库配置 ======
    kb_llm_clean_enabled: bool = True     # 是否启用LLM清洗
    kb_llm_clean_model: str = "qwen3.7-max" # LLM清洗使用的模型(§4.5/2026-06-06 内部生成→qwen3.7-max)
    kb_embedding_model: str = "text-embedding-v4"  # 向量化模型
    kb_rerank_enabled: bool = False       # 是否启用重排序
    
    # ====== LLM全局配置 ======
    llm_global_timeout: int = 180         # LLM调用默认超时（秒）
    llm_global_max_retries: int = 2       # LLM调用默认重试次数
    llm_global_temperature: float = 0.7   # LLM默认温度
    
    # ====== 操作确认码 ======
    confirm_codes: dict = {}              # action -> ConfirmCodeEntry

    # ====== 高级选项 ======
    debug_mode: bool = False              # 是否启用调试日志
    sse_heartbeat_interval: int = 30      # SSE心跳间隔（秒）

    # ====== M2 报告 2.0 灰度开关(CTO-15.9 2026-04-25 Codex bug 4 二审修) ======
    # services.report_writer_v2.is_report_v2_enabled() 读取本字段
    # 默认关 · 5 金标准用 whitelist 灰度 · Week 10 末决定全量或回滚
    report_v2_enabled: bool = False       # 全局 v2 开关 · True=所有 brand 走 v2
    report_v2_whitelist: dict = {         # 白名单灰度
        "brand_ids": [],                  # list[int] · 指定 brand 启用 v2
        "user_ids": [],                   # list[int] · 指定用户(代理)启用 v2
    }

    # ====== 快易播发稿渠道 (2026-07-27 接入 · 与媒介盒子并存) ======
    # token 优先读环境变量 KUAIYIBO_API_TOKEN;这里留空位便于 admin 热改。
    # **不要把真实 token 写进代码库或文档**。
    kuaiyibo_api_token: str = ""
    # 发布通道订单回调的共享密钥(路径鉴权)。空 = 回调端点直接 404,不可用。
    publish_channel_callback_secret: str = ""
    # 出货优先级:媒介盒子先用完,之后快易播成主力(Owner 2026-07-27)。
    # 只作同分兜底排序,不压过价格/被引强度等真实排序信号。
    media_provider_priority: list = ["mhz", "kyb"]

    # ====== 媒体平衡推荐灰度开关 (2026-07-29 · Owner 拍板"推荐还不成熟,先灰度") ======
    # services.media_balance_gate.is_media_balance_enabled() 读取本字段。
    # 关掉时推荐面回到上线前口径:主干/垂类按老的 _GENERIC_PLATFORMS 名单分组、
    # 评分不乘发布成功率、不返回「AI 最常引用的网站」与「建议怎么搭配媒体」两块。
    # **不受本开关影响**(Owner 明示保留):严审媒体档 v2 与下单前预审(合规+省钱)、
    # T4/T5 只读报表、can_geo 死过滤器修复(那是修 bug,关回去等于把 bug 放回生产)。
    media_balance_enabled: bool = False    # 全局开关 · True=所有代理看到新推荐口径
    # [2026-07-28 C-1] 放量维度 = industry_keys(效果按行业分化,同行业内新旧对照才读得出信号)。
    #   user_ids 降级为调试后门(强制拉某账号进灰度好复现问题),brand_ids 仅兼容既有配置、
    #   **不推荐用于放量**(跨行业零散样本组内方差大)。
    media_balance_whitelist: dict = {      # 白名单灰度
        "industry_keys": [],              # list[str] · **放量维度** · 该行业启用新推荐(大小写/空格不敏感)
        "user_ids": [],                   # list[int] · 调试后门 · 强制某代理进灰度(不作放量维度)
        "brand_ids": [],                  # list[int] · 仅兼容既有配置 · 不推荐用于放量
    }

    # ====== PR-A 客户决策页 v2 灰度开关 (CTO-15.23 2026-05-03 · 老板/Codex 拍 PR-A) ======
    # services.report_html_renderer.render_customer_decision_page_html() 走本开关
    # 默认关 · 老报告走原 render_report_html() · 0 regression
    # share_api.py:get_public_report_v2_html 路由判断
    customer_decision_v2_enabled: bool = False     # 全局开关 · True=所有 v2 报告走客户决策页
    customer_decision_v2_whitelist: dict = {       # 白名单灰度
        "brand_ids": [],                  # list[int] · 指定 brand 启用客户决策页
        "user_ids": [],                   # list[int] · 指定代理(shared_by)启用
    }

    # ====== 公开报告 v3 灰度 + LLM 扩写预算 ======
    report_v3_enabled: bool = False
    report_v3_whitelist_user_ids: list[int] = []
    report_v3_auto_enrich: bool = False
    # Public web reports default to customer-decision. This is a manual,
    # explicit emergency rollback only; missing/invalid config must not enable it.
    public_report_legacy_rollback_enabled: bool = False
    llm_narrative_monthly_yuan: float = 100.0
    llm_narrative_max_concurrent: int = 3
    llm_narrative_alert_yuan: float = 80.0

    # ====== 达标算法 v3 公平算法(CTO-15.23 2026-05-13 老板拍板 · 修"倒反天罡") ======
    # db.monitoring_db._compute_effective_rate_v3() 走本开关
    # v2 痛点:effective_rate=(hist_avg×hist_days+today)/(hist_days+1) 全期累计 · 1 天 100% 后
    #         掉 N 天 → 再打 100% 也拉不回 target(分母 N+1 永远稀释)· 老板报"32.7%"截图复现
    # v3 修法:近 N 天滚动窗口 + max(detection, rolling) 保护 + 连续 M 天不达标切瞬时(暴露懒政)
    # 2026-05-24 老板拍 C 彻底切 v3(2026-05-13 灰度验证后)· "客户 4/4 全检出 100% UI 显示不达标"根因
    # 改默认 False→True · settings.json 无 compliance 字段 → 默认生效全局走 v3 公平算法
    # 单调改善:v3 = max(detection_rate, rolling_avg) · 原达标的客户不会变不达标
    compliance_algorithm_v3_enabled: bool = True     # 全局开关 · True=所有客户走 v3
    compliance_v3_whitelist: dict = {                 # 白名单灰度
        "quote_ids": [],                  # list[int] · 指定 quote_id 启用 v3
    }
    compliance_v3_rolling_window_days: int = 7        # 滚动窗口天数
    compliance_v3_lazy_threshold_days: int = 7        # 连续不达标 N 天 → 切瞬时暴露懒政

def load_settings() -> SystemSettings:
    """加载设置，优先从文件，否则从环境变量"""
    settings = SystemSettings()
    
    # 1. 尝试从文件加载
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                settings = SystemSettings(**data)
        except Exception as e:
            print(f"加载设置文件失败: {e}")
    
    # 2. API Keys 优先级 (P14-v14 + review HIGH#3 fix)
    #
    # 默认: env > settings.json (跟原版 + 部署惯例对齐 · 防止旧 settings.json 误覆盖 prod env)
    # 显式 SETTINGS_JSON_OVERRIDE_ENV=true 才反转(本机 dev / UI 可控场景)
    #
    # 不论哪种 · 最终值都 sync 回 os.environ · 让 platforms.py / crawler.py
    # 用 os.getenv 也能透明拿到 UI 改的值 (UI save_settings 后立即生效不用重启)
    _env_key_map = {
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "doubao_api_key": "DOUBAO_API_KEY",
        "doubao_endpoint_id": "DOUBAO_ENDPOINT_ID",
        "kimi_api_key": "KIMI_API_KEY",
        "tikhub_api_key": "TIKHUB_API_KEY",
        "siliconflow_api_key": "SILICONFLOW_API_KEY",
        "metaso_api_key": "METASO_API_KEY",
        # P14-v14: Jina Reader (调研监测 stage_3) 也走 UI 统一管理
        "jina_api_key": "JINA_API_KEY",
    }
    _override_env = os.environ.get('SETTINGS_JSON_OVERRIDE_ENV', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    for field, env_name in _env_key_map.items():
        cur_val = getattr(settings, field, "") or ""
        env_val = os.environ.get(env_name, "") or ""
        if _override_env:
            # 反转模式: settings.json 有值就用 · 空才 fallback env
            if not cur_val and env_val:
                setattr(settings, field, env_val)
                cur_val = env_val
        else:
            # 默认模式: env 优先 · 覆盖 settings.json
            if env_val:
                setattr(settings, field, env_val)
                cur_val = env_val
        # sync 回 os.environ · 让 os.getenv 也能拿到
        if cur_val:
            os.environ[env_name] = cur_val

    return settings


def save_settings(settings: SystemSettings) -> bool:
    """保存设置到文件"""
    try:
        # 创建不含API Key的安全副本用于保存
        data = settings.model_dump()
        
        # 同时更新环境变量（运行时生效）
        if settings.dashscope_api_key:
            os.environ["DASHSCOPE_API_KEY"] = settings.dashscope_api_key
        if settings.deepseek_api_key:
            os.environ["DEEPSEEK_API_KEY"] = settings.deepseek_api_key
        if settings.openrouter_api_key:
            os.environ["OPENROUTER_API_KEY"] = settings.openrouter_api_key
        if settings.doubao_api_key:
            os.environ["DOUBAO_API_KEY"] = settings.doubao_api_key
        if settings.doubao_endpoint_id:
            os.environ["DOUBAO_ENDPOINT_ID"] = settings.doubao_endpoint_id
        if settings.kimi_api_key:
            os.environ["KIMI_API_KEY"] = settings.kimi_api_key
        if settings.tikhub_api_key:
            os.environ["TIKHUB_API_KEY"] = settings.tikhub_api_key
        if settings.siliconflow_api_key:
            os.environ["SILICONFLOW_API_KEY"] = settings.siliconflow_api_key
        if settings.metaso_api_key:
            os.environ["METASO_API_KEY"] = settings.metaso_api_key
        # P14-v14: Jina key 同步 os.environ · crawler.py 用 os.getenv 立即生效
        if settings.jina_api_key:
            os.environ["JINA_API_KEY"] = settings.jina_api_key

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        return True
    except Exception as e:
        print(f"保存设置失败: {e}")
        return False


def mask_api_key(key: str) -> str:
    """遮蔽API Key中间部分"""
    if not key or len(key) < 8:
        return key
    return key[:4] + "****" + key[-4:]


def get_settings_for_frontend() -> dict:
    """获取前端显示用的设置（API Key遮蔽）"""
    settings = load_settings()
    data = settings.model_dump()
    
    # 遮蔽敏感信息
    data["dashscope_api_key"] = mask_api_key(data["dashscope_api_key"])
    data["deepseek_api_key"] = mask_api_key(data["deepseek_api_key"])
    data["openrouter_api_key"] = mask_api_key(data["openrouter_api_key"])
    data["doubao_api_key"] = mask_api_key(data["doubao_api_key"])
    data["kimi_api_key"] = mask_api_key(data["kimi_api_key"])
    data["tikhub_api_key"] = mask_api_key(data["tikhub_api_key"])
    data["siliconflow_api_key"] = mask_api_key(data["siliconflow_api_key"])
    data["metaso_api_key"] = mask_api_key(data["metaso_api_key"])
    # [WO_248 2026-09-20] 这四个原来**没遮**,而 `load_settings()` 会把 env 值并进模型
    #   ⇒ 生产里非空的那几个被原样回给管理员浏览器。
    #   🔴 「只有管理员看得到」不是不遮的理由:遮蔽防的是浏览器缓存、截图、
    #      录屏、F12、以及任何把响应体带出这台机器的动作。
    #   类锁见 tests/settings_mask_2026_09_20 —— 防的是**第 13 个漏网的**:
    #   凭「记得加」守不住,只有按字段名形态枚举才守得住。
    data["doubao_endpoint_id"] = mask_api_key(data["doubao_endpoint_id"])
    data["jina_api_key"] = mask_api_key(data["jina_api_key"])
    data["kuaiyibo_api_token"] = mask_api_key(data["kuaiyibo_api_token"])
    data["publish_channel_callback_secret"] = mask_api_key(
        data["publish_channel_callback_secret"])

    # 移除确认码哈希，只返回元信息
    if "confirm_codes" in data:
        del data["confirm_codes"]

    return data


async def test_api_connection(provider: str) -> tuple[bool, str]:
    """测试API连接 —— 使用 GET /v1/models（零费用、仅验证Key有效性）"""
    settings = load_settings()
    import httpx

    # LLM 提供商统一用 GET /v1/models 检测
    _LLM_ENDPOINTS = {
        "dashscope": {
            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            "key": settings.dashscope_api_key,
            "name": "DashScope",
        },
        "deepseek": {
            "url": "https://api.deepseek.com/v1/models",
            "key": settings.deepseek_api_key,
            "name": "DeepSeek",
        },
        "openrouter": {
            "url": "https://openrouter.ai/api/v1/models",
            "key": settings.openrouter_api_key,
            "name": "OpenRouter",
        },
        "doubao": {
            "url": "https://ark.cn-beijing.volces.com/api/v3/models",
            "key": settings.doubao_api_key,
            "name": "豆包",
        },
        "kimi": {
            "url": "https://api.moonshot.cn/v1/models",
            "key": settings.kimi_api_key,
            "name": "Kimi",
        },
        "siliconflow": {
            "url": "https://api.siliconflow.cn/v1/models",
            "key": settings.siliconflow_api_key,
            "name": "SiliconFlow",
        },
    }

    try:
        if provider in _LLM_ENDPOINTS:
            ep = _LLM_ENDPOINTS[provider]
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    ep["url"],
                    headers={"Authorization": f"Bearer {ep['key']}"},
                )
                if resp.status_code == 200:
                    # 尝试统计模型数量
                    try:
                        data = resp.json()
                        model_count = len(data.get("data", []))
                        return True, f"{ep['name']} 连接正常，可用模型: {model_count}个"
                    except Exception:
                        return True, f"{ep['name']} 连接正常"
                elif resp.status_code in (401, 403):
                    return False, f"{ep['name']} API Key 无效或已过期 ({resp.status_code})"
                elif resp.status_code == 429:
                    return False, f"{ep['name']} 请求过于频繁 (429)"
                else:
                    return False, f"{ep['name']} 返回 HTTP {resp.status_code}"

        elif provider == "tikhub":
            from tools.llm_call_tracker import llm_track

            async with httpx.AsyncClient(timeout=10) as client:
                async with llm_track(
                    "settings_health_check",
                    "tikhub",
                    model="endpoint_probe",
                ) as tracker:
                    resp = await client.get(
                        "https://api.tikhub.io/",
                        headers={"Authorization": f"Bearer {settings.tikhub_api_key}"},
                    )
                    tracker.record(
                        success=resp.status_code < 400,
                        error_msg=None if resp.status_code < 400 else resp.text[:200],
                    )
                if resp.status_code == 200:
                    return True, "TikHub 连接正常"
                else:
                    return False, f"TikHub 返回 HTTP {resp.status_code}"

        elif provider == "metaso":
            from tools.llm_call_tracker import llm_track

            async with httpx.AsyncClient(timeout=15) as client:
                async with llm_track(
                    "settings_health_check",
                    "metaso",
                    model="search",
                    metadata={"scope": "webpage", "size": 1},
                ) as tracker:
                    resp = await client.post(
                        "https://metaso.cn/api/v1/search",
                        headers={
                            "Authorization": f"Bearer {settings.metaso_api_key}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json={"q": "test", "scope": "webpage", "size": 1},
                    )
                    tracker.record(
                        success=resp.status_code < 400,
                        error_msg=None if resp.status_code < 400 else resp.text[:200],
                    )
                if resp.status_code == 200:
                    return True, "Metaso 连接正常"
                elif resp.status_code in (401, 403):
                    return False, f"Metaso API Key 无效或权限不足 ({resp.status_code})"
                elif resp.status_code == 402:
                    return False, "Metaso 账户余额不足"
                else:
                    return False, f"Metaso 返回 HTTP {resp.status_code}"

        elif provider == "5118_longtail":
            import os as _os
            key = _os.environ.get("API_5118_LONGTAIL_KEY", "")
            if not key:
                return False, "未配置 API_5118_LONGTAIL_KEY 环境变量"
            from tools.llm_call_tracker import llm_track

            async with httpx.AsyncClient(timeout=15) as client:
                async with llm_track(
                    "settings_health_check",
                    "5118",
                    model="longtail",
                    metadata={"page_size": 1},
                ) as tracker:
                    resp = await client.post(
                        "https://apis.5118.com/keyword/word/v2",
                        headers={
                            "Authorization": key,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        data={"keyword": "测试", "page_index": "1", "page_size": "1"},
                    )
                    tracker.record(
                        success=resp.status_code < 400,
                        error_msg=None if resp.status_code < 400 else resp.text[:200],
                    )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if data.get("errcode") == "0":
                            return True, "5118 长尾词 API 连接正常"
                        else:
                            return False, f"5118 长尾词 API 错误: {data.get('errmsg', data.get('errcode'))}"
                    except Exception:
                        return True, "5118 长尾词 API 连接正常（HTTP 200）"
                else:
                    return False, f"5118 长尾词 API 返回 HTTP {resp.status_code}"

        elif provider == "5118_searchvol":
            import os as _os
            key = _os.environ.get("API_5118_SEARCH_VOLUME_KEY", "")
            if not key:
                return False, "未配置 API_5118_SEARCH_VOLUME_KEY 环境变量"
            from tools.llm_call_tracker import llm_track

            async with httpx.AsyncClient(timeout=15) as client:
                async with llm_track(
                    "settings_health_check",
                    "5118",
                    model="search_volume",
                    metadata={"keyword_count": 1},
                ) as tracker:
                    resp = await client.post(
                        "https://apis.5118.com/keywordparam/v2",
                        headers={
                            "Authorization": key,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        data={"keywords": "测试"},
                    )
                    tracker.record(
                        success=resp.status_code < 400,
                        error_msg=None if resp.status_code < 400 else resp.text[:200],
                    )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if data.get("errcode") == "0":
                            return True, "5118 搜索量 API 连接正常"
                        else:
                            return False, f"5118 搜索量 API 错误: {data.get('errmsg', data.get('errcode'))}"
                    except Exception:
                        return True, "5118 搜索量 API 连接正常（HTTP 200）"
                else:
                    return False, f"5118 搜索量 API 返回 HTTP {resp.status_code}"

        return False, f"未知的提供商: {provider}"

    except httpx.TimeoutException:
        return False, f"连接超时（>15秒）"
    except httpx.ConnectError as e:
        return False, f"无法连接: {str(e)[:100]}"
    except Exception as e:
        return False, f"检测异常: {str(e)[:100]}"


# 全局设置缓存
_cached_settings: Optional[SystemSettings] = None


def get_current_settings() -> SystemSettings:
    """获取当前设置（带缓存）"""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = load_settings()
    return _cached_settings


def reload_settings():
    """重新加载设置"""
    global _cached_settings
    _cached_settings = load_settings()
    return _cached_settings


# ==========================================
# v2.7.1 GEO 文体改造 · SSOT helper(keyword-only unit 必填)
# ==========================================

def get_effective_content_ratios(industry: str | None = None,
                                  *,  # v2.3 keyword-only barrier
                                  unit: Literal["percent", "fraction"]) -> dict:
    """SSOT · content_ratios 8 angle key 运行时实际生效值

    Args:
        industry: 行业 key(医疗健康 / 法律商务 等)
        unit: KEYWORD-ONLY 必填 · "percent" 0-100 / "fraction" 0.0-1.0

    Returns:
        8 key dict · percent sum=100 / fraction sum=1.0
    """
    settings = get_current_settings()
    overrides = getattr(settings, "industry_overrides", None) or {}

    if industry and industry in overrides:
        ratios = overrides[industry].get("content_ratios", settings.content_ratios)
    else:
        ratios = settings.content_ratios

    ratios = dict(ratios)
    try:
        from writing.evidence_first_policy import is_evidence_first_enabled
        if is_evidence_first_enabled():
            removed = int(ratios.get("authority", 0) or 0)
            ratios["authority"] = 0
            ratios["deep_dive"] = int(ratios.get("deep_dive", 0) or 0) + removed // 2
            ratios["checklist"] = int(ratios.get("checklist", 0) or 0) + removed - removed // 2
    except Exception:
        pass

    if unit == "fraction":
        return {k: v / 100.0 for k, v in ratios.items()}
    return dict(ratios)


# [WP12 P0-2] 榜单族 = 复活的榜单 + 推荐 + 对比。默认档要求三者合计 ≥50%。
RANKING_FAMILY_STYLE_CODES: tuple = (
    "ranking_v2", "authority_ranking", "recommendation_review", "comparison_review",
)
RANKING_FAMILY_DEFAULT_MIN_SHARE: int = 50


def ranking_family_share(ratios: dict | None) -> int:
    """榜单/推荐/对比三类在一份配比里的合计百分比。"""
    data = ratios or {}
    total = 0
    for code in RANKING_FAMILY_STYLE_CODES:
        try:
            total += int(data.get(code, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def describe_style_ratio_effectiveness() -> dict:
    """运行时自检:持久化的 settings.json 是否把默认档压回了退榜单口径。

    settings.json 是 gitignored 的运行时配置,历史上出现过"代码改了默认值但
    生产实际跑的是持久化旧值"的静默漂移。这里给出可观测结论,供启动诊断、
    admin 页面和判别测试共用,而不是让漂移无声发生。
    """
    settings = get_current_settings()
    default_ratios = dict(getattr(settings, "style_ratios", None) or {})
    share = ranking_family_share(default_ratios)
    return {
        "default_ranking_family_share": share,
        "required_min_share": RANKING_FAMILY_DEFAULT_MIN_SHARE,
        "meets_wp12_default": share >= RANKING_FAMILY_DEFAULT_MIN_SHARE,
        "persisted_settings_file_present": SETTINGS_FILE.exists(),
        "category_sets": sorted((getattr(settings, "style_ratio_category_sets", None) or {}).keys()),
        "repair_hint": (
            ""
            if share >= RANKING_FAMILY_DEFAULT_MIN_SHARE
            else "默认档榜单族占比低于 50%：生产 settings.json 可能仍固化着退榜单口径，"
                 "需在系统设置里重存文体配比或更新该文件。"
        ),
    }


def resolve_style_ratio_category(industry: str | None) -> str:
    """[WP12 P0-2] 行业 → 文体类目档。未命中返回 ""(= 默认档)。

    子串双向匹配,和 placement/head-fallback 的既有口径一致:客户填的行业文本
    常是"深圳全屋定制"这种自由文本,精确 key 匹配基本命中不了。
    """
    search = str(industry or "").strip()
    if not search:
        return ""
    settings = get_current_settings()
    mapping = getattr(settings, "industry_style_categories", None) or {}
    for category, keywords in mapping.items():
        for keyword in (keywords or []):
            token = str(keyword or "").strip()
            if not token:
                continue
            if token in search or search in token:
                return str(category)
    return ""


def get_effective_style_ratios(industry: str | None = None,
                                *,  # v2.3 keyword-only barrier
                                unit: Literal["percent", "fraction"]) -> dict:
    """SSOT · style_ratios 11 style key 运行时实际生效值

    Args:
        industry: 行业 key
        unit: KEYWORD-ONLY 必填 · "percent" / "fraction"

    Returns:
        11 key dict · sum=100 / 1.0 · 不含 company_profile(走 fixed_count=1)
    """
    settings = get_current_settings()
    overrides = getattr(settings, "industry_overrides", None) or {}

    if industry and industry in overrides:
        # 精确行业 override(医疗/法律强制 0)优先级最高,分治不得覆盖它。
        ratios = overrides[industry].get("style_ratios", settings.style_ratios)
    else:
        # [WP12 P0-2] 类目分治:命中登记类目用该类目档,未命中走默认档。
        category = resolve_style_ratio_category(industry)
        category_sets = getattr(settings, "style_ratio_category_sets", None) or {}
        ratios = category_sets.get(category) if category else None
        if not ratios:
            ratios = settings.style_ratios

    ratios = dict(ratios)
    # [SSOT geo-commercial-intent-governance-v1.0 §4.4 · Review-CTO 2026-07-23
    #  P1-4 返工] 运行时不再强制改写任何文体配比(此前 ranking_v2 /
    # authority_ranking / trojan_horse 在 evidence-first 下被强制 0):
    # 非法律问题只能提示并由有权限的人决定,配比是 Owner 可调的运营配置,
    # 不是代码钉死的禁令。trojan_horse 默认配比为 0 由配置本身表达,
    # Owner 如需启用可在设置中调整并自担内容责任。

    if unit == "fraction":
        return {k: v / 100.0 for k, v in ratios.items()}
    return dict(ratios)


# ==========================================
# 批量健康检查
# ==========================================

# 所有可检测的外部服务
HEALTH_CHECK_SERVICES = [
    {"id": "dashscope", "name": "DashScope (阿里云)", "type": "LLM", "key_field": "dashscope_api_key"},
    {"id": "deepseek", "name": "DeepSeek", "type": "LLM", "key_field": "deepseek_api_key"},
    {"id": "openrouter", "name": "OpenRouter", "type": "LLM", "key_field": "openrouter_api_key"},
    {"id": "doubao", "name": "豆包 (字节跳动)", "type": "LLM", "key_field": "doubao_api_key"},
    {"id": "kimi", "name": "Kimi (月之暗面)", "type": "LLM", "key_field": "kimi_api_key"},
    {"id": "siliconflow", "name": "SiliconFlow", "type": "LLM", "key_field": "siliconflow_api_key"},
    {"id": "tikhub", "name": "TikHub (社媒数据)", "type": "数据服务", "key_field": "tikhub_api_key"},
    {"id": "metaso", "name": "Metaso (秘塔搜索)", "type": "数据服务", "key_field": "metaso_api_key"},
    {"id": "5118_longtail", "name": "5118 长尾词挖掘", "type": "数据服务", "key_field": "_env_5118_longtail"},
    {"id": "5118_searchvol", "name": "5118 搜索量查询", "type": "数据服务", "key_field": "_env_5118_searchvol"},
]


async def batch_health_check():
    """
    并行检测所有外部服务连通性（async generator）。
    所有服务同时发起检测，谁先完成谁先 yield，通过 SSE 实时推送。
    """
    import asyncio
    import time
    from datetime import datetime

    settings = load_settings()
    total = len(HEALTH_CHECK_SERVICES)
    queue: asyncio.Queue = asyncio.Queue()
    done_count = 0

    async def _check_one(service: dict):
        """单个服务检测任务，完成后将结果放入 queue"""
        # 5118 等服务的 key 存在环境变量而非 settings.json 中
        if service["key_field"].startswith("_env_"):
            key_value = True
        else:
            key_value = getattr(settings, service["key_field"], "")

        if not key_value:
            result = {
                "id": service["id"],
                "name": service["name"],
                "type": service["type"],
                "status": "unconfigured",
                "message": "未配置 API Key",
                "latency_ms": None,
            }
        else:
            t0 = time.time()
            try:
                success, message = await test_api_connection(service["id"])
                latency = round((time.time() - t0) * 1000)
                result = {
                    "id": service["id"],
                    "name": service["name"],
                    "type": service["type"],
                    "status": "ok" if success else "error",
                    "message": message,
                    "latency_ms": latency,
                }
            except Exception as e:
                latency = round((time.time() - t0) * 1000)
                result = {
                    "id": service["id"],
                    "name": service["name"],
                    "type": service["type"],
                    "status": "error",
                    "message": f"检测异常: {str(e)[:100]}",
                    "latency_ms": latency,
                }
        await queue.put(result)

    # 并行启动所有检测任务
    tasks = [asyncio.create_task(_check_one(s)) for s in HEALTH_CHECK_SERVICES]

    # 从 queue 中逐个读取结果（谁先完成谁先推送）
    for i in range(total):
        result = await queue.get()
        done_count += 1
        yield {
            "type": "service_result",
            "progress": done_count,
            "total": total,
            "service": result,
        }

    # 确保所有任务都完成
    await asyncio.gather(*tasks, return_exceptions=True)

    # 最后发送完成信号
    yield {
        "type": "complete",
        "checked_at": datetime.now().isoformat(),
    }
