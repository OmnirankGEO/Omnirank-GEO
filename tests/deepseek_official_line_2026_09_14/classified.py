# -*- coding: utf-8 -*-
"""WO_206 · 模型名字面量的**人读**分类表(签入)。

🔴 为什么不自动判供货线:试过「最近网关标记」的启发式,抽核当场被骗两次 ——
   `writing/llm_utils.py:50` 上方注释写着「落回 dashscope/qwen3.6-plus」
   (那段其实走官方线);跳过注释后又因三条线的代码彼此挨着判成 openrouter。
   **自动判供货线在本仓产出的是自信的错答案**,而错答案会让我去改不该改的行 ——
   把百炼那条链改成一个它不认识的名字,只在运行时报「模型不存在」,
   或者更糟,静默落到别的档。
   所以:**枚举靠脚本(可靠),分类靠人读(每行一句依据)**。

🔴🔴 2026-09-14 返工:第一版的枚举锚是**形状猜测**,漏了 126 处生产字面量
   (`model_id=` / `model_name=` / 位置参数 / `MODEL_NAME=` / `or 兜底` …),
   其中确有真发出点。现在 `census.py` 枚举**所有**像模型名的字面量,
   本表要对**每一条**签字 —— 包括「它不是发出点」这句话本身。
   「这不是发出点」必须是**签了字的话**,不能是锚没覆盖造成的**沉默**。

🔴 本表是仓级锁的分母:重新枚举出的字面量不在表里 ⇒ 红。
   它同时是 `docs/AI-CONTEXT/DEEPSEEK_OFFICIAL_EMIT_SITES_2026-09-14.md` 的数据源
   —— 文档由它生成,两边不会漂。

判决 = `(kind, provider, scope, reason, plan)`:
  kind     ∈ **五种**(Review 09-14 定的口径,不许再长出一个动物园):
             `emit` 真发出去的模型名(含「面向用户的可选项」—— 选中后原样成为发出名)
             `registry_key` 注册表键 / 库里存的档位键,经二次解析才知道真模型
             `compare` 比较 / 分档 / 查表的键(计价表、思考开关、归一用的别名集)
             `label` 记账 / 展示标签(记的不一定是真跑的那个)
             `doc` 注释、docstring 里提到的名字,不是代码
             —— 外加一个**诚实的非分类** `unread`:社媒 / IP 板块的行,
             本窗不碰,因此**没有逐行读**。它不是第六种 kind,是一句
             「我没读过这一行」。标它比硬塞进五种里更安全。
  provider ∈ official | dashscope | openrouter | registry | mixed | n/a
  scope    ∈ a(GEO 干活模型)| b(被监测引擎)| c(社媒/IP)| x(非生产)
  reason   —— 人读一句:凭什么这么判
  plan     ∈ switch(本单要改)| keep(不动)| later(后续批次)

🔴 **分母的排除规则**(Review 要求写明,好让两份独立计数对得上):
   分母 = `git ls-files '*.py'` 里**除 `tests/` 与 `scripts/` 之外**的全部文件。
   排除这两处的理由是它们不进生产路径;代价是判据自己写的模型名不入账 ——
   这恰好让冻结数不会因为我改一句注释而漂。
   对账(2026-09-14):我数 **213** 条 / 87 文件,Review 独立数 **210** 条 / 87 文件。
   差的正好是三条**带斜杠**的名字(Review 的「以 `deepseek-` 开头」把它们滤掉了):
   `deepseek-ai/DeepSeek-V3`(SiliconFlow)、`deepseek/deepseek-r1`(OpenRouter)、
   `DeepSeek/v4-flash`(展示标签)。前两条**必须**留在分母里 ——
   它们是别家的 ID,正是最不该被顺手归一的那种。

🔴🔴 **Owner 2026-09-14 裁定的作用域**(原文照抄,别拿「全部」两个字去改社媒):
   Owner 原话「**全部改成 deepseek-flash**」——
   Review 09-14 在 WO_206 §9.1 订正了它的作用域:**只对 GEO 干活线(scope=a)生效**。
   Deploy 全仓实测 `deepseek-v4-pro` 命中 30 个文件,其中社媒 / IP 板块那些是
   2026-05-11 Social-CTO 老板拍板**刻意选的**(flywheel_engine_v2(已随开源 E3 B2 删) 的 MODEL_CRITICAL、
   multi_ai_voter 的 L3 蒸馏、generation_modes(已随开源 E3 B2 删) 的 professional/super、
   industry_knowledge_collector 的 L3),而官方仍在供应 `deepseek-v4-pro` ⇒ 显式排除:
   `tools/social_operator/**`(已随开源 E3 B2 删) · `services/multi_ai_voter.py` ·
   `tools/industry_knowledge_collector.py` · `api/social_*` · `advisors/**`。
   本表里这些行本来就是 unread / keep,c1b② 不碰。

   🔴 `unread` 那一批(社媒 / IP)只配 `keep`,且 **c1c / c1d 的分母不含它** ——
   它们等社媒窗口自己来读。当前实测 34 条 / 15 文件
   (Review 读到 39 / 16,差的 5 条 1 文件是 `deepseek_key_pool.py`:
    它在社媒目录下却是 GEO 官方 key 池,已人读改判,退出了这一批)。

   🔴 另一条排除:**裸 `"deepseek"`**(后面不跟 `-` 或 `/`)不进分母 ——
   它是 provider 名 / 注册表别名键,全仓 175 处,不是模型名。
   代价记在这里:c1a 表里 `employees/project_director.py` 那一行
   (`model='deepseek'`)因此**退出了分母**,不是被我悄悄删掉的。
   它仍受保护:哪天有人把它改成 `model='deepseek-v4-flash'`,
   新锚立刻收得到,而那一条会因为「没人分类过」当场转红。
"""

