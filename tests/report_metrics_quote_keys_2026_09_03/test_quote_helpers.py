"""两个 quote 取值 helper 的直接判据(单一来源本体)。

🔴 与 `test_completeness_values.py` 分开放,是因为**红臂**要在 39cbb3ea8 上跑,
   而那棵树没有这两个函数 —— 放在一起 import 会整文件 collection error,
   端到端那几条的逐条读数会被一起吞掉。
   在基线树上本文件报 collection error 是**预期**的,那正是"这两个 helper 还不存在"。

单测 helper **不能**替代端到端:它证明函数对,证明不了 `compute_data_completeness` 用了它们
(接线要由 `test_completeness_values.py` 打)。
"""

from __future__ import annotations

import json

from services.report_metrics import _competitors_count

TWO_COMPETITORS = json.dumps(
    [{"name": "阿里云", "desc": "综合云厂商"}, {"name": "腾讯云", "desc": "综合云厂商"}],
    ensure_ascii=False,
)


# 🔴 [#54/#55 2026-09-04] 原 `test_publications_helper_does_not_read_the_nonexistent_key`
#    已删:它测的 `_quote_publications_count` 本身被删掉了 ——
#    本单裁定**禁止**「已发布证据」从 quote 取任何东西(`total_articles` 是
#    「报价承诺篇数」不是「已发布」),事实源改成 media_publications,由调用方传入。
#    ⇒ 那条判据钉的是**已被取代的口径**,留着会与新行为对打。
#    取代它的是 tests/report_metrics_quote_keys_2026_09_03/test_published_evidence_tri_state.py
#    里的「反臂:quote 里放任意 total_articles 都不改变三态结论」。

def test_competitor_list_counts_elements():
    assert _competitors_count({"competitors_jsonb": []},
                              {"competitor_list": TWO_COMPETITORS}) == 2
    # 坏 JSON 不许退回"数字符"
    assert _competitors_count({}, {"competitor_list": "not json at all"}) == 0
    # 空/缺失
    assert _competitors_count({}, {}) == 0
    assert _competitors_count({}, {"competitor_list": ""}) == 0


def test_brand_competitors_jsonb_keeps_priority():
    """反臂:品牌档案有竞品时,口径与改前**逐字相同**(优先级没被动过)。

    端到端那一半在 test_completeness_values.py —— 本文件只碰 helper。
    """
    brand = {"competitors_jsonb": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}
    assert _competitors_count(brand, {"competitor_list": TWO_COMPETITORS}) == 3
