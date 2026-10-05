# -*- coding: utf-8 -*-
"""WO_217-c1a · 人读签字表。

普查器(`census.py`)只回答「这里出现了一个像模型名的字面量」。
**它是不是发出点、该不该切**,由这里逐条签字 —— 一句签了字的话,
而不是「锚没覆盖」造成的沉默。
"""

# ══════════════════════════════════════════════════════════════════
# 冻结读数(树尖 fd6e46713 · 2026-09-15)
#   两侧都锁:多了说明有新发出点没分类,少了说明有文件被删/改名。
#   两种都必须红 —— 只锁一侧的表会在"变少"时静静变绿。
# ══════════════════════════════════════════════════════════════════
#: 🔴 [WO_220-c1 2026-09-15] 重冻。差账**逐行对得上**,不是「红了就改数字」:
#:   生产侧 −3:`tools/vision/image_describe.py` 3 处 +
#:   `services/geo_douyin/ocr_qa.py` 3 处(两份逐字相同的 `_vision_model`)移出,
#:   新的单点解析器 `config/vision_routing.py` 3 处移入 ⇒ 470-6+3 = 467。
#:   全仓 +9:本单的判据包与注毒台(tests/vision_deepseek_flash_2026_09_15/ +
#:   scripts/poison_220_c1.py)里有模型名字面量;它们是**别的包**的仪器,
#:   不进 `_INSTRUMENT_FILES`(那只排除本普查器自己)。
#:   🔴 这次是**先 `git add` 再量**的 —— c1a 那次正是提交前量、提交后就不对。
#:   九次重冻:全仓 +1 = 补的成本身份判据里的 `deepseek-flash` 字面量(在 tests/);
#:   生产四项 583/466/57/60 逐字不变。
#:   八次重冻(WO_221-c1' ⑦):生产 -1 —— `db/monitoring_db.py` 2->1,
#:   成本身份改成从 `PLATFORM_CONTRACT['deepseek']` 取(与同文件 qwen 那格同法),
#:   本文件不再自己写模型名。全仓 1154->1152 同源。
#:   七次重冻(WO_221-c1' ⑥):全仓 1146->1154(+8)= 206 判据包(deepseek_official_line)
#:   的签字表新增 6 条 deepseek-flash 分类 + 两张冻结表重写;**全在 tests/**,
#:   生产四项 584/467/57/60 与文件层逐字不变。
#:   六次重冻(WO_221-c1'):生产 +1 —— `platforms.py` 12->13,归一后
#:   `DEEPSEEK_OFFICIAL_FLASH` 在同一段里多出一处引用(兜底值 + `or` 兜底)。
#:   monitored_engine 56->57 同源。全仓 1132->1146 = 新判据文件与新增 5 发毒。
#:   五次重冻(WO_221-c1 判据包):全仓 1106 -> 1132(+26)= 新判据包与注毒台里的
#:   模型名字面量,**全在 tests/ 与 scripts/**;生产四项 583/467/56/60 与文件层逐字不变。
#:   🔴 四次重冻(WO_221-c1)—— 这次**不是内容变了,是仪器修了**:
#:   普查器的 `_is_import` 按行首判断,看不见**多行 import 的续行**,
#:   于是 `from config.deepseek_models import (` 换行后的 `DEEPSEEK_OFFICIAL_FLASH,`
#:   被当成常量**引用**数进分母。改用 AST 认 import 行之后:
#:     · config/vision_routing.py 3 -> 2(它的多行 import 一直被多数一处)
#:     · tools/ai_visibility/ai_tester.py 本单新加的多行 import 不再虚增(仍 17)
#:   ⇒ 之前冻的 1111/584/468 本身就偏高。新值 1106/583/467,monitored_engine 56 不变
#:   (五处退役名换成常量引用,C 腿照数,总数不动 —— C 腿正是为这一刻设计的)。
#:   抓住它的是两侧冻结:**冻结把仪器的缺陷变成了红**。
#:   三次重冻(c1' 补锁):全仓 +14 = 新增 5 面判据与 6 发毒的条目,**全在 tests/ 与
#:   scripts/**;**生产四项 584/468/56/60 与文件层逐字不变** —— c1' 只动测试与两行注释,
#:   模型名字面量一个没变,对得上。
#:   二次重冻(同单,实证之后):+1 生产 = `db/social_preferences_db.py` 新增
#:   `geo_vision_model` 默认值;全仓 +3 = 新增的两条判据与注毒 N8。
#: 🔴 [WO_259 · 开源 E0 2026-09-22] `geo_work` **466 → 476**、
#:    `social_owner_excluded` **60 → 50**,而 `total_prod` 一个没变(583)。
#:    原因是**搬家不是增删**:`deepseek_key_pool.py` 与 `advisor_llm.py`
#:    从 `tools/social_operator/`(已随开源 E3 B2 删) 上提到 `services/llm/`(E3 要整包删社媒,
#:    而 GEO 主链 33 个在役文件引着这两个模块)⇒ 同样 10 条发出点从
#:    「社媒 owner」桶挪进「geo work」桶。两个桶此消彼长、总数守恒,
#:    正是这条判据该看见的那种变化。
#: 🔴 `total_all` **1155 → 1157**(+2,只在 `tests/` 侧):
#:    上提之后 `advisor_llm.py` 不再被路径规则判成 unread,它那两个名字要
#:    **进另一个包的冻结表**(`deepseek_official_line_2026_09_14` 的
#:    FROZEN_OFFICIAL_KEEP),于是 tests/ 里多了两处字面量。
#:    生产侧 `total_prod` 一个没变(583)—— 这两条是判据自己写的字,不是代码。
#: 🔴 [E0c · 开源 E3 前置 2026-09-27] `geo_work` **476 → 480**、`social_owner_excluded` **50 → 46**,
#:    `total_all` / `total_prod` 一个没变(1157 / 583)。原因同 WO_259:**搬家不是增删** ——
#:    小帮识图依赖的 `_vision_image_to_text`(含 4 处视觉模型默认 / 兜底名)从
#:    社媒主路径 router(社媒桶,已随 E3 B1b-2b 整删)上提到 `services/image_to_text.py`(GEO 桶),
#:    同样 4 条从一个桶挪进另一个桶。视觉属「阿里特有能力」,按 Owner 09-14 口径不切 flash。
#: 🔴 [开源 E3 · B1b-2b · 2026-09-28] 这回是**删**,不是搬:社媒主路径 router 整删 ⇒ 它剩下的 2 条(社媒桶)消失,
#:    `total_prod` 583 → 581、`social_owner_excluded` 46 → 44;测试侧 `test_api_cost_alignment` 退役采访 ASR 那一格
#:    少 1 条 ⇒ `total_all` 1157 → 1154(= −2 生产 −1 测试)。`geo_work` / `monitored_engine` 一个没变。
#:    逐文件差分(基线 84d9368b4 vs 本片):只有 `api/social_mainpath_api.py` 2→0 与 `tests/test_api_cost_alignment.py` 35→34。
#: 🔴 [开源 E3 · B2 · 2026-09-28] B3a / B3b / B4 删端点时本表没跟(9ae1c8ac7 上已是红,两臂同红被藏住),本片一并对齐:
#:    E3 前 308330514 实测 (1157, 583, 480, 57, 46) → 待发 (1053, 523, 466, 57, 0)。逐文件差分(b2_census_attrib):
#:    生产侧差异全在 E3 动过的文件 —— 社媒工具包 20 个文件全 0、advisor_api 5→0、content_api 3→0、社媒主路径 router 2→0、
#:    server.py 30→24(B3a 删内联端点)、config/model_config.py 60→59(face_analysis 路由条目随社媒包删)、
#:    db/topic_similarity 与 meeting/meeting_room 各 1→0(E3 孤儿);社媒桶(social_owner_excluded)随之清零。
#:    E3 没动却有差异的文件 0 个。
FROZEN_TOTALS = {
    "total_all": 1053,          # 含 scripts/ tests/
    "total_prod": 523,
    "geo_work": 466,
    "monitored_engine": 57,
    "social_owner_excluded": 0,
}

