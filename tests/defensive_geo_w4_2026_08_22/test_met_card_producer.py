"""MET 呈现接线判据 —— **全链驱动**(G-3),外加 IDOR 回归。

G-3 逐字:「投影判据必须从 raw answer→aggregate **全链驱动**,
禁止夹具直造 DTO 层;每组 MET 抽样配一发 producer 层变异」。

所以本文件里**没有**任何一处直接构造 ``SampleSummary`` 或 ``CardView``:
每一条都从 attempt 账本行出发,经 evidence → summary → cards 走完整条链。
"""

from __future__ import annotations

import ast
import io

import pytest

from services.defensive_geo.monitoring import card_producer as CP
from services.defensive_geo.presentation import projection as PROJ
from services.defensive_geo.presentation import registries as REG
from tests.defensive_geo_w4_2026_08_22.conftest import ROOT


def _seed_attempt(cur, *, plan_cell_id, ordinal, state, error=None,
                  provider_called=True, terminal_at=None,
                  monitoring_result_id=None):
    """插一条终态 attempt。

    🔴 [工单 V3-C · C-4] terminal_at 可指定 —— 冻结面判据要摆出
       "cutoff 之前 / 之后"两种时刻。缺省仍是 NOW()(既有判据逐字不变)。
       扩既有 seeder 而不是另写一个:两个 seeder 会各自漂。
    """
    cur.execute(
        "INSERT INTO defgeo_monitoring_attempts "
        "(attempt_id, plan_cell_id, attempt_ordinal, run_authority_id, "
        " tenant_owner_user_id, brand_id, actual_provider, actual_model, "
        " actual_surface, actual_search_mode, request_hash, provider_called, "
        " terminal_state, error_code, terminal_at, monitoring_result_id, "
        " ledger_version) "
        "VALUES (md5(%s || %s), %s,%s,'ra',7,1,'doubao','m','s','sm','rh',%s,"
        " %s,%s,COALESCE(%s, NOW()),%s,'v1')",
        (plan_cell_id, str(ordinal), plan_cell_id, ordinal, provider_called,
         state, error, terminal_at, monitoring_result_id),
    )


class TestFullChainProduction:
    def test_cards_are_produced_from_the_attempt_ledger(self, cur):
        """全链:账本 → evidence → summary → 五卡。

        两格:一格首发 TIMEOUT 后 fallback 成功,一格纯 engine_error。
        """
        pc_ok, pc_err = "c" * 32 + "1" * 32, "c" * 32 + "2" * 32
        _seed_attempt(cur, plan_cell_id=pc_ok, ordinal=1,
                      state="engine_error", error="TIMEOUT")
        _seed_attempt(cur, plan_cell_id=pc_ok, ordinal=2, state="answered")
        _seed_attempt(cur, plan_cell_id=pc_err, ordinal=1,
                      state="engine_error", error="RATE_LIMITED")

        cards = CP.produce_cards(cur, plan_cell_ids=[pc_ok, pc_err])

        # MET-29:五卡**恰各一次**,顺序来自 registry。
        assert [c.key for c in cards] == list(REG.CARD_KEYS)
        s = cards[0].summary
        assert s.planned == 2
        assert s.valid == 1              # 只有 pc_ok 答上来了
        assert s.engineErrors == 1       # pc_err 终态是 engine_error
        assert s.attemptRecords == 3     # 真实调用数,不是格数
        assert s.attemptErrors == 2      # 两条带 error 的 attempt 都在
        assert s.terminal == 2

    def test_fallback_success_keeps_the_first_failure_in_the_counts(self, cur):
        """MON-10 在**呈现层**的下游:首发失败不能从汇总里消失。

        拆红:把 ``evidence_cells_from_ledger`` 的 attempts 收集改成只收
        canonical 那一条 —— ``attemptErrors`` 变 1,本判据立刻红。
        """
        pc = "d" * 64
        _seed_attempt(cur, plan_cell_id=pc, ordinal=1,
                      state="engine_error", error="TIMEOUT")
        _seed_attempt(cur, plan_cell_id=pc, ordinal=2, state="answered")
        evidence, attempts = CP.evidence_cells_from_ledger(
            cur, plan_cell_ids=[pc])
        assert [c.status for c in evidence] == ["answered"]
        assert len([a for a in attempts if a["error"]]) == 1

    def test_no_terminal_attempt_means_the_cell_is_not_yet_evidence(self, cur):
        """还在跑的格不进 evidence —— 但**仍在 planned 里**。

        这一条守的是「不得因平台失败或样本不足静默缩小计划分母」(§6.3)。
        """
        pc = "e" * 64
        cur.execute(
            "INSERT INTO defgeo_monitoring_attempts "
            "(attempt_id, plan_cell_id, attempt_ordinal, run_authority_id, "
            " tenant_owner_user_id, brand_id, actual_provider, actual_model, "
            " actual_surface, actual_search_mode, request_hash, ledger_version) "
            "VALUES (md5(%s), %s,1,'ra',7,1,'p','m','s','sm','rh','v1')",
            (pc, pc))
        cards = CP.produce_cards(cur, plan_cell_ids=[pc])
        s = cards[0].summary
        assert s.planned == 1
        assert s.terminal == 0
        assert cards[0].state == "no_conclusion"   # valid=0 ⇒ 必 no_conclusion

    def test_zero_valid_never_gets_a_level(self, cur):
        """MET-36:valid=0 ⇒ no_conclusion ⇒ 等级必须 unknown。"""
        cards = CP.produce_cards(cur, plan_cell_ids=["f" * 64])
        for c in cards:
            assert c.state == "no_conclusion"
            assert c.level_key == "unknown"

    def test_unsigned_policy_never_yields_a_score(self, cur):
        """MET-36 逐字:「未签 policy 不得 enrollment 对客 v2 terminal Presentation」。

        🔴 [包F ⑧ · 2026-08-24] 本判据**换向**了。一期它先断言
           ``REG.unsigned_policies()`` 非空(当时的事实),再验 producer 恒返
           unknown;那句前置断言里写着「它已被签了 ⇒ 需要重写这条判据并补
           真正的阈值判据」—— Owner 按 Z-6 签发之后就是那个时刻。

           前置断言**删掉**,但判据本体**加强**:MET-36 要守的不变式
           与"当前全局签没签"无关,而是「``policy_signed=False`` 这条路径上
           恒不出分」。所以现在直接打那条路径,并配一发反向对照
           (签发态 + 有出现率 ⇒ 真的出分)——
           两臂合起来才证明 ``policy_signed`` 这个入参真的在起作用;
           只有前一臂的话,把整个阈值算法删掉同样全绿。
        """
        pc = "0" * 64
        _seed_attempt(cur, plan_cell_id=pc, ordinal=1, state="answered")

        # 臂 A:未签 ⇒ 恒 unknown(与全局签发状态无关)
        unsigned_cards = CP.produce_cards(cur, plan_cell_ids=[pc],
                                          policy_signed=False)
        assert all(c.level_key == "unknown" for c in unsigned_cards), (
            "policy_signed=False 却出了分 —— 那是用一个没人签过的标准发承诺")

        # 臂 B:签发态 + 真出现率 ⇒ 必须出分(证明入参真的在起作用)
        from services.defensive_geo.presentation import level_policy as LP

        signed_level = CP.derive_level(
            "identity", summary=unsigned_cards[0].summary,
            policy_signed=True, rate=100.0)
        assert signed_level != "unknown", (
            "签发态 + 出现率 100% 仍然不出分 —— 阈值政策没接上,"
            "那么臂 A 的绿是「因为根本不会出分」,不是「因为闸在守」")
        assert signed_level == LP.level_for_rate(100.0), (
            "producer 自己算了一套等级,没走签发的阈值政策")


