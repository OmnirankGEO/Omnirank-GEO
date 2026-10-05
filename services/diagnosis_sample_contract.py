"""[C组·V10/§12.1]诊断「最小有效样本」版本化合同。

替代散落在 `_assert_ai_visibility_sufficient` 里的 `>0.5` 等裸常量:是否足够、怎么计费、
怎么向用户表述,全部由本合同**单点、版本化**决定(纯函数,不碰资金原语,便于判别)。

Master §12.1 裁决:
- **零可用结果 / payload 损坏 / 无法形成可信 summary → H0 技术失败**(整单不交付,全额退/释放);
- **有足够有效样本但部分 provider 失败 → 降级交付**:失败平台**排除分母**并显示覆盖率,
  **未履约的那部分不计费**(部分退款/释放),**已有有效结果不整批抹掉**;
- 失败必须有重试、退款/释放状态与人工出口(由调用方按 §13 合同给)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# 版本化阈值:改判定标准必须 bump 版本(审计/血缘可追溯用哪一版判的)。
SAMPLE_CONTRACT_VERSION = "diagnosis-min-sample-v1"

#: 交付所需的最少**成功**测试数。低于此 → H0 技术失败(不是"过半失败"的裸常量)。
MIN_SUCCESSFUL_TESTS = 1

OUTCOME_SUFFICIENT = "sufficient"      # 计划内全部履约
OUTCOME_DEGRADED = "degraded"          # 部分 provider 失败 → 降级交付 + 部分计费
OUTCOME_INSUFFICIENT = "insufficient"  # H0:零可用/损坏 → 不交付,全额退


class DiagnosisNotMeasuredError(RuntimeError):
    """[P0-3 · 2026-08-24] 这一单**根本没测成**(计划跑了引擎但零次成功观测)。

    专门起一个类型,是为了让资金侧和呈现侧能分开对待:
      · 资金侧 —— 它是 RuntimeError 子类,`_do_settlement` 现有的
        `except (RuntimeError, TypeError, ValueError, json.JSONDecodeError)`
        原样接住 → commit 自动改道 release(不需要在结算机器里新开分支);
      · 呈现侧 —— 调用方 `isinstance` 一判就知道该说「这次没有测成,算力已退回」,
        而不是把内部异常文本或「隐形级」这种**等级词**丢给用户
        (0 分隐形级的意思是"测了,AI 不认识你";没测成的意思是"我们没测出来",
         两件事对客户完全不同,混为一谈就是误导)。
    """


@dataclass(frozen=True)
class SampleVerdict:
    version: str
    outcome: str
    planned: int
    succeeded: int
    failed: int
    coverage_ratio: float          # 已履约占计划的比例(0..1),用户可见"覆盖"
    billable_ratio: float          # 可计费比例 == coverage_ratio(未履约不计费)
    delivered_platforms: tuple = ()
    failed_platforms: tuple = ()
    reason_code: str = ""
    message: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def deliverable(self) -> bool:
        """是否可以交付报告(降级也算可交付——已有有效结果不整批抹掉)。"""
        return self.outcome in (OUTCOME_SUFFICIENT, OUTCOME_DEGRADED)


# ============================================================================
# [工单 C-2(a) · Codex 终审 P1-9] 结算分母 = **计费执行面**,不是"跑了哪些引擎"
# ============================================================================
#
# 实证链(全部亲核,不是转述):
#   · `workflows/diagnosis_workflow.py:443-445` —— 没显式传引擎时默认集是
#     `config.ai_engines.DIAGNOSIS_RUNTIME_ENGINES`
#     = ("dashscope","deepseek","doubao","yuanbao");
#   · `config/ai_engines.py:128-133` —— **计价集**是
#     `DEFENSIVE_GEO_BILLABLE_ENGINES` = ("dashscope","deepseek","doubao","kimi"),
#     那一段逐字写着 Owner 2026-08-23 的口径:「元宝保持现状:诊断侧继续跑,
#     但**不进计价、不进对客承诺、不进交付链**」;
#   · `tools/ai_visibility/ai_tester.py:4011` ——
#     `total_planned = len(questions) * len(engines)`,
#     `total_tests = sum(brand_stats[e]["total"] for e in ...)`,
#     两个数都**含元宝**。
#
# 于是 `evaluate_sample` 算出来的 coverage/billable 比例分母里混进了一个
# 客户既没买、也不会在报告里读到的观测面。方向是双向错的:
#   · 元宝挂了、四个计费引擎全成功 ⇒ 比例被摊成 3/4,**少收 25%**;
#   · 豆包挂了、元宝成功 ⇒ 比例是 3/4,而客户真正丢的是 1/3 —— **多收**。
#
# 修法:分母**机械枚举**自 `config/ai_engines`,与那一份计价集同源;
# 分子按 `engine_stats` 逐引擎取,只取计费面那几个。
# 🔴 不在这里手抄引擎名:手抄的那份会在 Owner 调整计价集时静默漂移,
#    而没有任何判据会红(本仓记过「手写分母漏掉的那一项不会让任何判据变红」)。


def billable_engines_planned(engines: Sequence[Any]) -> tuple[str, ...]:
    """本次计划跑的引擎里,**属于计费面**的那几个(按传入顺序、去重)。

    分母来自 `config.ai_engines.DEFENSIVE_GEO_BILLABLE_ENGINES` —— 客户能买、
    能在报告里读到的那一份。取不到常量时返回空元组,调用方据此**保持现状不改判**
    (绝不因为 import 失败就去动钱)。
    """
    try:
        from config.ai_engines import DEFENSIVE_GEO_BILLABLE_ENGINES as _BILLABLE
    except Exception:                                     # pragma: no cover - 环境缺件
        return ()
    seen: set = set()
    out: list[str] = []
    for e in engines or ():
        key = str(e or "").strip()
        if not key or key in seen or key not in _BILLABLE:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)


def _billable_counts(av: Mapping[str, Any], planned: int) -> Optional[tuple[int, int, tuple]]:
    """把 (planned, succeeded) 收窄到计费执行面。算不出来返回 ``None``。

    返回 ``(billable_planned, billable_succeeded, non_billable_engines)``。

    🔴 三个"算不出来"一律返回 ``None`` ⇒ 调用方**保持原口径**:
      · 没有 `engines_tested`(老结构 / social / skipped);
      · 没有 `engine_stats`(逐引擎成功数拿不到,收窄分子就只能猜);
      · 计费面为空(本次一个计费引擎都没跑)—— 那是另一件事,
        不该由本函数顺手判成"零履约"去动钱。
    不猜就是这条:少收一格远好过按一个编出来的比例扣款。
    """
    engines = _engine_list(av.get("engines_tested"))
    stats = av.get("engine_stats")
    if not engines or not isinstance(stats, Mapping):
        return None
    billable = billable_engines_planned(engines)
    if not billable:
        return None
    non_billable = tuple(e for e in engines if e not in billable)
    if not non_billable:
        return None                       # 全是计费面 ⇒ 原口径已经对了,零改动
    # 每个引擎的计划题数 = 总计划 ÷ 引擎数(产出方逐字:planned = 题数 × 引擎数)。
    # 整除不了说明上游口径变了 —— 不猜,退回原口径。
    if planned <= 0 or planned % len(engines) != 0:
        return None
    per_engine = planned // len(engines)
    succeeded = 0
    for e in billable:
        cell = stats.get(e)
        if not isinstance(cell, Mapping):
            return None                   # 计费引擎缺逐引擎明细 ⇒ 不猜
        try:
            # 🔴 [工单 C · 外选 EXTC-04 真洞 · Review 2026-08-26 裁定] **逐引擎封顶**。
            #
            #    原来只在求和之后按总量封顶(见下面 return 那一行的 `min`)——
            #    那挡不住这一格:某个计费引擎**超报**(`engine_stats[e]["total"]`
            #    由采集侧逐格累加,重试/双报会让它大于自己的计划题数)时,
            #    多出来的那几次会**盖住另一个计费引擎的整死**。
            #    实测 per_engine=8:dashscope 16 / deepseek 8 / doubao 0
            #    ⇒ 分子 24 = 分母 24 ⇒ `full_coverage` ⇒ 按 100% 扣款,
            #    而客户买的三面里有一面一次都没测成。
            #
            #    一个引擎最多只能履约它自己那一份 `per_engine`,多报的不算履约 ——
            #    这就是「按真实履约扣费」在分子上的形态。
            #    只影响未来结算,不追溯已落库的 delivery_verdict 快照。
            succeeded += min(per_engine, max(0, int(cell.get("total") or 0)))
        except (TypeError, ValueError):
            return None
    return per_engine * len(billable), min(succeeded, per_engine * len(billable)), non_billable


def _platform_split(ai_visibility: Mapping[str, Any]) -> tuple[tuple, tuple]:
    """从平台明细里分出已履约/失败平台(缺明细时返回空,不臆造)。"""
    delivered: list[str] = []
    failed: list[str] = []
    platforms = (ai_visibility or {}).get("platforms")
    if isinstance(platforms, Mapping):
        items: Sequence = list(platforms.items())
    elif isinstance(platforms, Sequence) and not isinstance(platforms, (str, bytes)):
        items = [(str((p or {}).get("platform") or ""), p) for p in platforms if isinstance(p, Mapping)]
    else:
        items = []
    for name, detail in items:
        name = str(name or "").strip()
        if not name:
            continue
        d = detail if isinstance(detail, Mapping) else {}
        ok = d.get("error") in (None, "", False) and (
            d.get("tests") or d.get("total_tests") or d.get("success") or 0
        )
        (delivered if ok else failed).append(name)
    return tuple(delivered), tuple(failed)


def evaluate_sample(ai_visibility: Any) -> SampleVerdict:
    """对一次诊断采集结果做最小有效样本判定(纯函数,无副作用)。"""
    av = ai_visibility if isinstance(ai_visibility, Mapping) else {}
    summary = av.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    delivered_platforms, failed_platforms = _platform_split(av)

    def _int(key: str) -> int:
        # [P0-3 · 2026-08-24] 计数**先读顶层、再回落 summary**。
        #
        #   原实现只读 `av["summary"][key]`,而**两个真实产出方都把计数放在顶层**
        #   (workflows/diagnosis_workflow.py:921 销售版 / :2163 技术版 都是
        #   `ai_data = {"total_tests": ..., "total_planned": ..., ...}`,全文件没有
        #   任何地方给 ai_visibility 造过 "summary" 子字典)。
        #   于是生产每一次诊断读到的都是 planned=0 → 落到下面第 ③ 档
        #   `legacy_shape_no_planned` → SUFFICIENT / billable_ratio=1.0 →
        #   **四引擎全失败也全额扣费**,整个最小样本合同 + 部分计费从上线起从未生效。
        #   (老测试全绿是因为夹具 `_av()` 自己造了 `av["summary"]` —— 生产从不发这个键。)
        #   顶层优先、summary 兜底:嵌套形态的老调用方行为不变。
        for source in (av, summary):
            raw = source.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
        return 0

    # 计数口径以真实产出方 tools/ai_visibility/ai_tester.py 为准:
    #   total_tests   = **已成功完成**的测试数(不是尝试数)
    #   total_planned = 问题数 × 引擎数
    #   total_failed  = total_planned - total_tests
    # 因此 succeeded 就是 total_tests 本身;绝不能再减一次 failed(会少算成功数 → 误判不足/错计费)。
    planned = _int("total_planned")
    tests = _int("total_tests")
    failed = _int("total_failed")
    succeeded = max(0, min(tests, planned) if planned > 0 else tests)

    # [工单 C-2(a)] 把分母/分子收窄到**计费执行面**(见本文件上方那一大段实证)。
    # 收窄不成立时 `_billable_counts` 返 None,以下四行整段跳过 ⇒ 与改动前逐位相同。
    excluded_engines: tuple = ()
    narrowed = _billable_counts(av, planned)
    if narrowed is not None:
        planned, succeeded, excluded_engines = narrowed
        failed = max(0, planned - succeeded)

    # ① 采集层整体 error 且没有可信 summary → payload 损坏/整体失败:H0
    if av.get("error") and not summary:
        return SampleVerdict(
            SAMPLE_CONTRACT_VERSION, OUTCOME_INSUFFICIENT, planned, 0, planned,
            0.0, 0.0, delivered_platforms, failed_platforms,
            "collection_error_no_summary",
            "AI 搜索引擎本次访问异常,诊断未完成,算力已退回,请稍后重试。",
            {"raw_error": str(av.get("error"))[:200]},
        )
    # ② 有计划但零可用结果 → H0(全部 provider 都没给出可用结果)
    if planned > 0 and succeeded < MIN_SUCCESSFUL_TESTS:
        return SampleVerdict(
            SAMPLE_CONTRACT_VERSION, OUTCOME_INSUFFICIENT, planned, succeeded, failed,
            0.0, 0.0, delivered_platforms, failed_platforms,
            "zero_usable_result",
            "AI 搜索引擎本次全部访问失败,诊断未完成,算力已退回,请稍后重试。",
            {"min_successful_tests": MIN_SUCCESSFUL_TESTS,
             "non_billable_engines_excluded": list(excluded_engines)},
        )
    # ③ 老结构/无 planned 字段:不臆断失败(保持既有放行行为,避免误杀)
    if planned <= 0:
        return SampleVerdict(
            SAMPLE_CONTRACT_VERSION, OUTCOME_SUFFICIENT, planned, succeeded, failed,
            1.0, 1.0, delivered_platforms, failed_platforms,
            "legacy_shape_no_planned", "", {},
        )

    coverage = max(0.0, min(1.0, succeeded / planned))
    if failed <= 0 and succeeded >= planned:
        return SampleVerdict(
            SAMPLE_CONTRACT_VERSION, OUTCOME_SUFFICIENT, planned, succeeded, failed,
            1.0, 1.0, delivered_platforms, failed_platforms, "full_coverage", "",
            {"non_billable_engines_excluded": list(excluded_engines)},
        )
    # ④ 部分 provider 失败但仍有有效样本 → 降级交付 + 只按已履约计费
    return SampleVerdict(
        SAMPLE_CONTRACT_VERSION, OUTCOME_DEGRADED, planned, succeeded, failed,
        coverage, coverage, delivered_platforms, failed_platforms,
        "partial_provider_failure",
        f"本次有 {failed}/{planned} 次引擎访问未成功,报告按已完成的 {succeeded} 次结果出具;"
        f"未完成部分不计费。",
        {"coverage_ratio": round(coverage, 4),
         # 审计位:这一单的分母把哪几个非计费面排除掉了。
         # 退款争议要能答"这个比例是按哪几个引擎算的"。
         "non_billable_engines_excluded": list(excluded_engines)},
    )


def billable_points(verdict: SampleVerdict, frozen_points: int) -> int:
    """按履约比例算应计费点数(未履约不计费)。

    - insufficient → 0(全额释放)
    - sufficient   → 全额
    - degraded     → 向下取整按覆盖率计费,但**至少 1 点**(确有交付就不白送),且不超过冻结额。
    """
    try:
        total = int(frozen_points or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0 or not verdict.deliverable:
        return 0
    if verdict.outcome == OUTCOME_SUFFICIENT:
        return total
    # [E2-1 · Codex 二审 P1-F1 · 2026-08-26] 整数分账,ratio 不参与算钱。
    #
    # 这一处用的是 dataclass 上的**原始** float(不是持久化那份 4dp 截断),
    # 所以误差面比结算侧小 —— 但**不是零**。实测 p<2..200 × s<p × 五个总额:
    # 133 组两式不等,而且撞在最常见的档上:
    #   planned=10 succeeded=7 total=650 → 650*0.7 = 454.99999999999994
    #   → int 得 454,整数精确 (650*7)//10 = 455 ⇒ **少收 1**。
    # 70% 覆盖率一点都不冷僻,所以这一处照样得改。
    #
    # 🔴 计数不自洽(degraded 却 succeeded 不在 (0, planned) 内)⇒ **抛**,
    #    不静默回落 float:回落等于把这个 P1 换个地方原样保留。
    planned, succeeded = int(verdict.planned), int(verdict.succeeded)
    if planned <= 0 or not (0 < succeeded < planned):
        raise ValueError(
            "degraded 判定的计数不自洽(planned=%r succeeded=%r)· 拒绝按 float 回猜计费"
            % (verdict.planned, verdict.succeeded))
    return max(1, min(total, (total * succeeded) // planned))


# ============================================================================
# [P0-3 · 2026-08-24] 耐久行判定 —— 覆盖「没走 workflow 闸门」的那些诊断
# ============================================================================
#
# 上面的 evaluate_sample 只在 workflows/diagnosis_workflow.py:1183 被调用,而那一处
# 外层是 `if diagnosis_scope == "geo":` —— 也就是说 **full(技术版)/ 老数据 / sweeper
# 重试 / 收尸后补结算** 这些路径压根不经过它。资金判定不能只挂在采集内存对象上,
# 必须能从**落库后的耐久行**独立回答一句话:「这一单到底有没有拿到过一次成功的引擎观测?」
#
# 能这么问是因为 db/diagnosis_db.py:2355 把成功数落了列:
#     ai_total_tests    = ai_visibility["total_tests"]      # **成功**完成的测试数
#     ai_engines_tested = json.dumps(engines_tested)        # 本次计划跑的引擎清单
# 二者都不依赖内存快照,legacy 行同样有。

def _engine_list(raw: Any) -> list:
    """把 ai_engines_tested 列还原成引擎清单(JSON 文本 / 列表 / 逗号串都认)。

    认不出来一律返回空列表 —— 空列表的语义是「无法证明本次计划跑过引擎」,
    调用方据此**保持现状不改判**(绝不因为解析失败就去动钱)。
    """
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("["):
        import json as _json
        try:
            parsed = _json.loads(text)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def observed_engine_delivery(record: Mapping[str, Any]) -> Optional[SampleVerdict]:
    """从耐久 diagnosis_records 行判「本次有没有任何一次成功的引擎观测」。

    只回答 H0 这一问(零成功 → 不可交付、整单退)。**不**在这里算部分计费比例:
    耐久行推不出可靠的 planned(``total_questions_tested`` 在 dual/custom 模式下含自定义题,
    与 ai_tester 的 ``total_planned``(只数主问题)口径不同),按它算比例会少收 —— 既不多收
    也不少收,比例一律交给口径精确的 ``delivery_verdict`` 路径。

    返回:
      - SampleVerdict(INSUFFICIENT) —— 计划跑过引擎但**零次成功**;
      - None —— 该行回答不了这一问(没跑引擎 / 有成功样本 / 老行缺列),调用方保持现状。
    """
    row = record if isinstance(record, Mapping) else {}
    engines = _engine_list(row.get("ai_engines_tested"))
    if not engines:
        return None                      # 没跑引擎(social / skipped / 老行缺列)→ 不改判
    try:
        succeeded = int(row.get("ai_total_tests") or 0)
    except (TypeError, ValueError):
        return None                      # 列脏 → 不猜,不改判
    if succeeded > 0:
        return None                      # 有成功样本 → 比例的事交 delivery_verdict
    return SampleVerdict(
        SAMPLE_CONTRACT_VERSION, OUTCOME_INSUFFICIENT, 0, 0, 0,
        0.0, 0.0, (), tuple(engines),
        "persisted_zero_engine_observation",
        "本次 AI 搜索引擎全部访问失败,没有测成,算力已退回,请稍后重试。",
        {"engines_planned": len(engines), "successful_tests": 0},
    )
