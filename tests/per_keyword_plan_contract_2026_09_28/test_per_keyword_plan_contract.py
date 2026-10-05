# -*- coding: utf-8 -*-
"""写作大厅「新词出标题」的 per_keyword_plan 前后端契约(Review 09-28 · 生产上自 09-19 每次 400)。

病:WO_243 乙(f94ef07a7)起,前端 WritingHall 每项发 {confirmed_keyword_id, planned_count},
    后端 server._apply_per_keyword_plan 读 int(item.get("count")) ⇒ int(None) ⇒ 400。
    三元按钮「新词面」与单个新词「生成标题」都走它 —— 收费路径,一条不出一分不扣。
    当时两边各有一把锁,各自都绿:前端锁把出错那行原文钉死(verify-topic-gen-charge E5b),
    后端锁只测 planned_count 落到关键词行之后;**没有任何东西核对两边用的是同一个键**。

修法(Review 定):前端只发槽数 `slots`,槽 -> 条只在后端做,复用写作详情 planned_posts_default 的同一个换算器
(`media_slot_conversion.default_posts_converter`);`count`(条)保留,两者同时给 ⇒ 400;0 槽 ⇒ 0 条。

  C1 🔴 键名契约(静态):前端组包里的每个键都是后端 _apply_per_keyword_plan 会读的键;
        数量键恰好一个且在 PER_KEYWORD_PLAN_QUANTITY_KEYS 里。毒:前端改回 planned_count ⇒ 红;后端只认 count ⇒ 红
  C2 🔴 接口格:用 node 真跑前端那段组包表达式得到**前端原样 JSON**,喂后端原样函数 + 真换算器:
        自媒体单 7 槽 ⇒ 35 条(= 详情 planned_posts_default)、0 槽 ⇒ 0 条、子集外 ⇒ 0;
        出题侧 _required_article_count 与计费侧 plannable_keywords / keyword_count 读到同一结果(只收 1 份)
  C3 🔴 count 与 slots 同给 ⇒ 400;都不给 ⇒ 400;slots 越界 / 非整数 ⇒ 400;老 count(条)照旧直通
  C4 🔴 写作详情的 planned_posts_default 与 slots 换算走同一个 default_posts_converter(不许各拼一份)
  C5 🔴 受理凭据只为本次真正出题的词落(= 计费份数同一个 plannable_keywords)。本机真浏览器实测:
        原来传整张关键词表,一次全 0 的请求给 3 个词都落了凭据,没付过钱的「新词七槽」从此被划进免费补救面
"""
from __future__ import annotations

import ast
import functools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SERVER = ROOT / "server.py"


# ───────────────────────────── 取两边原文 ─────────────────────────────

def _plan_expr(tsx: str) -> str:
    """前端 generateTitles 里 `const perKeywordPlan = <表达式>;` 的表达式原文。"""
    m = re.search(r"const perKeywordPlan = (.*?\n\s*: null);", tsx, re.S)
    assert m, "前端组包表达式没找到(改名了?判据要跟着改,不许静默跳过)"
    return m.group(1)


def _front_keys(expr: str) -> list[str]:
    m = re.search(r"keywords\.map\(\(kw\) => \(\{(.*?)\}\)\)", expr, re.S)
    assert m, "前端逐词对象字面量没找到"
    return re.findall(r"^\s*(\w+)\s*:", m.group(1), re.M)


@functools.lru_cache(maxsize=1)
def _server_parts() -> str:
    """server.py 只解析一次(2 万多行,每次 ~2s):取 _apply_per_keyword_plan 源码 + 它用的两行模块常量。"""
    src = SERVER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_apply_per_keyword_plan")
    consts = [l for l in src.splitlines()
              if l.startswith(("PER_KEYWORD_PLAN_MAX_COUNT =", "PER_KEYWORD_PLAN_QUANTITY_KEYS ="))]
    assert len(consts) == 2, consts
    return "\n".join(consts) + "\n\n\n" + ast.get_source_segment(src, fn) + "\n"


def _backend_read_keys(src: str) -> tuple[set, tuple]:
    """后端 _apply_per_keyword_plan 读 item 的键 = item.get("<常量>") ∪ PER_KEYWORD_PLAN_QUANTITY_KEYS。
    `src` = `_server_parts()` 那段(常量行 + 函数),毒就下在这段上。"""
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_apply_per_keyword_plan")
    keys = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "item"
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            keys.add(n.args[0].value)
    qty = ()
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PER_KEYWORD_PLAN_QUANTITY_KEYS"
                                             for t in n.targets):
            qty = tuple(ast.literal_eval(n.value))
    return keys | set(qty), qty