#: 生产代码里出现模型名的文件数(FILE_LITERAL_COUNT 的键数)
FROZEN_PROD_FILES = 114  # E0c +1:services/image_to_text.py;B1b-2b −1:社媒主路径 router 整删;E3 B2 −22:社媒工具包 18 个 + 两个孤儿 + advisor_api / content_api 不再出现模型名


# ══════════════════════════════════════════════════════════════════
# K 腿(校准)签字:前缀并集认不出、但名字里含厂商字样的 19 条。
#
# 🔴 这张表的存在理由:L 腿的锚**一定会漏**。本单实测漏了两次 ——
#    第一次漏掉整个 qwen3.x 族(整单的主角),第二次漏掉 siliconflow。
#    两次都是 K 腿喊出来的。凡 K 腿列出而 L 腿不认的,
#    要么并入分母(下面标 IS_MODEL),要么在这里写明它不是模型名。
# ══════════════════════════════════════════════════════════════════
CALIBRATION_SIGNED = {
    # ── 真的是模型指定 ⇒ 已并入分母(census 的 M 腿) ──
    "dashscope-deepseek-v4-pro": (
        "IS_MODEL", "server.py:15232 `model_key: str = ...` —— 写文章端点的默认档,"
                    "索引进 writing/llm_providers.LLM_PROVIDERS。c1b 要连这套 key 一起换,"
                    "否则改了 model_id 而 caller 还按旧 key 取配置。"),
    "dashscope-qwen-max": (
        "IS_MODEL", "writing/llm_providers.py:170 `DEFAULT_MODEL` —— 注册表自己那个文件里的"
                    "真发出点(内部 model_id 是 qwen3.7-max)。"),

    # ── 路由 / 通道 key:决定走哪条链,不是发出去的模型名 ──
    "dashscope_deepseek": ("ROUTE_KEY", "tools/ai_visibility/ai_tester.py:1130 通道名"),
    "dashscope_deepseek_compatible": ("ROUTE_KEY", "ai_tester.py:1675 通道名"),
    "deepseek_official": ("ROUTE_KEY", "ai_tester.py:1466 通道名"),
    "doubao_responses": ("ROUTE_KEY", "ai_tester.py:929 通道名"),
    "doubao_app": ("ROUTE_KEY", "豆包 ai_search 服务形态的通道名"),
    "kimi_search": ("ROUTE_KEY", "ai_tester.py:606 通道名"),
    "deepseek": ("PROVIDER_NAME", "employees/project_director.py:159 provider 名"),
    "doubao": ("PROVIDER_NAME", "services/quote_media_mix.py:55 provider 名"),

    # ── 配置键名:指向"配的是哪个模型",本身不是模型名 ──
    "model_deepseek_via_dashscope": ("CONFIG_KEY", "lineage.py:162 settings 键名"),
    "model_doubao_app": ("CONFIG_KEY", "lineage.py:77 settings 键名"),
    "model_kimi_via_dashscope": ("CONFIG_KEY", "lineage.py:243 settings 键名"),
    "model_qwen_default": ("CONFIG_KEY", "lineage.py:90 settings 键名"),

    # ── 人读标签 / 环境变量名 / 版本 tag ──
    "DeepSeek": ("HUMAN_LABEL", "ai_visibility_agent.py:505 报表里给人看的引擎名"),
    "Kimi": ("HUMAN_LABEL", "ai_visibility_agent.py:506 同上"),
    "Doubao": ("HUMAN_LABEL", "ai_visibility_agent.py:507 同上"),
    "DOUBAO_ENDPOINT_ID": ("ENV_VAR_NAME", "employees/base_employee.py:91 环境变量名"),
    "v17-qwen37max-1": ("VERSION_TAG", "placement_service.py:3354 一次实验的版本 tag,不是模型名"),
}

