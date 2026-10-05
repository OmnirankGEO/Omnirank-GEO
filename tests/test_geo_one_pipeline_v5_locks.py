"""SSOT 锁 · 一条流水线 v5(2026-08-06 · 生产堵塞排查包)

本批全部来自**生产实证**,不是推演。三条堵塞:

  W1  `brands` 表没有 `brand_name` 这一列 —— 四个调用点全写的 `.get("brand_name")`,
      `SELECT *` 出来的 dict 里没这个键,`or ""` 静默吞成空串。
      上一包加了「产出不点名客户就判废」之后,图文链 **100% 倒在
      `brand_name_missing`**(生产 post 15/16 为证)。
  W2  `llm_unavailable` 是个假标签 —— 生产 post 17 对应的 `llm_call_log`
      记的是 `success=t / output_tokens=15999`(天花板 16000),
      HTTP 200、模型真跑了,只是推理把额度吃光、正文为空。
      用户看到「写作服务暂时不可用,稍后重试」→ 重试 → 再撞顶。
  W3  「先看看标题」整步删除;「AI 蒸馏选题」三缺陷(灰按钮不给理由 /
      只有一行不动的字 / 切 tab 结果丢失但钱已扣)。

写锁的规矩(本仓踩过的坑,逐条对应):
  · 每条"必须命中"都要配一条**必须不命中**;
  · 判据不打在源码串上就尽量不打(打了就写清为什么这里只能打串);
  · 断言不许跟着一条恒真的条件走(v3 那条 `if len(kwonlyargs)==1` 就是这样废掉的)。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "api" / "geo_douyin_api.py"
GEN = ROOT / "services" / "geo_douyin" / "content_generator.py"
DISTILLER = ROOT / "services" / "geo_douyin" / "topic_distiller.py"
DISTILL_TASK = ROOT / "services" / "geo_douyin" / "distill_task.py"
DDB = ROOT / "db" / "geo_douyin_db.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23,09-08 #150 §4)。
# 读它的格改指现役 /writing/image-note 的后继文件、按后继内容重判;随整页消失的格原位退役,
# 登记在 tests/RETIRED_TESTS.txt(接替者写在那里)。
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 选词 / 蒸馏 / 开始制作
TOPICS = WRITING / "imageNoteTopics.ts"             # 按钮文案 / 进度的纯函数
PRODUCTION = WRITING / "imageNoteProduction.ts"     # 下单载荷


def _tsx(p: pathlib.Path) -> str:
    """剥注释再断言 —— 本项目同一个坑踩过五次以上。"""
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"^\s*//.*$", " ", s, flags=re.M)
    return s


def _fn(path: pathlib.Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _code(path: pathlib.Path, name: str) -> str:
    """函数源码,剥掉 docstring —— 否则判据会被注释/文档字符串命中。"""
    fn = _fn(path, name)
    body = list(getattr(fn, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


# ═══════════════════════════════════════════════════════════════
# W1 · 客户名称:列名必须是真的
# ═══════════════════════════════════════════════════════════════

def _brands_columns() -> set:
    """真库里 `brands` 的列名。拿不到库就 skip —— **不退回读仓库 DDL**。

    🔴 为什么不读 DDL:本仓两份 DDL 都不描述生产(记忆里有账)。
       拿一份不对的清单去核,会得出"列名没问题"的假绿。
    """
    import os
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("没有 TEST_DATABASE_URL —— 列名核验需要真库,不猜")
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"连不上测试库:{str(e)[:60]}")
    try:
        cur = conn.cursor()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'brands'")
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def test_brand_row_columns_all_exist_in_the_real_table():
    """🔴 `_BRAND_ROW_COLUMNS` 里的每一个都必须是 brands 的真列。

    这是本批最贵那个 bug 的正锁:把 `brand_name` 写进这一组会当场红。
    """
    from api.geo_douyin_api import _BRAND_ROW_COLUMNS

    cols = _brands_columns()
    assert cols, "读不到 brands 列名,断言无意义"
    missing = [c for c in _BRAND_ROW_COLUMNS if c not in cols]
    assert not missing, f"这些列在 brands 表里根本不存在:{missing}"


def test_mapped_keys_are_deliberately_not_columns():
    """🔴 反向对照 + 分组自证:`_BRAND_MAPPED_KEYS` 必须**不是**真列。

    两组分开不是洁癖:`brand_name` 是别处映射过的 dict 才有的键,
    把它塞进 `_BRAND_ROW_COLUMNS` 会让上一条锁恒红;
    而它要是哪天真成了 brands 的列,这个分组本身就该重审 —— 这条就是那个哨兵。
    同时这也证明上一条锁**不是恒真**:确实存在会被它抓住的键。
    """
    from api.geo_douyin_api import _BRAND_MAPPED_KEYS

    cols = _brands_columns()
    overlap = [k for k in _BRAND_MAPPED_KEYS if k in cols]
    assert not overlap, \
        f"{overlap} 现在是 brands 的真列了 —— 两组的划分要重审"


# [WO_267 拆格] 原 test_only_one_place_reads_the_brand_row 已搬到
#   tests/brand_row_single_reader_2026_09_23(有运行者、带牙证);本文件在冻结名单里没人跑,锁住在这里等于没锁。


def test_brand_display_name_union_order():
    """🔴 合并后的取值顺序:name → company_name → brand_name。

    🔴 这条与 hotfix 的 A1/A2/A3 是**同一个函数的两面**,不是重复:
       那边锁"真行形状能取到 / 映射形状也认 / name 压 brand_name",
       这边锁**中间那一档 company_name** —— 合并前 hotfix 版没有它,
       合并时漏掉就是把 v5 那一半悄悄丢了(生产上确有 name 空只填公司名的行)。
    """
    from api.geo_douyin_api import _brand_display_name as f

    assert f({"name": "QZQZ木作美学定制"}) == "QZQZ木作美学定制"
    assert f({"name": "", "company_name": "全域上榜（深圳）"}) == "全域上榜（深圳）", \
        "name 为空时没退到 company_name —— 合并时把 v5 那一半丢了"
    assert f({"company_name": "只有公司名"}) == "只有公司名"
    assert f({"brand_name": "已映射形状"}) == "已映射形状", \
        "映射形状取不到了 —— 合并时把 hotfix 那一半丢了"
    # 必须不命中面:三档同在时严格按 name → company_name → brand_name
    assert f({"name": "甲", "company_name": "乙", "brand_name": "丙"}) == "甲"
    assert f({"name": "", "company_name": "乙", "brand_name": "丙"}) == "乙", \
        "company_name 没排在 brand_name 前面"
    assert f(None) == "" and f({}) == ""
    assert f({"name": "  空白  "}) == "空白", "没去空白"


def test_create_post_fails_fast_when_the_client_has_no_name():
    """🔴 没名字必须**在下单前**拦,不能等花完 390 再退款。

    生产实测:名字为空 → 冻结 → 跑 LLM 139s → 判废 → 退款,
    用户等两分多钟只拿到一句含糊的失败。这条链结构上做不成,
    早说比晚说便宜。
    """
    code = _code(API, "api_create_and_produce")
    assert "fetch_brand_display_name" in code, "下单没走统一的取名"
    assert "BRAND_NAME_MISSING" in code, "没名字时没有当场拦下"
    # 必须不命中面:拦截必须发生在**建行/派发之前**。
    # 🔴 比的是调用点不是 import —— 第一版拿 "dispatch_production" 裸串去比,
    #    命中的是那行 `from ... import dispatch_production`(它当然在前面),
    #    于是这条断言恒红。判据要打在真正做事的那一句上。
    gate = code.index("BRAND_NAME_MISSING")
    assert gate < code.index("dispatch_production("), "拦截排在派发之后 —— 等于没拦"
    assert gate < code.index("ddb.create_post"), \
        "拦截排在建行之后 —— 会留下一条永远做不成的空记录"

    # 🔴 上面三条只证明"那段代码在,而且位置靠前",**证明不了它会执行**。
    #    变异 W1c 把 `if not brand_name:` 换成 `if False:` —— 三条全绿,拦截却已经死了。
    #    所以还要证明:那个 raise 确实挂在 `not brand_name` 这个条件下面。
    fn = _fn(API, "api_create_and_produce")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.UnaryOp)
                and isinstance(node.test.op, ast.Not)
                and isinstance(node.test.operand, ast.Name)
                and node.test.operand.id == "brand_name"):
            continue
        if "BRAND_NAME_MISSING" in ast.unparse(ast.Module(body=node.body,
                                                          type_ignores=[])):
            guarded = True
    assert guarded, "拦截没有挂在 `not brand_name` 上 —— 那段代码永远不会执行"


# ═══════════════════════════════════════════════════════════════
# W2 · 撞 token 天花板不许谎报成"服务不可用"
# ═══════════════════════════════════════════════════════════════

def test_max_tokens_is_a_ladder_not_a_single_value():
    """🔴 额度是阶梯,且第一档不是最大档。

    定值 16000 在 prompt 变长之后退化了(生产 input 2488 → 4751,
    7 天 47 次里撞顶 5 次)。阶梯的意义是"够用就停",
    所以第二档必须严格大于第一档,而且不能只有一档。
    """
    from services.geo_douyin.content_generator import MAX_TOKENS_LADDER

    assert len(MAX_TOKENS_LADDER) >= 2, "额度没有第二档 —— 撞顶就只能失败"
    assert list(MAX_TOKENS_LADDER) == sorted(MAX_TOKENS_LADDER), "阶梯不是递增的"
    assert len(set(MAX_TOKENS_LADDER)) == len(MAX_TOKENS_LADDER), \
        "阶梯里有重复档位 —— 重试一次用同一个额度等于没重试"


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """只够 `_call_llm` 用的最小 httpx 替身。记录每次请求体,好核 max_tokens。"""

    def __init__(self, payloads, seen):
        self._payloads = list(payloads)
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002
        self._seen.append(dict(json or {}))
        return _FakeResp(self._payloads.pop(0) if len(self._payloads) > 1
                         else self._payloads[0])


def _stub_llm(monkeypatch, payloads):
    """把 `_call_llm` 的三个外部依赖全换掉:key / httpx / 计费留痕。

    🔴 这是**行为锁**不是源码串锁。上一版这条锁只断言源码里有
       "finish_reason" / "'truncated'" 这些串,于是变异把
       `truncated = (not text) and finish == "length"` 换成 `truncated = False`
       之后,那些串**一个都没少**,锁全绿而撞顶识别已经死了(W2c 存活)。
    """
    import contextlib
    import httpx

    import services.geo_douyin.content_generator as cg
    import tools.llm_call_tracker as tracker_mod

    seen: list = []
    monkeypatch.setattr(cg, "_get_api_key", lambda: "test-key")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _FakeClient(payloads, seen))

    class _T:
        def __init__(self):
            self.records: list = []

        def record(self, **kw):
            self.records.append(kw)

    t = _T()

    @contextlib.asynccontextmanager
    async def _fake_track(*a, **kw):
        yield t

    monkeypatch.setattr(tracker_mod, "llm_track", _fake_track)
    return seen, t


def _payload(content, finish, completion=100):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 4751, "completion_tokens": completion}}


@pytest.mark.asyncio
async def test_truncation_is_detected_and_reported_as_its_own_reason(monkeypatch):
    """🔴 行为锁:`finish_reason == "length"` + 正文空 = 撞顶。

    生产原样:HTTP 200、模型真跑了、`output_tokens=15999`(天花板 16000),
    只是推理把额度吃光、正文为空。旧代码把它和"连不上"一起返回 None。
    """
    from services.geo_douyin.content_generator import _call_llm

    seen, tracker = _stub_llm(monkeypatch, [_payload("", "length", 15999)])
    diag: dict = {}
    got = await _call_llm("p", diag=diag)

    assert got is None, "撞顶还返回了正文"
    assert diag.get("reason") == "truncated", \
        f"撞顶没识别出来,reason={diag.get('reason')!r}"
    # 撞顶要真的换更大额度重试一次(不是原地再打一遍同样的请求)
    budgets = [b.get("max_tokens") for b in seen]
    assert budgets == [16000, 24000], f"没有按阶梯重试,实际用的额度:{budgets}"
    # 必须不命中面:留痕不许记成功
    assert all(r.get("success") is False for r in tracker.records), \
        "撞顶仍记 success —— 观测面说'成功'而任务说'失败',排障时两边对不上"


@pytest.mark.asyncio
async def test_normal_answer_is_not_mistaken_for_truncation(monkeypatch):
    """🔴 反向对照:正常出文时**不许**被判成撞顶,也不许多打一次 LLM。

    没有这一条,把 `truncated` 恒置真也能让上面那条过 ——
    那样每次都白白多花一次调用的钱。
    """
    from services.geo_douyin.content_generator import _call_llm

    seen, tracker = _stub_llm(monkeypatch, [_payload('{"body":"x"}', "stop", 900)])
    diag: dict = {}
    got = await _call_llm("p", diag=diag)

    assert got == '{"body":"x"}', "正常出文却没拿到"
    assert diag.get("reason") is None, "正常出文被判成了失败"
    assert len(seen) == 1, "正常出文还多打了一次 LLM"
    assert tracker.records and tracker.records[0].get("success") is True, \
        "正常出文没记成功"


@pytest.mark.asyncio
async def test_empty_content_without_length_is_not_called_truncation(monkeypatch):
    """🔴 说不清的空**不硬猜**成撞顶。

    `finish_reason` 不是 length 而正文为空 —— 这是另一类问题,
    按撞顶报会把人引到"少做两张"这个解决不了的动作上。
    """
    from services.geo_douyin.content_generator import _call_llm

    seen, _ = _stub_llm(monkeypatch, [_payload("", "stop", 0)])
    diag: dict = {}
    got = await _call_llm("p", diag=diag)

    assert got is None
    assert diag.get("reason") == "empty_content", \
        f"非撞顶的空被归成了 {diag.get('reason')!r}"
    assert len(seen) == 1, "非撞顶也去重试了 —— 白花一次钱"


def test_truncated_maps_to_its_own_error_code_on_both_chains():
    """🔴 两条链(文案 / 蒸馏)都要把撞顶报成 `llm_truncated`。

    只修一条 = 另一条继续把人引到"稍后重试"这个永远无效的动作上。
    """
    gen = _code(GEN, "generate_image_post_content")
    assert "llm_truncated" in gen, "文案链没有区分撞顶"
    dis = _code(DISTILLER, "distill_topics")
    assert "llm_truncated" in dis, "蒸馏链没有区分撞顶"


def test_truncated_has_a_human_message_that_suggests_a_different_action():
    """🔴 话术必须指向一个**真能改变结果**的动作。

    「稍后重试」对撞顶是无效动作(同一条 prompt 必然再撞)。
    所以两处人话里都不许再落回"重试"那句。
    """
    from api.geo_douyin_api import humanize_failure
    from services.geo_douyin.distill_task import humanize_distill_failure

    a = humanize_failure("copy", "llm_truncated")
    b = humanize_distill_failure("llm_truncated")
    for msg in (a, b):
        assert msg, "撞顶没有人话"
        assert "暂时不可用" not in msg, "撞顶还在说'服务不可用'"
    # 必须不命中面:两句必须与 llm_unavailable 的那句**不同**,
    # 否则加了错误码却共用一句话 = 用户看到的东西没变。
    assert a != humanize_failure("copy", "llm_unavailable"), "文案链两种失败共用一句话"
    assert b != humanize_distill_failure("llm_unavailable"), "蒸馏链两种失败共用一句话"


def test_retry_does_not_secretly_enlarge_the_caller_budget():
    """🔴 二次尝试不许放大调用方给的超时预算。

    上一次事故正是"后端用了比调用方窗口更长的时间":nginx 60s 掐断、
    后台还在跑、钱照扣。撞顶重试是同一个陷阱的入口。
    """
    code = _code(GEN, "_call_llm")
    assert "timeout=float(timeout_s or _LLM_TIMEOUT_S)" in code.replace(" ", "").replace(
        "timeout=float(timeout_sor_LLM_TIMEOUT_S)",
        "timeout=float(timeout_s or _LLM_TIMEOUT_S)"), \
        "超时不再是调用方给的那个预算"
    for bad in ("max(timeout", "timeout_s * ", "timeout_s*"):
        assert bad not in code, f"出现了放大预算的写法:{bad}"


def test_diag_is_optional_so_the_monkeypatch_seam_still_works():
    """🔴 `diag` 必须是可选关键字参数。

    它要是必填,所有 monkeypatch `_call_llm` 的测试假体当场全挂;
    而假体挂掉最容易被"改一下测试"糊过去,那就等于把 seam 弄坏了。
    """
    sig = _fn(GEN, "_call_llm")
    names = [a.arg for a in sig.args.kwonlyargs]
    assert "diag" in names, "_call_llm 没有 diag 出参"
    default = sig.args.kw_defaults[names.index("diag")]
    assert isinstance(default, ast.Constant) and default.value is None, \
        "diag 不是可选的"


# ═══════════════════════════════════════════════════════════════
# W3 · 蒸馏三缺陷
# ═══════════════════════════════════════════════════════════════

def test_disabled_distill_button_says_why():
    """🔴 灰按钮必须自己说明为什么灰。

    生产实测:admin 默认落在「全部客户」,`brandId` 为 null,
    按钮死灰、点上去零反馈 —— 这就是 Owner 报的「点击了没用」。
    而且客户选择只存在 sessionStorage,关一次浏览器就没了,
    所以这个状态是常态不是边角。
    """
    # [WO_271] 改指选题面板,按后继重判:没选客户时整块换成一句「先在左上角选一个客户」
    # (不再画一颗死灰按钮);价读不到时按钮灰、下面明说「价目暂时读不到」。
    tsx = _tsx(TOPIC_PANEL)
    assert re.search(r'if \(!brandId\) \{\s*return \(\s*<div[^>]*data-testid="topics-no-client"', tsx), \
        "没选客户时没有说为什么"
    # 灰条件是理由清单的分母:多一个灰的条件,就要多一句理由 —— 这里一变就红,逼人去补
    assert "disabled={starting || taskId !== null || distillPoints === null}" in tsx, \
        "蒸馏按钮的灰条件变了 —— 先核每个灰的条件都有一句话"
    assert re.search(r'\{distillPoints === null && \(\s*<p[^>]*data-testid="topics-distill-noprice"', tsx), \
        "价读不到时按钮灰了却不说为什么"


def test_distill_progress_comes_from_backend_fields():
    """🔴 进度取后端的 percent / eta,不在前端模拟。

    后端每一跳都回这两个值,原来一个都没用上,用户盯着一句不动的字
    等 40~100 秒(实测波动这么大)。
    反面同样重要:不许用 Date.now() 或轮询计数自己编一条进度。
    """
    # [WO_271] 改指:进度的读法在 imageNoteTopics.distillProgress(纯函数),面板只照它画。
    #   原来的第二条(eta)后继没接 → 拆到下一格 xfail(WO_283-F6),这一格守其余几条。
    topics = _tsx(TOPICS)
    fn = topics.split("export function distillProgress", 1)[1].split("\nexport function", 1)[0]
    assert "o.percent" in fn, "没用后端的 percent"
    assert "o.stage_label" in fn, "那句话不是后端的 stage_label —— 又是一句不动的字"
    tsx = _tsx(TOPIC_PANEL)
    assert "distillProgress(d)" in tsx and 'data-testid="topics-distill-progress"' in tsx, \
        "进度没渲染出来"
    for src in (tsx, topics):
        assert "Date.now()" not in src, "前端又开始自己造进度了"


def _eta_problems(topics: str, panel: str) -> list:
    fn = topics.split("export function distillProgress", 1)[1].split("\nexport function", 1)[0]
    out = []
    if "Number(o.eta_seconds)" not in fn:
        out.append("distillProgress 没读后端的 eta_seconds")
    if "active && o.eta_seconds" not in fn:
        out.append("ETA 没挂在「还在跑」上 —— 终态还会显示一个倒数")
    if "{progress.eta}" not in panel or 'data-testid="topics-distill-eta"' not in panel:
        out.append("面板没把 ETA 画出来")
    return out


def test_distill_eta_from_backend_reaches_the_screen():
    """🔴 后端每一跳都回 `eta_seconds`(distill_task.describe_distill_progress),上一格原来的第二条。

    [WO_271] 旧入口页用上了它;后继选题面板只画 percent + stage_label —— 而 percent
    按阶段取档(_band_low),最长的「distilling」那一段里条不动、字不动,只有 ETA 在倒数。
    [WO_283-F6 已修] distillProgress 在还在跑时给出「约 N 秒」(估不出 / 终态就空串,不编),
    面板画在进度那一行旁边。行为由 build 链 verify-image-note-topics A14 真调钉住。
    """
    problems = _eta_problems(_tsx(TOPICS), _tsx(TOPIC_PANEL))
    assert not problems, "\n".join(problems)


def test_distill_eta_lock_has_power():
    """反臂(WO_283):不读 eta_seconds / 面板不画 ⇒ 上一格必须报;现役源码不报(对照)。"""
    topics, panel = _tsx(TOPICS), _tsx(TOPIC_PANEL)
    assert _eta_problems(topics, panel) == []
    assert _eta_problems(topics.replace("o.eta_seconds", "o.eta_x"), panel)
    assert _eta_problems(topics, panel.replace("{progress.eta}", "{''}"))


def test_distilled_topics_survive_a_remount():
    """🔴 花了钱的选题必须能找回来。

    生产实测:蒸出 5 条 → 切一次 tab → `distilled-topics` 节点消失,
    而结果一直躺在 `geo_douyin_distill_tasks.result` 里。
    用户能看到的不该比我们存下来的少。
    """
    # [WO_271] 改指选题面板,按后继重判:蒸出来的选题不再只躺在任务结果里等前端「取回上次」,
    # 而是任务成功时**直接落进选题表**(run_distill_task → insert_distilled_topics),
    # 面板每次挂载都从 /clients/{id}/topics 重读 —— 切 tab / 刷新都丢不了。
    tsx = _tsx(TOPIC_PANEL)
    assert "/api/geo-douyin/clients/${bid}/topics?" in tsx, "面板没从服务端的选题表读"
    assert "void load(brandId, ac.signal);" in tsx, "挂载时没重读"
    assert "if (brandId) void load(brandId);" in tsx, "蒸馏结束没重读列表 —— 结果只在本地 state 里"
    assert "insert_distilled_topics" in _code(DISTILL_TASK, "run_distill_task"), \
        "蒸出来的选题没落表 —— 切一次 tab 就丢,钱已经扣了"
    api_src = API.read_text(encoding="utf-8")
    assert '@router.get("/clients/{brand_id}/topics")' in api_src, "后端没有这个只读端点"


def test_latest_topics_endpoint_only_returns_succeeded_tasks():
    """🔴 只回**成功**的那一次。

    回失败/在跑的那一行会把空结果或半成品当成"上次的选题"给用户看,
    比不显示更糟。
    """
    src = DDB.read_text(encoding="utf-8")
    fn = _code(DDB, "get_latest_distill_task")
    assert "succeeded" in fn, "没有只取成功的那一次"
    assert "ORDER BY id DESC" in fn, "没有取最近的那一次"
    assert "brand_id = %s" in fn, "没有按客户过滤 —— 会串到别的客户"
    del src


def test_latest_topics_endpoint_checks_brand_access():
    """🔴 越权:读别人客户的选题是要拦的,而且 target 就是路径上那个 brand_id。"""
    code = _code(API, "api_latest_distilled_topics")
    assert "require_brand_access(request, int(brand_id))" in code, \
        "只读端点没做归属校验"


def test_latest_topics_endpoint_never_charges():
    """🔴 只读端点不许碰任何计费/派发。

    它每次进页面都会被调用,一旦沾上扣费就是"打开页面就扣钱"。
    """
    code = _code(API, "api_latest_distilled_topics")
    for bad in ("charge", "freeze", "dispatch", "run_distill", "acquire_inflight"):
        assert bad not in code, f"只读端点里出现了 {bad}"


# ═══════════════════════════════════════════════════════════════
# W3b · 一次性下单
# ═══════════════════════════════════════════════════════════════

def test_batch_submit_stops_on_first_failure():
    """🔴 一条失败就停,不许把剩下的继续发出去。

    第一条就因为算力不足失败,后面九条再发九次是纯骚扰,
    而且每一次都会把错误提示覆盖成一模一样的第二遍。
    """
    # [WO_271] 改指选题面板,按后继重判:不再逐条发 —— 整批**一次请求**(posts/batch,带 request_id,
    # 重试同一清单不会重复建单),哪几条没进去由服务端逐条回、一次列全,不会一条条覆盖着报。
    tsx = _tsx(TOPIC_PANEL)
    m = re.search(r"const doStart = async \(\) => \{(.+?)\n    \};", tsx, flags=re.S)
    assert m, "找不到 doStart 的函数体"
    body = m.group(1)
    assert body.count("authFetch(") == 1 and "'/api/geo-douyin/posts/batch'" in body, \
        "下单不是一次整批请求 —— 又会一条条往外发"
    assert "failures.map(" in body and ".join('；')" in body, "没进去的几条没有一次列全"
    assert "request_id: requestId" in _tsx(PRODUCTION), "整批请求没带 request_id —— 重试会重复建单"


def test_batch_button_shows_the_total_price():
    """🔴 按钮上必须明码标价合计。

    全站已改静默扣费 + 事后通知(不弹确认框),那么"要花多少"这件事
    只剩按钮这一个地方能说。一次点下去是 N × 单条价,不说等于埋雷。
    反面:价读不到时**不许猜一个** —— 显示一个猜的价比不显示更糟。
    """
    # [WO_271] 改指选题面板,按后继重判:合计由服务端 production-quote 的 total_points 给
    # (前端不做乘加);读不到就置空、按钮点不了,并明说读不到。
    tsx = _tsx(TOPIC_PANEL)
    assert "startLabel(selectedPending.length, quotePoints)" in tsx, "按钮没标合计"
    assert "/api/geo-douyin/production-quote" in tsx and "d?.total_points" in tsx, "合计不是服务端给的"
    assert "Number.isFinite(p) && p >= 0 ? Math.floor(p) : null" in tsx, "合计读坏时没有置空 —— 会猜一个数"
    assert re.search(r'\{selectedPending\.length > 0 && quotePoints === null && \(\s*<p[^>]*data-testid="topics-start-noprice"', tsx), \
        "价读不到时没有明说"
    topics = _tsx(TOPICS)
    assert "价目读不到" in topics and "price(input?.points) !== null" in topics, \
        "价读不到时按钮上没说 / 还能点"


# ── [WO_271] 退役:test_per_city_count_is_clamped ──
# 它读的是已删的旧入口页(「每城做几条」输入框 + clampPerCity / MAX_POSTS_PER_CITY)。
# 后继没有这个输入:一行 = 一条,勾几行做几条,上限「每次最多制作 20 条」由 imageNoteProduction
# 在下单前拦、服务端再拦 —— NaN 条 / 手滑几十条的前提随输入框一起没了。
# 接替者:frontend/scripts/verify-image-note-topics.mjs A5(npm run build 链每次都跑:新面板里
# 没有「按数量做」输入);tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted
# 另守 clampPerCity 不许回来。登记:tests/RETIRED_TESTS.txt。


# ── [WO_271] 退役:test_clamp_per_city_semantics_are_pinned_in_the_source(10 个参数)──
# 同上一格:钉的是已删页里 clampPerCity 的实现形态,函数随页删除,没有对象了。
# 接替者同上(verify-image-note-topics.mjs A5)。登记:tests/RETIRED_TESTS.txt。


# ═══════════════════════════════════════════════════════════════
# W4 · 拒绝自卖自夸(Owner 2026-08-06)
# ═══════════════════════════════════════════════════════════════

def _content(body: str, brand: str = "全域上榜（深圳）科技有限公司",
             brand_line: str = ""):
    from services.geo_douyin.content_generator import GeneratedContent

    return GeneratedContent(body=body, brand_name_expected=brand,
                            closing={"brand_line": brand_line})


@pytest.mark.parametrize("body", [
    "我们全域上榜（深圳）科技有限公司，2018年就开始做生成式引擎优化",   # 生产 post 18 原文
    "我们的全域上榜（深圳）科技有限公司自研了系统",
    "我司专注GEO八年",
    "本公司提供全链路交付",
    "咱家做的就是这块",
    "我们公司在深圳",
    "我们团队负责内容资产",
    "我们这边可以出诊断报告",
])
def test_seller_voice_is_rejected(body):
    """🔴 卖方自称一律判废。

    生产实证:post 18 正文第一段就是「**我们**全域上榜（深圳）科技有限公司，
    2018年就开始做…」—— 这内容发在**素人矩阵账号**上,账号不是品牌本人。
    """
    assert _content(body).self_praise, f"没抓到卖方自称:{body}"


@pytest.mark.parametrize("body", [
    # 🔴 这一组是这条锁的**全部价值**:第一人称本身不是罪。
    #    实证里「第一人称(我/亲测/实测)」在 geo_优化服务 是 +27.7pp,
    #    全行业最稳的一招 —— 判据要是把它一起拦了,等于把最有效的写法禁掉。
    "我实测了深圳几家做GEO的，全域上榜（深圳）科技有限公司是2018年就开始做的",
    "我对比了三家，全域上榜（深圳）科技有限公司的做法是先出诊断报告",
    "我问了全域上榜（深圳）科技有限公司，对方说要先做可见度诊断",
    "我是全域上榜（深圳）科技有限公司的老客户，说点真实感受",
    "我们平时挑服务商，第一看有没有诊断能力",          # 泛指的"我们"
    "全域上榜（深圳）科技有限公司成立于2018年",         # 纯第三方陈述
])
def test_reviewer_voice_is_not_mistaken_for_self_praise(body):
    """🔴 必须不命中面:测评者/体验者的第一人称**不许**被误伤。"""
    hit = _content(body).self_praise
    assert not hit, f"误伤了测评者口吻:{body!r} → 命中 {hit!r}"


def test_brand_line_is_scanned_too():
    """🔴 收尾卡那句"要被 AI 摘出来的推荐句"也在扫描面内。

    只扫正文的话,自夸挪到 brand_line 就绕过去了 ——
    而那一句恰恰是最可能被豆包整句摘走的。
    """
    c = _content("全域上榜（深圳）科技有限公司做GEO",
                 brand_line="我们全域上榜（深圳）科技有限公司欢迎咨询")
    assert c.self_praise, "brand_line 没被扫描"


def test_self_praise_voids_the_whole_job():
    """🔴 判废而不是降级。

    降级(替客户改口吻)产出的还是我们写的推荐语;而且用户拿到的是
    一条已经被我们动过手脚的内容,却不知道。
    """
    code = _code(GEN, "generate_image_post_content")
    assert "self_praise" in code, "生成侧没有接自夸判定"
    assert "self_praise_voice" in code, "没有独立的错误码"
    # 必须不命中面:不许出现"把自称替换掉"这类降级写法
    for bad in (".replace('我们'", '.replace("我们"', "strip_self_praise"):
        assert bad not in code, f"出现了降级改写:{bad}"


def test_self_praise_and_not_promoted_are_a_pair():
    """🔴 两条闸必须都在:一头拦"不点名"(白做),一头拦"自述"(打广告)。

    只留一头就会滑到另一头 —— 08-05 加了点名闸,模型就滑成了自述广告。
    """
    code = _code(GEN, "generate_image_post_content")
    assert "brand_not_promoted" in code and "self_praise_voice" in code, \
        "两条闸不齐,产出会滑到另一个极端"


def test_prompt_tells_the_model_third_party_voice():
    """🔴 立场要写进 prompt,不能只靠事后判废。

    只有事后闸没有事前指令 = 每一单都先花 140 秒再失败。
    """
    from services.geo_douyin.card_templates import promote_block

    b = promote_block("全域上榜（深圳）科技有限公司", city="深圳",
                      industry_key="geo_优化服务")
    assert "第三方" in b, "prompt 没说明是第三方写法"
    assert "我司" in b and "本公司" in b, "prompt 没把卖方自称列成禁用"
    # 必须不命中面:不许再出现"以服务方本人的口吻"这类反向指令
    assert "服务方本人" not in b, "prompt 里还留着让模型自述的旧指令"
    # 点名要求不许一起被删掉(不能从"打广告"滑回"不点名")
    assert "必须出现" in b, "点名要求被删了 —— 会滑回 08-05 那个 P0"


@pytest.mark.asyncio
async def test_self_praise_voids_the_whole_job_end_to_end():
    """🔴 行为锁:整条生成链拿到自述文案 → **整条判废**。

    🔴 为什么必须有这条:上一版只有
       ① 属性锁(直接测 `self_praise`)和 ② 源码串锁(`"self_praise" in code`)。
       变异 W4a 把 `hit = out.self_praise` 换成 `hit = ''` 之后 ——
       属性锁照样绿(属性本身没动),源码串锁也照样绿
       (`error="self_praise_voice"` 里就含 `self_praise` 这个子串)。
       **闸已经拆了,两条锁都没红。** 只有把整条链跑一遍才抓得住。
    """
    import json

    import services.geo_douyin.content_generator as cg
    from services.geo_douyin.knowledge_context import BrandContext

    def _payload(body: str) -> str:
        return json.dumps({
            "body": body,
            "cards": [{"entity": f"品牌{i}", "caveat": "样本有限",
                       "points": [f"要点{i}"]} for i in range(1, 3)],
            "cover": {"title": "标题"}, "closing": {"headline": "收尾"},
            "title": "深圳某某服务哪家好？5家实测对比与避坑要点说清",
            "hashtags": ["a", "b", "c", "d", "e"]}, ensure_ascii=False)

    def _stub(value):
        async def _f(*a, **k):
            return value
        return _f

    async def _ctx(*a, **k):
        return BrandContext(brand_name="某客户")

    orig_llm, orig_ctx = cg._call_llm, cg.build_brand_context
    cg.build_brand_context = _ctx
    try:
        # 卖方口吻(生产 post 18 的形状)→ 判废
        cg._call_llm = _stub(_payload("我们某客户在深圳做这一行，欢迎咨询。"))
        bad = await cg.generate_image_post_content("词", card_count=4,
                                                   brand_name="某客户")
        assert bad.ok is False and bad.error == "self_praise_voice", \
            f"自述文案却通过了:ok={bad.ok} error={bad.error}"

        # 必须不命中面:第三方口吻 + 点名 → 必须通过。
        # 少了这一面,把闸做成"一律判废"也能让上面那条绿。
        cg._call_llm = _stub(
            _payload("我实测了几家，某客户在深圳做这一行，下面是对比。"))
        good = await cg.generate_image_post_content("词", card_count=4,
                                                    brand_name="某客户")
        assert good.ok is True, f"第三方口吻反而失败:{good.error}"
        assert good.promotes_brand is True and not good.self_praise
    finally:
        cg._call_llm, cg.build_brand_context = orig_llm, orig_ctx


# ═══════════════════════════════════════════════════════════════
# W5 · 截断按句边界(2026-08-06 · 生产 post 19 实证)
# ═══════════════════════════════════════════════════════════════

def test_card_text_fields_all_go_through_clip_text():
    """🔴 **会印到卡面上的**每个文字字段都必须走 clip_text。

    🔴 判据形态订正:第一版是"全文件不许出现 `[:N]`",一跑就把
       `str(e)[:160]`(日志)、hashtag 长度、视觉色名一起判红 ——
       那些根本不上图,截断了也不会印出半句话。
       **判据要打在"会不会印到图上"这件事上,不是打在切片语法上。**
       打宽了的锁最后一定会被人加豁免,加着加着就没判别力了。
    """
    fields = ("title", "subtitle", "entity", "one_liner", "metric",
              "caveat", "headline", "summary", "brand_line")
    src = GEN.read_text(encoding="utf-8")
    tpl = (ROOT / "services" / "geo_douyin" / "card_templates.py").read_text(
        encoding="utf-8")
    # 🔴 只扫**真正构造产出的那两个函数体**,不扫整份文件 ——
    #    `_PROMPT` 里有一份给模型看的 JSON 模板,里面同样有 "title": "…" 这种行,
    #    扫全文件会把那份模板也当成赋值判红(第二版判据就栽在这)。
    #    `_code()` 顺带剥掉 docstring,注释里的示例也不会误伤。
    bodies = "\n".join(_code(GEN, fn) for fn in
                       ("generate_image_post_content", "_clamp_cards"))
    for f in fields:
        for m in re.finditer(r"'%s':\s*([^,\n]+)" % f, bodies):
            expr = m.group(1)
            assert "clip_text" in expr, f"卡面字段 {f} 没走 clip_text:{expr[:60]}"
    # points 是列表,单独看。
    # 🔴 判据必须钉在**赋值那一句**上:第一版写的是 `points.*clip_text\\(`,
    #    它撞到了同一函数里另一行 `'points': points, 'metric': clip_text(...)` ——
    #    于是 points 换回盲切照样绿(变异 W5f 第一轮就是这么活下来的)。
    #    "锚点撞到别处"和"锚点没命中"表现相反、危害相同:后者会红,前者永远不红。
    assert re.search(r"points = \[clip_text\(", bodies), "points 没走 clip_text"
    # 两个"卡面字数上限"常量按定义就只用于卡面文字,任何裸切都算漏网
    for name, text in (("content_generator.py", src), ("card_templates.py", tpl)):
        body = re.sub(r'"""(?:.|\n)*?"""', " ", text)
        body = re.sub(r"^\s*#.*$", " ", body, flags=re.M)
        bad = re.findall(r"\[:\s*(?:CLOSING_TEXT_MAX|COVER_TEXT_MAX)\s*\]", body)
        assert not bad, f"{name} 里还有按卡面上限的盲切:{bad}"


def test_the_clip_lock_would_catch_a_real_blind_cut():
    """🔴 反向对照:上一条不是恒真 —— 同样的判据打在**修复前的原文**上必须命中。"""
    old_line = '                 "brand_line": str(closing_obj.get("brand_line") or "").strip()[:80]},'
    m = re.search(r'"brand_line":\s*([^,\n]+)', old_line)
    assert m and "clip_text" not in m.group(1), "判据连修复前的原文都抓不到 —— 是废的"


@pytest.mark.parametrize("text,limit,want_end", [
    # 生产 post 19 原样:盲切把「不承诺固定排名。」整句砍掉(**合规声明**)
    ("星野智能是深圳一家AI搜索可见度优化服务商，2019年成立，自研星野引擎，"
     "已服务300+客户，白帽打法，不承诺固定排名。", 60, "，"),
])
def test_clip_falls_back_to_a_sentence_boundary(text, limit, want_end):
    """🔴 断点必须落在句读符号后,不许断在词中间。"""
    from services.geo_douyin.card_templates import clip_text

    got = clip_text(text, limit)
    assert len(got) <= limit
    assert got and got[-1] not in "，,、", "以逗号收尾,读着像没写完"
    del want_end


def test_clip_never_leaves_a_dangling_comma():
    """🔴 必须不命中面:结果不许以逗号/顿号结尾。"""
    from services.geo_douyin.card_templates import clip_text

    for t in ("一二三四五，六七八九十，十一十二", "甲、乙、丙、丁、戊、己、庚"):
        got = clip_text(t, 10)
        assert not got.endswith(("，", ",", "、")), f"{got!r} 以逗号收尾"


def test_clip_hard_cuts_when_there_is_no_boundary():
    """🔴 窗口里没有断点时仍要截到上限内 —— 不许因为找不到断点就整段放行。"""
    from services.geo_douyin.card_templates import clip_text

    got = clip_text("A" * 200, 30)
    assert len(got) == 30, f"没有断点时没有硬切,实得 {len(got)} 字"


def test_clip_returns_empty_rather_than_a_half_recommendation():
    """🔴 客户名被切掉时交空串,不交半句。

    生产 post 19:「…白帽打法的公司，比如全域上榜（深圳）科技有限公司」
    被盲切成「…比如全」—— 图上就印着「比如全」。
    更隐蔽的是 `promotes_brand` 判的是**截断之后**的文本,名字被切掉还会
    让整单报 `brand_not_promoted`,失败原因完全指不到真因。
    """
    from services.geo_douyin.card_templates import clip_text

    who = "全域上榜（深圳）科技有限公司"
    late = ("选服务商到底看什么？我问下来主要是三件事：第一能不能拿出多引擎的"
            "诊断报告，第二有没有从内容到监测的完整闭环，第三是不是白帽打法，"
            + who + "这几条都占。")
    assert clip_text(late, 80, keep=who) == "", "名字被切掉了却还交出半句"
    # 必须不命中面:名字在前面时**不许**因为有 keep 就返回空
    early = who + "，2019年成立，自研引擎，白帽打法，不承诺固定排名，值得看看。"
    got = clip_text(early, 40, keep=who)
    assert got and who in got, f"名字明明保得住却被判空:{got!r}"


def test_brand_line_clip_passes_the_client_name_as_keep():
    """🔴 `brand_line` 那一处必须带 keep —— 它是唯一"名字必须活下来"的字段。"""
    code = _code(GEN, "generate_image_post_content")
    m = re.search(r"clip_text\([^)]*brand_line[^)]*keep\s*=", code) \
        or re.search(r"'brand_line':\s*clip_text\((.|\n)*?keep=", code)
    assert m, "brand_line 截断没带 keep,名字仍可能被切掉"


# ═══════════════════════════════════════════════════════════════
# W6 · 正文必须点名 + 空信息条不画(Owner 2026-08-06)
# ═══════════════════════════════════════════════════════════════

def test_promotes_brand_looks_at_the_body_only():
    """🔴 名字必须在**正文**里(Owner:「必须要有客户名字,不然白打」)。

    生产 post 19 踩的正是这个缝:正文通篇没有客户名,名字只在收尾句和卡片上,
    而闸判的是 `body + brand_line` 二选一 —— **闸比 prompt 松**
    (prompt 的硬要求一直写的是「正文里必须出现」)。
    正文是抖音把标题和描述当一整段 caption 索引的那一段,也是豆包最容易抓的文本面。
    """
    from services.geo_douyin.content_generator import GeneratedContent

    who = "星野智能（深圳）科技有限公司"
    # 正面:名字在正文 → 通过
    ok = GeneratedContent(body=f"我对比了几家，{who}是2019年开始做的",
                          brand_name_expected=who)
    assert ok.promotes_brand is True

    # 🔴 必须不命中面:名字**只**在收尾句 → 不通过。
    #    这一条就是 post 19 的形状,收紧之前它是放行的。
    only_line = GeneratedContent(body="我对比了几家，各有各的做法",
                                 closing={"brand_line": f"{who}成立于2019年"},
                                 brand_name_expected=who)
    assert only_line.promotes_brand is False, \
        "名字只在收尾句却放行了 —— 又回到 post 19 那个缝里"

    assert GeneratedContent(body="谁也没提", brand_name_expected=who
                            ).promotes_brand is False
    assert GeneratedContent(body=f"{who}很好", brand_name_expected=""
                            ).promotes_brand is False, "没有期望名却判过"


def test_empty_footer_bar_is_not_drawn():
    """🔴 `footer_bar` 为空时**不生成那句 prompt**。

    生产 post 19 第 4 张图底部有一道**空的色块条** —— 因为 prompt 里
    无条件拼了「底部一条深空灰色块条，写「」」,生图模型照做画了个空条。
    而 `footer` 参数两个调用点(production_task / redraw)**都没传**,恒为空。
    """
    from services.geo_douyin.card_templates import (
        STYLE_PRESETS, build_closing_prompt, build_content_prompt,
        build_style_tokens,
    )

    preset = STYLE_PRESETS["design_text"]
    empty = build_style_tokens("词", "深圳")
    assert empty.footer_bar == "", "本用例前提是 footer 没人传"
    for prompt in (
        build_closing_prompt("标题", "小结", empty, preset, caveat="提醒"),
        build_content_prompt(1, "实体", ["要点"], empty, preset=preset),
    ):
        assert "「」" not in prompt, "空信息条还是被写进 prompt 了"
        assert "信息条" not in prompt or "写「" not in prompt

    # 必须不命中面:真给了 footer 就**必须**画出来 —— 不能顺手把功能删了
    filled = build_style_tokens("词", "深圳", footer="数据来源：公开渠道")
    got = build_closing_prompt("标题", "小结", filled, preset, caveat="提醒")
    assert "数据来源：公开渠道" in got, "给了 footer 反而不画了"


def test_footer_is_still_an_unwired_parameter():
    """🔴 挂账锁:`footer` 至今**没有任何调用点在传**。

    这条锁不是要求它必须被传,而是**把"没接线"这件事钉在明面上** ——
    哪天有人接上了,这条会红,提醒把上面那条"空条不画"的兜底一起重审。
    同型问题(加了参数没接线)在本包已经出现三次:画幅选择器、style_key 入口、footer。
    """
    import ast

    callers = []
    for name in ("production_task", "redraw"):
        path = ROOT / "services" / "geo_douyin" / f"{name}.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "build_style_tokens"):
                callers.append((name, {k.arg for k in node.keywords}))
    assert callers, "没找到 build_style_tokens 的调用点 —— 判据失效了"
    wired = [n for n, kw in callers if "footer" in kw]
    assert not wired, f"{wired} 开始传 footer 了 —— 去重审"