def contract_problems(tsx: str, server_src: str) -> list[str]:
    front = _front_keys(_plan_expr(tsx))
    read, qty = _backend_read_keys(server_src)
    out = [f"前端发了后端不读的键 {k!r}" for k in front if k not in read]
    q = [k for k in front if k in qty]
    if len(q) != 1:
        out.append(f"前端数量键应恰好一个且属于 {qty},实际 {q}")
    if "confirmed_keyword_id" not in front:
        out.append("前端没发 confirmed_keyword_id")
    return out


def test_c1_front_and_back_agree_on_keys():
    tsx, src = HALL.read_text(encoding="utf-8"), _server_parts()
    assert _front_keys(_plan_expr(tsx)) == ["confirmed_keyword_id", "slots"]
    assert contract_problems(tsx, src) == []
    # 毒①:前端改回 09-19 的 planned_count ⇒ 红
    line = "slots: onlyKeywordIds.includes(kw.id) ? (kw.required_articles ?? 0) : 0,"
    assert tsx.count(line) == 1, "毒没下成"
    bad = tsx.replace(line, "planned_count: onlyKeywordIds.includes(kw.id) ? (kw.required_articles || 1) : 0,")
    assert contract_problems(bad, src)
    # 毒②:后端只认 count(= 09-19 的后端)⇒ 前端 slots 红
    qline = 'PER_KEYWORD_PLAN_QUANTITY_KEYS = ("count", "slots")'
    assert src.count(qline) == 1, "毒没下成"
    assert contract_problems(tsx, src.replace(qline, 'PER_KEYWORD_PLAN_QUANTITY_KEYS = ("count",)'))


# ───────────────────────────── 接口格 ─────────────────────────────

class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(f"{status_code} {detail}")
        self.status_code = status_code


def _backend_apply():
    """server.py 里 _apply_per_keyword_plan 原样取出(不 import server:那要起整个应用和库)。"""
    ns = {"HTTPException": HTTPException}
    exec(_server_parts(), ns)
    return ns["_apply_per_keyword_plan"]


@pytest.fixture
def self_media_quote(monkeypatch):
    """一张自媒体口径的单:只替换两个读库函数,换算器本身(default_posts_converter / posts_for_slots)是真的。"""
    import services.media_slot_conversion as msc
    monkeypatch.setattr(msc, "_read_media_mix", lambda qid, cursor=None: {
        "delivery_perspective": msc.PERSPECTIVE_SELF_MEDIA, "conversion_version": 1})
    monkeypatch.setattr(msc, "_fetch_version", lambda v: dict(msc.SEED_V1))
    return msc


def _node_payload(tsx: str, keywords: list, only_ids: list) -> list:
    node = shutil.which("node")
    if not node:
        pytest.fail("本格要 node 真跑前端组包表达式;没有 node = 判不了,不许当通过")
    js = ("const keywords = %s; const onlyKeywordIds = %s;\n"
          "const out = %s;\nprocess.stdout.write(JSON.stringify(out));"
          % (json.dumps(keywords), json.dumps(only_ids), _plan_expr(tsx)))
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


KWS = [{"id": 11, "required_articles": 7}, {"id": 12, "required_articles": 0}, {"id": 13, "required_articles": 7}]


def test_c2_front_end_payload_through_backend_to_generator_and_charge(self_media_quote):
    from services.topic_gen_charge import keyword_count, plannable_keywords
    from writing.keyword_topic_generator import _required_article_count
    payload = _node_payload(HALL.read_text(encoding="utf-8"), KWS, [11, 12])
    assert payload == [{"confirmed_keyword_id": 11, "slots": 7}, {"confirmed_keyword_id": 12, "slots": 0},
                       {"confirmed_keyword_id": 13, "slots": 0}]
    rows = [dict(k) for k in KWS]
    _backend_apply()(rows, payload, quote_id=1)
    detail_default = self_media_quote.default_posts_converter(1)(7)
    assert detail_default == 35                                      # 7 槽 × 自媒体 5 条/槽
    assert [r["planned_count"] for r in rows] == [35, 0, 0]          # 7 槽 ⇒ 详情同款 35 条;0 槽 ⇒ 0;子集外 ⇒ 0
    assert [_required_article_count(r) for r in rows] == [35, 0, 0]  # 出题侧读到同一个数
    assert [r["id"] for r in plannable_keywords(rows)] == [11] and keyword_count(rows) == 1  # 只收 1 份


