"""被测 AI 引擎合同 —— **零依赖** SSOT(模型 / 端点 / 联网参数 / 平台映射)。

🔴 为什么单独一个模块,而不是留在 ``services/monitoring_lineage.py``
------------------------------------------------------------------
``monitoring_lineage`` 里有一句 ``from services.question_evolution import ...``,
而 ``question_evolution`` 引 ``db.connection``。于是任何**在事务里惰性 import**
``monitoring_lineage`` 的代码,其传递闭包都会碰到 ``db.connection`` ——
而 import ``db.connection`` 会触发 ``init_db`` 去抢 ACCESS EXCLUSIVE。
2026-08-10 **把生产打成 503 十六分钟**的自死锁就是这个形态。

包F ① 的账本桥正是"事务内惰性 import":``db/monitoring_db.py`` 在
claim/finish 的事务中间 ``from ...run_ledger_bridge import open_for_claim``。
桥又要读引擎合同 ⇒ 如果合同住在 ``monitoring_lineage`` 里,这条链就成立了。
窗D 那条既有锁 ``test_bridge_import_closure_touches_no_db_module`` 当场判红,
而它是对的。

所以合同搬到这里:**本模块只允许 stdlib**,判据
``test_engine_contract_has_no_heavy_imports`` 钉死这一条。
``monitoring_lineage`` 从这里 re-export,对它的既有调用方零变化。

🔴 一个 SSOT,两个消费者
-----------------------
· ``tools/ai_visibility/ai_tester.py`` —— 真正发请求的那一行;
· ``services/monitoring_lineage.py`` / ``db/monitoring_db.py`` —— 记 lineage 与计价。

历史教训(2026-08-23 census 实测):这两边**曾经分叉** ——
按 ``qwen3.7-plus`` 计价、按 ``qwen3-max`` 干活,而全仓没有一条判据会红。
收成一个常量之后,分叉在结构上不可能发生。
"""

from __future__ import annotations

from typing import Any, Final

# ══════════════════════════════════════════════════════════════════════════
# [包F ⑥ · 2026-08-24 Owner 终裁] 千问被测引擎 = qwen3.7-plus
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 **被测千问引擎的唯一 SSOT**:模型名 + 端点 + 联网参数一体。
#:
#: 保真锁(``tests/defensive_geo_pkgf_*/test_engine_fidelity.py``)锚在本常量:
#: 模型名 / 端点路径 / 任一 search_option 漂移即红。
#:
#: 🔴 端点是 **multimodal-generation**,不是 text-generation。
#: 2026-08-15 那次探测得出的「qwen3.7-plus 必 400 / 只能走 Responses API /
#: 零角标」是**走错端点**的结论(打的是 text-generation);
#: 2026-08-24 生产原地复探:原生 multimodal-generation 端点 HTTP 200,
#: 全套 search_options 活(1377 字 / 6 来源 / 30 个正文角标)。
#: 旧结论连同它衍生的「无技术解、需 Owner 取舍角标缩水」一并作废。
#:
#: 🔴 ``content`` 是**数组**形状(``[{"text": ...}]``)——多模态端点的入参约定。
#: 响应侧 ``message.content`` 同样是数组,由
#: ``ai_tester._dashscope_message_text`` 统一折平(两种形状都吃)。
#: 🔴 **``base_parameters`` 是 R2 F-2 补回来的**(2026-08-24 Review 生产两臂实测)。
#:
#: R1 我把 ``enable_thinking`` 从请求里删了,理由写的是「这个键不在 Owner 给定的
#: 探测参数集里,传没探过的键=拿生产去试」。**那句前提是错的** —— 08-24 那次
#: 打通的探测(``probe_qwen37_mm.sh`` 第 10 行)参数集里明确含
#: ``"enable_thinking": false``;删掉它才是未探过的偏离。
#:
#: 同题同参、仅差这一个键的生产两臂:
#:
#: ==================  ======================  ====================
#: 指标                 臂A = 省略该键(R1)      臂B = ``false``(本值)
#: ==================  ======================  ====================
#: HTTP                200                     200
#: output_tokens       2876(reasoning 2031)   **981**
#: ``message`` 里       出现 reasoning_content  无
#: 正文 / 角标          1343 字 / 17            **1701 字 / 26**
#: ==================  ======================  ====================
#:
#: 即:多模态端点上**省略该键 = thinking 默认开** ⇒ 输出 token 2.9 倍、
#: 答案反而更短更少角标(max_tokens 预算被 reasoning 吃掉)。资金 × 质量
#: 双向流血,而且每一次监测调用都在流。
#:
#: 🔴 ``enable_search`` 一并收进来:它原先手写在调用点,是同一个分叉形态
#: (F-2 的本体就是「一个参数漂出 SSOT 而没有判据会红」)。收进来之后
#: 判据 ``test_request_parameters_all_come_from_the_ssot`` 能钉死
#: 「``parameters`` 里除 ``max_tokens`` 外不许有手写键」—— 这条锁的是
#: **形态**,所以下一个被人顺手删掉/加上的参数也会当场红,不只是
#: ``enable_thinking`` 这一个键。收进来时线上字节不变。
#:
#: 本仓的既有共识也站在这一侧:``writing/llm_utils.py:148`` 写着
#: 「DashScope Qwen 系列: {"enable_thinking": False}」,``ai_tester`` 里
#: deepseek 那条链(同样走 DashScope)至今传 ``enable_thinking: False``,
#: 注释还写着「对齐 qwen 调用的 enable_thinking:False」。R1 删掉之后,
#: 那句注释指向的对象已经不存在了。
QWEN_ENGINE: Final[dict[str, Any]] = {
    "model": "qwen3.7-plus",
    "endpoint": (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
        "multimodal-generation/generation"
    ),
    "content_shape": "multimodal_array",
    "base_parameters": {
        "enable_thinking": False,
        "enable_search": True,
    },
    "search_options": {
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[<number>]",
        "forced_search": True,
        "search_strategy": "turbo",
    },
}

