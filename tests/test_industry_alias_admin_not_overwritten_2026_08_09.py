# -*- coding: utf-8 -*-
"""admin 行业改判不得被 LLM 覆盖 · 真 PG 并发判别锁(2026-08-09 · Review P1 返工)

## 缺陷

`write_alias` 原来是无条件 `ON CONFLICT (normalized_alias) DO UPDATE`,把
`industry_id / resolved_by / reviewed_by / active` 一律改成本次入参;
调用方用 `if resolve_alias(...) is None: write_alias(...)` 做守卫 ——
典型的 **check-then-act 非原子**。两句之间落进来的写会被无声覆盖,
包括 **admin 人工改判**(`update_alias_industry` 置 `resolved_by='admin'` + `reviewed_by`)。
调用方注释宣称的「不覆盖 admin 人工改判」,旧实现**保证不了那句话**。

## 🔴 关于可达性,如实写(定级依据要准)

我按当前代码逐条走过,**通过产品路径**这条覆盖走不通:
  · admin **没有新建别名的入口** —— 全仓唯一 `INSERT INTO geo_research_industry_aliases`
    就是 `write_alias` 自己;`update_alias_industry(alias_id, ...)` 只能改**已存在**的行。
  · 别名一旦存在且 active,`resolve_readonly` 第 1 步就命中早返,走不到 LLM 与写别名。
  · 唯一能绕开的是 `active=false` 的别名(`resolve_alias` 过滤 active)——
    而**全仓没有任何把别名置 inactive 的代码**,生产实测 total=3 / inactive=0 / admin=0。

所以它是**真实的健壮性缺陷,但当前没有可达的 admin 覆盖路径** ——
离可达只有一个改动之遥(谁加个 admin 建别名入口、或别名停用功能,立刻可达;
停用那条还是**确定性**覆盖而不是竞态)。

**这不是降级理由** —— 修复廉价、严格更好、且能让注释那句话变成真的,所以照修。
下面的用例因此**必须构造**:直接把 admin 那条写进库,再让 LLM 侧去写。
这与"真数据零触发就得造用例"是同一条规矩。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from db.research_selfserve_db import (
    resolve_alias, update_alias_industry, write_alias,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
DB_SRC = REPO / "db" / "research_selfserve_db.py"

_ALIAS = "adminwin_probe_行业原文"
_IND_ADMIN = "adminwin_probe_行业A"
_IND_LLM = "adminwin_probe_行业B"
_ADMIN_UID = 4242


def _strip_py_comments(src: str) -> str:
    """🔴 剥注释再判 —— 本仓「判据命中自己写的注释」已翻过四次车。"""
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in src.splitlines())


# ===========================================================================
# 结构:SQL 必须是原子「仅首次写入」
# ===========================================================================

def test_write_alias_sql_is_insert_if_absent_not_upsert():
    src = _strip_py_comments(DB_SRC.read_text(encoding="utf-8"))
    i = src.find("def write_alias(")
    assert i > 0
    body = src[i: src.find("\ndef ", i + 10)]
    assert "ON CONFLICT (normalized_alias) DO NOTHING" in body, \
        "write_alias 不是「仅首次写入」—— 守卫又回到调用方的时间差里了"
    assert "DO UPDATE" not in body, "write_alias 里还有 DO UPDATE 分支"


def test_write_alias_rereads_after_the_insert():
    """`DO NOTHING` 时 `RETURNING` 不出行 —— 必须无条件再读一次,否则返回值恒 None。"""
    src = _strip_py_comments(DB_SRC.read_text(encoding="utf-8"))
    i = src.find("def write_alias(")
    body = src[i: src.find("\ndef ", i + 10)]
    assert "SELECT industry_id, resolved_by" in body, "写完没有重读生效值"
    assert body.find("SELECT industry_id, resolved_by") > body.find("DO NOTHING"), \
        "重读写在 INSERT 之前 —— 那读的是写之前的状态"


def test_no_caller_does_check_then_act_anymore():
    """🔴 反向对照:三个调用点都不许再自己写 `if resolve_alias(...) is None:` 守卫。

    留着它不算错,但它会让人以为守卫在那儿 —— 而真正的守卫在数据库里。
    更糟的是它是**假的**:两句之间仍有时间差。
    """
    pat = re.compile(r"if\s+resolve_alias\([^)]*\)\s+is\s+None\s*:")
    for rel in ("services/industry_canonical.py",
                "services/research_monitor/industry_resolver.py",
                "api/research_selfserve_api.py"):
        src = _strip_py_comments((REPO / rel).read_text(encoding="utf-8"))
        assert not pat.search(src), f"{rel} 还留着 check-then-act 守卫"


# ===========================================================================
# 真库行为:admin 先落,LLM 后写 → 必须保住 admin
# ===========================================================================

@pytest.fixture
def seeded_admin_alias():
    """造出 admin 已改判的那条别名,并在用例结束后清干净(幂等)。"""
    from db.connection import get_connection

    # 🔴 造行业用**仓库自己的** `ensure_research_industry`,不手写 INSERT ——
    #    我第一版手写,当场撞 `geo_research_industries.slug` 的 NOT NULL
    #    (SQL 4 维核验里"列名/data_type"那两条)。手写 DDL/DML 造 fixture,
    #    等于在测试里维护第二份 schema 知识,迟早和真表打架。
    from services.research_monitor.industry_registry import ensure_research_industry

    def _ensure_industry(name: str) -> int:
        return int(ensure_research_industry(name))

    def _purge():
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM geo_research_industry_aliases "
                        "WHERE normalized_alias LIKE %s", ("%adminwin_probe%",))
            conn.commit()
        finally:
            conn.close()

    a_id, b_id = _ensure_industry(_IND_ADMIN), _ensure_industry(_IND_LLM)
    # 🔴 每次开跑先清 —— 上一轮残留会让"仅首次写入"在第二轮变成"本来就有",
    #    用例照样绿但**证明力是假的**(我在上一个包被自己的残留骗过一次)。
    _purge()

    # ① 别名先由某次 llm 归并落地(admin 没有建别名的入口,只能改已有的)
    first = write_alias(_ALIAS, b_id, 0.9, resolved_by="llm")
    assert first and int(first["industry_id"]) == b_id
    # ② admin 人工改判到 A
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM geo_research_industry_aliases "
                    "WHERE normalized_alias = %s", (_ALIAS,))
        alias_id = int(cur.fetchone()["id"])
    finally:
        conn.close()
    update_alias_industry(alias_id, a_id, reviewed_by=_ADMIN_UID)
    yield {"alias_id": alias_id, "admin_industry_id": a_id, "llm_industry_id": b_id}
    _purge()


def _current(alias: str) -> dict:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT industry_id, resolved_by, reviewed_by, active
              FROM geo_research_industry_aliases WHERE normalized_alias = %s
        """, (alias,))
        return dict(cur.fetchone())
    finally:
        conn.close()