def test_c2_the_09_19_payload_shape_is_rejected_as_it_was_in_production(self_media_quote):
    """对照臂:把前端改回 09-19 原文,走同一条链 ⇒ 400(= 生产现象)。证明 C2 的链真能看见这个病。"""
    tsx = HALL.read_text(encoding="utf-8").replace(
        "slots: onlyKeywordIds.includes(kw.id) ? (kw.required_articles ?? 0) : 0,",
        "planned_count: onlyKeywordIds.includes(kw.id) ? (kw.required_articles || 1) : 0,")
    payload = _node_payload(tsx, KWS, [11, 12])
    assert payload[1] == {"confirmed_keyword_id": 12, "planned_count": 1}   # 旧 `|| 1` 还把 0 槽吃成 1
    with pytest.raises(HTTPException) as e:
        _backend_apply()([dict(k) for k in KWS], payload, quote_id=1)
    assert e.value.status_code == 400


def test_c2_portal_quote_converts_one_slot_to_one_post(monkeypatch, self_media_quote):
    msc = self_media_quote
    monkeypatch.setattr(msc, "_read_media_mix", lambda qid, cursor=None: {
        "delivery_perspective": msc.PERSPECTIVE_PORTAL, "conversion_version": 1})
    rows = [dict(k) for k in KWS]
    _backend_apply()(rows, [{"confirmed_keyword_id": 11, "slots": 7}], quote_id=1)
    assert rows[0]["planned_count"] == 7


@pytest.mark.parametrize("item", [
    {"confirmed_keyword_id": 11, "count": 3, "slots": 1},     # 两个都给
    {"confirmed_keyword_id": 11},                             # 都不给
    {"confirmed_keyword_id": 11, "planned_count": 3},         # 09-19 前端形状
    {"confirmed_keyword_id": 11, "slots": -1},
    {"confirmed_keyword_id": 11, "slots": 51},
    {"confirmed_keyword_id": 11, "slots": "x"},
    {"confirmed_keyword_id": 11, "slots": True},
])
def test_c3_bad_items_are_400(item, self_media_quote):
    with pytest.raises(HTTPException) as e:
        _backend_apply()([dict(k) for k in KWS], [item], quote_id=1)
    assert e.value.status_code == 400


def test_c3_count_in_posts_still_passes_through_unconverted(self_media_quote):
    rows = [dict(k) for k in KWS]
    _backend_apply()(rows, [{"confirmed_keyword_id": 11, "count": 3}, {"confirmed_keyword_id": 13, "count": 0}],
                     quote_id=1)
    assert [r.get("planned_count") for r in rows] == [3, None, 0]


@functools.lru_cache(maxsize=1)
def _generate_titles_src() -> str:
    src = SERVER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    return ast.get_source_segment(src, fn)


def reservation_problems(fn_src: str) -> list[str]:
    """generate-titles 里落受理凭据的那次调用,第二个参数必须是 `_topic_plannable(...)`(= 计费份数同一个集合)。"""
    tree = ast.parse(fn_src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "reserve_title_slots"]
    if len(calls) != 1:
        return [f"reserve_title_slots 调用应恰好 1 处,实际 {len(calls)}"]
    arg = calls[0].args[1] if len(calls[0].args) > 1 else None
    ok = isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "_topic_plannable"
    return [] if ok else ["受理凭据没按 plannable 落:置 0 的新词会拿到凭据行,被划进免费补救面"]


def test_c5_reservation_covers_only_keywords_that_will_be_generated():
    fn = _generate_titles_src()
    assert reservation_problems(fn) == []
    good = 'request.quote_id, _topic_plannable(detail["keywords"]), _title_request_id'
    assert fn.count(good) == 1, "毒没下成"
    assert reservation_problems(fn.replace(good, 'request.quote_id, detail["keywords"], _title_request_id'))


def test_c5_plannable_drops_the_keywords_a_subset_plan_zeroed(self_media_quote):
    """行为臂:前端原样 payload 落到行上之后,凭据集合 = 只剩被选中的那个 7 槽新词。"""
    from services.topic_gen_charge import plannable_keywords
    rows = [dict(k) for k in KWS]
    _backend_apply()(rows, _node_payload(HALL.read_text(encoding="utf-8"), KWS, [12, 13]), quote_id=1)
    assert [r["id"] for r in plannable_keywords(rows)] == [13]      # 12 是 0 槽,11 是子集外 ⇒ 都不落凭据


def test_c4_detail_and_plan_share_one_converter():
    detail = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    a = detail.index("def get_writing_project_detail(")
    body = detail[a:detail.index("\ndef ", a + 10)]
    assert "default_posts_converter(quote_id)" in body and "posts_for_slots(" not in body
    fn = _server_parts()
    assert "default_posts_converter(quote_id)" in fn and "posts_for_slots(" not in fn
