# -*- coding: utf-8 -*-
"""WO_220-c2 · 写作大厅「补全知识库」基础资料表 —— 落库判据。

被测的缺陷:六个字段今天被前端拍平成 markdown 走 `/api/knowledge/upload`,
`client_profiles` 里没有结构化落点 ⇒ **客户刷新后表单永远空**。

本包钉的不变量(每条都配了毒,见交付单自毒清单):
  R1 六字段**逐字往返** —— 尤其含顿号/逗号的自由文本
  R2 只改一栏,其余五栏原样(JSONB 那两个是**合并写**不是整块替换)
  R3 禁写列零写入(negative_feedback / products / success_cases)
  R4 老档案读出六个空串,**不抛**
  R5 显式传 "" = 清空;不传 = 不覆盖(两者不是一回事)
  R6 别名转换必须在下游同步**之前**(放后面会静默绕过 persona/business 同步)
  R7 迁移已登记 manifest(接线锁)
  R8 映射表**自证分母**:六个字段各恰好一个去处
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.writing_basics_2026_09_16.conftest import PID_PREFIX, conn  # noqa: E402

from db.profile_db import get_profile, update_profile                  # noqa: E402
from services.writing_basics_contract import (                          # noqa: E402
    BASIC_FIELDS, COLUMN_FOR, FORBIDDEN_COLUMNS, JSONB_COLUMN, JSONB_FIELDS,
    from_profile, to_update_kwargs)

#: 🔴 每条都带**顿号 + 半角逗号 + 中文逗号** —— 正是 `profile_db` 的
#:    `array_fields` 分支会动手切的形状。不带这些标点的样本测不出本单的核心风险。
SAMPLES = {
    "business_summary": "我们做租车、代驾,还有商务接送,不承诺最低价",
    "target_customers": "本地中小企业主、需要长期用车的公司,以及机场接送客户",
    "products_services": "日租、月租、带司机商务接送,含保险",
    "key_selling_points": "车龄新、24 小时响应,可开专票",
    "proof_cases": "某租车公司,3 个月询盘翻倍、成本降 20%",
    "forbidden_notes": "不许说最便宜、不许承诺一定能上榜,不提竞品名",
}


def _new_profile(name="220c2 判据") -> str:
    pid = PID_PREFIX + uuid.uuid4().hex[:10]
    c = conn()
    try:
        c.cursor().execute(
            "INSERT INTO client_profiles (id, name) VALUES (%s, %s)", (pid, name))
    finally:
        c.close()
    return pid


def _save(pid: str, values: dict) -> bool:
    """走与 PUT 同一条路:契约翻译 -> update_profile。"""
    return update_profile(pid, **to_update_kwargs(values, get_profile(pid)))


# ── R8 映射表自证分母 ────────────────────────────────────────────────

def test_every_form_field_has_exactly_one_home():
    """🔴 分母自证:六个字段各**恰好一个**去处,不能漏也不能两处都占。

    漏一个 = 那一栏永远存不进去(正是本单要修的症状);
    两处都占 = 同一份事实两个住处,改一处另一处悄悄过期。
    """
    homes = set(COLUMN_FOR) | set(JSONB_FIELDS)
    assert homes == set(BASIC_FIELDS), (
        "字段去处不全:%s" % sorted(set(BASIC_FIELDS) ^ homes))
    assert not (set(COLUMN_FOR) & set(JSONB_FIELDS)), (
        "有字段同时占了真列和 JSONB:%s" % sorted(set(COLUMN_FOR) & set(JSONB_FIELDS)))
    assert len(BASIC_FIELDS) == 6, "表单是六栏,映射表里有 %d 个" % len(BASIC_FIELDS)


def test_mapping_never_points_at_a_forbidden_column():
    """🔴 反臂:映射表不许指向禁写列。

    `forbidden_notes` 的**前端读链**第三优先就是 `negative_feedback` ——
    照着读链写回是个很容易犯的错,而它会污染飞轮(那一列有专门的
    `add_negative_feedback` 追加口,装的是「我有意见」的负反馈)。
    """
    assert not (set(COLUMN_FOR.values()) & FORBIDDEN_COLUMNS), (
        "映射指向了禁写列:%s" % sorted(set(COLUMN_FOR.values()) & FORBIDDEN_COLUMNS))
    assert "negative_feedback" in FORBIDDEN_COLUMNS
    assert "products" in FORBIDDEN_COLUMNS and "success_cases" in FORBIDDEN_COLUMNS


# ── R1 逐字往返 ──────────────────────────────────────────────────────

def test_all_six_fields_round_trip_verbatim():
    """🔴 保存 → 读回**逐字相同**。

    这条是本单的核心。样本带顿号和逗号:`products` / `success_cases`
    在 `update_profile` 这条路上属 `array_fields`,纯字符串会被
    「按顿号/逗号拆分为数组」—— 实测「我们做租车、代驾,还有商务接送,不承诺最低价」
    变成 ["我们做租车","代驾","还有商务接送","不承诺最低价"],
    「不承诺最低价」凭空成了一个独立条目。本单因此不复用那两列。

    🔴 措辞要准:**不能说「`success_cases` 是数组」**。
       它的形态**取决于写入方** —— `db/profile_db.py` 的 `_json_text_or_none`
       只在调用方传 list/dict 时才 `json.dumps`,传 str 则**原样存**。
       所以生产上一部分档案里它是字符串、一部分是 JSON。
       结论(禁写、不复用)不变,但理由是「形态不定且会被这条路切碎」,
       不是「它是个数组」。把形态说死会让下一个人按错误的前提去改它。
    """
    pid = _new_profile()
    assert _save(pid, SAMPLES), "update_profile 返回假 —— 有列不在写白名单里"
    got = from_profile(get_profile(pid))
    for field in BASIC_FIELDS:
        assert got[field] == SAMPLES[field], (
            "%s 往返不一致:写 %r,读回 %r —— 用户的原话被改了"
            % (field, SAMPLES[field], got[field]))


def test_business_summary_column_is_actually_written():
    """🔴 `business_summary` 这一列**一直存在**,但本单之前从来不在写白名单里 ⇒
    `update_profile(business_summary=...)` 静默返回 False、列恒为 NULL。
    一个从不被写的列,让任何基于它的读数都没有意义。直接看**列**,不看回包。
    """
    pid = _new_profile()
    assert _save(pid, {"business_summary": SAMPLES["business_summary"]})
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT business_summary FROM client_profiles WHERE id=%s", (pid,))
        row = dict(cur.fetchone())
    finally:
        c.close()
    assert row["business_summary"] == SAMPLES["business_summary"], (
        "business_summary 列里是 %r —— 白名单没放行,写入被静默丢掉" % row["business_summary"])


# ── R2 只改一栏不碰别栏 ──────────────────────────────────────────────

def test_updating_one_field_leaves_the_other_five_untouched():
    """🔴 JSONB 那两个字段是**合并写**,不是整块替换。

    整块替换会让「改一栏」变成「清掉同住一列的另一栏」,
    而且不会有任何东西报错 —— 用户只会发现「我明明填过的案例没了」。
    """
    pid = _new_profile()
    assert _save(pid, SAMPLES)
    changed = "改成:只有日租"
    assert _save(pid, {"products_services": changed})

    got = from_profile(get_profile(pid))
    assert got["products_services"] == changed
    for field in BASIC_FIELDS:
        if field == "products_services":
            continue
        assert got[field] == SAMPLES[field], (
            "改 products_services 把 %s 也改了:%r -> %r" % (field, SAMPLES[field], got[field]))


def test_the_jsonb_column_keeps_both_of_its_tenants():
    """同住 JSONB 的两个字段,单独写一个时另一个必须在列里原样留着(直接看列)。"""
    pid = _new_profile()
    assert _save(pid, {"proof_cases": SAMPLES["proof_cases"]})
    assert _save(pid, {"products_services": SAMPLES["products_services"]})
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT %s AS blob FROM client_profiles WHERE id=%%s" % JSONB_COLUMN, (pid,))
        blob = dict(cur.fetchone())["blob"]
    finally:
        c.close()
    if isinstance(blob, str):
        blob = json.loads(blob)
    assert set(JSONB_FIELDS) <= set(blob or {}), (
        "%s 里只剩 %s —— 第二次写整块替换掉了第一次" % (JSONB_COLUMN, sorted(blob or {})))
    assert blob["proof_cases"] == SAMPLES["proof_cases"]


# ── R3 禁写列零写入(反臂)────────────────────────────────────────────

def test_saving_the_form_writes_nothing_into_the_forbidden_columns():
    """🔴 反臂:整张表单存一遍,三个禁写列必须**仍然是 NULL**。

    只断言「六个字段对」是不够的 —— 那只说明我写对了地方,
    不说明我**没同时写错地方**。飞轮那一列被污染不会让任何判据变红。
    """
    pid = _new_profile()
    assert _save(pid, SAMPLES)
    c = conn()
    try:
        cur = c.cursor()
        cur.execute(
            "SELECT negative_feedback, products, success_cases "
            "FROM client_profiles WHERE id=%s", (pid,))
        row = dict(cur.fetchone())
    finally:
        c.close()
    for col, val in row.items():
        assert val is None, (
            "保存基础资料表把 %s 也写了:%r —— 那一列不归这张表管" % (col, val))


def test_contract_raises_if_a_mapping_ever_targets_a_forbidden_column(monkeypatch):
    """契约出门前的自检必须**真的会抛**,不是写在注释里的承诺。

    🔴 这条的第一版是假的:我自己 `raise AssertionError` 再自己 `pytest.raises` 接住,
       对被测代码一个字都没断言 —— 判据名声称的事,断言从来没验过。
       真的验法是**把映射改坏**(指向禁写列),再走一遍 `to_update_kwargs`。
    """
    import services.writing_basics_contract as contract

    broken = dict(contract.COLUMN_FOR)
    broken["forbidden_notes"] = "negative_feedback"     # 正是前端读链的第三优先
    monkeypatch.setattr(contract, "COLUMN_FOR", broken)

    with pytest.raises(AssertionError) as ei:
        contract.to_update_kwargs({"forbidden_notes": "不许说最便宜"}, None)
    assert "禁写列" in str(ei.value) and "negative_feedback" in str(ei.value), str(ei.value)

    # 正样本:映射没坏时同一个调用不许抛(否则上面那条是恒红,不是有牙)
    monkeypatch.undo()
    assert contract.to_update_kwargs({"forbidden_notes": "不许说最便宜"}, None) == {
        "brand_constraints": "不许说最便宜"}


# ── R4 老档案 ────────────────────────────────────────────────────────

def test_a_profile_created_before_this_migration_reads_as_six_empty_strings():
    """🔴 老档案这些位置本来就没东西,那不是错误 —— 读出 None 要返空串,不许抛。"""
    pid = _new_profile("建于迁移之前")
    got = from_profile(get_profile(pid))
    assert set(got) == set(BASIC_FIELDS)
    assert all(v == "" for v in got.values()), got


def test_from_profile_on_none_does_not_blow_up():
    """档案不存在(404 路径)时也不许抛 —— 仪器坏了要出声,但这不是仪器坏。"""
    assert from_profile(None) == {f: "" for f in BASIC_FIELDS}


# ── R5 不传 vs 传空串 ────────────────────────────────────────────────

def test_a_field_not_submitted_is_not_overwritten():
    """不传 = 不覆盖。"""
    pid = _new_profile()
    assert _save(pid, SAMPLES)
    assert _save(pid, {"business_summary": "只改这一栏"})
    got = from_profile(get_profile(pid))
    assert got["business_summary"] == "只改这一栏"
    assert got["forbidden_notes"] == SAMPLES["forbidden_notes"], "没传的字段被清掉了"


def test_an_explicitly_empty_string_does_clear_the_field():
    """🔴 显式传 "" = 用户把这一栏删空了,**必须写进去**。

    与「不传」不是一回事。把空串当成「没传」的话,用户永远删不掉已填内容,
    而界面上看起来保存成功了。
    """
    pid = _new_profile()
    assert _save(pid, SAMPLES)
    assert _save(pid, {"forbidden_notes": "", "proof_cases": ""})
    got = from_profile(get_profile(pid))
    assert got["forbidden_notes"] == "", "显式空串没能清空真列那一路"
    assert got["proof_cases"] == "", "显式空串没能清空 JSONB 那一路"
    assert got["business_summary"] == SAMPLES["business_summary"], "顺手把别的清了"


def test_none_means_not_submitted_not_clear():
    """FastAPI 把缺省字段填成 None —— None 必须当「没传」,不能当「清空」。"""
    assert to_update_kwargs({"business_summary": None}, None) == {}
    assert to_update_kwargs({}, None) == {}


# ── R6 别名转换的位置 ────────────────────────────────────────────────

def test_alias_conversion_happens_before_the_downstream_syncs():
    """🔴 别名转换必须在 completeness / persona 同步 / business 同步**之前**。

    那些下游只认真列名(`target_users` 等)。转换放在它们后面的话,
    从写作大厅走别名写进来的值会**绕过全部下游同步,而且一声不响** ——
    落库是对的,派生的东西全不更新,查起来毫无线索。

    钉法:在 `api/profile_api.py` 的 PUT 函数体内,按**出现次序**断言
    `to_update_kwargs` 早于三个下游标记。行号顺序在这里就是执行顺序
    (同一函数体、无分支跳转)。
    """
    from tests._shared.source_slice import code_only, function_body

    body = code_only(function_body("api/profile_api.py", "api_update_profile"))
    conv = body.find("to_update_kwargs(")
    assert conv > 0, "PUT 里找不到 to_update_kwargs —— 别名根本没被翻译"
    for marker in ("completion_fields", "_PERSONA_FIELDS", "_BUSINESS_FIELDS"):
        pos = body.find(marker)
        assert pos > 0, "PUT 里找不到下游标记 %s —— 判据的参照物没了,先核对象" % marker
        assert conv < pos, (
            "别名转换出现在 %s 之后 —— 走别名写进来的值会绕过这一路同步" % marker)


def test_put_strips_alias_keys_before_calling_update_profile():
    """别名不是列名。忘了剔除的话,`update_profile` 的白名单会静默丢掉它们,
    表现为「保存成功但没存上」—— 与本单要修的缺陷同形。"""
    from tests._shared.source_slice import code_only, function_body

    body = code_only(function_body("api/profile_api.py", "api_update_profile"))
    assert "update_data.pop(_f, None)" in body, "PUT 没有剔除别名键"


# ── R7 迁移接线 ──────────────────────────────────────────────────────

def test_migration_is_registered_in_the_manifest():
    """🔴 迁移号由登记处分配,分支自取只是临时号。

    没登记 = `prestart` 永远不会跑它 = 生产上那一列不存在,
    而判据在自己建的库上全绿。
    """
    manifest = io.open(REPO / "db" / "migration_manifest.py", encoding="utf-8").read()
    name = "db/migration_062_client_profile_basic_info_fields_2026_09_16.sql"
    assert name in manifest, "迁移 062 没登记进 manifest —— prestart 不会跑它"
    assert (REPO / name).exists(), "manifest 里登记了但文件不在"


def test_the_migration_body_contains_no_dml():
    """迁移体内禁 DML(本仓红线)。"""
    sql = io.open(
        REPO / "db" / "migration_062_client_profile_basic_info_fields_2026_09_16.sql",
        encoding="utf-8").read()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE"):
        assert verb not in code.upper(), "迁移体内出现 DML:%s" % verb


def test_self_healing_ddl_and_migration_agree_on_the_column():
    """🔴 本仓一张表有四套 schema 定义。只改迁移,旧库上那一列不会出现;
    只改自愈 DDL,生产上不会出现。两处的列名与类型必须逐字一致。"""
    sql = io.open(
        REPO / "db" / "migration_062_client_profile_basic_info_fields_2026_09_16.sql",
        encoding="utf-8").read()
    heal = io.open(REPO / "db" / "profile_db.py", encoding="utf-8").read()
    assert "basic_info_fields JSONB DEFAULT '{}'::jsonb" in sql.replace("\n", " ")
    assert "('basic_info_fields', \"JSONB DEFAULT '{}'::jsonb\")" in heal, (
        "自愈 DDL 里没有这一列,或类型与迁移不一致")
