"""DRIFT-D 判别测试 —— 报价 DTO 的 commercial_delivery_eligible 单点来自唯一引擎。

覆盖 selection_api 的四处报价 DTO 填充逻辑收敛到的共享 helper
``_delivery_eligible_field`` 与纯函数 DTO builder ``_build_compat_pricing_data``:

- ALLOWED→(guard 放行)：商业词 → eligible True → 前端展示守卫不误标红；
- DENIED→(guard 不强制放行)：知识/裸词 → eligible False → 守卫按既有规则处理；
- 既有显式盖章优先：existing True/False 不被引擎二次翻转；
- 零改价：DTO 只多一个布尔字段，任何价格数值不变(§9.6)。

真实边界：直接跑唯一引擎 CommercialQueryPolicy(纯规则层)+ 真实 DTO builder,
不 mock、不断言源码字符串。
"""
import copy

import inspect

from api.selection_api import (
    _delivery_eligible_field,
    _build_compat_pricing_data,
    _hydrate_delivery_eligibility,
)


# —— 唯一引擎判定的确定性 fixture(已实测) ——
COMMERCIAL_KW = "深圳装修公司哪家好"   # engine → eligible True / commercial
COMMERCIAL_KW2 = "装修公司推荐"        # engine → eligible True / commercial
KNOWLEDGE_KW = "什么是GEO"             # engine → eligible False / knowledge
FRAGMENT_KW = "装修"                   # engine → eligible False / seo_fragment


class TestDeliveryEligibleField:
    def test_commercial_keyword_is_eligible(self):
        assert _delivery_eligible_field(COMMERCIAL_KW) is True
        assert _delivery_eligible_field(COMMERCIAL_KW2) is True

    def test_knowledge_and_fragment_not_eligible(self):
        assert _delivery_eligible_field(KNOWLEDGE_KW) is False
        assert _delivery_eligible_field(FRAGMENT_KW) is False

    def test_existing_stamp_wins_over_engine(self):
        # 上游已排除的知识词标 False → 尊重既有终值(即使引擎会算 True)。
        assert _delivery_eligible_field(COMMERCIAL_KW, existing=False) is False
        # 上游已确认商业 True → 保持 True(即使文本引擎会算 False)。
        assert _delivery_eligible_field(KNOWLEDGE_KW, existing=True) is True

    def test_existing_none_falls_back_to_engine(self):
        assert _delivery_eligible_field(COMMERCIAL_KW, existing=None) is True
        assert _delivery_eligible_field(KNOWLEDGE_KW, existing=None) is False

    def test_blank_keyword_safe(self):
        # 空词不得抛异常;引擎判 False(无购买对象)。
        assert _delivery_eligible_field("") is False
        assert _delivery_eligible_field(None) is False


def _cluster(core_keywords):
    return {"clusters": [{"is_selected": True, "core_keywords": core_keywords}]}


class TestCompatPricingDataDTO:
    """[价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 第 4 个参数由 `cache_expires: str`
    (无条件今天+7 的假锁期)换成 `lock_map: dict`(逐词真实锁期)。本类只关心
    commercial_delivery_eligible,传 `{}` = 本次没锁价,对被测字段无影响。
    锁期本身的判据在 tests/test_price_lock_promise_2026_08_05.py。"""

    def _kw(self, text, price):
        return {
            "keyword": text,
            "is_selected": True,
            "entry": {"price": price, "articles": 2},
            "standard": {"price": price, "articles": 3},
            "flagship": {"price": price, "articles": 5},
            "intent": "informational",
        }

    def test_dto_carries_commercial_delivery_eligible(self):
        cluster_data = _cluster([
            self._kw(COMMERCIAL_KW, 500),
            self._kw(KNOWLEDGE_KW, 300),
        ])
        kw_id_map = {COMMERCIAL_KW: 1, KNOWLEDGE_KW: 2}
        out = _build_compat_pricing_data(cluster_data, kw_id_map, [], {})
        by_kw = {k["keyword"]: k for k in out["keywords"]}
        # 每个 DTO 都带该字段(前端守卫即生效)。
        assert "commercial_delivery_eligible" in by_kw[COMMERCIAL_KW]
        assert "commercial_delivery_eligible" in by_kw[KNOWLEDGE_KW]
        # 商业词 True(守卫不误标红) / 知识词 False(守卫按既有规则)。
        assert by_kw[COMMERCIAL_KW]["commercial_delivery_eligible"] is True
        assert by_kw[KNOWLEDGE_KW]["commercial_delivery_eligible"] is False

    def test_dto_respects_source_stamp(self):
        # 源 kw 已显式标 False → DTO 尊重,不被引擎翻回 True。
        kw = self._kw(COMMERCIAL_KW, 500)
        kw["commercial_delivery_eligible"] = False
        out = _build_compat_pricing_data(_cluster([kw]), {COMMERCIAL_KW: 1}, [], {})
        assert out["keywords"][0]["commercial_delivery_eligible"] is False

    def test_zero_price_change(self):
        # §9.6:加字段不得改动任何价格数值。
        kw_c = self._kw(COMMERCIAL_KW, 500)
        kw_k = self._kw(KNOWLEDGE_KW, 300)
        before = copy.deepcopy([kw_c, kw_k])
        out = _build_compat_pricing_data(
            _cluster([kw_c, kw_k]), {COMMERCIAL_KW: 1, KNOWLEDGE_KW: 2}, [], {}
        )
        by_kw = {k["keyword"]: k for k in out["keywords"]}
        for orig in before:
            got = by_kw[orig["keyword"]]
            for tier in ("entry", "standard", "flagship"):
                assert got[tier]["price"] == orig[tier]["price"]
        # tier 总价 = 两词标准价之和(未被字段污染)。
        assert out["tiers"]["standard"]["total_price"] == 500 + 300


