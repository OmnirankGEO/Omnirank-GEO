"""诊断关键词的派生阶梯(订正二十六 · Review §20 · 2026-09-05)。

**这些判据是从 `test_empty_keywords_derivation.py` 搬过来的**,因为派生本身
从 DTO 搬到了 `start_diagnosis`(DTO 无库,而派生要读 `client_profiles.seed_keywords`)。
🔴 搬家最容易出的事是**旧址留空壳、新址没进任何分母** —— 两边都没人跑,
   变异恒存活。所以本文件末尾有一条 `test_the_old_site_left_no_hollow_shell`。

⑤b 之前表单从品牌档案预填 keywords 提交;之后前端一字不发。
上一版我只补 `[brand_name]` —— 而 keywords 有**三个 LLM 输入**消费点
(`diagnosis_workflow.py:652 / :716 / :1729`),有档案词的品牌会从
「策划过的词表」缩成一个品牌名。这是 ⑤b 单独引入的**质量回退**。
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib
import types

import pytest

# 🔴🔴 `server` 一进来就重放迁移、刷几百行日志,**会压垮 pytest 的 capture**
#    (`ValueError: I/O operation on closed file`)。这件事有三种表现,一种比一种坏:
#      ① 放在测试函数体内导入 ⇒ 单文件跑时**那条判据红**(看得见);
#      ② 放在模块级裸导入 ⇒ **收集期**就炸,pytest 中断收集,
#         同包另外 4 个文件 **50 条判据静默离开分母**(看不见,而且全绿);
#      ③ 导入时压住日志 ⇒ 正常。
#    ② 比 ① **严格更坏** —— 我一度把 ① 改成 ②,以为是修好了。
#    **一条红换成分母缩水,不是修复。**
import logging as _logging

_logging.disable(_logging.CRITICAL)
try:
    from server import DiagnosisRequest, start_diagnosis
finally:
    _logging.disable(_logging.NOTSET)
from services import diagnosis_keyword_source as KS

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Cur:
    """只回答 `seed_keywords` 的假 cursor。"""

    def __init__(self, seed=None, boom=False):
        self._seed, self._boom, self.calls = seed, boom, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._boom:
            raise RuntimeError("档案表读不到")

    def fetchone(self):
        # 🔴 生产已改读 diagnosis_records.keywords —— 夹具的键要跟着走。
        #    夹具回答一个生产不再问的列名,所有阶梯判据会一起变成「查不到」。
        return None if self._seed is None else {"keywords": self._seed}


def _resolve(seed=None, given=None, brand_id=7, brand_name="喜茶", industry="茶饮", boom=False):
    return KS.resolve_keywords(_Cur(seed, boom), given=given, brand_id=brand_id,
                               brand_name=brand_name, industry=industry)


# ══════════════════════════════════════════════════════════════════════════
# 三档正样本
# ══════════════════════════════════════════════════════════════════════════

def test_profile_keywords_win_over_the_brand_name():
    """🔴 [Review §20 反臂] 档案有词时**不得**被品牌名覆盖 —— 这正是回退本身。"""
    assert _resolve(seed=["奶茶加盟", "茶饮品牌"]) == ["奶茶加盟", "茶饮品牌"]


def test_falls_back_to_brand_name_then_industry():
    assert _resolve(seed=[]) == ["喜茶"]
    assert _resolve(seed=[], brand_name="") == ["茶饮"]


def test_all_three_sources_empty_is_rejected():
    """三源皆空 ⇒ 拒。放行等于收了钱跑一次零搜索词的诊断。"""
    with pytest.raises(KS.NoKeywordSource):
        _resolve(seed=[], brand_name="", industry="")


def test_supplied_keywords_are_never_overwritten():
    """派生只补空,**不夺权** —— 她自己给了词就用她的,哪怕档案里有别的。"""
    assert _resolve(seed=["档案词"], given=["她自己的词"]) == ["她自己的词"]


# ══════════════════════════════════════════════════════════════════════════
# 档案词绕过了 DTO 校验 ⇒ 必须过同一解的上限
# ══════════════════════════════════════════════════════════════════════════

def test_twenty_one_profile_keywords_clamp_to_twenty(caplog):
    """🔴 [Review §20] 档案词是在 `validate_keywords` **之后**赋进去的,绕过 20/50。

    超限**不 422**:档案词不是这次提交填的,拒了她没法改这次提交来救。
    """
    with caplog.at_level(logging.WARNING):
        got = _resolve(seed=["kw%d" % i for i in range(21)])
    assert len(got) == KS.MAX_KEYWORDS == 20
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "截断没留 WARN"


def test_overlong_profile_keyword_is_dropped_not_fatal(caplog):
    with caplog.at_level(logging.WARNING):
        got = _resolve(seed=["x" * 51, "正常词"])
    assert got == ["正常词"]
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "丢弃没留 WARN"


def test_profile_read_failure_falls_back_instead_of_exploding():
    """档案读不到 ⇒ 回落品牌名(不中断对话),但要留 WARN。"""
    assert _resolve(boom=True) == ["喜茶"]


def test_the_limits_have_exactly_one_definition():
    """🔴 20/50 只许有一处定义 —— DTO 校验与档案词收敛必须同解。

    两处各写一遍,漂了不会有任何东西报错:
    DTO 放行 20 条而档案路径截到 15,或反过来,都无人发现。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "validate_keywords":
            continue
        seg = ast.unparse(n)
        assert "MAX_KEYWORDS" in seg and "MAX_KEYWORD_CHARS" in seg, (
            "validate_keywords 没从唯一定义处取上限:%s" % seg[:200])
        for lit in ("> 20", "> 50"):
            assert lit not in seg, "validate_keywords 里还留着字面量 %r" % lit
        return
    pytest.fail("找不到 validate_keywords —— 分母塌了,不是通过")