#: 上面判为 IS_MODEL 的,必须真的能在 census 里查到(否则这张签字表本身过期了)
CALIBRATION_MUST_APPEAR = [k for k, v in CALIBRATION_SIGNED.items() if v[0] == "IS_MODEL"]


# ══════════════════════════════════════════════════════════════════
# 必要例外表 —— 逐条理由,不许「其它」。
#   判据只锁一件事:**理由这一栏不能空**,且每个模型在树上仍有出现处。
#   「DeepSeek 顶不了」是能力事实,不是口味。
# ══════════════════════════════════════════════════════════════════
CAPABILITY_EXCEPTIONS = {
    "qwen3-asr-flash":            "语音识别 —— DeepSeek 官方线没有 ASR 端点,输入是音频不是文本",
    "qwen3-asr-flash-filetrans":  "长音频异步文件转写 —— DeepSeek 无对应服务形态",
    "qwen-audio-asr":             "语音识别(计价表行)—— 同上",
    "text-embedding-v4":          "向量化 —— DeepSeek 官方线不提供 embedding;换掉会让已有向量库维度不兼容",
    #: 🔴 [c1a' 复审纠正] 原写「输入含图像」是**错的** —— 我按模型名里的 `vl`
    #:   推了用途,没读代码路径。knowledge_pipeline.py:59 打的是
    #:   `.../services/rerank/text-rerank/text-rerank`,是**文本重排**。
    #:   真理由是端点形态不同(query+documents→scores),不是 chat completions。
    #:   本仓 name-match-is-not-attribution-follow-the-code-path。
    "qwen3-vl-rerank":            "文本重排 —— DeepSeek 官方线没有 rerank 端点(query+documents→scores,与 chat completions 不同形态)",
    #: 🔴 [c1a' 复审纠正] `deepseek-v4-flash-vision-exp` **已退役**
    #:   (官方页,由最新 Flash 承接),且 Review 探针实测 deepseek-flash 能识图。
    #:   ⇒ 它不是「例外」,是**待切换**项,归 WO_220-c1(→ DEEPSEEK_OFFICIAL_FLASH)。
    #:   整个「视觉」这一类因此不再构成必要例外。
    "gpt-image-2":                "图像生成 —— 文本模型不产图",
    "google/gemini-3-pro-image-preview": "图像(计价表行)—— 同上",
    "doubao-ai-search":           "联网搜索 —— 豆包 ai_search 是带检索的服务形态,不是纯生成",
    "doubao-native-search":       "联网搜索 —— 同上",
}