class TestProducerDoesNotFabricate:
    def test_producer_never_constructs_a_summary_by_hand(self):
        """G-3 的**结构性**证明:producer 里不许出现 ``SampleSummary(`` 直造。

        summary 必须由窗B 的 ``build_sample_summary`` 从三个源重建 ——
        自己 new 一个等于把守恒式绕过去了。
        拆红:在 card_producer 里写一句 ``_proj.SampleSummary(...)``。
        """
        tree = ast.parse(io.open(
            ROOT / "services/defensive_geo/monitoring/card_producer.py",
            encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in ("SampleSummary", "CardView"):
                    bad.append(name)
        assert not bad, f"producer 直造了 DTO {bad} —— G-3 明令禁止"

    def test_detector_would_catch_a_hand_built_summary(self):
        """探测器活性自证。"""
        tree = ast.parse("x = SampleSummary(planned=1)")
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "SampleSummary"]
        assert hits

    def test_card_keys_come_from_the_registry_not_a_hand_list(self):
        """分母 = 窗B registry。手抄的那份会在窗B 改卡时静默漂移。"""
        assert CP.card_keys() == REG.CARD_KEYS


class TestReportRouteWiring:
    def test_card_out_filter_drops_extra_keys_instead_of_500(self):
        """🔴 本仓**三次**生产事故的形态:``extra='forbid'`` + 多一个键 = 裸 500。

        producer payload 带 ``summary``(provider 面数据),CardOut 没声明它。
        路由按 alias 全集过滤 ⇒ 受控丢弃;直接 ``CardOut(**c)`` ⇒ 构造期抛。
        这条判据两边都验。
        """
        import api.defensive_geo_report_api as R

        payload = {
            "key": "identity", "question": "AI 认得我吗?", "ordinal": 1,
            "levelKey": "unknown", "levelLabel": "暂无结论", "levelTone": "muted",
            "state": "no_conclusion", "actions": ["retest_same_scope"],
            "summary": {"planned": 1},          # ← 多出来的那一个
        }
        # 直接展开:必须抛(证明 forbid 真的在)
        with pytest.raises(Exception):
            R.CardOut(**payload)
        # 过滤后:必须成功
        ok = R.CardOut(**{k: v for k, v in payload.items()
                          if k in R._CARD_OUT_ALIASES})
        assert ok.key == "identity"

    def test_card_out_alias_set_is_derived_not_hand_written(self):
        """alias 全集从模型机械导出 —— 手抄会在加字段那天悄悄漏掉新字段。"""
        import api.defensive_geo_report_api as R

        derived = {(f.alias or n) for n, f in R.CardOut.model_fields.items()}
        assert R._CARD_OUT_ALIASES == derived

    def test_report_route_ownership_is_window_a_helper_and_single_sourced(self):
        """🔴 IDOR 回归判据(原交付单 §10.1)—— 合流后重锚。

        窗A 在 ``dead9e69f`` 已把归属做成
        ``_require_diagnosis_ownership``(走现役 ``require_diagnosis_access``,
        typed 同形信封)。窗D 原来那份内联授权(``require_brand_access`` + 裸 404)
        **已撤** —— 两份并存是「同一谓词写两处 ⇒ 必有一处没人验」,
        而且两种 404 形状打架(裸 dict vs SafeError 信封)。

        所以本判据现在钉三件事:
          ① 授权**存在**且是窗A 那一个;
          ② 它排在**取数之前**;
          ③ 路由里**不再有**第二份归属谓词。
        拆红:把 ``_require_diagnosis_ownership(request, diagnosis_id)`` 那行删掉,
        或把 ``require_brand_access`` 加回路由体。
        """
        src = io.open(ROOT / "api" / "defensive_geo_report_api.py",
                      encoding="utf-8").read()

        # ① 存在
        call_at = src.find("_require_diagnosis_ownership(request, diagnosis_id)")
        assert call_at > 0, "报告呈现端点没有对象级授权(IDOR)"

        # ② 排在取数之前
        bind_at = src.find("binding = resolve_binding(cur")
        assert bind_at > call_at, (
            "授权检查排在 resolve_binding 之后 —— 那时数据已经读出来了")
        conn_at = src.find("conn = _db()")
        assert conn_at > call_at, "授权排在开库连接之后 —— 应当在取数之前"

        # ③ 单一来源:路由体里不许再有第二份归属谓词。
        #    定义处的 docstring 会提到它,所以只扫**路由体**那一段。
        route_body = src[call_at:]
        assert "require_brand_access(request" not in route_body, (
            "路由体里出现第二份归属谓词 —— 同一谓词写两处,必有一处没人验;"
            "而且两份的拒绝形状不同(裸 404 vs SafeError 信封)")

    def test_brand_id_query_no_longer_gates_authorization(self):
        """brand_id 查询**保留给卡片链**,但不再承担授权职责。

        它查不到时只能如实留白,**不许**再造一个 404 分支 ——
        那会变成第二个"对象存在性" oracle,与窗A 的同形信封打架。
        """
        src = io.open(ROOT / "api" / "defensive_geo_report_api.py",
                      encoding="utf-8").read()
        call_at = src.find("_require_diagnosis_ownership(request, diagnosis_id)")
        route_body = src[call_at:]
        assert "SELECT brand_id FROM diagnosis_records" in route_body, (
            "卡片链要用的 brand_id 查询被一起删掉了")
        assert "status_code=404" not in route_body, (
            "路由体里仍有裸 404 —— 拒绝形状必须只由窗A 的 _not_found 签发")

    def test_projection_failure_does_not_break_the_report(self):
        """§20.3:「report presentation 失败:保留上一个冻结 snapshot,
        不回退到实时拼装假报告」。

        结构锚:五卡那段必须包在 try/except 里且 except 分支把 cards 置空,
        不是让整个端点 500。
        """
        src = io.open(ROOT / "api" / "defensive_geo_report_api.py",
                      encoding="utf-8").read()
        assert "五卡投影失败,如实留白" in src
        assert "cards_payload = []" in src


# ══════════════════════════════════════════════════════════════════════════
# [工单 V3-C · C-4 · Codex 三审 P1-6] 冻结报告不许被迟到 retry 改写
# ══════════════════════════════════════════════════════════════════════════
# Codex 真 PG16 反例:同一份 report snapshot 初次投影是
#   attemptRecords=1 / response=0 / engineErrors=1 / no_conclusion,
# 插入一条 cutoff **之后**的 fallback answered attempt 后,**同一个 snapshot**
# 变成 attemptRecords=2 / response=1 / engineErrors=0 / ready。
#
# 也就是说:一份已经发给客户的报告,会在她背后自己变。她拿去对账时,
# 对不上的是我们。而 `card_producer.evidence_cells_from_ledger` 读的是
# 这些 plan 的**全部** attempt —— 没有任何 cutoff 约束。
class TestFrozenReportIsNotRewrittenByLateRetries:
    _PC = "f" * 32 + "9" * 32

    @staticmethod
    def _shape(cards):
        """把五卡压成可比对的形状 —— 比对的是**投影结果**,不是中间值。"""
        s = cards[0].summary
        return (s.planned, s.attemptRecords, s.attemptErrors,
                s.response, s.valid, s.engineErrors,
                tuple(c.state for c in cards))

    def test_a_late_attempt_does_not_change_the_frozen_cards(self, cur):
        """🔴 反例原样重放:同 snapshot、cutoff 之后来一条 answered。

        修前:同一份冻结报告的五卡从 no_conclusion 变成 ready。
        修后:**逐字不变**。
        """
        cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut,"
                    "       NOW() + INTERVAL '1 minute'  AS after_cut")
        t = cur.fetchone()

        _seed_attempt(cur, plan_cell_id=self._PC, ordinal=1,
                      state="engine_error", error="TIMEOUT",
                      terminal_at=t["before_cut"])
        frozen_before = self._shape(
            CP.produce_cards(cur, plan_cell_ids=[self._PC],
                             cutoff_at=t["cutoff"]))

        # 迟到的 fallback:真的落库了(下面那条判别力臂会证明它落进去了)
        _seed_attempt(cur, plan_cell_id=self._PC, ordinal=2,
                      state="answered", terminal_at=t["after_cut"])
        frozen_after = self._shape(
            CP.produce_cards(cur, plan_cell_ids=[self._PC],
                             cutoff_at=t["cutoff"]))

        assert frozen_after == frozen_before, (
            f"冻结报告被 cutoff 之后的 retry 改写了:\n"
            f"  冻结时 {frozen_before}\n  现在   {frozen_after}")

    def test_the_live_view_does_see_the_late_attempt(self, cur):
        """判别力自证:同一批数据、**不带 cutoff**(实时面)必须看得见它。

        没有这一条,上一条可以靠"迟到 attempt 根本没插进去"通过 ——
        那样测的是夹具没生效,不是 cutoff 生效了。
        """
        cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut,"
                    "       NOW() + INTERVAL '1 minute'  AS after_cut")
        t = cur.fetchone()
        pc = "e" * 32 + "8" * 32

        _seed_attempt(cur, plan_cell_id=pc, ordinal=1,
                      state="engine_error", error="TIMEOUT",
                      terminal_at=t["before_cut"])
        live_before = self._shape(CP.produce_cards(cur, plan_cell_ids=[pc]))
        _seed_attempt(cur, plan_cell_id=pc, ordinal=2, state="answered",
                      terminal_at=t["after_cut"])
        live_after = self._shape(CP.produce_cards(cur, plan_cell_ids=[pc]))

        assert live_after != live_before, (
            "实时面也看不见这条迟到 attempt —— 说明它压根没落库,"
            "上一条判据测的是夹具失效,不是 cutoff 生效")
        frozen = self._shape(
            CP.produce_cards(cur, plan_cell_ids=[pc], cutoff_at=t["cutoff"]))
        assert frozen == live_before, (
            f"同一批数据:冻结面 {frozen} 应当等于迟到之前的实时面 {live_before}")

    def test_an_attempt_still_in_flight_is_not_in_the_frozen_face(self, cur):
        """还没终态的 attempt(``terminal_at IS NULL``)不进冻结面。

        冻结那一刻它还不是事实。不排除的话,一份"冻结"报告里会含一条
        当时还在跑的调用 —— 而它后来是成是败,报告上看不出来。
        """
        cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut")
        t = cur.fetchone()
        pc = "d" * 32 + "7" * 32

        _seed_attempt(cur, plan_cell_id=pc, ordinal=1, state="engine_error",
                      error="TIMEOUT", terminal_at=t["before_cut"])
        cur.execute(
            "INSERT INTO defgeo_monitoring_attempts "
            "(attempt_id, plan_cell_id, attempt_ordinal, run_authority_id, "
            " tenant_owner_user_id, brand_id, actual_provider, actual_model, "
            " actual_surface, actual_search_mode, request_hash, provider_called, "
            " terminal_state, terminal_at, ledger_version) "
            "VALUES (md5(%s || 'inflight'), %s, 2,'ra',7,1,'doubao','m','s','sm',"
            " 'rh', TRUE, NULL, NULL, 'v1')", (pc, pc))

        frozen = self._shape(
            CP.produce_cards(cur, plan_cell_ids=[pc], cutoff_at=t["cutoff"]))
        assert frozen[1] == 1, (
            f"在飞 attempt 进了冻结面(attemptRecords={frozen[1]},应为 1)")


