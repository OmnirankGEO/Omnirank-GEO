"""AI 引擎清单唯一常量源(P0-2 · 2026-07-26)。

生产实证(修复前):
  - 诊断 ``tools/ai_visibility/ai_tester.py`` 2950/3048/3148 三处硬编码
    ``["dashscope", "deepseek", "doubao", "yuanbao"]``;
  - 监测 ``db/monitoring_db.py:28`` ``"dashscope,deepseek,kimi,doubao"``。
  两侧口径不同 → **同一客户在诊断报告和监测面板看到不同的引擎清单和不同答案**。

Owner 裁决(2026-07-26):统一为五引擎
``dashscope / deepseek / doubao / kimi / yuanbao``。成本影响(4→5 引擎)在 EXIT 说明。

本模块是**唯一常量源**:诊断与监测都从这里消费,改这里两侧同步变。
不得在别处再硬编码引擎列表(判别测试 ``tests/test_ai_engine_registry_single_source_2026_07_26.py``
会扫源码,新增硬编码即转红)。

--------------------------------------------------------------------------
为什么监测侧还需要 ``product_version``
--------------------------------------------------------------------------
监测是**已售商品**:``monitoring_product_platform_matrices`` 登记「某个商品版本
= 哪几个平台」,``confirmed_keywords.monitoring_product_version`` 是外键快照,
``client_keywords.platforms`` / ``extra_keywords.platforms`` 持久化**下单当时**
解析出的矩阵。SSOT §16「已完成账本、退款和报价快照不可无痕重写」→ 老词继续按
购买时的 classic4 跑,**不追溯改写**;新词按统一五引擎的新版本跑。
因此这里同时导出新版本号与历史版本号,迁移 additive 只**新增**一行矩阵。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 引擎注册表(canonical key → 对客户展示名)
# ---------------------------------------------------------------------------
# 对客户永远只显示产品名,不显示供应商/模型(feedback_no_supplier_names_to_users)。
ENGINE_LABELS: Final[dict[str, str]] = {
    "dashscope": "通义千问",
    "deepseek": "DeepSeek",
    "doubao": "豆包",
    "kimi": "Kimi",
    "yuanbao": "元宝",
}

# 统一五引擎(Owner 2026-07-26 裁决)· 顺序即报告/监测的稳定展示顺序
UNIFIED_ENGINES: Final[tuple[str, ...]] = (
    "dashscope",
    "deepseek",
    "doubao",
    "kimi",
    "yuanbao",
)

# 诊断侧消费(付费诊断 8 问 × N 引擎)
DIAGNOSIS_ENGINES: Final[tuple[str, ...]] = UNIFIED_ENGINES

# 监测侧消费(新售商品矩阵)
MONITORING_ENGINES: Final[tuple[str, ...]] = UNIFIED_ENGINES
MONITORING_PLATFORMS_CSV: Final[str] = ",".join(MONITORING_ENGINES)

# 监测商品版本:新售 = 统一五引擎;历史 classic4 行保留供老词外键与快照解析。
MONITORING_PRODUCT_VERSION_UNIFIED5: Final[str] = "monitoring-unified5-v1"
MONITORING_PRODUCT_VERSION_CLASSIC4: Final[str] = "monitoring-classic4-v1"
MONITORING_PLATFORMS_CLASSIC4_CSV: Final[str] = "dashscope,deepseek,kimi,doubao"

# ---------------------------------------------------------------------------
# 监测耐久账本的**可执行面**(唯一白名单常量 · 2026-08-04)
# ---------------------------------------------------------------------------
# 🔴 这**不是**「商品卖了几个引擎」,是「``monitoring_run_cells`` 能物理存下几个
# 引擎」。两者是不同的问题,今天数值恰好都等于 classic4 四路,但语义必须分开:
# 商品矩阵(``monitoring_product_platform_matrices``)会随新售版本增长且老行永久
# 冻结;账本可执行面则由 DB CHECK ``chk_monitoring_run_cells_platform`` 钉死。
#
# 生产实证(2026-08-04 只读取证):
#   CHECK (platform = ANY (ARRAY['dashscope','deepseek','kimi','doubao']))
#   monitoring_run_cells 建表至今 804 行,platform 取值分布恰好只有这四个,
#   yuanbao 单元数 = 0(不是「还没跑到」,是物理写不进去)。
#
# 为什么必须有这个常量:2026-07-26 的 unified5 收口迁移只把**授权侧**(商品矩阵
# 行 + 四处列默认值 + confirmed_keywords.monitoring_product_version 默认值)推到
# 五引擎,**执行账本侧没有同步**。于是任何 entitlement 含 yuanbao 的词都会让
# ``create_monitoring_run_cells`` 的 ``executable ⊆ entitlement`` 守卫炸掉**整批**
# 任务(生产 monitoring_tasks 1543/1544 双 failed 即此)。修法不是放宽守卫(它是
# 资金/授权闸),而是让**裁 eligible 的那一层和落 cell 的那一层读同一个白名单**。
#
# 🔴 要放宽到五引擎,必须**同一批**改三处,少一处就出事:
#   1. CHECK 迁移(``chk_monitoring_run_cells_platform`` 加值);
#   2. ``db.monitoring_db.assert_monitoring_cell_retry_ready`` 里逐字比对的期望
#      CHECK 定义 —— 不同步改,容器启动守卫直接 raise,两槽全起不来;
#   3. 本常量。
# 只改 1 不改 2 = 起不来;只改 3 不改 1 = INSERT 被 CHECK 打回。
MONITORING_RUN_CELL_PLATFORMS: Final[tuple[str, ...]] = (
    "dashscope",
    "deepseek",
    "kimi",
    "doubao",
)

# ---------------------------------------------------------------------------
# 诊断链的**执行面**(唯一白名单常量 · 2026-08-23 · P0-2 返修③)
# ---------------------------------------------------------------------------
# 🔴 这**不是**「诊断卖了几个引擎」,是「诊断管线今天真的会去跑哪几个引擎」。
# 与上面 MONITORING_ENGINES(授权面)/ MONITORING_RUN_CELL_PLATFORMS(执行面)
# 是**同一个形状**:监测侧早就把这两件事分开了,诊断侧一直没有 —— 于是出现了
# 下面这个已被实测确认的坏结果。
#
# 生产实证(门四 2026-08-22 真跑 diagnosis_id=3):
#   · 前端按 ['qwen','deepseek','kimi','doubao'] 计价并扣算力;
#   · 管线真跑的是 ["dashscope","deepseek","doubao","yuanbao"];
#   · 报告 ai_visibility 里出现 doubao / yuanbao,**kimi 零出现**;
#   · 'qwen' 这个键在本仓任何引擎注册表里都不存在(canonical 键是 dashscope)。
#   即:客户为 kimi 付了钱、拿到的是 yuanbao,而且有一个键谁都不认识。
#
# 🔴 口径时间线(已由 Owner 2026-08-23 收口)
#   · 2026-07-19 009a74e30 把 workflows/diagnosis_workflow.py 的默认改成
#     ["dashscope","deepseek","doubao","yuanbao"],注释写「新售现役四平台;
#     Kimi 仅保留历史数据/显式旧权益兼容」;
#   · 2026-07-26 本文件落地,写明「Owner 裁决:统一为五引擎」含 kimi;
#   · 两者一直没收口 —— 于是前端按含 kimi 的清单收钱,管线跑的却是含 yuanbao
#     的另一份,客户为 kimi 付钱而报告里 kimi 零出现(门四 diagnosis_id=3 实证)。
#   · 2026-08-23 Owner 定:**计价集 = 执行集 = 下面这四个,Kimi 加回并真跑**;
#     元宝**保持现状**(见 DIAGNOSIS_RUNTIME_ENGINES),但不进计价、不进对客承诺、
#     不进交付链。
#
# 🔴 谁消费它(三处,全部从这里取,不许各写一份):
#   1. 计价 —— api/defensive_geo_api 的 planned_cells + 平台白名单;
#   2. 前端可选集 —— frontend/src/lib/defensiveGeoEngines.ts(判据跨语言锁死);
#   3. 执行器传参 —— services/defensive_geo/run_executor 交给管线的 ai_engines。
#   「计价集与真跑集是两套」正是这次要修的那个缺陷本身。
DEFENSIVE_GEO_BILLABLE_ENGINES: Final[tuple[str, ...]] = (
    "dashscope",
    "deepseek",
    "doubao",
    "kimi",
)

# 诊断管线的**默认**执行集(legacy 入口:没有显式传 ai_engines 的那些调用)。
#
# 🔴 元宝在这里、**不在**上面那个计价集里,是 Owner 2026-08-23 的原话:
#   「元宝保持现状:诊断侧继续跑,但不进计价、不进对客承诺、不进交付链」。
#   所以它是一个**商业上不可见的观测面**:我们自己继续看它的表现,
#   但客户既不为它付钱,也不会在报告承诺里读到它。
# 🔴 防御型 GEO 那条链**不吃这个默认**:执行器显式传客户买下的那一份
#   (见 run_executor),所以「客户买了什么就跑什么」在那条链上是结构性成立的,
#   不依赖这里的默认值。
DIAGNOSIS_RUNTIME_ENGINES: Final[tuple[str, ...]] = (
    "dashscope",
    "deepseek",
    "doubao",
    "yuanbao",
)


def unknown_engines(keys) -> tuple[str, ...]:
    """挑出**不在计价集**里的引擎键,按传入顺序返回、去重。

    membership 谓词也只此一处:调用方各写一遍 ``k not in TUPLE`` 就会出现
    「白名单加了一个引擎、但某处判断没跟上」——而那一处不会有任何判据变红。

    🔴 判的是 ``DEFENSIVE_GEO_BILLABLE_ENGINES``(客户能买的那一份),
       不是管线默认集:元宝我们自己继续跑,但客户**不能点它、不为它付钱**。
    """
    seen: set[str] = set()
    out: list[str] = []
    for k in keys or ():
        key = str(k or "").strip()
        if key in DEFENSIVE_GEO_BILLABLE_ENGINES or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)

# 历史/legacy 引擎:调用方可显式传入,但不进新售默认矩阵。
LEGACY_ENGINES: Final[tuple[str, ...]] = ("metaso",)

# 无联网检索的表面(纯模型原生回答)。这些平台的「引用数」是**不适用**,
# 不是「真实为 0」——报告必须渲染 "—" 并说明原因,禁止和有检索平台的引用数并排比大小。
#
# [2026-07-27] 元宝已移出本集合:TokenHub 控制台"联网搜索"开通 + 模型换 hy3-preview 后,
#   实测真返 ``message.search_results``(13 条)与 ``usage.tool_usage.web_search_call``。
#   ⚠️ 移出的前提是**这两件事都成立**;若哪天额度耗尽或被关回,adapter 会把
#   ``search_enabled`` 记成 False 并打 warning(见 openai_compat._fetch),
#   而不是悄悄退化成"引用数真实为 0"——那比标"不适用"更糟。
#   五引擎现已全部具备联网检索,故本集合当前为空。
NO_SEARCH_ENGINES: Final[frozenset[str]] = frozenset()

# 每个漏斗层的最少有效样本数(与 tools/scoring/funnel_score.py 的 medium 置信阈值同源)。
# 诊断选词分配据此保证「每层 ≥5 样本」:样本 = 该层题数 × 引擎数。
MIN_SAMPLES_PER_FUNNEL_LAYER: Final[int] = 5


def engine_label(engine: str) -> str:
    """canonical key → 客户可见平台名;未知 key 原样返回(不造名)。"""
    key = (engine or "").strip().lower()
    return ENGINE_LABELS.get(key, engine or "未知平台")


def default_diagnosis_engines() -> list[str]:
    """诊断默认引擎清单(新 list,调用方可安全改)。"""
    return list(DIAGNOSIS_ENGINES)


def default_monitoring_engines() -> list[str]:
    """监测默认引擎清单(新 list,调用方可安全改)。"""
    return list(MONITORING_ENGINES)


def is_no_search_engine(engine: str) -> bool:
    """该引擎是否为无联网检索表面(引用数不适用)。"""
    return (engine or "").strip().lower() in NO_SEARCH_ENGINES


def min_questions_per_layer(engine_count: int) -> int:
    """保证每层 ≥ MIN_SAMPLES_PER_FUNNEL_LAYER 个样本所需的最少题数。

    样本 = 题数 × 引擎数。五引擎时 1 题即 5 样本;引擎降级(部分不可用)时
    自动要求更多题,避免出现「满分 + 数据不足」那种左右脑互搏(P0-5)。
    """
    count = max(1, int(engine_count or 0))
    floor = MIN_SAMPLES_PER_FUNNEL_LAYER
    return max(1, -(-floor // count))  # ceil(floor / count)