# ══════════════════════════════════════════════════════════════════
# [c1a' 2026-09-15] 从例外表**挪走**的行 —— 留登记,不许悄悄消失。
#   例外表变短有两种可能:①真的不再必要(好事)②被谁顺手删了(坏事),
#   而两者在表上长得一模一样。所以挪走必须写明**去哪了**。
# ══════════════════════════════════════════════════════════════════
MOVED_OUT_OF_EXCEPTIONS = {
    "deepseek-v4-flash-vision-exp":
        "已退役(官方页:由最新 Flash 承接);Review 探针实测 deepseek-flash 能识图 "
        "⇒ 不是例外是待切换,归 WO_220-c1 改成 DEEPSEEK_OFFICIAL_FLASH",
}


# ══════════════════════════════════════════════════════════════════
# 🔴 独立缺陷登记(不属于 217 的切换范围,但必须留痕)
#   官方页:`deepseek-v4-flash` 已退役。206-d2 实打:旧名仍返 200,回显 `deepseek-flash`。
#   监测线这三处发出的是已退役名 ⇒ 实际被测的是 flash,而我们记成 v4-flash。
#   对监测来说**模型身份就是被测量本身**,所以这是数据正确性缺陷,不是命名问题。
#   判据只钉「这三处还在」—— 哪天有人改了,这条登记会红,提醒去更新结论。
# ══════════════════════════════════════════════════════════════════
#: 🔴 [WO_221-c1 翻面] 原来这里登记「这三处还在」并有判据钉住;现在已修,
#:   所以改成钉**官方线零处**。翻面时顺带发现工单点的三处**有两处判错**:
#:     · 工单点的 `lineage.py:191` 是 `deepseek_metaso_proxy`(provider=metaso,
#:       env=METASO_API_KEY,availability=unavailable)—— 不是 DeepSeek 官方直连;
#:     · 工单**漏了** `lineage.py:175`(`deepseek_native_with_search`,
#:       provider=deepseek_official,availability=active,default_enabled=True)
#:       —— 真正在污染监测数据的就是这一面;
#:     · 也漏了 `batch_monitor.py:154` —— 那一格是**写进观测账本的血缘标签**本身。
#:   逐处取证靠的是「同处最近的 key 环境变量名 / base URL」,不是 census 的
#:   `gateway` 启发式 —— 那个字段自己的文档写着「只是提示」,而它把 :175 判成了 dashscope。
#: 🔴 百炼那 13 处与秘塔 1 处**一个字不动**:百炼上 `deepseek-v4-flash` 是
#:   另一家的**活**模型 ID,DeepSeek 官方改名不改百炼(206 早写过这一刀)。
OFFICIAL_LINE_RETIRED_NAME_SITES_MUST_BE_ZERO = True