@pytest.fixture
def tx_cur(cur):
    """把 autocommit 的判据连接摆进**事务里**,跑完 ROLLBACK。

    🔴 为什么必须:``occurrence_rate`` 走 ``run_ledger_bridge.guarded()``,
       而它用 SAVEPOINT 隔离"现役表缺失"。autocommit 连接上 SAVEPOINT 建不出来
       ⇒ guarded 走跳过分支 ⇒ 出现率**恒返 None**,于是"绑没绑冻结
       raw_result_ids"这件事在本包测不到、判据恒绿(零判别力的绿)。
       生产上这条链跑在 ``get_connection()`` 的事务里,所以这样更像生产。
    """
    cur.execute("BEGIN")
    try:
        yield cur
    finally:
        cur.execute("ROLLBACK")


class TestOccurrenceRateIsBoundToTheFrozenRawResults:
    """出现率也必须绑冻结面 —— 它的分子分母都从 ``monitoring_results`` 取,
    而那张表在冻结之后还会长新行。"""

    @staticmethod
    def _seed_result(cur, *, detected: bool) -> int:
        cur.execute(
            "INSERT INTO monitoring_results (task_id, is_detected) "
            "VALUES (1, %s) RETURNING id", (1 if detected else 0,))
        return int(cur.fetchone()["id"])

    def test_only_the_frozen_raw_results_count(self, tx_cur):
        tx_cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut")
        t = tx_cur.fetchone()
        pc1, pc2 = "a" * 32 + "1" * 32, "a" * 32 + "2" * 32

        rid_hit = self._seed_result(tx_cur, detected=True)
        rid_miss = self._seed_result(tx_cur, detected=False)
        _seed_attempt(tx_cur, plan_cell_id=pc1, ordinal=1, state="answered",
                      terminal_at=t["before_cut"], monitoring_result_id=rid_hit)
        _seed_attempt(tx_cur, plan_cell_id=pc2, ordinal=1, state="answered",
                      terminal_at=t["before_cut"], monitoring_result_id=rid_miss)

        # 冻结时只认了命中的那一条 ⇒ 100%
        assert CP.occurrence_rate(
            tx_cur, plan_cell_ids=[pc1, pc2], cutoff_at=t["cutoff"],
            raw_result_ids=[rid_hit]) == 100.0
        # 两条都认 ⇒ 50%(判别力自证:绑定真的在起作用)
        assert CP.occurrence_rate(
            tx_cur, plan_cell_ids=[pc1, pc2], cutoff_at=t["cutoff"],
            raw_result_ids=[rid_hit, rid_miss]) == 50.0

    def test_an_empty_frozen_result_set_is_a_blank_not_a_zero(self, tx_cur):
        """``raw_result_ids == []`` ⇒ 返 ``None``(没测到),**不是** 0.0。

        §15.6:None 与 0 是不同事实。压成 0 会把留白讲成坏消息。
        """
        tx_cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut")
        t = tx_cur.fetchone()
        pc = "b" * 32 + "3" * 32
        rid = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="answered",
                      terminal_at=t["before_cut"], monitoring_result_id=rid)

        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc],
                                  cutoff_at=t["cutoff"],
                                  raw_result_ids=[]) is None
        # 反向自证:同一批数据在实时面上是有数的 —— 否则上面那条恒真
        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc]) == 100.0