def test_llm_write_does_not_overwrite_admin_mapping(seeded_admin_alias):
    """🔴 主判据:admin 改判之后,LLM 再写同一别名 → 行业 / resolved_by / 审核信息全部不动。"""
    s = seeded_admin_alias
    before = _current(_ALIAS)
    assert before["resolved_by"] == "admin" and before["reviewed_by"] == _ADMIN_UID

    ret = write_alias(_ALIAS, s["llm_industry_id"], 0.99, resolved_by="llm")

    after = _current(_ALIAS)
    assert after["industry_id"] == s["admin_industry_id"], "admin 的行业被 LLM 覆盖了"
    assert after["resolved_by"] == "admin", f"resolved_by 被改成了 {after['resolved_by']}"
    assert after["reviewed_by"] == _ADMIN_UID, "审核信息被抹掉了"
    # 返回值必须是**库里生效的那条**,不是调用方想写的那条
    assert ret and int(ret["industry_id"]) == s["admin_industry_id"], \
        f"返回的不是生效值:{ret}"
    assert ret["resolved_by"] == "admin"


@pytest.mark.asyncio
async def test_freeze_industry_adopts_the_admin_mapping(seeded_admin_alias, monkeypatch):
    """🔴 判据打在**主链真实入口**上,不是直调 `write_alias`。

    `freeze_industry` 里 LLM 判成 B,而库里 admin 已定 A → 冻结件必须是 **A**。
    (打桩的只有 LLM 那一跳 —— 它是外部依赖;别名写入与重读走真库真实现。)
    """
    import services.industry_canonical as ic
    s = seeded_admin_alias

    async def _fake_llm(raw, **kw):
        return {"industry_id": s["llm_industry_id"], "industry_name": _IND_LLM,
                "resolved_by": "llm", "confidence": 0.99, "is_new": False}

    import services.research_monitor.industry_resolver as ir
    monkeypatch.setattr(ir, "resolve_or_create_industry", _fake_llm, raising=True)

    ci = await ic.freeze_industry(_ALIAS)
    assert ci.industry_id == s["admin_industry_id"], (
        f"冻结用了 LLM 的行业而不是 admin 的:{ci.industry_id} != {s['admin_industry_id']}")
    assert ci.canonical_name == _IND_ADMIN, f"冻结的行业名不是 admin 定的:{ci.canonical_name}"
    assert _current(_ALIAS)["resolved_by"] == "admin", "冻结过程把 admin 改成了 llm"