# ══════════════════════════════════════════════════════════════════════════
# 接线 + 搬家没留空壳
# ══════════════════════════════════════════════════════════════════════════

def _server_src() -> str:
    return (ROOT / "server.py").read_text(encoding="utf-8")


def test_the_derivation_result_is_assigned_to_request_keywords():
    """🔴 接线锁 —— **赋值目标同一性**,不是「有没有这个调用」。

    实测:毒把 `request.keywords = _resolve_kw(...)` 改成
    `_unused_kw = _resolve_kw(...)` —— 调用**仍在**,上一版照绿,
    而真正被两个消费点吃掉的 `request.keywords` 根本没被赋值。
    这是同一个病今天的第四张脸(短路形 / 旁路形 / 常量右值 / 换赋值目标)。
    """
    for n in ast.walk(ast.parse(_server_src())):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "start_diagnosis":
            continue
        ok = []
        for a in ast.walk(n):
            if not isinstance(a, ast.Assign):
                continue
            tgt_ok = any(isinstance(tg, ast.Attribute) and tg.attr == "keywords"
                         and isinstance(tg.value, ast.Name) and tg.value.id == "request"
                         for tg in a.targets)
            val_ok = (isinstance(a.value, ast.Call)
                      and getattr(a.value.func, "id", None) == "_resolve_kw")
            if tgt_ok and val_ok:
                ok.append(ast.unparse(a))
        assert len(ok) == 1, (
            "恰需 1 处 `request.keywords = _resolve_kw(...)`,实得 %d 处" % len(ok))
        return
    pytest.fail("找不到 start_diagnosis")


def test_the_derivation_precedes_both_consumers():
    """🔴 派生必须在**两个消费点之前**:落库 与 spawn workflow。

    在它们之后派生,落库记的是空、workflow 跑的是派生值 ——
    事后说不清那次诊断按什么词跑的,而且不报错。
    """
    src = _server_src()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "start_diagnosis":
            body = ast.unparse(n)
            at_derive = body.index("_resolve_kw(")
            for marker, why in (("json.dumps(request.keywords", "落库"),
                                ("run_diagnosis_task(", "spawn workflow")):
                assert marker in body, "消费点 %s 不见了 —— 分母塌了" % why
                assert at_derive < body.index(marker), "派生排在了%s之后" % why
            return
    pytest.fail("找不到 start_diagnosis")