# ══════════════════════════════════════════════════════════════════════════
# [工单 V4-C · C-2 · Codex fix-of-fix P1-3] summary 也要绑冻结 raw_result_ids
# ══════════════════════════════════════════════════════════════════════════
# 上一轮只有 occurrence_rate 绑了冻结结果集,而卡面上的 attemptRecords /
# response / valid —— 也就是那几个**计数** —— 仍由全部 attempt 决定。
# 于是"这份报告认哪些 raw result"只管住了一个百分比,管不住它旁边的数。
class TestSummaryIsBoundToTheFrozenRawResults:

    @staticmethod
    def _seed_result(cur, *, detected: bool) -> int:
        cur.execute(
            "INSERT INTO monitoring_results (task_id, is_detected) "
            "VALUES (1, %s) RETURNING id", (1 if detected else 0,))
        return int(cur.fetchone()["id"])

    def test_an_answered_attempt_outside_the_frozen_set_does_not_count(self, tx_cur):
        """窗内、cutoff 内、但**不在**冻结结果集里的 answered ⇒ 不进 summary。

        这是 Codex 点名的第二条真 PG:空/局部 raw_result_ids + 窗内 answered。
        """
        tx_cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                       "       NOW() - INTERVAL '10 minutes' AS before_cut")
        t = tx_cur.fetchone()
        pc = "5" * 32 + "a" * 32
        rid = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="answered",
                      terminal_at=t["before_cut"], monitoring_result_id=rid)

        # 认了它 ⇒ 这一格有终态、有回答
        bound = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                                 cutoff_at=t["cutoff"], raw_result_ids=[rid])
        assert bound[0].summary.response == 1, bound[0].summary
        assert bound[0].summary.attemptRecords == 1, bound[0].summary

        # 冻结时**没认**它 ⇒ 这一格在这份快照里不该有回答
        unbound = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                                   cutoff_at=t["cutoff"], raw_result_ids=[])
        assert unbound[0].summary.response == 0, (
            f"不在冻结结果集里的 answered 仍然进了 summary:{unbound[0].summary}")
        assert unbound[0].summary.attemptRecords == 0, unbound[0].summary
        assert unbound[0].summary.valid == 0, unbound[0].summary

    def test_engine_errors_are_not_filtered_away_by_the_frozen_set(self, tx_cur):
        """判别力 + 反向保护:``engine_error`` **不**受结果集约束。

        🔴 它们本来就没有 result 行。拿结果集去筛会把"平台报错"整类从卡上抹掉
           —— 客户看到的会变成"没测到问题",而真相是我们没拿到回答。
           §6.3 / MON-02 明令不许压平,所以这一条是上一条的**边界**。

        🔴 [工单 V5-C · C-1] 这条豁免的判据被订正过。上一版写的是
           「约束只打 answered,不打别的终态」—— 那句话把一个**推论**
           当成了规则,而它不成立:``entity_ambiguous`` 也带 result 行
           (``run_ledger_bridge.close_for_result`` 的 state_map 两条都写),
           于是"不打别的终态"这句话直接放它越过冻结边界。
           真正的规则是**带不带 raw result**:engine_error 没有 ⇒ 豁免;
           answered / entity_ambiguous 有 ⇒ 都受约束。
           本条断言的行为不变(engine_error 仍必须留下),变的是它的理由。
        """
        tx_cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                       "       NOW() - INTERVAL '10 minutes' AS before_cut")
        t = tx_cur.fetchone()
        pc = "5" * 32 + "b" * 32
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="engine_error",
                      error="RATE_LIMITED", terminal_at=t["before_cut"])

        cards = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                                 cutoff_at=t["cutoff"], raw_result_ids=[])
        assert cards[0].summary.engineErrors == 1, (
            f"平台报错被冻结结果集筛掉了 —— 那会把「我们没拿到回答」讲成"
            f"「没测到问题」:{cards[0].summary}")
        assert cards[0].summary.attemptRecords == 1, cards[0].summary