def test_first_write_still_works(seeded_admin_alias):
    """成对反向:别名**不存在**时必须照常写进去 —— 否则守卫把正常沉淀也挡了。

    (没有这条,把 write_alias 改成"什么都不做"也能让上面几条全绿。)
    """
    s = seeded_admin_alias
    fresh = _ALIAS + "_全新"
    assert resolve_alias(fresh) is None
    ret = write_alias(fresh, s["llm_industry_id"], 0.8, resolved_by="llm")
    assert ret and int(ret["industry_id"]) == s["llm_industry_id"], "首次写入被挡住了"
    assert resolve_alias(fresh) == s["llm_industry_id"]


# ===========================================================================
# 🔴 停用别名不得被当成有效缓存(Review 第四轮 P1)
# ===========================================================================
#
# 我上一版把 `active` 判断塞进了"生效 ID ≠ 我写的 ID"那个分支里,于是
# **停用别名恰好指向 LLM 选中的同一个行业**时整段判断被跳过,照样返回
# `resolved_by=alias_cache`。Review 在 PG16 上构造出来了:
#     返回 industry_id=6 / resolved_by=alias_cache;库里 active=false / industry_id=6
# 「是不是同一个行业」与「它停没停用」是**两个独立条件**,写进一个 if 就是把它们绑死。
#
# 🔴 同 ID / 不同 ID 两条都要有 —— 只测"不同 ID"那条,正是上一版全绿却有洞的原因。


def _deactivate(alias: str) -> None:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE geo_research_industry_aliases SET active = FALSE "
                    "WHERE normalized_alias = %s", (alias,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_industry", [True, False],
                         ids=["停用且同ID", "停用且不同ID"])
async def test_inactive_alias_is_never_treated_as_a_valid_cache(
        seeded_admin_alias, monkeypatch, same_industry):
    """🔴 主链判据:别名 `active=false` 时,`freeze_industry` **一律不得**返回 alias_cache。

    两个参数化分支缺一不可:
      · 同 ID   —— 上一版的洞就在这里(active 判断被 ID 比较挡在门外)。
      · 不同 ID —— 上一版能过,留着做成对反向,防止修的时候把另一半改坏。
    """
    import services.industry_canonical as ic
    import services.research_monitor.industry_resolver as ir
    s = seeded_admin_alias
    _deactivate(_ALIAS)

    llm_id = s["admin_industry_id"] if same_industry else s["llm_industry_id"]

    async def _fake_llm(raw, **kw):
        return {"industry_id": llm_id, "industry_name": _IND_ADMIN if same_industry else _IND_LLM,
                "resolved_by": "llm", "confidence": 0.99, "is_new": False}

    monkeypatch.setattr(ir, "resolve_or_create_industry", _fake_llm, raising=True)

    ci = await ic.freeze_industry(_ALIAS)
    assert ci.resolved_by != ic.BY_ALIAS_CACHE, (
        f"停用的别名被当成有效缓存了(same_industry={same_industry}):"
        f"resolved_by={ci.resolved_by} industry_id={ci.industry_id}")
    assert not ci.merged, f"停用别名却报 merged(industry_id={ci.industry_id})"
    # 反向对照:库里那条**仍然**是 admin 的、仍然停用 —— 没被这一趟改写
    cur = _current(_ALIAS)
    assert cur["active"] is False and cur["resolved_by"] == "admin", \
        f"停用/归属被这一趟改动了:{cur}"
