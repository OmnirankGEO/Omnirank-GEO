"""飞轮全景「行业口径」+ 缓存 single-flight 的接线锁。

工单:`WO_FLYWHEEL_PANORAMA_INDUSTRY_SCOPE_2026-08-16`(P2 口径误导)
背景:页面顶部有行业选择器,而 `flywheel-panorama` 端点从建立起就不接行业参数 ——
     选「汽车」五个数仍是全站总数(生产实测 汽车 17,948 vs 全站 239,298)。

🔴 判据成对(每条"必须命中"都配了"必须不命中",锁能被拆红):
  - 正向:选行业 → 有行业列的三环真过滤,且用的是**别名全集**(中文名 + 英文 key);
  - 反向:选「全部/general/空」→ SQL 与加过滤前**逐字相同**(不许把全量口径改坏);
  - 判别夹具:用 汽车 vs 全站 这对天然可判别的数(不是两边都为 0 的空夹具);
  - 不硬造:没有行业维度的两环(review/apply)恒标 industry_scope="site",数值不动;
  - degraded 行为不被本改动触碰(31 班刚修好的)。
"""
from __future__ import annotations

import threading
import time

import pytest

import services.flywheel_panorama as pano
from writing.flywheel_cache import SCOPE_PANORAMA, clear_all, get_or_compute, make_key

# ============================ 夹具:可判别的假库 ============================
# 🔴 天然可判别:汽车 17,948 vs 全站 239,298。两个数不同,所以"过滤有没有生效"这件事
#    在结果里看得见 —— 如果夹具两边都返回同一个数,全绿也证明不了任何东西。
SITE_TOTALS = {
    "geo_research_raw": 239298,
    "geo_research_source_signals": 107904,
    "writing_article_version_fingerprints": 4120,
}
AUTO_TOTALS = {
    "geo_research_raw": 17948,
    "geo_research_source_signals": 9239,
    "writing_article_version_fingerprints": 311,
}
SITE_CITED = 91125
AUTO_CITED = 6002