# ══════════════════════════════════════════════════════════════════════════
# [工单 V5-C · C-1 · Codex fix-of-fix2 P1-1] 冻结集约束的是**带 result 行的
# attempt**,不是"叫 answered 的那些"
# ══════════════════════════════════════════════════════════════════════════
# 上一版把边界写成「只筛 answered」。而 ``run_ledger_bridge.close_for_result``
# 的 state_map 是**两条**:
#
#     succeeded        → answered
#     pending_identity → entity_ambiguous
#
# 两条都调 ``_ledger.close_attempt(..., monitoring_result_id=...)``。
# 于是一条**不在**冻结结果集里的"身份待定"观测照样进已发报告,
# 而且和"在里面"长得完全一样(Codex 真 PG16 反例:``raw_result_ids=[]`` 时
# 仍得到 ``response=1 / identityAmbiguous=1 / attemptRecords=1``)。
#
# 🔴 规则与推论要分开:
#    规则 = 「这条 attempt 背后有没有一条 raw result」;
#    推论 = engine_error / policy_skipped 没有 result 行 ⇒ 不受约束。
#    上一版把推论当成规则写死,新终态或新写点一出现就静默漏过。
class TestTheFrozenSetBindsEveryAttemptCarryingAResult:

    @staticmethod
    def _seed_result(cur, *, detected: bool) -> int:
        cur.execute(
            "INSERT INTO monitoring_results (task_id, is_detected) "
            "VALUES (1, %s) RETURNING id", (1 if detected else 0,))
        return int(cur.fetchone()["id"])

    @staticmethod
    def _window(cur):
        cur.execute("SELECT NOW() - INTERVAL '5 minutes' AS cutoff,"
                    "       NOW() - INTERVAL '10 minutes' AS before_cut")
        return cur.fetchone()

    def test_an_ambiguous_result_outside_the_frozen_set_does_not_reach_the_report(
            self, tx_cur):
        """Codex 反例逐字:被排除的 ambiguous 结果不得进已发报告。"""
        t = self._window(tx_cur)
        pc = "6" * 32 + "a" * 32
        rid = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1,
                      state="entity_ambiguous", terminal_at=t["before_cut"],
                      monitoring_result_id=rid)

        s = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                             cutoff_at=t["cutoff"], raw_result_ids=[])[0].summary
        assert s.identityAmbiguous == 0, (
            f"不在冻结结果集里的「身份待定」观测进了已发报告:{s}")
        assert s.response == 0, s
        assert s.attemptRecords == 0, s

    def test_the_same_ambiguous_result_does_count_when_it_is_frozen_in(
            self, tx_cur):
        """对照臂 —— 冻结集里有它时**必须**出数,否则上一条恒真。"""
        t = self._window(tx_cur)
        pc = "6" * 32 + "b" * 32
        rid = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1,
                      state="entity_ambiguous", terminal_at=t["before_cut"],
                      monitoring_result_id=rid)

        s = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                             cutoff_at=t["cutoff"],
                             raw_result_ids=[rid])[0].summary
        assert s.identityAmbiguous == 1, s
        assert s.response == 1, s
        assert s.attemptRecords == 1, s

    def test_every_terminal_state_is_bound_exactly_when_it_carries_a_result(
            self, tx_cur):
        """终态 × 带不带 result 行的**全矩阵**。

        🔴 分母机械读 ``_ledger.TERMINAL_STATES``,不手抄 —— 手抄的那一份
           在加第五个终态时不会红,而那正是本缺陷的形态:规则被写成一份
           终态名单,名单没跟上写点。
        """
        from services.defensive_geo.monitoring import attempt_ledger as _ledger

        states = list(_ledger.TERMINAL_STATES)
        assert len(states) >= 2, (
            f"终态全集只有 {states} —— 分母塌了,下面的断言会恒真")

        t = self._window(tx_cur)
        with_result: dict[str, str] = {}     # state -> plan_cell_id
        without_result: dict[str, str] = {}
        frozen_ids: list[int] = []
        for i, state in enumerate(states):
            # 库层 CHECK:engine_error 必须带 code;policy_skipped 恒零 provider 调用。
            err = "RATE_LIMITED" if state == "engine_error" else None
            called = state != "policy_skipped"
            pc_with = f"7{i}".ljust(32, "a") + "1" * 32
            pc_without = f"7{i}".ljust(32, "b") + "2" * 32
            rid = self._seed_result(tx_cur, detected=True)
            frozen_ids.append(rid)
            _seed_attempt(tx_cur, plan_cell_id=pc_with, ordinal=1, state=state,
                          error=err, provider_called=called,
                          terminal_at=t["before_cut"], monitoring_result_id=rid)
            _seed_attempt(tx_cur, plan_cell_id=pc_without, ordinal=1, state=state,
                          error=err, provider_called=called,
                          terminal_at=t["before_cut"], monitoring_result_id=None)
            with_result[state] = pc_with
            without_result[state] = pc_without

        cells = sorted(list(with_result.values()) + list(without_result.values()))

        # ── 冻结集为空:带 result 行的那一半全部出局,另一半原样留下 ──────
        _, attempts = CP.evidence_cells_from_ledger(
            tx_cur, plan_cell_ids=cells, cutoff_at=t["cutoff"],
            raw_result_ids=[])
        kept = {a["planCellId"] for a in attempts}
        assert kept == set(without_result.values()), (
            "冻结集为空时留下的格与「没有 result 行」那一半对不上。"
            f"多留了 {sorted(kept - set(without_result.values()))};"
            f"少留了 {sorted(set(without_result.values()) - kept)}")

        # ── 判别力对照臂:冻结集含全部 result 时,一格都不该被筛掉 ────────
        _, all_in = CP.evidence_cells_from_ledger(
            tx_cur, plan_cell_ids=cells, cutoff_at=t["cutoff"],
            raw_result_ids=frozen_ids)
        assert {a["planCellId"] for a in all_in} == set(cells), (
            "冻结集含全部 result 时仍有格被筛掉 —— 过滤器把不该管的也管了")


