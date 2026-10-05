"""完整度里三个 quote 取值的**值级**判据
(工单 `WO_REPORT_METRICS_QUOTE_KEYS_2026-09-03.md` §3)。

## 为什么判据必须打在值上,不能打在"跑得通"上

改之前这三处**全都不报错**:
```
quote.keywords            键不存在 → `or` 兜底 → 静默走 diagnosis
quote.publications_count  键不存在 → int(None or 0) → 恒 0
quote.competitor_list     键存在但是 JSON 文本 → len(text) → 数字符
```
每一处都有一个"看起来合理"的兜底把错误吸收掉了 ——
**"改完不报错"正是它们改之前的状态**,拿它当验收等于什么都没验。

## 观测点选择

`compute_data_completeness` 不回吐 `merged`,只回 `score / groups / missing_summary`。
所以断言打在 **`missing_summary` 的字段键**上 —— 它是这些值真正影响到的**下游终态**,
同时也证明了**接线**(只测两个 helper 只能证明函数对,证明不了 `compute_data_completeness` 用了它们)。

## 🔴 竞品夹具为什么是 **2** 条而不是 4 条

`competitors_count` 的阈值是 3。
- 给 4 条:新代码算出 4(≥3,不缺);**旧代码 `len('[{...},{...}]')` 也有几十(≥3,也不缺)**
  ⇒ 两臂读数相同,**零区分力**。
- 给 2 条:新代码算出 2(<3,**缺**);旧代码数字符仍有几十(≥3,**不缺**)
  ⇒ 两臂相反,判据才有牙。
"""

from __future__ import annotations

import json

# 🔴 本文件**只 import 公开函数** —— helper 级用例在 test_quote_helpers.py。
#    理由:红臂要在 39cbb3ea8 上跑,那棵树没有 `_competitors_count` / `_quote_publications_count`,
#    混在一起 import 会**整文件 collection error**,把端到端那几条的逐条读数一起吞掉。
from services.report_metrics import compute_data_completeness

TWO_COMPETITORS = json.dumps(
    [{"name": "阿里云", "desc": "综合云厂商"}, {"name": "腾讯云", "desc": "综合云厂商"}],
    ensure_ascii=False,
)


#: 🔴 [#54/#55] `published` 现在是 **required keyword-only**。本文件测的是竞品/关键词,
#:    与「已发布证据」无关 ⇒ 统一喂 no_quote(该项退出分母,不干扰这里的断言)。
NO_QUOTE = {"count": None, "available": False, "reason": "no_quote"}


def _missing_keys(**kw) -> set[str]:
    kw.setdefault("published", NO_QUOTE)
    out = compute_data_completeness(**kw)
    # 🔴 字段名是 `missing_fields` 不是 `missing`(report_metrics.py 的 groups_out)。
    #    第一版我写成 `missing` ⇒ 取到**空集** ⇒ "in missing" 恒假、"not in" 恒真:
    #    **一半判据恒绿、一半恒红,而两种都不是被测对象的读数。**
    keys = {m["key"] for g in out["groups"] for m in g.get("missing_fields", [])}
    assert out["max_score"] == 100, "返回结构变了 —— 判据的观测点要跟着改"
    assert keys, "missing_fields 取到空集 —— 观测点抽错了(而不是「什么都不缺」)"
    return keys


# ── publications_count ────────────────────────────────────────────
# 🔴 [#54/#55 2026-09-04] 原来这里两条判据钉的是「读 quotes.total_articles」,
#    而本单裁定**禁读**它:那是「报价承诺篇数」,标签写的却是「已发布证据」。
#    事实源改为 media_publications,值由调用方传入,并且分三态。
#    ⇒ 这两条**必须删**,不是放宽 —— 判据钉着旧口径会与正确的新行为对打。
#    接替它们的是同目录 test_published_evidence_tri_state.py(三态三臂 + 反臂 + 接线锁)。

# ── competitors_count:曾经在数字符 ─────────────────────────────────
def test_competitor_list_is_parsed_not_measured_by_length():
    """两条竞品 ⇒ 2 < 阈值 3 ⇒ **应当**记为缺失。

    39cbb3ea8 上 `len(JSON 文本)` 有几十 ⇒ ≥3 ⇒ **不记缺失**(红臂,方向相反)。
    """
    missing = _missing_keys(brand={"competitors_jsonb": []},
                            quote={"competitor_list": TWO_COMPETITORS})
    assert "competitors_count" in missing, (
        f"只有 2 条竞品(阈值 3)却没记缺失 —— 多半又在数 JSON 文本的字符数;"
        f"该文本长 {len(TWO_COMPETITORS)} 字符")


# ── keywords:那一截被删掉了,行为必须与"走 diagnosis"完全一致 ───────
def test_keywords_come_from_diagnosis_only():
    """quote 里塞任何关键词形态的东西都不许影响分层计数。

    🔴 这条同时挡住"把键名改成 `total_keywords`"那个看似合理的修法:
       它是 integer,而分层循环被 `isinstance(raw_keywords, list)` 包着,
       整数会被**静默跳过** —— 三个计数仍然全 0,bug 从可见变成不可见。
    """
    diag = {"keywords": ["某某品牌怎么样", "杭州 云服务 哪家好", "如何选云服务商",
                         "云服务 价格", "云厂商 对比"]}
    base = _missing_keys(diagnosis=diag)
    for noisy_quote in ({"keywords": ["无关词"]}, {"total_keywords": 99}, {"total_keywords": 0}):
        assert _missing_keys(diagnosis=diag, quote=noisy_quote) == base, (
            f"quote={noisy_quote} 改变了关键词分层结果 —— "
            "关键词只有 diagnosis 这一个来源(真身在 confirmed_keywords,见卡 #47)")


def test_brand_competitors_jsonb_keeps_priority_end_to_end():
    """反臂:品牌档案有竞品(3 条)⇒ 达标,且**不受** quote 里那 2 条影响。

    没有这条,上面那条"2 条应记缺失"可能只是因为竞品这一格**永远缺**。
    """
    brand = {"competitors_jsonb": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}
    assert "competitors_count" not in _missing_keys(
        brand=brand, quote={"competitor_list": TWO_COMPETITORS})