def test_the_old_site_left_no_hollow_shell():
    """🔴 搬家锁:DTO 上**不许**再留一份派生。

    判据搬走却在旧址留个非空壳、新址又没进任何分母,两边都没人跑 ——
    这是我记过账的坑,所以正着反着各钉一次。
    """
    tree = ast.parse(_server_src())
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "DiagnosisRequest":
            names = [f.name for f in n.body
                     if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
            assert "_derive_keywords_when_empty" not in names, (
                "DTO 上还留着派生 —— 两处派生,迟早一处看到档案词另一处看到品牌名")
            return
    pytest.fail("找不到 DiagnosisRequest")

def test_derived_keywords_actually_reach_a_non_empty_search():
    """🔴 [Review §16 正臂 · 随派生一起搬] 不是「看 :752 插了一项」,是**驱到实际入参**。

    派生从 DTO 搬走之后,这条也必须跟着搬 —— 留在旧址它驱动的是一个
    已经不再派生的对象,会**恒红**;删掉又丢覆盖。搬,不是删。

    同时钉住一格既有缺陷:brand_name 空白时 `insert(0, brand_name)` 会把
    **空串**塞进搜索词第 0 位 —— 一个空白搜索词等于白搜一次。
    """
    from workflows.diagnosis_workflow import build_social_search_keywords

    for seed, brand, industry in [(["档案词"], "喜茶", "茶饮"),
                                  ([], "喜茶", "茶饮"),
                                  ([], "", "茶饮")]:
        kws = _resolve(seed=seed, brand_name=brand, industry=industry)
        terms = build_social_search_keywords(kws[:5], brand)
        assert terms, "seed=%r brand=%r 派生后搜索词仍为空" % (seed, brand)
        blanks = [x for x in terms if not str(x).strip()]
        assert not blanks, "搜索词里有空白项 %r —— 等于白搜一次" % terms


# ══════════════════════════════════════════════════════════════════════════
# 打真库:假 cursor 证不了「这个列真的存在」
# ══════════════════════════════════════════════════════════════════════════

def test_the_real_query_actually_finds_seeded_keywords():
    """🔴 打真库,且**驱动生产函数** —— 上一版这条自己写了一遍 SQL。

    实测:把生产查询改成 `client_profiles`(那张表没这列)时它**存活** ——
    因为毒改的是生产那一行,而判据跑的是自己手写的那一行。
    **判据自构中间值 = 与自己对话**;它必须让被测代码去跑那条线。

    这条尺子是唯一能分开「取对了表」与「取错了但被 try/except 咽掉」的:
    两者都返回 [] 并静默回落品牌名,在假 cursor 下完全同形。
    """
    import os

    import psycopg2
    import psycopg2.extras

    dsn = os.getenv("TEST_DATABASE_URL")
    assert dsn, "没有 TEST_DATABASE_URL —— 记「未评估」,不是通过"
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO brands (name) VALUES ('C4夹具品牌') RETURNING id")
            bid = cur.fetchone()["id"]
            cur.execute(
                # 必填列取自 information_schema(机械枚举),不是我猜的:
                #   brand_name / industry / session_id 三个 NOT NULL 无默认。
                "INSERT INTO diagnosis_records "
                "(brand_id, brand_name, industry, session_id, keywords, total_score) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (bid, "C4夹具品牌", "测试行业", "c4-fixture-session",
                 json.dumps(["上次的词A", "上次的词B"]), 60))
            got = KS.latest_published_diagnosis_keywords(cur, brand_id=bid)
    finally:
        conn.rollback()
        conn.close()
    assert got == ["上次的词A", "上次的词B"], (
        "生产查询没取到刚写进去的词(实得 %r)—— 取错表/列被 try 咽掉时正是这个读数" % got)


def test_the_predicate_is_shared_with_the_prefill_endpoint():
    """🔴 「哪次诊断算已发布」这条谓词只许有一份。

    预填端点(`api_get_brand_latest_diagnosis_params`)与派生各写一份的话,
    哪天口径改了,她在表单里看到的词与真正跑诊断用的词会**不一样**,且不报错。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    def _norm(x):
        return " ".join(x.split())
    assert _norm(KS.LATEST_PUBLISHED_WHERE) in _norm(src), (
        "端点里的谓词与 LATEST_PUBLISHED_WHERE 逐字不一致 —— 两套口径")


def test_last_published_diagnosis_keywords_win():
    """[Review §28 正样本] 有上次诊断 ⇒ 派生 == 上次那批词(复诊逐值等价)。"""
    assert _resolve(seed=["上次A", "上次B"]) == ["上次A", "上次B"]


# ══════════════════════════════════════════════════════════════════════════
# 终态锁:提交侧三源皆空必须是 422,不是「抛了个什么」
# ══════════════════════════════════════════════════════════════════════════

def test_submit_with_no_keyword_source_is_422_not_500():
    """🔴 [Review 订正] 锁的是**终态码**,不是「抛异常」。

    上面 `test_all_three_sources_empty_is_rejected` 在**阶梯层**断言 raises,
    但阶梯抛出来之后端点怎么处置它,那条锁一无所知 ——
    Review 实测:把 `except _NoKwSrc` 整个删掉(三源皆空变成 **500**),
    endpoint / ladder / derivation **三个文件 0 红**。
    `pytest.raises(Exception)` 会把 500 一起放过,这正是「布尔中继/宽判据」那一格:
    **500 和 422 对她是两件完全不同的事** —— 一个是「我们坏了」,
    一个是「你少填了东西,而且钱没动」。

    派生排在价格闸的 `_ppm` **之前**(实测位置 524 < 1385),所以给个占位
    `price_preview_id` 就能驱到这一格,不必造一个合法的价格哈希。
    """
    import asyncio

    from fastapi import HTTPException

    req = DiagnosisRequest(brand_name="", industry="", keywords=[],
                           price_preview_id="placeholder-not-checked-yet")
    http = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": 7, "is_admin": False}))
    with pytest.raises(HTTPException) as ex:
        asyncio.run(start_diagnosis(req, http))
    assert ex.value.status_code == 422, (
        "三源皆空返回了 %s —— 500 是「我们坏了」,422 才是「你少填了东西」"
        % ex.value.status_code)
    assert "没有扣除任何算力" in str(ex.value.detail), "拒绝时没说清钱没动"