class _SQLRecorder:
    """记下每条 SQL 与参数,并按「有没有 WHERE ... = ANY(%s)」返回行业数/全站数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def scalar(self, sql: str, params: tuple = ()) -> int:
        self.calls.append((sql, params))
        scoped = "ANY(%s)" in sql
        if "is_answer_cited" in sql:
            return AUTO_CITED if scoped else SITE_CITED
        table = next((t for t in SITE_TOTALS if t in sql), "")
        if not table:
            return 0
        return (AUTO_TOTALS if scoped else SITE_TOTALS)[table]

    def sql_for(self, table: str, *, cited: bool = False) -> str:
        for sql, _ in self.calls:
            if table in sql and (("is_answer_cited" in sql) == cited):
                return sql
        raise AssertionError(f"没有任何 SQL 打到 {table}(cited={cited});实际={[c[0] for c in self.calls]}")

    def params_for(self, table: str) -> tuple:
        for sql, params in self.calls:
            if table in sql and "is_answer_cited" not in sql:
                return params
        raise AssertionError(f"没有 {table} 的调用")


@pytest.fixture()
def rec(monkeypatch: pytest.MonkeyPatch) -> _SQLRecorder:
    r = _SQLRecorder()
    monkeypatch.setattr(pano, "_scalar", r.scalar)
    monkeypatch.setattr(pano, "_scalar_opt", r.scalar)
    # 这两环走 style_control / 绑定候选扫描,与本工单无关 → 钉死成常量,避免测试碰真库
    monkeypatch.setattr(pano, "_pending_reviews", lambda: {"binding_candidates": 4, "writing_drafts": 5})
    monkeypatch.setattr(pano, "_active_writing_versions", lambda: 0)
    clear_all()
    return r


def _nodes(payload: dict) -> dict[str, dict]:
    return {n["key"]: n for n in payload["nodes"]}


# ============================ 正向:选行业 → 真过滤 ============================
def test_selecting_industry_filters_the_three_sources_that_have_an_industry_column(rec: _SQLRecorder) -> None:
    out = pano.get_flywheel_panorama("汽车")
    n = _nodes(out)

    # 判别夹具:三个有行业列的环拿到的是**行业数**,不是全站数
    assert n["collect"]["value"] == 17948, "采集环没按行业过滤(还是全站 239,298 = 本工单要修的 bug)"
    assert n["learn"]["value"] == 9239, "学习环没按行业过滤"
    assert n["outcome"]["value"] == 311, "效果回流环没按行业过滤"
    # 必须不命中:三个环都不许还等于全站数
    assert n["collect"]["value"] != SITE_TOTALS["geo_research_raw"]
    assert n["learn"]["value"] != SITE_TOTALS["geo_research_source_signals"]
    assert n["outcome"]["value"] != SITE_TOTALS["writing_article_version_fingerprints"]

    assert out["industry_scoped"] is True
    assert out["industry_key"] == "汽车"


def test_filter_uses_alias_superset_because_the_two_tables_speak_different_vocabularies(rec: _SQLRecorder) -> None:
    """🔴 raw 存中文「汽车」,shadow 表存英文 key「auto」——只拿一个字符串两边比会静默查出 0 行。"""
    pano.get_flywheel_panorama("汽车")
    vals = rec.params_for("geo_research_raw")[0]
    assert "汽车" in vals, "别名全集里缺中文名 → geo_research_raw 会查出 0 行"
    assert "auto" in vals, "别名全集里缺英文 key → geo_research_source_signals 会查出 0 行"
    # 两张表必须用**同一份**别名全集,否则口径不可比
    assert rec.params_for("geo_research_source_signals")[0] == vals
    assert rec.params_for("writing_article_version_fingerprints")[0] == vals


def test_english_key_and_chinese_name_resolve_to_the_same_scope(rec: _SQLRecorder) -> None:
    """选择器传 `auto` 还是 `汽车`,结果必须一致 —— 否则同一个行业两套数。"""
    a = pano.get_flywheel_panorama("汽车")
    b = pano.get_flywheel_panorama("auto")
    assert _nodes(a)["collect"]["value"] == _nodes(b)["collect"]["value"]
    assert set(a["industry_values"]) == set(b["industry_values"])


# ============================ 反向:全站口径逐字不变 ============================
@pytest.mark.parametrize("scope", ["", "general", "all", "通用", "全部行业", "通用/全部行业", None])
def test_all_industry_scope_keeps_the_exact_pre_change_sql(rec: _SQLRecorder, scope) -> None:
    """🔴 反向判据:全站口径下 SQL 必须与加行业过滤**之前逐字相同**,不许把全量口径改坏。"""
    out = pano.get_flywheel_panorama(scope)

    assert rec.sql_for("geo_research_raw") == "SELECT COUNT(*) FROM geo_research_raw"
    assert rec.sql_for("geo_research_source_signals") == "SELECT COUNT(*) FROM geo_research_source_signals"
    assert (
        rec.sql_for("writing_article_version_fingerprints")
        == "SELECT COUNT(*) FROM writing_article_version_fingerprints"
    )
    assert (
        rec.sql_for("geo_research_raw", cited=True)
        == "SELECT COUNT(*) FROM geo_research_raw WHERE is_answer_cited = TRUE"
    )
    # 必须不命中:全站口径下一个 ANY(%s) 都不许出现
    assert all("ANY(%s)" not in sql for sql, _ in rec.calls), "全站口径下混进了行业过滤"

    n = _nodes(out)
    assert n["collect"]["value"] == SITE_TOTALS["geo_research_raw"] == 239298
    assert out["industry_scoped"] is False
    assert out["site_scope_nodes"] == []


# ============================ 不硬造:没有行业维度的环如实标注 ============================
def test_sources_without_an_industry_column_are_labelled_site_scope_not_faked(rec: _SQLRecorder) -> None:
    """人工把关 / 应用到写作在库里没有行业维度 —— 不硬造过滤(硬造 = 新的误导),只如实标。"""
    out = pano.get_flywheel_panorama("汽车")
    n = _nodes(out)
    assert n["review"]["industry_scope"] == "site"
    assert n["apply"]["industry_scope"] == "site"
    assert out["site_scope_nodes"] == ["review", "apply"]
    # 必须不命中:这两环的数值不许因为选了行业就变(它们本来就没有行业维度)
    site = pano.get_flywheel_panorama("")
    assert n["review"]["value"] == _nodes(site)["review"]["value"] == 9
    assert n["apply"]["value"] == _nodes(site)["apply"]["value"] == 0


def test_industry_scoped_nodes_carry_industry_scope_label(rec: _SQLRecorder) -> None:
    n = _nodes(pano.get_flywheel_panorama("汽车"))
    for key in ("collect", "learn", "outcome"):
        assert n[key]["industry_scope"] == "industry", f"{key} 环该标 industry 却标了 {n[key]['industry_scope']}"
    # 全站口径时这三环标 site(因为确实是全站数),前端据此不显示「全站」角标
    n2 = _nodes(pano.get_flywheel_panorama(""))
    for key in ("collect", "learn", "outcome"):
        assert n2[key]["industry_scope"] == "site"


def test_citation_rate_is_computed_within_the_same_scope(rec: _SQLRecorder) -> None:
    """引用率的分子分母必须同口径 —— 分子按行业、分母全站 = 造出一个假比率。"""
    out = pano.get_flywheel_panorama("汽车")
    assert out["citation_rate"] == round(AUTO_CITED / AUTO_TOTALS["geo_research_raw"], 4)
    site = pano.get_flywheel_panorama("")
    assert site["citation_rate"] == round(SITE_CITED / SITE_TOTALS["geo_research_raw"], 4)
    assert out["citation_rate"] != site["citation_rate"]


# ============================ degraded 不被触碰(31 班刚修好的) ============================
def test_degraded_behaviour_is_untouched_by_the_industry_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """取数失败仍要标 degraded —— 「算不出来」绝不能再被伪装成「真的是 0」。

    🔴 必须在 **cur.execute 这一层**造失败,不能让 get_connection() 抛:
       `_scalar()` 的 `conn = get_connection()` 在 try 之外,连接本身挂掉是**上抛**不是 fail-soft
       (既有行为,本包未改动)。在错的那一层造失败,测的就不是 degraded 这条路。
    """
    monkeypatch.setattr(pano, "_pending_reviews", lambda: {"binding_candidates": 0, "writing_drafts": 0})
    monkeypatch.setattr(pano, "_active_writing_versions", lambda: 0)

    class _DeadCursor:
        def execute(self, sql, params=None):
            raise RuntimeError("relation does not exist")

        def fetchone(self):  # pragma: no cover - execute 先抛,到不了这里
            return None

    class _DeadConn:
        def cursor(self):
            return _DeadCursor()

        def close(self):
            pass

    monkeypatch.setattr(pano, "get_connection", _DeadConn)
    for scope in ("", "汽车"):
        out = pano.get_flywheel_panorama(scope)
        assert out["degraded"] is True, f"scope={scope} 取数全失败却没标 degraded"
        assert out["degraded_reasons"], "degraded 为真但没给原因"
        # 数值仍是 0(下游按 int 消费),但 degraded 告诉前端这些 0 不可信
        assert _nodes(out)["collect"]["value"] == 0


def test_healthy_path_is_not_marked_degraded(rec: _SQLRecorder) -> None:
    """必须不命中:一切正常时绝不能误标 degraded(否则前端恒显示「数据加载失败」)。"""
    for scope in ("", "汽车"):
        out = pano.get_flywheel_panorama(scope)
        assert out["degraded"] is False, f"scope={scope} 正常取数却被标成 degraded"
        assert out["degraded_reasons"] == []


# ============================ 缓存 key 必须带行业 ============================
def test_cache_key_is_partitioned_by_industry(rec: _SQLRecorder) -> None:
    """🔴 key 不带行业 = 第一个行业的结果被后续所有行业命中 = 换个马甲的同一个 bug。"""
    k_all = make_key(SCOPE_PANORAMA, "all")
    k_auto = make_key(SCOPE_PANORAMA, "汽车")
    assert k_all != k_auto

    a = get_or_compute(k_all, 60.0, lambda: pano.get_flywheel_panorama(None))
    b = get_or_compute(k_auto, 60.0, lambda: pano.get_flywheel_panorama("汽车"))
    assert _nodes(a)["collect"]["value"] == 239298
    assert _nodes(b)["collect"]["value"] == 17948, "行业 key 命中了全站缓存"


# ============================ single-flight(504 治理) ============================
def test_single_flight_collapses_concurrent_misses_on_the_same_key() -> None:
    """同 key 并发只算一次 —— 这是把冷加载并发从 9.35s 压到 3.87s 的那一层。"""
    clear_all()
    calls: list[int] = []
    started = threading.Event()

    def slow():
        calls.append(1)
        started.set()
        time.sleep(0.6)
        return {"v": 1}

    outs: list[dict] = []
    threads = [threading.Thread(target=lambda: outs.append(get_or_compute("sf:same", 60.0, slow))) for _ in range(6)]
    for t in threads:
        t.start()
        started.wait(1.0)  # 保证领跑者先进入 compute,后到者才是真的"后到"
    for t in threads:
        t.join(10)

    assert len(calls) == 1, f"同 key 并发算了 {len(calls)} 次(single-flight 没生效)"
    assert all(o == {"v": 1} for o in outs), "后到者拿到的值与领跑者不一致"


def test_single_flight_does_not_collapse_different_keys() -> None:
    """必须不命中:不同 key 之间绝不能互相等待/串味。"""
    clear_all()
    calls: list[str] = []

    def make(tag: str):
        def f():
            calls.append(tag)
            time.sleep(0.2)
            return {"tag": tag}

        return f

    outs: dict[str, dict] = {}
    threads = [
        threading.Thread(target=lambda t=t: outs.__setitem__(t, get_or_compute(f"sf:{t}", 60.0, make(t))))
        for t in ("a", "b", "c")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert sorted(calls) == ["a", "b", "c"], "不同 key 被错误合流"
    assert outs == {"a": {"tag": "a"}, "b": {"tag": "b"}, "c": {"tag": "c"}}


def test_leader_failure_does_not_strand_waiters() -> None:
    """🔴 领跑者抛异常时后到者必须被放行并自己算 —— 否则一个坏 compute 会把整页钉死。"""
    clear_all()
    state = {"n": 0}
    gate = threading.Event()

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            gate.set()
            time.sleep(0.3)
            raise RuntimeError("leader boom")
        return {"ok": True}

    leader_err: list[BaseException] = []

    def lead():
        try:
            get_or_compute("sf:flaky", 60.0, flaky)
        except BaseException as e:  # noqa: BLE001
            leader_err.append(e)

    out: list[dict] = []
    t1 = threading.Thread(target=lead)
    t1.start()
    gate.wait(2.0)
    t2 = threading.Thread(target=lambda: out.append(get_or_compute("sf:flaky", 60.0, flaky)))
    t2.start()
    t1.join(10)
    t2.join(30)

    assert leader_err and isinstance(leader_err[0], RuntimeError), "领跑者的异常必须照常上抛给调用方 fail-soft"
    assert out == [{"ok": True}], "领跑者失败后,后到者没能自己算出结果(被钉死了)"