#: 整文件同一条供货线、同一种 kind 的,写在这里(省得逐行重复)。
#: 🔴 **blanket 有风险**:分母变大时,新冒出来的字面量会被它悄悄裹进去。
#:    `config/settings_manager.py:129` 就是这么被裹成 switch 的(它其实是百炼侧),
#:    所以每次分母变大,都要看一遍「哪些行是新被 blanket 裹进来的」。
FILE_VERDICT = {
    # ── 官方线 · GEO 干活 · 本单要改 ──────────────────────────────
    "agents/chief_editor_agent.py": ("emit", "official", "a", "文件顶 DEEPSEEK_API_URL = api.deepseek.com,同文件唯一网关", "switch"),
    "agents/report_enhancement_agent.py": ("emit", "official", "a", "文件顶 DEEPSEEK_API_URL = api.deepseek.com,同文件唯一网关;:175 是同一条线的 or 兜底", "switch"),
    "agents/review_agent.py": ("emit", "official", "a", "请求体直接 POST api.deepseek.com/v1/chat/completions", "switch"),
    "agents/source_relevance_agent.py": ("emit", "official", "a", "请求体直接 POST api.deepseek.com/v1/chat/completions", "switch"),
    "agents/provider_config.py": ("emit", "official", "a", "OpenAIChatModel(…, base_url=https://api.deepseek.com/v1) —— 官方线直连", "switch"),
    "api/monitoring_api.py": ("emit", "official", "a", "洞察文案调用 POST api.deepseek.com/chat/completions(非被监测引擎)", "switch"),
    "api/xiaobang_api.py": ("emit", "official", "a", "DEEPSEEK_API_URL 默认 api.deepseek.com/v1;小帮是 GEO 助手(纯问答),属 a 不属社媒", "switch"),
    "services/ai_ops/chat_intent.py": ("emit", "official", "a", "_DEEPSEEK_URL = api.deepseek.com/v1/chat/completions", "switch"),
    "services/domain_authority_ai.py": ("emit", "official", "a", "_DEEPSEEK_URL 官方线;注释自称 deepseek-chat 是 v4-flash 别名", "switch"),
    "services/geo_douyin/content_generator.py": ("emit", "official", "a", "文件头写明「官方 DeepSeek V4 Flash 直连 api.deepseek.com」", "switch"),
    "services/geo_observation/entity_review.py": ("emit", "official", "a", "DEEPSEEK_OFFICIAL_URL = api.deepseek.com/v1/chat/completions", "switch"),
    "services/geo_observation_analytics/contract.py": ("emit", "official", "a", "SEMANTIC_BASE_URL = https://api.deepseek.com", "switch"),
    "services/marketing/advisor_llm.py": ("emit", "official", "a", "_DEEPSEEK_URL 官方线 + pick_deepseek_api_key 官方 key 池", "switch"),
    "services/media_domain_directory.py": ("emit", "official", "a", "DISTILL_MODEL 上方注释写明「官方 DeepSeek V4 Flash 直连 · 禁代理版」", "switch"),
    "tools/cluster_gatekeeper.py": ("emit", "official", "a", "has_deepseek_key() + 官方 key 池 failover", "switch"),
    "tools/competitor/competitor_identifier.py": ("emit", "official", "a", "adeepseek_post_with_failover(官方 key 池)", "switch"),
    "tools/industry_baseline_dynamic.py": ("emit", "official", "a", "POST api.deepseek.com/v1/chat/completions", "switch"),
    "tools/keyword_cluster.py": ("emit", "official", "a", "has_deepseek_key() + adeepseek_post_with_failover", "switch"),
    "tools/keyword_generator.py": ("emit", "official", "a", "url=https://api.deepseek.com/chat/completions", "switch"),
    "tools/llm_keyword_expander.py": ("emit", "official", "a", "DEFAULT_ENDPOINT = api.deepseek.com/v1/chat/completions", "switch"),
    "tools/llm_pricing_scorer.py": ("emit", "official", "a", "DEFAULT_ENDPOINT = api.deepseek.com/v1/chat/completions", "switch"),
    "tools/pricing_llm_assessor.py": ("emit", "official", "a", "直接传 https://api.deepseek.com/v1/chat/completions + 官方 key 池 failover", "switch"),
    "tools/scoring/llm_geo_scorer.py": ("emit", "official", "a", "DEEPSEEK_API_URL = api.deepseek.com/v1/chat/completions", "switch"),
    "tools/sentiment_classifier.py": ("emit", "official", "a", "base_url 取官方线;两处同名一起改:一处是 llm_track 的记账标签、一处是请求体里真发出去的,记的必须和发的一致", "switch"),
    "config/settings_manager.py": ("emit", "official", "a", "geo_scoring / 三个飞轮解释层条目都写着 provider: deepseek(官方线)", "switch"),

    # ── 百炼 / DashScope · **一个字不动** ──────────────────────────
    #: 🔴 [WO_221-c1'] 改判:这一格是**写进账本的计划模型**(`run_ledger_bridge`
    #:   直接落 actual_provider/actual_model)。2026-07-27 起 DeepSeek 监测换官方
    #:   原生检索,合同却一直写着 dashscope + 退役名 ⇒ 账本那一列一直是错的。
    #:   本模块是**零依赖** SSOT(只许 stdlib),所以名字**刻意写字面量**、不取常量。
    "services/engine_contract.py": ("emit", "official", "a", "PLATFORM_CONTRACT['deepseek'] 的计划模型;provider 已改 deepseek_official,名字随实发。零依赖模块,刻意用字面量", "switch"),
    "tools/keyword_expander.py": ("emit", "dashscope", "a", "走 self.llm_endpoint(百炼兼容模式),模型名是百炼侧 ID", "keep"),
    "tools/keyword/longtail_matrix.py": ("emit", "dashscope", "a", "同文件 DASHSCOPE_API_URL = dashscope.aliyuncs.com/compatible-mode", "keep"),
    #: 🔴 [WO_221-c1' · Review 发现] 这条判词**过期了**:那一格是 DeepSeek 监测平台的
    #:   成本占位(monitoring_token_usage -> api_costs -> 财务/看板),而该平台
    #:   2026-07-27 起已换官方原生检索 —— 继续写 dashscope 就是把成本记到百炼名下。
    #:   已改成从 `batch_monitor` 的血缘单点取(不再手写第二份)。
    #: 🔴 [WO_221-c1' ⑦] `db/monitoring_db.py` **已从本表移除**:它不再含任何模型名
    #:   字面量 —— 成本身份改成从 `PLATFORM_CONTRACT['deepseek']` 取(与同文件里
    #:   qwen 那一格同法)。**不是名字被删了,是这个文件不再自己写名字**。
    "api/research_monitor_config_api.py": ("emit", "dashscope", "a", "配置键名就叫 model_deepseek_via_dashscope", "keep"),
    "services/geo_observation/source_hooks.py": ("emit", "dashscope", "a", "元组里显式写 dashscope + deepseek_dashscope_search_legacy", "keep"),
    "services/knowledge_pipeline.py": ("emit", "dashscope", "a", "KB 清洗仍走 DashScope compatible-mode(api_key=DASHSCOPE_API_KEY,老板 05-11 只换模型不换通道)", "keep"),
    "services/research_monitor/article_intent_classifier.py": ("emit", "dashscope", "a", "同文件 DASHSCOPE_COMPAT_URL 是唯一网关", "keep"),
    "services/research_monitor/industry_resolver.py": ("emit", "dashscope", "a", "同文件 DASHSCOPE_COMPAT_URL 是唯一网关(注释自述抄 article_intent_classifier)", "keep"),
    "services/research_monitor/query_intent_classifier.py": ("emit", "dashscope", "a", "同文件 DASHSCOPE_COMPAT_URL 是唯一网关", "keep"),
    "services/geo_observation/promotion.py": ("emit", "dashscope", "a", "调用方传入 provider,默认走百炼兼容模式", "keep"),

    # ── 注册表键 / 库默认值:不是发出去的名字,由二次解析决定真模型 ──
    "employees/diagnosis/ai_tester.py": ("registry_key", "registry", "a", "model= 是注册表键,实际网关由 base_employee.MODEL_CONFIGS 解析 → 百炼", "keep"),
    "employees/diagnosis/competitor_analyst.py": ("registry_key", "registry", "a", "model= 传注册表键,MODEL_CONFIGS 该档 base_url 是百炼兼容模式", "keep"),
    "employees/diagnosis/data_collector.py": ("registry_key", "registry", "a", "同上一行:注册表键经 MODEL_CONFIGS 解析,该档走百炼兼容模式", "keep"),
    "employees/diagnosis/report_writer.py": ("registry_key", "registry", "a", "注册表键 v4-pro 档,MODEL_CONFIGS 里该档 base_url 是百炼兼容模式", "keep"),
    "employees/content/content_writer.py": ("registry_key", "registry", "a", "注册表键经 MODEL_CONFIGS 解析,该档走百炼兼容模式", "keep"),
    "employees/support/chief_editor.py": ("registry_key", "registry", "a", "注册表键 reasoner 档;reasoner 是官方线另一个模型,本单不归一", "keep"),
    "employees/support/ppt_specialist.py": ("registry_key", "registry", "a", "注册表键经 MODEL_CONFIGS 解析,该档走百炼兼容模式", "keep"),
    "employees/dynamic_employee.py": ("registry_key", "registry", "a", "函数签名默认值,落到 MODEL_CONFIGS 解析;改它等于改存量员工的档位", "keep"),
    "employees/employee_registry.py": ("registry_key", "registry", "a", "row['model_id'] 的 or 兜底,仍是注册表键", "keep"),
    "api/employee_api.py": ("registry_key", "registry", "a", "emp.get('model_id', …) 的兜底,是注册表键不是发出名", "keep"),
    "db/diagnosis_db.py": ("registry_key", "registry", "a", "员工种子 model_id / SQL DEFAULT / settings 种子 —— 都是注册表键,真网关由 MODEL_CONFIGS 决定", "keep"),
    "tools/industry_knowledge_collector.py": ("registry_key", "registry", "a", "call_ai() 第一参是 multi_ai_voter.AI_CONFIGS 的**键**;该键落官方 pro 档,而 pro 名字本来就不改", "keep"),

    # ── 被监测引擎(范围 b):换它 = 换被测对象,不动 ────────────────
    "services/ai_surface_monitoring/lineage.py": ("registry_key", "mixed", "b", "SurfaceSpec 是**被监测表面**清单,default_model_key 指被测模型 —— 换它=换被测对象", "keep"),
    "tools/monitoring/batch_monitor.py": ("emit", "mixed", "b", "批量监测:把 DeepSeek 当搜索引擎查,属被测对象", "keep"),

    # ── 记账 / 展示标签:记的不一定是真跑的那个,硬编码是既有缺陷 ────
    "services/marketing/patrol.py": ("label", "n/a", "a", "写进营销案记录的 llm_model 字段;硬编码一个名字本身就是缺陷(记的不一定是真跑的),另立单", "later"),
    "tools/article_generator.py": ("label", "n/a", "a", "calculate_token_cost / save_token_usage 的成本标签,不是发给谁的模型名;硬编码同上", "later"),
    "tools/batch_pricing_llm.py": ("label", "n/a", "a", "写进 quote_data['_llm_model'] 的记录字段,不是发出去的名字", "later"),

    # ── 计价表 / 常量 SSOT ────────────────────────────────────────
    "tools/llm_call_tracker.py": ("compare", "n/a", "a", "计价表的键(provider, model):它**读**名字算钱,不发名字;价目对账见 206-c1p", "keep"),
    "config/deepseek_models.py": ("compare", "official", "a", "本单常量 SSOT 的**别名集**:归一时用来比对历史名,它比名字不发名字", "keep"),

    # ── 路径在社媒目录、实际归 GEO 的:必须人读,不能让路径规则替我判 ──
    # 🔴 Review 09-14 点名:它当时虽在 tools/social_operator/(已随开源 E3 B2 删) 下,却是**GEO 官方 key 池**,
    #    c1a 还专门改过它(加认新名)。让路径规则把它落成 unread,等于把一个
    #    我改过的文件标成「我没读过」—— 那是一句假话。
    "services/llm/deepseek_key_pool.py": ("compare", "official", "a", "deepseek_role_for_model 按**子串比**模型名分 key 池档位:它比名字,不发名字;c1a 已加认新名", "keep"),

    # 🔴 [WO_259 · 开源 E0 2026-09-22] `advisor_llm.py` 跟着 key 池一起上提
    #    (E3 整包删社媒,而 GEO 主链 33 个在役文件引着这两个模块)。
    #    路径一离开 `tools/social_operator/`(已随开源 E3 B2 删),路径规则就不再替它判 —— 这是**要的**:
    #    它现在是 GEO 主链的地基,得有人读过。读了:三处模型名都在
    #    `advisor_flash` 的 **flash 档路由**上,由 `SOCIAL_FLASH_PROVIDER` /
    #    `SOCIAL_FLASH_MODEL` 两个环境变量驱动(:132 / :145 的 `os.getenv` 默认值),
    #    既不是官方线常量的来源、也不改档 —— 属"读名字不发名字"的比较位。
    #    ⚠️ 那个默认值仍是**旧名**;官方 09-13 已改名。
    #       本单是纯搬家不改行为,所以**不动它**,单独记一笔待办(见交付单)。
    "services/llm/advisor_llm.py": ("compare", "official", "a", "advisor_flash 的 flash 档路由:两个 SOCIAL_FLASH_* 环境变量的默认值与判断,读名字不发名字;默认值仍是旧名,待办另记", "keep"),

    # ── 板块边界(社媒/IP):路径不在 census._SOCIAL 里,显式点名 ────
    "tools/agent_loop/model_config.py": ("unread", "n/a", "c", "SOCIAL_AGENT_DEFAULT_MODEL —— 社媒 agent 的模型档,板块边界(元指令 12),本窗不碰", "keep"),

    # ── 需要**逐行**判的文件,见 SITE_VERDICT ────────────────────
    # employees/base_employee.py / server.py / config/model_config.py /
    # services/multi_ai_voter.py / tools/ai_visibility/ai_tester.py /
    # tools/multi_llm_caller.py / workflows/diagnosis_workflow.py /
    # writing/llm_utils.py / writing/llm_providers.py / writing/style_registry.py /
    # writing/evidence_verifier.py / services/flywheel_judgment.py /
    # services/placement_service.py / services/research_monitor/platforms.py /
    # services/article_ai_review.py / api/content_api.py
}

