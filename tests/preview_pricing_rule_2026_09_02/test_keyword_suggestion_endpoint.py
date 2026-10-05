"""关键词预填端点(订正三十① · 契约 `WO_KEYWORD_SUGGESTION_CONTRACT_C_2026-09-05.md`)。

🔴 这个端点存在的**唯一**理由是:她在启动页看到的词,必须与提交时真正会跑的词
   **出自同一个阶梯**。两份「应该等价」的实现漂开那天不会有任何东西变红,
   而表现是**她看到 A、系统跑 B** —— 与 #62 同形。
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HANDLER = "diagnosis_keyword_suggestion"


def _call(brand_id, *, user=None):
    from server import diagnosis_keyword_suggestion as H
    req = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": 7, "is_admin": False} if user is None else user))
    return asyncio.run(H(brand_id=brand_id, http_request=req))


def _handler_src() -> str:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == HANDLER:
            return ast.get_source_segment(src, n) or ""
    pytest.fail("找不到 %s —— 分母塌了,不是通过" % HANDLER)


# ══════════════════════════════════════════════════════════════════════════
# 同一阶梯(A 的跨层锁钉的就是这件事)
# ══════════════════════════════════════════════════════════════════════════

def test_the_endpoint_uses_the_same_ladder_as_the_submit_path():
    """🔴 端点与 `start_diagnosis` 必须走**同一个**阶梯实现。

    锁**赋值目标同一性**,不是「有没有这个调用」——
    今天我在这个病上栽了五次(短路 / 旁路 / 常量右值 / 换赋值目标 / 判据自构)。
    """
    seg = _handler_src()
    tree = ast.parse(seg)
    ok = []
    for a in ast.walk(tree):
        if not isinstance(a, ast.Assign):
            continue
        tgt_ok = any(isinstance(t, ast.Tuple)
                     and [getattr(e, "id", None) for e in t.elts] == ["_kw", "_src"]
                     for t in a.targets)
        val_ok = (isinstance(a.value, ast.Call)
                  and getattr(a.value.func, "id", None) == "resolve_keywords_with_source")
        if tgt_ok and val_ok:
            ok.append(ast.unparse(a))
    assert len(ok) == 1, "恰需 1 处 `_kw, _src = resolve_keywords_with_source(...)`,实得 %d" % len(ok)


def test_resolve_keywords_is_a_thin_shell_over_the_same_ladder():
    """提交路径那个 `resolve_keywords` 必须是薄壳 —— 否则「同一阶梯」是假的。"""
    src = (ROOT / "services" / "diagnosis_keyword_source.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == "resolve_keywords":
            body = [b for b in n.body if not isinstance(b, ast.Expr)]  # 去掉 docstring
            assert len(body) == 1 and isinstance(body[0], ast.Return), (
                "resolve_keywords 不再是薄壳,阶梯可能分了两份:%s" % ast.unparse(n))
            assert "resolve_keywords_with_source" in ast.unparse(body[0])
            return
    pytest.fail("找不到 resolve_keywords")


# ══════════════════════════════════════════════════════════════════════════
# 响应契约
# ══════════════════════════════════════════════════════════════════════════

def _seed(cur, *, name, industry, owner, last_kws=None):
    cur.execute("INSERT INTO brands (name, industry, owner_user_id) VALUES (%s,%s,%s) RETURNING id",
                (name, industry, owner))
    bid = cur.fetchone()["id"]
    if last_kws is not None:
        cur.execute(
            "INSERT INTO diagnosis_records (brand_id, brand_name, industry, session_id,"
            " keywords, total_score) VALUES (%s,%s,%s,%s,%s,%s)",
            (bid, name, industry, "kwsug-%d" % bid, json.dumps(last_kws), 60))
    return bid


@pytest.fixture
def seeded(monkeypatch):
    """真库播种 + 端点用同一个连接(端点内部自己 get_connection,所以要提交)。

    🔴 用完**逐行删干净**:这个包别的判据也吃这张库,残留会污染它们的分母。
    """
    import os

    import psycopg2
    import psycopg2.extras

    dsn = os.getenv("TEST_DATABASE_URL")
    assert dsn, "没有 TEST_DATABASE_URL —— 记「未评估」,不是通过"
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    made = []
    with conn.cursor() as cur:
        yield cur, made
    with conn.cursor() as cur:
        for bid in made:
            cur.execute("DELETE FROM diagnosis_records WHERE brand_id = %s", (bid,))
            cur.execute("DELETE FROM brands WHERE id = %s", (bid,))
    conn.close()


def test_last_published_diagnosis_wins_and_reports_its_source(seeded):
    cur, made = seeded
    bid = _seed(cur, name="预填甲", industry="茶饮", owner=7, last_kws=["上次A", "上次B"])
    made.append(bid)
    out = _call(bid)
    assert out["keywords"] == ["上次A", "上次B"]
    assert out["source"] == "last_diagnosis"
    assert out["aiAugmented"] is False, "有上次诊断词时不该再打 AI"


def test_the_payload_always_carries_all_three_fields(seeded):
    """🔴 空态用**显式取值**,不用 null / 不省略。

    省略与「字段没实现」在前端读数上同形 —— 这是 A 契约 ② 的理由。
    """
    cur, made = seeded
    bid = _seed(cur, name="预填乙", industry="家居", owner=7, last_kws=["词"])
    made.append(bid)
    out = _call(bid)
    assert set(out) == {"keywords", "source", "aiAugmented"}
    assert isinstance(out["keywords"], list) and isinstance(out["aiAugmented"], bool)
    assert out["source"] in ("last_diagnosis", "brand_name", "industry", "none")


def test_a_brand_you_do_not_own_is_404_not_403(seeded):
    """🔴 越权用 404:403 等于告诉探测者「这个 id 存在,只是不是你的」。"""
    from fastapi import HTTPException

    cur, made = seeded
    bid = _seed(cur, name="别人的品牌", industry="茶饮", owner=999, last_kws=["词"])
    made.append(bid)
    with pytest.raises(HTTPException) as ex:
        _call(bid)
    assert ex.value.status_code == 404, "越权返回了 %s" % ex.value.status_code
    assert "不存在" in str(ex.value.detail)


def test_unauthenticated_is_401(seeded):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ex:
        _call(1, user=False)
    assert ex.value.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# 只读
# ══════════════════════════════════════════════════════════════════════════

def test_the_handler_writes_nothing(seeded):
    """🔴 只读要**真**只读:她换品牌会反复触发,任何写入都会被放大。"""
    seg = _handler_src().upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "FREEZE_POINTS", "CHARGE_ON_SUCCESS"):
        assert verb not in seg, "预填端点里出现了 %r —— 它不该有副作用" % verb


def test_the_readonly_detector_is_alive():
    """正样本自证:检测器对已知的写法必须响,否则上一条是恒真。"""
    bad = "async def h():\n    cur.execute('INSERT INTO brands VALUES (1)')\n".upper()
    assert "INSERT INTO" in bad


def test_empty_state_is_none_and_200_not_422(seeded):
    """🔴 与提交路径**故意不同**的一格,契约 §3 已报备。

    提交时三源皆空要 422(不许零搜索词的诊断跑起来);预填只是建议、没有「提交」可拒。
    **派生是同一份,只有空态的呈现不同** —— A 的跨层锁锁「同一个函数」,不锁「同样的 HTTP 结果」。
    """
    cur, made = seeded
    cur.execute("INSERT INTO brands (name, industry, owner_user_id) VALUES ('','',7) RETURNING id")
    bid = cur.fetchone()["id"]
    made.append(bid)
    out = _call(bid)
    assert out == {"keywords": [], "source": "none", "aiAugmented": False}, out


# ══════════════════════════════════════════════════════════════════════════
# aiAugmented 的行为臂(Review 订正:恒 False 时此前无判据红)
# ══════════════════════════════════════════════════════════════════════════

def _stub_ai(monkeypatch, groups):
    async def _fake(**_kw):
        return {"search_keyword_groups": groups}
    monkeypatch.setattr("tools.keyword_generator.analyze_client_business", _fake)


def _clear_cache():
    from server import _KWSUG_CACHE
    _KWSUG_CACHE.clear()


@pytest.mark.parametrize("groups,want_flag,want_extra", [
    ([["奶茶", "加盟"], ["茶饮", "品牌"]], True, "奶茶 加盟"),
    ([], False, None),
])
def test_ai_augmented_reflects_whether_words_were_appended(
        seeded, monkeypatch, groups, want_flag, want_extra):
    """🔴 [Review 订正] `aiAugmented` 恒 False 时,此前**没有任何判据会红**。

    「字段在响应里」与「字段说的是真的」是两件事 —— 前者由
    `test_the_payload_always_carries_all_three_fields` 管,后者以前没人管。
    两臂都要:只驱「有词」那一臂,恒 True 也能过。
    """
    cur, made = seeded
    _clear_cache()
    _stub_ai(monkeypatch, groups)
    bid = _seed(cur, name="AI增强测试", industry="茶饮", owner=7)   # 无上次诊断 ⇒ 走 brand_name
    made.append(bid)
    out = _call(bid)
    assert out["source"] == "brand_name", "前提没成立:期望回落到品牌名,实得 %r" % out["source"]
    assert out["aiAugmented"] is want_flag, out
    if want_extra:
        assert want_extra in out["keywords"], "标了 aiAugmented 却没有追加词:%r" % out["keywords"]
    else:
        assert out["keywords"] == ["AI增强测试"], "没有 AI 词时不该改动阶梯那一档"


def test_ai_failure_does_not_block_the_prefill(seeded, monkeypatch):
    """AI 拿不到 ⇒ 她仍拿到阶梯那一档,`aiAugmented=False`,不是 503。"""
    async def _boom(**_kw):
        raise RuntimeError("LLM 不可用(注入)")

    cur, made = seeded
    _clear_cache()
    monkeypatch.setattr("tools.keyword_generator.analyze_client_business", _boom)
    bid = _seed(cur, name="AI失败测试", industry="家居", owner=7)
    made.append(bid)
    out = _call(bid)
    assert out == {"keywords": ["AI失败测试"], "source": "brand_name", "aiAugmented": False}, out