#: 改完之后官方线上仍允许出现退役名的地方:一处都没有。
#: 这个集合留空**并且被判据钉住为空** —— 空集合与「判据没跑」在读数上同形,
#: 所以判据同时断言「百炼侧仍有 13 处」,证明它真的在数。
RETIRED_NAME_ON_MONITORING = []

#: 百炼线上合法存在的退役同名(**不是**退役 —— 那是另一家的活 ID)。
#: 数目冻结,少了说明有人顺手改了不该改的。
FROZEN_DASHSCOPE_SAME_NAME_SITES = 13


# ══════════════════════════════════════════════════════════════════
# 死配置登记:切它不改变任何运行时行为。
#   不是"不该切",是**不能拿它当"已完成切换"的证据**。
# ══════════════════════════════════════════════════════════════════
DEAD_CONFIG = {
    "config/model_config.py::GEO_MODEL_ROUTING":
        "只被 get_model_for_task 读,而该函数全仓零调用点"
        "(4 处引用:3 处在本文件内,第 4 处 services/marketing/advisor_llm.py:22 是注释)",
    "config/model_config.py::get_agentscope_model_configs":
        "全仓零调用点",
}


# ==================================================================
# FILE_LITERAL_COUNT —— 每个生产文件里出现多少处模型名(两侧都锁)。
#   多了:出现了没分类的新发出点 ⇒ 去读那几行,判清是 emit 还是别的,再调上来。
#   少了:文件被删/改名/那一行被改掉 ⇒ 结论可能已过期。
#   🔴 只锁一侧的表会在「变少」时静静变绿 —— 而「少了」恰恰是切换过程中最常发生的。
# ==================================================================
FILE_LITERAL_COUNT = {
    "advisors/advisor_registry.py": 4,
    "advisors/base_advisor.py": 9,
    "advisors/hybrid_retriever.py": 2,
    "advisors/vectorizer.py": 1,
    "agents/chief_editor_agent.py": 1,
    "agents/diagnostic_agent.py": 1,
    "agents/director_agent.py": 1,
    "agents/geo_writer_agent.py": 1,
    "agents/provider_config.py": 4,
    "agents/report_enhancement_agent.py": 3,
    "agents/review_agent.py": 2,
    "agents/source_relevance_agent.py": 2,
    "api/employee_api.py": 1,
    "api/media_entity_flywheel_api.py": 1,
    "api/monitoring_api.py": 6,
    "api/publish_api.py": 2,
    "api/research_monitor_config_api.py": 4,
    "api/xiaobang_api.py": 1,
    "config/deepseek_models.py": 6,
    "config/model_config.py": 59,  # E3 B2 −1:face_analysis 路由条目随社媒包删
    "config/settings_manager.py": 34,
    "config/vision_routing.py": 2,   # 3->2:多行 import 续行此前被错数(WO_221-c1 修仪器)
    "db/diagnosis_db.py": 24,
    "db/distillation_db.py": 1,
    "db/monitoring_db.py": 1,   # 2->1:成本身份改从 PLATFORM_CONTRACT 取,本文件不再自己写名字
    "db/social_preferences_db.py": 2,   # +1 = geo_vision_model 的默认值(WO_220-c1)
    "employees/base_employee.py": 14,
    "employees/content/content_planner.py": 1,
    "employees/content/content_writer.py": 1,
    "employees/content/douyin_creator.py": 1,
    "employees/content/xhs_creator.py": 1,
    "employees/diagnosis/ai_tester.py": 1,
    "employees/diagnosis/competitor_analyst.py": 1,
    "employees/diagnosis/data_collector.py": 1,
    "employees/diagnosis/report_writer.py": 1,
    "employees/dynamic_employee.py": 1,
    "employees/employee_registry.py": 1,
    "employees/support/chief_editor.py": 1,
    "employees/support/industry_expert.py": 1,
    "employees/support/legal_advisor.py": 1,
    "employees/support/ppt_specialist.py": 1,
    "routes/prompt_template_routes.py": 1,
    "server.py": 24,  # E3 B3a −6:内联端点删
    "services/ai_ops/chat_intent.py": 1,
    "services/ai_ops/glm_triage.py": 1,
    "services/ai_surface_monitoring/lineage.py": 8,
    "services/aliyun_ocr_service.py": 1,
    "services/article_ai_review.py": 1,
    "services/domain_authority_ai.py": 1,
    "services/engine_contract.py": 5,
    "services/flywheel_judgment.py": 3,
    "services/geo_douyin/content_generator.py": 1,
    "services/geo_observation/entity_review.py": 1,
    "services/geo_observation/promotion.py": 1,
    "services/geo_observation/source_hooks.py": 5,
    "services/geo_observation_analytics/contract.py": 1,
    "services/intake_ai.py": 1,
    "services/knowledge_pipeline.py": 5,
    "services/marketing/advisor_llm.py": 1,
    "services/marketing/image_client.py": 2,
    "services/marketing/patrol.py": 1,
    "services/media_domain_directory.py": 1,
    "services/multi_ai_voter.py": 13,
    "services/placement_service.py": 8,
    "services/quote_keyword_sync.py": 3,
    "services/research_monitor/article_intent_classifier.py": 2,
    "services/research_monitor/industry_resolver.py": 2,
    "services/research_monitor/platforms.py": 13,   # +1 = 归一后 DEEPSEEK_OFFICIAL_FLASH 多一处引用(WO_221-c1')
    "services/research_monitor/query_intent_classifier.py": 2,
    "services/research_monitor/round_runner.py": 1,
    "services/writing_style_reviewer.py": 2,
    "tools/agent_loop/model_config.py": 6,
    "tools/ai_visibility/ai_tester.py": 17,
    "tools/article_generator.py": 4,
    "tools/asr/asr_tool.py": 3,
    "tools/asr/asr_tools.py": 4,
    "tools/batch_collect.py": 2,
    "tools/batch_pricing_llm.py": 1,
    "tools/cluster_gatekeeper.py": 2,
    "tools/competitor/competitor_identifier.py": 2,
    "tools/distillation/_a2_create_keyword_insights.py": 1,
    "tools/distillation/distiller.py": 1,
    "tools/geo_managed/review_engine.py": 1,
    "tools/industry_baseline_dynamic.py": 1,
    "tools/industry_case_collector.py": 1,
    "tools/industry_knowledge_collector.py": 2,
    "tools/keyword/longtail_matrix.py": 1,
    "tools/keyword_cluster.py": 6,
    "tools/keyword_expander.py": 2,
    "tools/keyword_generator.py": 7,
    "tools/knowledge_rag.py": 1,
    "tools/llm_call_tracker.py": 55,
    "tools/llm_keyword_expander.py": 1,
    "tools/llm_pricing_scorer.py": 1,
    "tools/monitoring/batch_monitor.py": 6,
    "tools/multi_llm_caller.py": 2,
    "tools/pricing_auditor.py": 2,
    "tools/pricing_llm_assessor.py": 2,
    "tools/quote_generator.py": 2,
    "tools/scoring/llm_geo_scorer.py": 1,
    "tools/sentiment_classifier.py": 2,
    "services/llm/advisor_llm.py": 5,
    "services/image_to_text.py": 4,  # E0c 上提(视觉模型默认 / 兜底名)
    "services/llm/deepseek_key_pool.py": 5,
    "tools/unified_knowledge.py": 2,
    "tools/xiaobang_embed.py": 1,
    "workflows/diagnosis_workflow.py": 3,
    "writing/article_generator_service.py": 1,
    "writing/engine_targeting.py": 1,
    "writing/evidence_verifier.py": 2,
    "writing/llm_providers.py": 23,
    "writing/llm_utils.py": 6,
    "writing/prompt_template_manager.py": 2,
    "writing/style_registry.py": 14,
}