#: 同一个文件里两条线都在的,逐行判。**这是本单最危险的地方**。
#: 键可以是 `(path, line)`,也可以是 `(path, line, model)` —— 后者用于
#: **同一行上两个字面量判决不同**的情况(`writing/style_registry.py:989` 就是)。
SITE_VERDICT = {
    ("services/multi_ai_voter.py", "deepseek-chat", 1): ("doc", "official", "a", "c1b② 注释行:说明只改 model 不改键、以及下面 v4-pro 为何不动", "keep"),
    ("services/multi_ai_voter.py", "deepseek-chat", 2): ("registry_key", "official", "a", "PROVIDERS 的**键**:call_ai() 第一参传的就是它,改键要连所有调用点一起改", "keep"),
    ("services/multi_ai_voter.py", "deepseek-flash", 1): ("emit", "official", "a", "该条目 url = api.deepseek.com/v1/chat/completions;c1b② 已改取常量(原 deepseek-chat,官方定价页上已消失)", "switch"),
    ("services/multi_ai_voter.py", "deepseek-v4-pro", 1): ("registry_key", "official", "a", "PROVIDERS 的键(官方 pro 档)", "keep"),
    ("services/multi_ai_voter.py", "deepseek-v4-pro", 2): ("emit", "official", "a", "🔴 2026-05-11 Social-CTO 老板拍板刻意选的官方 v4-pro(L3 蒸馏),官方仍在供应 ⇒ Review §9.1 排除清单,一个字不动", "keep"),
    ("services/multi_ai_voter.py", "deepseek-v4-flash", 1): ("registry_key", "dashscope", "a", "PROVIDERS 的键(百炼档)", "keep"),
    ("services/multi_ai_voter.py", "deepseek-v4-flash", 2): ("emit", "dashscope", "a", "该条目 url = dashscope.aliyuncs.com/compatible-mode", "keep"),
    ("services/multi_ai_voter.py", "deepseek-chat", 3): ("doc", "n/a", "a", "函数 docstring 里的返回值示例,不是代码", "keep"),
    ("writing/style_registry.py", "deepseek-v4-pro", 1): ("emit", "dashscope", "a", "available_providers 里 id='dashscope' 那条的可选模型 —— 百炼侧 ID", "keep"),
    ("writing/style_registry.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "available_providers 里 id='dashscope' 那条的可选模型 —— 百炼侧 ID", "keep"),
    ("writing/style_registry.py", "deepseek-flash", 1): ("emit", "official", "a", "id='deepseek' 那条的 models[0]:前端选中该 provider 就自动选它,存进 settings.json 再由 get_llm_config 原样发出去 —— 角色是「发」;c1b② 已改取常量,并按 Owner 09-14「全部改成 deepseek-flash」 把两个跑同一档的名字并成一个", "switch"),
    ("writing/llm_utils.py", "deepseek-flash", 1): ("emit", "official", "a", "同段 default_provider='deepseek'(写作链既定官方线);c1b② 已改取常量", "switch"),
    ("writing/llm_utils.py", "deepseek-flash", 2): ("emit", "official", "a", "同段 provider='deepseek';c1b② 已改取常量(原 deepseek-chat)", "switch"),
    ("writing/llm_utils.py", "deepseek-v4", 1): ("compare", "official", "a", "思考开关的**分档判断**(c1a 已加认新名);它比名字,不发名字", "keep"),
    ("writing/llm_utils.py", "deepseek-flash", 3): ("compare", "official", "a", "思考开关的**分档判断**(c1a 已加认新名);它比名字,不发名字", "keep"),
    ("writing/llm_utils.py", "deepseek-reasoner", 1): ("compare", "official", "a", "同一个判断里的 reasoner 分支;c1d 随「reasoner 已并入别名」一起复核", "keep"),
    ("writing/llm_providers.py", "deepseek-v3", 1): ("registry_key", "official", "a", "LLM_PROVIDERS 的**键** 'deepseek-v3';调用方按键取档,改键要连调用点一起改", "keep"),
    ("writing/llm_providers.py", "deepseek-flash", 1): ("emit", "official", "a", "该条目 api_url = https://api.deepseek.com/v1/chat/completions;c1b② 已改取常量", "switch"),
    ("writing/llm_providers.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "该条目 api_url = dashscope.aliyuncs.com/compatible-mode,百炼侧 ID", "keep"),
    ("writing/llm_providers.py", "deepseek-v4-pro", 1): ("emit", "dashscope", "a", "该条目 api_url 是百炼兼容模式(v4-pro 档)", "keep"),
    ("writing/llm_providers.py", "deepseek-v4-flash", 2): ("emit", "dashscope", "a", "v3.2 键但实际指向百炼 v4-flash(2026-05-22 老板拍板自动升级)", "keep"),
    ("writing/llm_providers.py", "deepseek-ai/DeepSeek-V3", 1): ("emit", "openrouter", "a", "SiliconFlow 的模型路径 deepseek-ai/DeepSeek-V3,第三家的 ID", "keep"),
    ("services/article_ai_review.py", "deepseek-flash", 1): ("emit", "official", "a", "DEEPSEEK_BASE_URL = api.deepseek.com;c1e 把**发出名**与**幂等键身份串**拆开后改取常量(身份串逐字节冻住,见该文件注释)", "switch"),
    ("services/writing_style_reviewer.py", "deepseek-flash", 1): ("emit", "official", "a", "评审A 显式 provider=deepseek ⇒ 走官方线;原默认 deepseek-v3.2 在官方线返 400(Deploy 206-d2)⇒ 评审A 恒失败、恒单评审、replace 从没通过;c1b② 已改取常量", "switch"),
    ("config/settings_manager.py", "deepseek-flash", 1): ("emit", "official", "a", "geo_scoring 的档位;c1b② 已按 Owner 09-14「全部改成 deepseek-flash」 改取常量(原 deepseek-reasoner,官方线实测静默回 flash)", "switch"),
    ("config/settings_manager.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "🔴 cleaner_options 与 cleaner_model='qwen-turbo' 同块,KB 清洗走百炼兼容模式 —— 这是**百炼侧**的名字,被文件级 blanket 裹进来过", "keep"),
    ("config/model_config.py", "deepseek-chat", 1): ("registry_key", "official", "a", "DEEPSEEK_CONFIG['models'] 的键;调用方按这个键取档", "keep"),
    ("config/model_config.py", "deepseek-reasoner", 1): ("registry_key", "official", "a", "DEEPSEEK_CONFIG['models'] 的键(原 reasoner 档)", "keep"),
    ("config/model_config.py", "deepseek/deepseek-r1", 1): ("registry_key", "openrouter", "a", "OpenRouter 的模型路径(deepseek/deepseek-r1),另一家的 ID", "keep"),
    ("config/model_config.py", "deepseek-flash", 1): ("emit", "official", "a", "GEO_MODEL_ROUTING 的路由项,元组第一位是发出去的模型名;🔴 它今天**零调用方**(get_model_for_task 全仓没人调),c1c 仍改它,是为了接线那天不会捡起一个死名字", "switch"),
    ("config/model_config.py", "deepseek-flash", 2): ("emit", "official", "a", "🔴 c1a 判它 dashscope 是**错的**:该条目 model_type=openai_chat 且 base_url 取 DEEPSEEK_CONFIG(=api.deepseek.com/v1),是官方线发出点;c1c 批次改", "switch"),
    ("config/model_config.py", "deepseek-flash", 3): ("emit", "official", "a", "该条目 base_url 取 DEEPSEEK_CONFIG(官方线);c1b② 已按 Owner 09-14「全部改成 deepseek-flash」 改取常量", "switch"),
    ("employees/base_employee.py", "deepseek-flash", 1): ("emit", "official", "a", "该档 base_url = api.deepseek.com/v1", "switch"),
    ("employees/base_employee.py", "deepseek-reasoner", 1): ("doc", "official", "a", "c1b② 注释行:说明为什么只改 model_name 不改键", "keep"),
    ("employees/base_employee.py", "deepseek-reasoner", 2): ("registry_key", "official", "a", "MODEL_CONFIGS 的**键**(原 reasoner 档),调用方按它取档 —— 键不改,改的是它发出去的 model_name", "keep"),
    ("employees/base_employee.py", "deepseek-flash", 2): ("emit", "official", "a", "该档 base_url = api.deepseek.com/v1;c1b② 已按 Owner 09-14「全部改成 deepseek-flash」 改取常量", "switch"),
    ("employees/base_employee.py", "deepseek-v4-flash", 1): ("registry_key", "dashscope", "a", "MODEL_CONFIGS 的键(百炼 v4-flash 档)", "keep"),
    ("employees/base_employee.py", "deepseek-v4-flash", 2): ("emit", "dashscope", "a", "该档 base_url = dashscope.aliyuncs.com/compatible-mode/v1", "keep"),
    ("employees/base_employee.py", "deepseek-v4-pro", 1): ("registry_key", "dashscope", "a", "MODEL_CONFIGS 的键(百炼 v4-pro 档)", "keep"),
    ("employees/base_employee.py", "deepseek-v4-pro", 2): ("emit", "dashscope", "a", "该档 base_url = dashscope.aliyuncs.com/compatible-mode/v1(v4-pro)", "keep"),
    ("employees/base_employee.py", "deepseek-v3.2", 1): ("registry_key", "dashscope", "a", "MODEL_CONFIGS 的键(历史 v3.2 档)", "keep"),
    ("employees/base_employee.py", "deepseek-v4-flash", 3): ("emit", "dashscope", "a", "该档 base_url = dashscope.aliyuncs.com/compatible-mode/v1", "keep"),

    ("server.py", "deepseek-flash", 1): ("emit", "official", "a", "同段 POST api.deepseek.com/v1/chat/completions", "switch"),


    ("config/deepseek_models.py", "deepseek-flash", 1): ("emit", "official", "a", "官方线新名的**定义处**:所有 switch 行最终发出去的就是这个值,改它等于一次性改掉全部发出点", "keep"),
    ("config/deepseek_models.py", "deepseek-v4-pro", 1): ("emit", "official", "a", "官方线 pro 档名的定义处(名字本来就不改,留着是为了让两档都有唯一出处)", "keep"),

    # 🔴 [Review 09-14 · Deploy 206-d2 实打] 官方 /models 现在只剩 deepseek-flash 与
    #    deepseek-v4-pro;`deepseek-reasoner` 仍返 200,但**回显 deepseek-flash** ——
    #    也就是说这四处「要推理档」的发出点今天已经在 flash 上**静默降级**。
    #    这不是改名问题,是**换档**问题:要 v4-pro 真推理档,还是认账改 flash,
    #    是 Owner 的决定,不许被一条文件级 blanket 顺手决定。四处统一标 later。

    # 🔴 [Review 09-14 改口径] Deploy 206-d2 实打:`deepseek-v3.2` 在官方线上现在**返 400**。
    #    也就是说评审A 不是"将来会坏",是**现在就是坏的**(7 月它成功过)⇒ P1 故障。
    #    c1b② 直接改成官方线常量,并配一条判据钉「双评审都活着」。

    ("services/llm/deepseek_key_pool.py", "deepseek-flash", 1): ("doc", "official", "a", "注释行:解释上面那条子串匹配为什么要认新名,不是代码", "keep"),



    ("tools/ai_visibility/ai_tester.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "b", "platform='dashscope';且属**被监测引擎**,范围 b 不动", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-v4-flash", 2): ("label", "dashscope", "b", "metadata engine=dashscope_deepseek,范围 b", "keep"),
    #: 🔴 [WO_221-c1'] occ **位移**:老 occ3(`DEEPSEEK_OFFICIAL_MODEL`,官方线)
    #:   已改发常量 ⇒ 它从 deepseek-v4-flash 这一族里**离开**,后面两条各前移一位。
    #:   occ 是「该文件内该模型名按行序的第几次出现」—— **同族里删掉一条,
    #:   后面每一条的身份号都会变**。只删不移位的话,判词会对错行。
    ("tools/ai_visibility/ai_tester.py", "deepseek-v4-flash", 3): ("emit", "dashscope", "b", "platform='dashscope',范围 b(原 occ4)", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-v4-flash", 4): ("label", "dashscope", "b", "metadata engine=dashscope_deepseek_compatible,且属被监测引擎(原 occ5)", "keep"),
    #: 官方线那一条改名后进了 deepseek-flash 族,见下方 WO_221-c1' 新增段。
    ("tools/ai_visibility/ai_tester.py", "deepseek-chat", 1): ("emit", "official", "b", "query_deepseek_official —— 把 DeepSeek 当搜索引擎查,范围 b", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-chat", 2): ("emit", "official", "b", "query_deepseek_official 请求体同一处,范围 b 不动", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-chat", 3): ("emit", "official", "b", "第二处把 DeepSeek 当搜索引擎查的调用,范围 b 不动", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-chat", 4): ("emit", "official", "b", "同一处调用的请求体,范围 b 不动", "keep"),

    ("tools/multi_llm_caller.py", "deepseek-flash", 1): ("emit", "official", "a", "该条目 url = api.deepseek.com/v1/chat/completions", "switch"),
    ("tools/multi_llm_caller.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "该条目 url = dashscope.aliyuncs.com/compatible-mode", "keep"),

    ("tools/llm_call_tracker.py", "deepseek-v4", 1): ("compare", "n/a", "a", "`model.startswith('deepseek-v4')` 是**分档判断**不是计价行;新名不带 v4 前缀,c1d 要连它一起翻", "later"),

    ("workflows/diagnosis_workflow.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "同段 base_url = dashscope.aliyuncs.com/compatible-mode/v1", "keep"),
    ("workflows/diagnosis_workflow.py", "deepseek-flash", 1): ("emit", "official", "a", "call_llm_inline 走官方 key 池 failover", "switch"),

    ("services/flywheel_judgment.py", "deepseek-flash", 1): ("emit", "official", "a", "元组第一位是 'deepseek'(官方直连);**下一行紧挨着就是百炼的同名行**,只能改这一行", "switch"),
    ("services/flywheel_judgment.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "a", "元组第一位是 'dashscope' —— 同模型换通道的回落档,百炼侧 ID,一个字不动", "keep"),

    ("services/placement_service.py", "DeepSeek/v4-flash", 1): ("doc", "official", "a", "c1d 注释行:提醒下一行那个带斜杠的是展示标签不是模型 ID", "keep"),
    ("services/placement_service.py", "DeepSeek/v4-flash", 2): ("label", "official", "a", "'DeepSeek/v4-flash' 是这条 provider 记录的**展示标签**(带斜杠,不是模型 ID)", "later"),
    ("services/placement_service.py", "deepseek-flash", 1): ("emit", "official", "a", "同一个元组里紧跟 https://api.deepseek.com/v1/chat/completions + deepseek_key,是真发出的模型名", "switch"),

    #: 🔴 [WO_221-c1'] 同样的 occ 位移:老 occ1(官方通道的模型兜底)已改常量,
    #:   于是老 occ2(百炼侧 `model_deepseek_via_dashscope`)前移成 occ1。
    ("services/research_monitor/platforms.py", "deepseek-v4-flash", 1): ("emit", "dashscope", "b", "配置键 model_deepseek_via_dashscope,百炼侧被测表面(原 occ2)—— 🔴 这一条**刻意不归一**:百炼上该 ID 是另一家的活模型", "keep"),
    #: ══════════════════════════════════════════════════════════════
    #: 🔴 [WO_220-c1 / WO_221-c1'] 两单把官方线上的名字换成了常量,
    #:   于是本表多出这六处 `deepseek-flash`。逐条写依据,不写「同上」。
    #: ══════════════════════════════════════════════════════════════
    ("config/vision_routing.py", "deepseek-flash", 1): ("compare", "official", "a", "WO_220:`DEEPSEEK_VISION_CAPABLE = frozenset({DEEPSEEK_OFFICIAL_FLASH})` —— 官方线上**只有 flash 能识图**(探针实测 v4-pro 返 200 但答「无法查看该图片」)。它比名字,不发名字", "keep"),
    ("config/vision_routing.py", "deepseek-flash", 2): ("emit", "official", "a", "WO_220:`DEFAULT_VISION_MODEL` —— GEO 视觉线的兜底默认,已是常量", "keep"),
    ("db/social_preferences_db.py", "deepseek-flash", 1): ("emit", "official", "a", "WO_220:`geo_vision_model` 的**播种默认值**。共享键 `vision_model` 有四个消费方(两个仍只打百炼),所以 GEO 线另起了这个键;这一行是 ensure_schema 写进库的那个值", "keep"),
    ("services/research_monitor/platforms.py", "deepseek-flash", 1): ("emit", "official", "b", "WO_221:官方通道模型的**兜底默认**(`_load_model_from_config` 取不到配置时)。属被监测引擎,范围 b —— 但名字必须是真的:官方页旧名已退役", "keep"),
    ("services/research_monitor/platforms.py", "deepseek-flash", 2): ("emit", "official", "b", "WO_221:同一段 `normalize_deepseek_model(...) or DEEPSEEK_OFFICIAL_FLASH` 的 or 兜底。🔴 归一是这条线的**真防线** —— Anthropic 端点请求旧名会原样回显,回显锁在那儿是同义反复", "keep"),
    ("tools/ai_visibility/ai_tester.py", "deepseek-flash", 1): ("emit", "official", "b", "WO_221:`DEEPSEEK_OFFICIAL_MODEL`(走 /anthropic/v1/messages)。原为退役名 deepseek-v4-flash,即老 occ3", "keep"),



    ("services/knowledge_pipeline.py", "deepseek-v4-flash", 1): ("doc", "dashscope", "a", "注释行:告诉 Deploy 要同步改 settings.json,不是代码", "keep"),




    # 🔴 这两条是**记账标签**不是发出去的模型名:`verify_selected_spans(model=...)`
    #    只把名字记进核验结果,真实 provider 由调用方的 provider_name 派生。
    #    硬编码一个名字本身就是缺陷(记的不一定是真跑的那个),但改它属于另一件事,
    #    本单只标注、不动 —— 动了会让「记的是什么」这件事在没有判据的情况下变。
    ("writing/evidence_verifier.py", "deepseek-v4-flash", 1): ("label", "n/a", "a", "记账标签,provider 由调用方派生;硬编码是既有缺陷,另立单", "later"),
    ("writing/evidence_verifier.py", "deepseek-v4-flash", 2): ("label", "n/a", "a", "重试路径的记账标签,provider 同样由调用方派生", "later"),
}



#: 🔴 [Review 09-14] **blanket 的冻结字面量数**。
#:   文件级判决是个抽屉:分母一变大,新冒出来的字面量会被它**自动**裹进去,
#:   于是「每一条都已分类」那条锁照样绿 —— Review 的 P5 实测就是这么过去的。
#:   所以每个 blanket 都记下它当初盖住了几条;超出这个数 ⇒ 那一条按**未分类**算,
#:   必须有人去读一眼再把数字调上来。加行要人点头,这正是 blanket 缺的那一步。
#:   (字面量换成常量引用时总数不变 —— census 两样都数,见它的 _CONST_REF。)
FILE_LITERAL_COUNT = {
    "agents/chief_editor_agent.py": 1,
    "agents/provider_config.py": 1,
    "agents/report_enhancement_agent.py": 2,
    "agents/review_agent.py": 2,
    "agents/source_relevance_agent.py": 2,
    "api/employee_api.py": 1,
    "api/monitoring_api.py": 2,
    "api/research_monitor_config_api.py": 1,
    "api/xiaobang_api.py": 1,
    "config/deepseek_models.py": 6,   # 09-14 别名集并入 deepseek-reasoner(Owner 拍板),+1
    "config/settings_manager.py": 5,
    "db/diagnosis_db.py": 12,
    #: [WO_221-c1' ⑦] 该文件已无模型名字面量,见上方 FILE_VERDICT 处说明
    "employees/content/content_writer.py": 1,
    "employees/diagnosis/ai_tester.py": 1,
    "employees/diagnosis/competitor_analyst.py": 1,
    "employees/diagnosis/data_collector.py": 1,
    "employees/diagnosis/report_writer.py": 1,
    "employees/dynamic_employee.py": 1,
    "employees/employee_registry.py": 1,
    "employees/support/chief_editor.py": 1,
    "employees/support/ppt_specialist.py": 1,
    "services/ai_ops/chat_intent.py": 1,
    "services/ai_surface_monitoring/lineage.py": 4,
    "services/domain_authority_ai.py": 1,
    "services/engine_contract.py": 1,
    "services/geo_douyin/content_generator.py": 1,
    "services/geo_observation/entity_review.py": 1,
    "services/geo_observation/promotion.py": 1,
    "services/geo_observation/source_hooks.py": 1,
    "services/geo_observation_analytics/contract.py": 1,
    "services/knowledge_pipeline.py": 3,
    "services/marketing/advisor_llm.py": 1,
    "services/marketing/patrol.py": 1,
    "services/media_domain_directory.py": 1,
    "services/research_monitor/article_intent_classifier.py": 2,
    "services/research_monitor/industry_resolver.py": 2,
    "services/research_monitor/query_intent_classifier.py": 2,
    "tools/agent_loop/model_config.py": 3,
    "tools/article_generator.py": 3,
    "tools/batch_pricing_llm.py": 1,
    "tools/cluster_gatekeeper.py": 2,
    "tools/competitor/competitor_identifier.py": 2,
    "tools/industry_baseline_dynamic.py": 1,
    "tools/industry_knowledge_collector.py": 2,
    "tools/keyword/longtail_matrix.py": 1,
    "tools/keyword_cluster.py": 6,
    "tools/keyword_expander.py": 2,
    "tools/keyword_generator.py": 2,
    # c1p 后实测 19(原 10):+8 是 OFF_PEAK_PRICING 的空闲档键,+1 是空闲档里
    # 新增的 ("deepseek_official","deepseek-flash") 行。逐条看过:全是**计价表的键**,
    # 与该文件 blanket 的 compare/keep 判决一致,没有新角色冒出来。
    "tools/llm_call_tracker.py": 19,
    "tools/llm_keyword_expander.py": 1,
    "tools/llm_pricing_scorer.py": 1,
    "tools/monitoring/batch_monitor.py": 2,
    "tools/pricing_llm_assessor.py": 1,
    "tools/scoring/llm_geo_scorer.py": 1,
    "tools/sentiment_classifier.py": 2,
    "services/llm/deepseek_key_pool.py": 5,
    # [WO_259 2026-09-22] 跟着上提进来的第二个文件。3 条 = flash 档路由那两处
    # (一个 provider 判断 + 一个模型默认值)。数字是**实测**不是估的。
    "services/llm/advisor_llm.py": 3,
}

#: 路径级的**机械**规则。只按路径判,不读代码 —— 路径是语法事实不是语义判断,
#: 所以这里允许自动化;供货线那种要读懂上下文的,一律人读(见文件头)。
_NOT_PRODUCTION = ("tests/", "scripts/")
#: [开源 E3 · B2] 社媒工具包目录随包整删,从分域表去掉(再点名已删路径不分出任何东西)
_SOCIAL = ("api/social_", "api/advisor_api.py", "advisors/")


def verdict_for(row):
    """返回 `(kind, provider, scope, reason, plan)`;表里没有 ⇒ None。

    入参是 **census 的一行**(dict),不是 `(path, line)`。

    🔴 [0913e 集成] 这里原来按 `(path, line)` 查表,于是**任何一次合车**
       ——别人在同一个文件靠前的位置加了几行——都会让这张表整体对不上:
       2026-09-15 合成树上 `server.py` 与 `services/placement_service.py`
       各漂了一处,dead rows / in-sync / 冻结表共 7 红。那是**仪器脆**,
       不是产品出事;而一张"为无关原因转红"的表,下一个人只会把它放宽,
       放宽之后它就再也不会为真原因转红了。
       现在按 `(path, model, occ)` 查:`occ` 是该 (文件, 名字) 在**该文件内**
       按行序的第几次出现。行整体平移时它不动;而这一行的**名字**被改了,
       它照样对不上 —— 后者正是这张表要抓的东西。
    🔴 签名故意**不收 line**:收了就总有人会拿它当键,而位置不是身份。
    """
    p = str(row["path"]).replace("\\", "/")
    k = (p, row["model"], int(row["occ"]))
    if k in SITE_VERDICT:
        return SITE_VERDICT[k]
    if p in FILE_VERDICT:
        return FILE_VERDICT[p]
    if any(p.startswith(x) or ("/" + x) in p for x in _NOT_PRODUCTION):
        return ("not_production", "n/a", "x",
                "路径在 tests/ 或 scripts/ —— 不是生产代码(按路径判:路径是语法事实)", "keep")
    if any(x in p for x in _SOCIAL):
        return ("unread", "n/a", "c",
                "路径在社媒 / IP 线,板块边界(元指令 12),本窗不碰", "keep")
    return None