class TestReadPathHydration:
    """[Owner 2026-07-25 裁决 A] 存量报价单读路径回填的判别。"""

    def _stored(self):
        """模拟改动前持久化的 pricing_data —— 关键词全部**没有**该字段。"""
        return {
            "generated_at": "2026-07-01 10:00:00",
            "tiers": {"standard": {"total_price": 800, "total_articles": 6}},
            "keywords": [
                {"id": 1, "keyword": COMMERCIAL_KW, "intent": "informational",
                 "standard": {"price": 500, "articles": 3}},
                {"id": 2, "keyword": KNOWLEDGE_KW, "intent": "informational",
                 "standard": {"price": 300, "articles": 3}},
            ],
        }

    def test_backfills_missing_field_from_engine(self):
        out = _hydrate_delivery_eligibility(self._stored())
        by_kw = {k["keyword"]: k for k in out["keywords"]}
        # 存量单读出来即带资格 → 前端守卫不再对合法商业词误标红。
        assert by_kw[COMMERCIAL_KW]["commercial_delivery_eligible"] is True
        assert by_kw[KNOWLEDGE_KW]["commercial_delivery_eligible"] is False

    def test_never_overwrites_existing_value(self):
        """只补缺失:已有值(含上游显式 False)一律尊重,不翻转既有结论。"""
        data = self._stored()
        data["keywords"][0]["commercial_delivery_eligible"] = False   # 上游已排除
        data["keywords"][1]["commercial_delivery_eligible"] = True    # 上游已确认
        out = _hydrate_delivery_eligibility(data)
        assert out["keywords"][0]["commercial_delivery_eligible"] is False
        assert out["keywords"][1]["commercial_delivery_eligible"] is True

    def test_zero_price_change_on_read(self):
        """§9.6:读路径回填不得改动任何价格/篇数/总价。"""
        data = self._stored()
        before = copy.deepcopy(data)
        out = _hydrate_delivery_eligibility(data)
        assert out["tiers"] == before["tiers"]
        for got, orig in zip(out["keywords"], before["keywords"]):
            assert got["standard"] == orig["standard"]

    def test_never_drops_or_blocks_any_keyword(self):
        """不阻断、不过滤:回填前后关键词集合完全一致(Owner:不能影响业务正常进行)。"""
        data = self._stored()
        n_before = len(data["keywords"])
        kws_before = [k["keyword"] for k in data["keywords"]]
        out = _hydrate_delivery_eligibility(data)
        assert len(out["keywords"]) == n_before
        assert [k["keyword"] for k in out["keywords"]] == kws_before

    def test_tolerates_none_and_malformed_without_raising(self):
        """健壮性:None / 非 dict / 缺 keywords / 脏元素都不得抛异常阻断读取。"""
        assert _hydrate_delivery_eligibility(None) is None
        assert _hydrate_delivery_eligibility("not-a-dict") == "not-a-dict"
        assert _hydrate_delivery_eligibility({}) == {}
        assert _hydrate_delivery_eligibility({"keywords": None}) == {"keywords": None}
        dirty = {"keywords": [None, "junk", {"keyword": COMMERCIAL_KW}]}
        out = _hydrate_delivery_eligibility(dirty)
        assert out["keywords"][2]["commercial_delivery_eligible"] is True

    def test_only_wired_into_owner_live_branches_not_frozen(self):
        """🔴 硬约束:冻结快照分支绝不调用回填(§5.5 已确认报价按冻结快照)。"""
        import api.selection_api as sapi
        src = inspect.getsource(sapi.get_selection_page)
        # 恰好两处调用,且都紧跟 session.get("pricing_data") 活数据读取。
        assert src.count("_hydrate_delivery_eligibility(") == 2
        for seg in src.split("_hydrate_delivery_eligibility(")[1:]:
            head = seg[:200]
            assert 'session.get("pricing_data")' in head, "回填被接到了非活数据分支"
        # frozen 分支取的是 pricing_snapshot,其后不得出现回填调用。
        for seg in src.split('frozen.get("pricing_snapshot")')[1:]:
            assert "_hydrate_delivery_eligibility" not in seg[:400], \
                "冻结快照分支被回填污染(违反 §5.5)"
