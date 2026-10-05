"""#145 · 出题提议每条自带 `side`,不让下游按顺序猜侧别。

## 改之前

`POST /api/diagnosis/suggest-questions` 的 full 模式返的是
`g_qs[:4] + d_qs[:4]` —— 一个**扁平字符串列表**,没有侧别标记。
下游只能按位置推:「前 4 条是增长题,后 4 条是防守题」。

## 🔴 订正工单的失效机理(我核过)

工单说「顺序一变增长题会被标成防守题」。按**当前实现**这推不出来:
`g_qs[:4] + d_qs[:4]` 要凑够 8 条,**必须两侧各取满 4**;任何其他组合总数都 < 8。
所以下游那条「恰 8 条才对半切」的保守判定,**今天是安全的**。

真正的问题不是「现在会标错」,而是它依赖一个
**没人声明、也没人锁**的不变量:「总数 8 ⟺ 两侧各 4」。
哪天有人加去重、换拼接顺序、或缓存里躺着旧形状 ——
增长题会被标成防守题,**而屏幕上一切正常**。

⇒ 修法不是去诊断那根轴,是**消灭它**:让 side 从产出的那一刻就带上,
   `questions` 从 `candidates` 一处派生,两者不可能不一致。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _client():
    """真 router + `response_model`;中间件塞一个登录用户。

    🔴 必须走**真出口**:#139 的教训 —— 判据在响应层之下时,
       `response_model` 的校验失败(500)完全看不见。
    """
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
def fake_generator(monkeypatch):
    """把生成器换成可控桩:两侧各返回可区分的题面。

    只替换**外部输入**(LLM 出题),被测的是端点怎么给它们打标签。
    """
    import tools.keyword_generator as kg

    async def _fake(**kw):
        framing = kw.get("question_framing")
        tag = "DEF" if framing == "brand_directed" else "GROW"
        return {"real_user_questions": ["%s-%d" % (tag, i) for i in range(1, 7)]}

    monkeypatch.setattr(kg, "analyze_client_business", _fake)

    import db.diagnosis_db as ddb
    monkeypatch.setattr(ddb, "get_brand_by_id",
                        lambda bid: {"name": "测试品牌", "industry": "测试行业",
                                     "cities": "深圳"})
    import server
    server._SUGGEST_CACHE.clear()          # 缓存会跨用例带旧形状进来
    return True


def _post(client, mode, scope="s-%d"):
    return client.post("/api/diagnosis/suggest-questions",
                       json={"brand_id": 1, "mode": mode,
                             "business_scope": scope % id(client)})


def test_full_mode_labels_each_candidate_with_its_side(fake_generator):
    """🔴 本单那一格:full 模式每条候选都带 side,且与生成臂一致。

    断言打**真 HTTP 响应**,不打服务函数返回值。
    """
    resp = _post(_client(), "full")
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    cands = body["candidates"]
    assert len(cands) == 8, "full 模式应各取 4:%r" % [c["question"] for c in cands]
    for c in cands:
        assert c["side"] in ("growth", "defensive"), c
        # 桩把侧别写进了题面 ⇒ 标签必须与**产出它的那一臂**一致,
        # 而不是与它在列表里的位置一致。
        expect = "growth" if c["question"].startswith("GROW") else "defensive"
        assert c["side"] == expect, (
            "这条题来自 %s 臂,却被标成 %s:%r" % (expect, c["side"], c))


def test_questions_is_derived_from_candidates_not_built_twice(fake_generator):
    """`questions` 必须与 `candidates` 逐条同序 —— 一处派生。

    两处各拼一遍的话,可以出现「列表里是 A 的题、标签是 B 的侧别」,
    而两者各自看都正常。
    """
    body = _post(_client(), "full").json()
    assert body["questions"] == [c["question"] for c in body["candidates"]]


@pytest.mark.parametrize("mode,expect", [("growth", "growth"),
                                         ("defensive", "defensive")])
def test_single_sided_modes_label_every_candidate(fake_generator, mode, expect):
    """单侧模式:每条都是那一侧(side 与 mode 语义同源)。

    只证 full 模式不够 —— 单侧模式标错同样会让下游把题归到另一边。
    """
    body = _post(_client(), mode).json()
    assert body["candidates"], "单侧模式返了空 —— 分母塌了,不是通过"
    assert {c["side"] for c in body["candidates"]} == {expect}


def test_the_response_model_actually_declares_side():
    """结构臂:`side` 必须在**响应模型**里,不只是字典里恰好有这个键。

    🔴 只断言响应体里有 `side`,证不了它被声明过 ——
       没声明的键会被 `response_model` 悄悄丢掉(或让整个响应 500)。
    """
    import server

    fields = server.SuggestedQuestion.model_fields
    assert "side" in fields and "question" in fields, sorted(fields)
    ann = str(fields["side"].annotation)
    assert "growth" in ann and "defensive" in ann, (
        "side 不是受限取值 —— 拼错的侧别会被原样放行:%s" % ann)


def test_the_endpoint_declares_the_response_model():
    """端点必须挂 `response_model` —— 否则上面那条结构臂守的是一个没人用的模型。"""
    src = io.open(ROOT / "server.py", encoding="utf-8").read()
    i = src.index('@app.post("/api/diagnosis/suggest-questions"')
    head = src[i:i + 200]
    assert "response_model=SuggestQuestionsResponse" in head, head[:160]


def test_the_side_is_not_inferred_from_position():
    """🔴 数据流锁:`side` 不许由**位置**算出来。

    形如 `"growth" if i < 4 else "defensive"` 的写法在顺利路径上与正确实现
    读数完全相同 —— 行为臂分不出来(这正是本单要消灭的那根轴)。
    所以这条打源码:两个侧别必须各自出现在**自己那一臂**的推导式里,
    而不是出现在一个按下标分支的表达式里。
    """
    src = io.open(ROOT / "server.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "suggest_diagnosis_questions")
    body = ast.unparse(fn)

    # 🔴 [#149 · 2026-09-08] 候选构造抽成了 `_cand(q, side, types)`(要同时带 layer),
    #    side 从此是**传进去的实参**而不是 dict 字面量里的值。
    #    本条钉的性质一个字没变(side 来自产出它的那一臂,不是列表位置),
    #    所以锚跟着形状走:两个取值必须各自作为**常量**出现在候选构造处 ——
    #    dict 字面量的 'side' 值,**或**传给构造 helper 的实参。
    #    只认旧形状的话,一次无关重构就会让这条从「有牙」变成「恒红」。
    produced = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "side"
                        and isinstance(v, ast.Constant)):
                    produced.add(v.value)
        if isinstance(node, ast.Call):
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value in ("growth", "defensive"):
                    produced.add(a.value)
    assert {"growth", "defensive"} <= produced, (
        "两个侧别没有各自作为常量出现在候选构造处 —— side 可能又是算出来的:%r"
        % sorted(produced))

    # 反向:不许出现按下标决定侧别的三元(上面那条挡不住
    # `"growth" if i < 4 else "defensive"` —— 它同样含两个常量)。
    for bad in ("if i < 4", "if idx < 4", "[:4] else", "index < 4"):
        assert bad not in body, "side 又按位置算了:%r" % bad