#: 切换日。监测历史**不重算**(§3.1 的取舍:换模型当天起新旧数据不可比,
#: 先导翻转率 16.7~60%,趋势图会出现人为断点)。所以这里只落一个
#: **断点标注**,让报告能如实说"这一天换了被测引擎",而不是悄悄接上去。
QWEN_ENGINE_SWITCHED_ON: Final[str] = "2026-08-24"
QWEN_ENGINE_PREVIOUS_MODEL: Final[str] = "qwen3-max"

#: 平台 → (provider, model, surface)。现役监测据此决定"这一格发给谁"。
#: 🔴 千问那一格的 model 逐值取 :data:`QWEN_ENGINE` —— 不再手写字符串。
PLATFORM_CONTRACT: Final[dict[str, dict[str, str]]] = {
    "dashscope": {"provider": "dashscope", "model": QWEN_ENGINE["model"],
                  "surface": "ai_search"},
    #: 🔴 [WO_221-c1' · 本单该类缺陷的第 6 处] 这一格是**写进账本的计划模型**
    #:   (`monitoring_lineage` 拿不到回显时回落到它,`run_ledger_bridge` 直接写
    #:   `actual_provider` / `actual_model`)。2026-07-27 起 DeepSeek 监测已换
    #:   官方原生检索,而这里仍写着 dashscope + 退役名 ——
    #:   计划与实发不符,账本里那一列就是错的,且不会报错。
    #: 🔴 改 provider 值不会 KeyError:两个消费方都只把它当字符串落库
    #:   (`contract.get('provider')`),没有按它查表 —— 动手前逐个核过。
    #: 🔴 这里**刻意写字面量**,不 import `config.deepseek_models` 取常量:
    #:   本模块是**零依赖** SSOT(docstring 与判据
    #:   `test_engine_contract_has_no_heavy_imports` 都钉着「只许 stdlib」)——
    #:   它被**事务内惰性 import** 的账本桥引用,多一条 import 就多一条
    #:   碰到 db.connection 的路径,而那正是 2026-08-10 把生产打成 503 十六分钟的形态。
    #:   同格的 kimi / doubao 也都是字面量,这是本模块的房规。
    #:   代价是这个名字有两个出处 —— 由 206 的发出点签入表逐行看着。
    "deepseek": {"provider": "deepseek_official", "model": "deepseek-flash",
                 "surface": "ai_search"},
    "kimi": {"provider": "kimi", "model": "kimi-k2.6", "surface": "ai_search"},
    "doubao": {"provider": "doubao", "model": "doubao-seed-2-0-pro-260215",
               "surface": "ai_search"},
}
