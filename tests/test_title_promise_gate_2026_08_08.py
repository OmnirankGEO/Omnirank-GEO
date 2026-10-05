"""W1 返工 ④:标题数量承诺 → 正文兑现闸。

实证起点:文章 1515 **标题承诺「排名前十」、正文只 1 家**。
判据分两层,缺一层都不算修完:
  · 函数层:承诺提取 / 确定性改写 / 三态(不知道 · 兑现了 · 没兑现);
  · **接线层:三条保存路径落库的 `title` 字段真的是改写后的那个**
    —— 「函数对了没接线」在本仓栽过三次,补发链那条更是当场抓到了
    「闸改 `_save_article["title"]`、INSERT 却第二次拼 f-string」。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_c4_review_autopilot_2026_07_27 import (  # noqa: E402
    _make_service,
    _wire_fake_db,
)
from writing.title_promise_gate import (  # noqa: E402
    enforce_title_promise,
    extract_quantity_promise,
    strip_body_promise_echo,
    strip_quantity_promise,
)

_BODY = "# T\n\n" + "\n\n".join(f"## 第 {i} 节\n\n正文。" for i in range(6))


# --- 承诺提取 -------------------------------------------------------------

@pytest.mark.parametrize(
    "title, expected",
    [
        ("2026年深圳TikTok代运营公司TOP5推荐", 5),
        ("贵阳工业除尘设备排名前十", 10),
        ("某行业十大品牌推荐", 10),
        ("2026年杭州装修公司前5名", 5),
        ("某某行业怎么选:7家在营企业的核验口径对比", 7),
        ("排行前三的服务商", 3),
    ],
)
def test_quantity_promise_is_extracted(title, expected):
    promise = extract_quantity_promise(title)
    assert promise is not None, f"没识别出承诺:{title}"
    assert promise[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "本地装修公司怎么选",
        "某品牌资质与交付能力核验指南",
        "本店3家分店营业时间",      # 品牌自身事实,不是候选家数承诺
        "某品牌的5家直营门店介绍",  # 同上
    ],
)
def test_non_promises_are_not_touched(title):
    """反向对照:没承诺候选家数的标题,一个字不许动。"""
    assert extract_quantity_promise(title) is None
    assert strip_quantity_promise(title) == title


def test_multiple_promises_take_the_strongest():
    """一个标题里两处承诺时按**最强**那句判 —— 按弱的判等于放行了强的那句。"""
    promise = extract_quantity_promise("前3名精选 · 十大品牌完整榜")
    assert promise is not None and promise[0] == 10


# --- 确定性改写 -----------------------------------------------------------

@pytest.mark.parametrize(
    "title, expected",
    [
        ("贵阳工业除尘设备排名前十", "贵阳工业除尘设备怎么选"),
        ("2026年深圳TikTok代运营公司TOP5推荐", "2026年深圳TikTok代运营公司推荐"),
        ("排行前三的服务商", "服务商怎么选"),
        ("某某行业怎么选:7家在营企业的核验口径对比", "某某行业怎么选:核验口径对比"),
    ],
)
def test_rewrite_is_deterministic_and_readable(title, expected):
    """改写必须是确定性的(**不调 LLM**:保存链上多一次模型调用 = 多一个失败点 + 一笔钱)。"""
    assert strip_quantity_promise(title) == expected


def test_rewritten_title_never_keeps_a_number_promise():
    for title in ["贵阳工业除尘设备排名前十", "某行业十大品牌推荐", "TOP20 服务商榜"]:
        assert extract_quantity_promise(strip_quantity_promise(title)) is None


# --- 三态 -----------------------------------------------------------------

def test_unknown_delivered_count_changes_nothing():
    """不知道兑现了几家 → 一个字不改。错改标题比漏改更坏,这不是安全闸。"""
    title = "贵阳工业除尘设备排名前十"
    out, note = enforce_title_promise(title, verified_entity_count=None)
    assert out is title and note is None


def test_promise_met_is_not_punished():
    """写实了就不该罚 —— 反向对照,证明这条闸是按兑现量判的,不是见数字就删。"""
    title = "贵阳工业除尘设备排名前十"
    out, note = enforce_title_promise(title, verified_entity_count=10)
    assert out is title and note is None


def test_promise_unmet_rewrites_and_records():
    title = "贵阳工业除尘设备排名前十"
    out, note = enforce_title_promise(title, verified_entity_count=1)
    assert out == "贵阳工业除尘设备怎么选"
    assert note == {
        "promised": 10, "delivered": 1, "matched": "排名前十",
        "original_title": title, "rewritten_title": out,
    }


# --- 接线层:落库的 title 真的是改写后的 ----------------------------------

def _run_save(monkeypatch, title: str, verified: int | None, body: str | None = None):
    import writing.article_generator_service as svc_mod

    async def _noop(t, c, topic, article, trust, target_entity=""):  # [R5.1] 与真 helper 契约同签名
        return c, trust

    monkeypatch.setattr(svc_mod, "apply_review_autopilot", _noop)
    monkeypatch.setattr(svc_mod, "_copy_article_distilled_lineage", lambda *a, **k: None)
    inserts: list = []
    _wire_fake_db(monkeypatch, [
        ("SELECT q.brand_id, b.name AS brand_name", None),
        ("SELECT id FROM topics WHERE id=%s FOR UPDATE", {"id": 11}),
        ("COALESCE(MAX(version),0)", {"max_version": 0}),
        ("SELECT COALESCE(q.owner_user_id", None),
        ("SELECT brand_id FROM quotes", None),
    ], inserts)
    service = _make_service()
    monkeypatch.setattr(
        service, "_freeze_topic_delivery_options",
        lambda topic: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )
    topic = {
        "id": 11, "publication_profile": "standard", "evidence_mode": "unknown",
        "style_code": "ranking_v2", "title": title, "keyword": "工业除尘",
    }
    _b = _BODY if body is None else body
    article = {
        "topic_id": 11, "title": title, "content": _b, "word_count": len(_b),
        "style": "ranking_v2", "publication_profile": "standard",
    }
    if verified is not None:
        article["verified_entity_count"] = verified
    assert asyncio.run(service._save_article(topic, article)) == 777
    assert inserts, "必须真的走到 INSERT"
    qw = inserts[0][7]
    return inserts[0][2], inserts[0][3], getattr(qw, "adapted", qw)


def test_save_path_persists_the_rewritten_title(monkeypatch):
    """判据打在**落库的 title 字段**上,不是 helper 返回值。"""
    saved_title, saved_content, qw = _run_save(
        monkeypatch, "贵阳工业除尘设备排名前十", verified=1)
    assert saved_title == "贵阳工业除尘设备怎么选"
    assert extract_quantity_promise(saved_title) is None
    assert qw and qw.get("title_promise", {}).get("promised") == 10


def test_persisted_h1_matches_the_rewritten_title(monkeypatch):
    """正文第一行那个 H1 也必须跟着改 —— 只改 title 列、正文里还写着「前十」等于没改。"""
    saved_title, saved_content, _qw = _run_save(
        monkeypatch, "贵阳工业除尘设备排名前十", verified=1)
    assert saved_content.splitlines()[0] == f"# {saved_title}"
    assert "排名前十" not in saved_content


def test_save_path_leaves_a_met_promise_alone(monkeypatch):
    saved_title, _content, qw = _run_save(
        monkeypatch, "贵阳工业除尘设备排名前十", verified=10)
    assert saved_title == "贵阳工业除尘设备排名前十"
    assert not (qw or {}).get("title_promise")


def test_save_path_without_verified_count_is_inert(monkeypatch):
    saved_title, _content, qw = _run_save(
        monkeypatch, "贵阳工业除尘设备排名前十", verified=None)
    assert saved_title == "贵阳工业除尘设备排名前十"
    assert not (qw or {}).get("title_promise")


def test_generate_single_reports_the_verified_count():
    """兑现闸靠这个字段判 —— 它没被产出,闸在生产上永远是惰性的。"""
    from writing.article_generator_service import ArticleGeneratorService

    src = inspect.getsource(ArticleGeneratorService._generate_single)
    assert '"verified_entity_count": len(_name_whitelist or [])' in src, (
        "verified_entity_count 没有从白名单长度产出"
    )


def test_all_three_save_paths_run_the_gate():
    from writing.article_generator_service import ArticleGeneratorService

    for obj, name in (
        (ArticleGeneratorService._save_article, "_save_article"),
        (ArticleGeneratorService.rewrite_article, "rewrite_article"),
    ):
        src = inspect.getsource(obj)
        assert "_apply_title_promise_gate(" in src, f"{name} 没接兑现闸"
        assert src.index("_apply_title_promise_gate(") < src.index(
            "_normalize_article_title_and_h1("
        ), f"{name} 的兑现闸必须在 H1 归一化之前,否则 H1 还是旧标题"

    tools_src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    assert "_apply_title_promise_gate(" in tools_src, "补发链没接兑现闸"


# ===========================================================================
# 小单 A(2026-08-09):正文回声 —— 标题不承诺了,正文不许还在承诺
# ===========================================================================

def test_body_echo_is_downgraded_in_headings_and_prose():
    from writing.title_promise_gate import strip_body_promise_echo

    body = ("## 贵阳工业除尘设备服务商排名前十\n\n"
            "导语一段。本文按排名前十的口径比较。\n\n"
            "### 一、怎么核验资质\n正文。\n")
    out, notes = strip_body_promise_echo(body)
    assert extract_quantity_promise(out) is None, f"正文里还有承诺:{out}"
    assert len(notes) == 2, notes
    # 标题行走「删除」,散句走「换成不带数量的量词」—— 两种位置安全边界不同。
    # [W1 返工 ③] 小标题**不补**「怎么选」尾巴(补了会产出「## 入选标准怎么选」),
    # 所以这里期望的是光秃秃的名词短语。
    assert "## 贵阳工业除尘设备服务商" in out
    assert "怎么选" not in out, "小标题不该被补上任务词尾巴"
    assert "本文按多家的口径比较" in out


def test_body_without_promise_is_untouched():
    """反向对照:正文本来就没承诺 → 原对象返回、零留痕。"""
    from writing.title_promise_gate import strip_body_promise_echo

    body = "# 某某怎么选\n\n正文照常,没有任何数量承诺。\n"
    out, notes = strip_body_promise_echo(body)
    assert notes == [] and out is body


def test_gate_rewrites_title_and_body_together(monkeypatch):
    """接线锁:兑现闸触发时,`article['content']` 必须真的被改过。"""
    from writing.article_generator_service import _apply_title_promise_gate

    art = {"title": "贵阳工业除尘设备服务商排名前十",
           "content": "## 贵阳工业除尘设备服务商排名前十\n\n本文按排名前十比较。\n",
           "verified_entity_count": 3}
    new_title, note = _apply_title_promise_gate(art["title"], art, where="test")
    assert extract_quantity_promise(new_title) is None
    assert "排名前十" not in art["content"], "标题改了、正文回声没改 = 等于没修"
    assert note["body_echo_rewritten"], "正文改了却没留痕"


def test_gate_leaves_body_alone_when_promise_is_met():
    """反向对照:兑现得了就一个字不改(标题和正文都是)。"""
    from writing.article_generator_service import _apply_title_promise_gate

    original = "## 贵阳工业除尘设备服务商排名前十\n\n本文按排名前十比较。\n"
    art = {"title": "贵阳工业除尘设备服务商排名前十",
           "content": original, "verified_entity_count": 10}
    _t, note = _apply_title_promise_gate(art["title"], art, where="test")
    assert note is None
    assert art["content"] == original


# ===========================================================================
# 小单 B(2026-08-09):标题被复读成首个 `##`
# ===========================================================================

@pytest.mark.parametrize("first_line", [
    "## 贵阳工业除尘设备服务商怎么选",
    "### 贵阳工业除尘设备服务商怎么选",
    "## 贵阳工业除尘设备服务商怎么选 ",
    "## 【贵阳工业除尘设备服务商怎么选】",
])
def test_title_repeated_as_first_heading_is_upgraded_not_duplicated(first_line):
    from writing.article_generator_service import _normalize_article_title_and_h1

    title = "贵阳工业除尘设备服务商怎么选"
    _t, out = _normalize_article_title_and_h1(title, f"{first_line}\n\n正文。\n")
    heads = [ln for ln in out.splitlines() if re.match(r"^#{1,6}\s", ln)]
    assert heads[0] == f"# {title}"
    assert len(heads) == 1, f"标题被复读了:{heads}"


def test_a_real_section_heading_is_never_swallowed():
    """反向对照:首行是**真的小标题**(不是标题复读)时,H1 照旧前置,不许吞掉它。"""
    from writing.article_generator_service import _normalize_article_title_and_h1

    title = "贵阳工业除尘设备服务商怎么选"
    _t, out = _normalize_article_title_and_h1(title, "## 一、怎么核验资质\n\n正文。\n")
    heads = [ln for ln in out.splitlines() if re.match(r"^#{1,6}\s", ln)]
    assert heads == [f"# {title}", "## 一、怎么核验资质"]


def test_replacement_insert_reads_the_gated_title():
    """补发链的真出口锁。

    第一版在这里踩了坑:闸改的是 `_save_article["title"]`,而 INSERT 参数里
    **第二次拼了 f"[补发] {safe_title}"** —— 闸改了个没人读的值,接线等于没接。
    """
    tools_src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    insert_at = tools_src.index("INSERT INTO articles")
    window = tools_src[insert_at:insert_at + 2500]
    assert '_save_article["title"]' in window, "补发链 INSERT 没取被闸处理过的标题"
    assert 'f"[补发] {safe_title}",' not in window, (
        "补发链 INSERT 又在参数里第二次拼标题 —— 闸会被绕过"
    )


# ===========================================================================
# [W1 返工 ② 2026-08-09] 接线锁:落库**正文**里的回声真的没了
#
# 🔴 上一版 35 条锁全绿,却挡不住正文回声原样落库 —— 原因不是判据写错,
#    是**夹具是空的**:`_BODY` 从头到尾没有一个 promise 短语,于是
#    「回声有没有被清掉」这件事在保存链上一次都没被走到过。
#    闸的单测(直接调 `strip_body_promise_echo`)当然全绿 —— 它绕开了接线。
#
#    本仓第五例同款。上一份交付单里我自己还写着「补发链第四次踩真出口」。
#    所以这一节的每一条判据都必须满足两件事:
#      · 夹具**含真 promise 短语**(不含就等于没测,见下面的元判据);
#      · 断言打在 `INSERT INTO articles` 的 **content 参数**上,不是任何返回值。
# ===========================================================================

_PROMISE_TITLE = "贵阳工业除尘设备排名前十"
#: 正文里的回声两种形态都要有 —— 小标题一种、散句一种,处置路径不同。
_PROMISE_BODY = (
    f"# {_PROMISE_TITLE}\n\n"
    "我们从公开渠道核验了前十家企业。\n\n"
    "## 排名前十的入选标准\n\n公开可查的资质与交付记录。\n\n"
    "## 价格区间怎么看\n\n按工况与风量分档。\n\n"
    "## 交付周期一般多久\n\n以合同为准。\n\n"
    "## 常见的三个坑\n\n先看资质再看案例。\n\n"
    "## 签合同前要确认什么\n\n验收口径写进合同。\n\n"
    "## 售后怎么算\n\n质保期与响应时效。\n"
)


def test_promise_fixture_is_not_empty():
    """元判据,放在最前面:夹具里**必须真有** promise 短语。

    这条就是上一版缺的那一条 —— 没有它,下面所有断言都可以被一个
    「正文里本来就没有承诺」的夹具白白喂绿。
    """
    assert extract_quantity_promise(_PROMISE_BODY) is not None
    heading_echo = [ln for ln in _PROMISE_BODY.splitlines()
                    if ln.startswith("## ") and extract_quantity_promise(ln)]
    prose_echo = [ln for ln in _PROMISE_BODY.splitlines()
                  if not ln.startswith("#") and extract_quantity_promise(ln)]
    assert heading_echo, "夹具缺小标题形态的回声"
    assert prose_echo, "夹具缺散句形态的回声"


def test_persisted_body_has_no_promise_echo_left(monkeypatch):
    """🔴 主判据:全链跑到落库,`INSERT` 的 content 参数里**一个数量承诺都不许剩**。"""
    saved_title, saved_content, _qw = _run_save(
        monkeypatch, _PROMISE_TITLE, verified=1, body=_PROMISE_BODY)

    assert extract_quantity_promise(saved_title) is None
    assert extract_quantity_promise(saved_content) is None, (
        f"落库正文里还留着承诺:{extract_quantity_promise(saved_content)}"
    )
    assert "排名前十" not in saved_content
    assert "前十家" not in saved_content
    # 改写后的两行确实还在(不是把整行删了糊弄过去)
    assert "我们从公开渠道核验了多家企业。" in saved_content
    assert "## 入选标准" in saved_content


def test_persisted_body_records_the_echo_rewrite(monkeypatch):
    """落库的 `quality_warning` 必须逐行记下改了什么 —— 静默改写不可追溯。"""
    _t, _c, qw = _run_save(
        monkeypatch, _PROMISE_TITLE, verified=1, body=_PROMISE_BODY)
    note = (qw or {}).get("title_promise") or {}
    echoes = note.get("body_echo_rewritten")
    assert echoes, f"没有正文回声留痕:{note}"
    assert any("前十家" in e["before"] for e in echoes), "散句那条没留痕"
    assert any(e["before"].startswith("## ") for e in echoes), "小标题那条没留痕"
    for e in echoes:
        assert e["before"] != e["after"], "留痕里出现了 before==after 的空条目"


def test_unmet_promise_body_is_untouched_when_count_is_met(monkeypatch):
    """反向对照:承诺兑现了(verified=10),正文回声**一个字都不许动**。

    少了这条,一个「无条件把所有数字都换成多家」的实现照样能让上面两条全绿。
    """
    _t, saved_content, qw = _run_save(
        monkeypatch, _PROMISE_TITLE, verified=10, body=_PROMISE_BODY)
    assert "排名前十" in saved_content, "写实了却被改写"
    assert "前十家企业" in saved_content
    assert not (qw or {}).get("title_promise")


def test_no_echo_note_when_nothing_was_rewritten(monkeypatch):
    """留痕真实性:标题被改了、但正文本来就没有回声时,**不许**记 `body_echo_rewritten`。

    空列表读起来像"跑过了、0 处",跟"压根没跑到正文这一段"长得一模一样,
    而这两件事的排障方向相反(一个查匹配、一个查接线)。
    """
    _t, _c, qw = _run_save(monkeypatch, _PROMISE_TITLE, verified=1, body=_BODY)
    note = (qw or {}).get("title_promise") or {}
    assert note.get("promised") == 10, "前提:标题这一半确实被改了"
    assert "body_echo_rewritten" not in note, "没改正文却留了正文改写的痕迹"


def test_save_path_feeds_the_gate_the_sanitized_body_and_reads_it_back():
    """源码级接线判据(判别的是"接没接",与上面产物级判据互补)。

    这条路径全程用局部 `_content`。闸改的是 `article["content"]`,所以
    **闸之前必须写回、闸之后必须再取** —— 少任何一句,闸对正文就是空转。
    """
    from writing.article_generator_service import ArticleGeneratorService

    src = inspect.getsource(ArticleGeneratorService._save_article)
    gate_at = src.index("_apply_title_promise_gate(")
    before, after = src[:gate_at], src[gate_at:]
    assert "article['content'] = _content" in before, (
        "闸拿不到清洗后的正文 —— 它会在未清洗的那一版上改,然后被丢弃"
    )
    assert "_content = article.get('content', _content)" in after.split(
        "_normalize_article_title_and_h1")[0], (
        "闸之后没把正文取回来 —— 正文那半边的改写会被局部 _content 覆盖掉"
    )


def test_rewrite_path_keeps_the_same_wiring():
    """路径 2/3 本来就是对的 —— 锁住,免得下次被"统一风格"改成路径 1/3 的老样子。"""
    from writing.article_generator_service import ArticleGeneratorService

    src = inspect.getsource(ArticleGeneratorService.rewrite_article)
    gate_at = src.index("_apply_title_promise_gate(")
    assert "article['content'] = polish_source_disclosure(" in src[:gate_at]
    assert "article.get('content', '')" in src[gate_at:gate_at + 600], (
        "rewrite 路径也开始用局部变量绕过 article['content'] 了"
    )


# ===========================================================================
# [W1 返工 ③] 正文匹配收窄:「前十分钟」不许被当成家数承诺
# ===========================================================================

@pytest.mark.parametrize("text", [
    "前十分钟内响应即可",
    "过去前三季度的出货量",
    "前五年的运行数据",
    "前两天刚完成验收",
    "报名截止前十分钟系统会提醒",
])
def test_time_and_period_phrases_are_not_quantity_promises(text):
    """误删反例锁。

    上一版是**裸**的 `前\\s*(N)`,于是「前十分钟」→「多家分钟」、
    「前三季度」→「多家季度」。这类词在正文里比真承诺常见得多。
    """
    assert extract_quantity_promise(text) is None, f"{text} 被误判成家数承诺"
    out, notes = strip_body_promise_echo(f"正文一行:{text}。\n")
    assert notes == [], f"{text} 被误改:{notes}"
    assert text in out


@pytest.mark.parametrize("text,expect", [
    ("郑州装修公司前十", 10),          # 行末 —— 真标题形态,靠它兜住
    ("本文列出前十的服务商", 10),      # 实体名词上下文
    ("入选名单前十，其余不列", 10),    # 全角句读
    ("入选名单前十, 其余不列", 10),    # 半角句读
])
def test_narrowed_pattern_still_catches_real_promises(text, expect):
    """反向对照:收窄不许把真承诺一起收掉。"""
    got = extract_quantity_promise(text)
    assert got is not None and got[0] == expect, f"{text} 漏判"


def test_sentence_punctuation_class_is_written_as_codepoints():
    """元判据:句读字符类必须用码位写。

    第一版把全角逗号直接敲进字符类,编辑链路上没活下来 —— 类里只剩半角,
    于是中文正文最常见的「名单前十，其中」一条都不命中,而所有半角用例全绿。
    """
    src = (ROOT / "writing" / "title_promise_gate.py").read_text(encoding="utf-8")
    block = src[src.index("_SENTENCE_END"):src.index("_CLOSERS")]
    assert "\\uff0c" in block and "\\uff1b" in block, "全角句读没用码位写"
