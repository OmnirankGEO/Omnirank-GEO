"""#147-B 约束 3 · 预览是**页面加载**路径:取词失败也绝不 500。

接上关键词阶梯之后多了一条新的失败路径:品牌名、行业、上次已发布诊断
**三源皆空**时,`resolve_keywords_with_source` 会抛 `NoKeywordSource`。
提交路径那边抛是对的(不许零搜索词的诊断跑起来),但预览只是建议、
没有「提交」可拒 —— 抛出去就是**整个页面打不开**。

🔴 断言必须打**真 HTTP 出口**:#139 的教训 —— 判据在响应层之下时,
   `response_model` 的校验失败(500)完全看不见。新加的 `keywordSource`
   字段如果没在模型里声明,就正是那种炸法。
"""

from __future__ import annotations

import pytest


def _client():
    """真 router + `response_model`;中间件塞一个登录用户。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": 1, "username": "u1", "is_admin": True}
        return await call_next(request)

    app.post("/api/diagnosis/suggest-questions",
             response_model=server.SuggestQuestionsResponse)(
        server.suggest_diagnosis_questions)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stubs(monkeypatch):
    """只替换**外部输入**:品牌行、生成器、库读、飞轮。被测的是端点的处置。"""
    import db.diagnosis_db as ddb
    import services.diagnosis_business_scope as bs
    import services.diagnosis_generator_inputs as gi
    import tools.keyword_generator as kg
    import server

    async def _fake(**kw):
        return {"real_user_questions": ["Q%d" % i for i in range(1, 7)]}

    async def _none():
        return None

    monkeypatch.setattr(kg, "analyze_client_business", _fake)
    monkeypatch.setattr(ddb, "get_brand_by_id",
                        lambda bid: {"name": "", "industry": "", "cities": ""})
    monkeypatch.setattr(bs, "resolve_brand_cities", lambda bid: "")
    monkeypatch.setattr(gi, "_flywheel_material", lambda *a, **k: _none())
    server._SUGGEST_CACHE.clear()
    return True


def test_no_keyword_source_still_returns_200(stubs, monkeypatch):
    """🔴 本单那一格:阶梯抛 `NoKeywordSource` ⇒ 端点仍 200。

    毒:把 helper 里 `except NoKeywordSource` 那一格去掉 ⇒ 本条红(500)。
    """
    import db.connection as dbc
    import services.diagnosis_keyword_source as ks

    # 🔴 **不打桩 `_resolve_keywords`** —— 被测的正是它的 except 分支。
    #    只把它的两个外部依赖换掉:连接(不打真库)与最里层的阶梯(改成抛)。
    class _Conn:
        def cursor(self):
            return object()

        def close(self):
            pass

    def _raise(cur, **kw):
        raise ks.NoKeywordSource("三源皆空")

    monkeypatch.setattr(dbc, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(ks, "resolve_keywords_with_source", _raise)

    resp = _client().post("/api/diagnosis/suggest-questions",
                          json={"brand_id": 1, "mode": "growth",
                                "business_scope": "regional"})
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert body["keywordSource"] == "none", body
    # 取不到词**不影响出题**:题仍然回来了。
    assert body["questions"], "取词失败把出题也一起弄没了"
    assert body["source"] == "ai"
    # 🔴 `fallbackReason` 的含义是「为什么没拿到题」—— 题拿到了就必须是 None。
    #    取词失败不许写进这个键,否则运维分不出该查哪一头。
    assert body["fallbackReason"] is None, body


def test_the_response_model_declares_keyword_source(stubs):
    """结构臂:`keywordSource` 必须在**响应模型**里。

    没声明的键会被 `response_model` 悄悄丢掉 —— 上一条的断言就成了空转。
    """
    import server
    assert "keywordSource" in server.SuggestQuestionsResponse.model_fields


def test_a_healthy_ladder_reports_its_rung(stubs, monkeypatch):
    """正样本臂:阶梯正常时报出**具体那一级**,证上面不是恒返 none。"""
    import services.diagnosis_generator_inputs as gi
    monkeypatch.setattr(gi, "_resolve_keywords",
                        lambda *a, **k: (["词A"], "last_diagnosis"))
    body = _client().post("/api/diagnosis/suggest-questions",
                          json={"brand_id": 1, "mode": "growth",
                                "business_scope": "regional"}).json()
    assert body["keywordSource"] == "last_diagnosis", body


def test_the_generator_actually_receives_the_brand_side_inputs(stubs, monkeypatch):
    """🔴 行为臂:预览端喂给生成器的**不再是**空词/无地域的「瞎猜」配置。

    这是本单的用户可见目的 —— 结构锁证「从同一处取」,这条证「取到了东西」。
    """
    import services.diagnosis_generator_inputs as gi
    import tools.keyword_generator as kg

    seen = {}

    async def _capture(**kw):
        seen.update(kw)
        return {"real_user_questions": ["Q1"]}

    monkeypatch.setattr(kg, "analyze_client_business", _capture)
    monkeypatch.setattr(gi, "_resolve_keywords",
                        lambda *a, **k: (["真词"], "brand_name"))

    _client().post("/api/diagnosis/suggest-questions",
                   json={"brand_id": 1, "mode": "growth",
                         "business_scope": "regional"})
    assert seen.get("keywords") == ["真词"], seen
    assert "client_location" in seen, "地域没传 —— 还是那个瞎猜配置"
    assert "flywheel_material" in seen, "飞轮素材这一项没传"
