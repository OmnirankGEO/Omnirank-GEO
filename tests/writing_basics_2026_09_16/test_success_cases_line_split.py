# -*- coding: utf-8 -*-
"""WO_227-c1 · `success_cases` 一行一个案例(不再按逗号/顿号切碎)。

被治的缺陷(实测,不是读代码推的):
    用户在客户档案页的自由文本框里打
        某租车公司,3 个月询盘翻倍、成本降 20%
    存进去变成
        ["某租车公司", "3 个月询盘翻倍", "成本降 20%"]
    下次打开显示
        某租车公司,3 个月询盘翻倍,成本降 20%
    —— **顿号被吃掉换成逗号,一个案例被切成三条。**
    损坏一次性(第二次保存不再继续劣化),但用户的原话已经被改了。

🔴 修的是**拆分规则**,不是那一列的语义:`success_cases` 仍是数组,
   只是「一行一个案例」而不是「一个逗号一个案例」。
🔴 `products` 等**零变化** —— 它们的数组语义是**有意的**
   (客户档案页 `match.products || []` 按数组用),反臂钉住。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.writing_basics_2026_09_16.conftest import PID_PREFIX, conn  # noqa: E402

from db.profile_db import get_profile, update_profile                  # noqa: E402

#: 一条案例里**本来就带**逗号和顿号 —— 老规则正是栽在这上面
ONE_CASE = "某租车公司,3 个月询盘翻倍、成本降 20%"
ANOTHER = "某物流公司、两个月内 GEO 上榜 12 词"


def _new_profile() -> str:
    pid = PID_PREFIX + uuid.uuid4().hex[:10]
    c = conn()
    try:
        c.cursor().execute(
            "INSERT INTO client_profiles (id, name) VALUES (%s, %s)", (pid, "227 判据"))
    finally:
        c.close()
    return pid


def _raw(pid: str, col: str):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT %s AS v FROM client_profiles WHERE id=%%s" % col, (pid,))
        return dict(cur.fetchone())["v"]
    finally:
        c.close()


def test_a_single_case_with_commas_stays_one_case():
    """🔴 本单的核心:一条带逗号顿号的案例,**整条**存进去,不许被切碎。"""
    pid = _new_profile()
    assert update_profile(pid, success_cases=ONE_CASE)
    stored = json.loads(_raw(pid, "success_cases"))
    assert stored == [ONE_CASE], (
        "一条案例被切成了 %d 条:%r —— 逗号顿号是句内标点,不是分隔符"
        % (len(stored), stored))


def test_two_lines_become_two_cases():
    """换行才是分隔符;空行丢掉。"""
    pid = _new_profile()
    assert update_profile(pid, success_cases=ONE_CASE + "\n\n" + ANOTHER + "\n")
    assert json.loads(_raw(pid, "success_cases")) == [ONE_CASE, ANOTHER]


def test_round_trip_through_get_profile_is_verbatim():
    """🔴 存进去再读回来,**每一条逐字相同**(读侧走 `_parse_json_fields`)。"""
    pid = _new_profile()
    assert update_profile(pid, success_cases=ONE_CASE + "\n" + ANOTHER)
    got = get_profile(pid)["success_cases"]
    assert got == [ONE_CASE, ANOTHER], got
    # 前端按 "\n" join 回去,必须还原成用户打的那段
    assert "\n".join(got) == ONE_CASE + "\n" + ANOTHER


def test_a_list_from_the_caller_is_not_re_split():
    """调用方给 list 时**不再拆分**(每一项就是一条案例)。

    🔴 这条原名叫 `..._is_stored_as_is`(原样存),**那个断言是错的** ——
       它喂的是没有空串的 list,所以永远绿,正好盖住了下面那条真缺口。
       「原样存」在「原样」本身就该被清理时是错的。复审抓的。
    """
    pid = _new_profile()
    assert update_profile(pid, success_cases=[ONE_CASE, ANOTHER])
    assert json.loads(_raw(pid, "success_cases")) == [ONE_CASE, ANOTHER]


def test_a_list_with_blank_items_comes_back_without_them():
    """🔴 前端送来的就是带空项的 list,而它**不该**滤空。

    `MarketingTab` 的 `onChange={v => updateField('success_cases', v.split('\n'))}`
    不滤空是对的 —— 滤了会吞掉用户正在打的换行。
    所以归一必须在**后端单点**做,否则空串项落库、再显示成空行。
    """
    pid = _new_profile()
    assert update_profile(pid, success_cases=[ONE_CASE, "", ANOTHER, "   ", ""])
    assert json.loads(_raw(pid, "success_cases")) == [ONE_CASE, ANOTHER]


def test_each_item_is_stripped():
    """每一项两端空白也要去掉(用户在行尾多敲的空格不该进库)。"""
    pid = _new_profile()
    assert update_profile(pid, success_cases=["  " + ONE_CASE + "  ", ANOTHER])
    assert json.loads(_raw(pid, "success_cases")) == [ONE_CASE, ANOTHER]


def test_products_list_input_is_still_untouched():
    """🔴 反臂:**只有** `split_rules` 里的字段做 list 归一。

    `products` 收到带空项的 list 时仍原样存 —— 它的数组语义是有意的,
    本单不许顺手替它清理。没有这一条,我把归一做成「对所有 array_fields 生效」
    也会全绿。
    """
    pid = _new_profile()
    assert update_profile(pid, products=["日租", "", "月租"])
    assert json.loads(_raw(pid, "products")) == ["日租", "", "月租"], (
        "products 的 list 输入被顺手归一了 —— 本单只该动 split_rules 里的字段")


# ── 反臂:别的字段零变化 ──────────────────────────────────────────

def test_products_still_splits_on_commas():
    """🔴 反臂:`products` 的**按逗号拆**是有意的,不许被本单顺手改掉。

    客户档案页按数组用它(`match.products || []`),后端 44 份 / 前端 26 份在读。
    没有这一条的话,我把拆分规则做成「全局换行」也会全绿。
    """
    pid = _new_profile()
    assert update_profile(pid, products="日租、月租,商务接送")
    assert json.loads(_raw(pid, "products")) == ["日租", "月租", "商务接送"]


def test_other_array_fields_keep_the_default_rule():
    """逐个验其余 array_fields 仍走默认规则 —— 不是只验 products 一个。"""
    pid = _new_profile()
    for col in ("pain_points", "competitors", "target_platforms"):
        assert update_profile(pid, **{col: "甲、乙,丙"})
        assert json.loads(_raw(pid, col)) == ["甲", "乙", "丙"], (
            "%s 的拆分规则被改了 —— 本单只该动 success_cases" % col)


def test_split_rules_table_names_only_success_cases():
    """结构锁:配置表里**只有** `success_cases` 被改了规则。

    行为锁逐个验字段有上限(array_fields 有十几个);这条直接钉配置本身,
    新加字段时若有人顺手改了别的规则,这里立刻红。
    """
    from tests._shared.source_slice import code_only, function_body

    body = code_only(function_body("db/profile_db.py", "update_profile"))
    assert "split_rules = {" in body, "拆分规则没有做成按字段配置"
    table = body[body.index("split_rules = {"):]
    table = table[:table.index("}") + 1]
    assert "'success_cases': 'newline'" in table, "success_cases 没配成按换行拆"
    assert table.count(":") == 1, (
        "split_rules 里不止一个字段被改了规则:%s —— 本单只该动 success_cases" % table)


# ── 历史行:不回填,但呈现会变 ────────────────────────────────────

def test_legacy_multi_element_rows_are_left_alone():
    """🔴 不回填历史:已存的多元素数组**原样不动**。

    但**「不回填」不等于「呈现不变」** —— 读侧改成按行显示后,
    这一行会从「一行逗号连排」变成「多行」。
    227-d1 实测:真品牌 json_array 9 行、其中元素 ≥2 的 **6 行**会看到这个变化。
    """
    pid = _new_profile()
    legacy = ["某租车公司", "3 个月询盘翻倍", "成本降 20%"]
    c = conn()
    try:
        c.cursor().execute(
            "UPDATE client_profiles SET success_cases=%s WHERE id=%s",
            (json.dumps(legacy, ensure_ascii=False), pid))
    finally:
        c.close()
    assert get_profile(pid)["success_cases"] == legacy, "历史行被动过了 —— 本单不回填"


def test_a_legacy_plain_text_row_reads_back_unchanged():
    """老的 plain_text 行(生产上真品牌有 8 行)读回来不许出错。"""
    pid = _new_profile()
    c = conn()
    try:
        c.cursor().execute(
            "UPDATE client_profiles SET success_cases=%s WHERE id=%s", (ONE_CASE, pid))
    finally:
        c.close()
    got = get_profile(pid)["success_cases"]
    assert got == ONE_CASE, (
        "plain_text 老行读回变成了 %r —— 它不是 JSON,`_parse_json_fields` 该原样放过" % got)