# ══════════════════════════════════════════════════════════════════════════
# [工单 V5-C · C-2 · Codex fix-of-fix2 P1-2] 出现率按 **canonical answered
# cell** 计数,不按 attempt 行
# ══════════════════════════════════════════════════════════════════════════
# docstring 与 summary 都把分母定义为 canonical answered cell,而上一版 SQL
# 直接 ``COUNT(*) ... WHERE terminal_state='answered'`` 数 attempt 行。
# 同一格有两条 answered 时,出现率与 summary 用的是**两套口径** ——
# Codex 真 PG16 反例:1 格 2 条 answered、canonical(ordinal 1)命中,
# 正确是 100.0%,上一版给 50.0%,而 summary 同时显示 valid=1。
# 这不是一个百分比好不好看的问题:出现率是 Z-6 等级口径的唯一输入,
# 数错了客户拿到的就是错的等级。
class TestOccurrenceRateCountsCanonicalCellsNotAttemptRows:

    @staticmethod
    def _seed_result(cur, *, detected: bool) -> int:
        cur.execute(
            "INSERT INTO monitoring_results (task_id, is_detected) "
            "VALUES (1, %s) RETURNING id", (1 if detected else 0,))
        return int(cur.fetchone()["id"])

    def test_two_answered_attempts_in_one_cell_count_once(self, tx_cur):
        """Codex 反例逐字:canonical 命中 ⇒ 100.0%,且 summary ``valid=1``。"""
        pc = "8" * 32 + "a" * 32
        rid_hit = self._seed_result(tx_cur, detected=True)
        rid_miss = self._seed_result(tx_cur, detected=False)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="answered",
                      monitoring_result_id=rid_hit)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=2, state="answered",
                      monitoring_result_id=rid_miss)

        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc]) == 100.0, (
            "出现率数的是 attempt 行不是 canonical 格 —— 同一格重试两次"
            "就把自己的出现率对半砍")
        # 口径统一:出现率的分母必须与 summary.valid 是同一件事。
        s = CP.produce_cards(tx_cur, plan_cell_ids=[pc])[0].summary
        assert s.valid == 1, s

    def test_the_non_canonical_attempt_does_not_get_a_vote(self, tx_cur):
        """反向臂:canonical 未命中、非 canonical 命中 ⇒ **0.0%**。

        没有这一条,上面那条可以被"两条都算、恰好都命中"蒙混过去。
        """
        pc = "8" * 32 + "b" * 32
        rid_miss = self._seed_result(tx_cur, detected=False)
        rid_hit = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="answered",
                      monitoring_result_id=rid_miss)   # canonical(序号最小)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=2, state="answered",
                      monitoring_result_id=rid_hit)

        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc]) == 0.0, (
            "非 canonical 的那条 attempt 也投了票 —— 多重试几次就能把"
            "没出现改写成出现了")

    def test_the_frozen_set_is_applied_before_canonical_selection(self, tx_cur):
        """冻结 × canonical 的**定义行为**:先按冻结集裁,再选 canonical。

        🔴 先写清再钉:冻结集回答的是「这份快照当时认哪些观测」。
           没被认下的那条,对这份快照而言**不存在** —— 因此它也不该参与择优。
           反过来(先选 canonical 再筛)会得到"这一格没有终态",
           把一条**明明被冻结认下了**的观测讲成留白。

        本例:ordinal 1 命中、ordinal 2 未命中,冻结集只含 ordinal 2 那条。
        裁完只剩 ordinal 2 ⇒ 它成为 canonical ⇒ 0.0% 且 valid=1。
        """
        pc = "8" * 32 + "c" * 32
        rid_canon = self._seed_result(tx_cur, detected=True)
        rid_other = self._seed_result(tx_cur, detected=False)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=1, state="answered",
                      monitoring_result_id=rid_canon)
        _seed_attempt(tx_cur, plan_cell_id=pc, ordinal=2, state="answered",
                      monitoring_result_id=rid_other)

        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc],
                                  raw_result_ids=[rid_other]) == 0.0
        s = CP.produce_cards(tx_cur, plan_cell_ids=[pc],
                             raw_result_ids=[rid_other])[0].summary
        assert s.valid == 1, s
        assert s.attemptRecords == 1, s
        # 对照臂:冻结集含 canonical 那条 ⇒ 回到 100.0%
        assert CP.occurrence_rate(tx_cur, plan_cell_ids=[pc],
                                  raw_result_ids=[rid_canon]) == 100.0

    def test_produce_cards_reads_the_ledger_once_per_cell(self, tx_cur,
                                                          monkeypatch):
        """收口不许以性能倒退为代价:一次 ``produce_cards`` 里,账本**每格只读一遍**。

        🔴 evidence 与出现率现在共用同一把裁剪刀。若两条链各自再查一次账本,
           一份报告的账本往返次数就翻倍 —— 而且没有任何判据会红。
           分母 = 本次传进去的**去重格数**,不是手写的数字。
        """
        from services.defensive_geo.monitoring import attempt_ledger as _ledger

        pcs = ["9" * 32 + "a" * 32, "9" * 32 + "b" * 32]
        rid = self._seed_result(tx_cur, detected=True)
        _seed_attempt(tx_cur, plan_cell_id=pcs[0], ordinal=1, state="answered",
                      monitoring_result_id=rid)
        _seed_attempt(tx_cur, plan_cell_id=pcs[1], ordinal=1,
                      state="engine_error", error="TIMEOUT")

        calls: list[str] = []
        real = _ledger.attempts_for_cell

        def counting(cur, *, plan_cell_id, **kw):
            calls.append(plan_cell_id)
            return real(cur, plan_cell_id=plan_cell_id, **kw)

        monkeypatch.setattr(_ledger, "attempts_for_cell", counting)
        # 同一个格传两遍:分母是**去重后**的格数。
        CP.produce_cards(tx_cur, plan_cell_ids=pcs + [pcs[0]])

        expected = len(set(pcs))
        assert len(calls) == expected, (
            f"账本被读了 {len(calls)} 次,{expected} 个格 —— "
            f"evidence 与出现率各查了一遍(实际读的格:{calls})")

    def test_the_canonical_rule_is_not_copied_into_sql(self):
        """canonical 规则**单点**:出现率不许在 SQL 里另抄一份择优。

        🔴 分母 = ``card_producer`` 里**每一个** ``cur.execute(...)`` 的第一个
           实参的源码原文(``ast.get_source_segment``,f-string 各段一起拿到)。
           不用"含 SELECT 的字符串常量"当分母 —— f-string 会把一条 SQL 拆成
           好几个 Constant,``'answered'`` 那一段里根本没有 ``SELECT``,
           于是那种写法的锁在**本缺陷现场恒绿**(这是本条第一版的真实形态)。

        🔴 同时钉住"SQL 只能是字面量":允许用变量传 SQL 的话,规则可以
           在别处拼好再送进来,上面那个分母就漏了。

        🔴 分母的作用域说清楚:是**本模块全部** ``.execute(`` 调用点,
           不是"只看 occurrence_rate 那一处"。所以新加一条查询也自动进分母。
           判据**不断言个数**(计数式判据会过期,集合式不会);
           当前枚举到几处会在失败信息里打出来,交付文里记的是同一个数。
        """
        import ast as _ast

        from services.defensive_geo.monitoring import attempt_ledger as _ledger

        path = (ROOT / "services" / "defensive_geo" / "monitoring" /
                "card_producer.py")
        src = io.open(path, encoding="utf-8", newline="").read()
        tree = _ast.parse(src)
        executes = [
            n for n in _ast.walk(tree)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "execute" and n.args
        ]
        where = [f"line {n.lineno}" for n in executes]
        assert executes, "card_producer 里一条 execute 都没有 —— 分母塌了"

        banned = sorted(_ledger._SELECTION_RANK)   # 择优序里的终态名
        for call in executes:
            arg = call.args[0]
            assert isinstance(arg, (_ast.Constant, _ast.JoinedStr)), (
                f"SQL 不是字面量(line {call.lineno})—— 规则可以在别处拼好"
                f"再送进来,本条的分母(本模块 {len(executes)} 处 execute:"
                f"{where})就漏了")
            seg = _ast.get_source_segment(src, arg) or ""
            upper = seg.upper()
            assert not ("ORDER BY" in upper and "ATTEMPT_ORDINAL" in upper), (
                f"SQL 里出现了按 attempt_ordinal 的择优 —— 那是 canonical "
                f"规则的第二份实现(line {call.lineno},本模块共 "
                f"{len(executes)} 处 execute:{where}):{seg}")
            hits = [s for s in banned if s in seg]
            assert not hits, (
                f"SQL 里写死了终态名 {hits} —— 择优必须单点来自 "
                f"attempt_ledger.canonical_attempt,不许在这里另抄一份"
                f"(line {call.lineno},本模块共 {len(executes)} 处 execute:"
                f"{where}):{seg}")

        # 判别力自证:同一把尺子量"抄了一份"的形态必须红。
        fake = ("f\"\"\"SELECT * FROM {T} WHERE a.terminal_state = 'answered'"
                " ORDER BY a.attempt_ordinal\"\"\"")
        assert [s for s in banned if s in fake]
        assert "ORDER BY" in fake.upper() and "ATTEMPT_ORDINAL" in fake.upper()
