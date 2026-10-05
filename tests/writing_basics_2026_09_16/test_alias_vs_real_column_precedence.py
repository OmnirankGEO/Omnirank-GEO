# -*- coding: utf-8 -*-
"""WO_220-c2' · 别名 vs 真列名 的**优先级**要有牙。

🔴 这个包为什么会有第二个文件:**我在代码注释里写了方向,还在跨窗消息里说
   「已定死并会配锁」—— 而那条锁我根本没写。**

Review 复审下的那发毒(P7)把 `api/profile_api.py` 里的

       update_data = {**to_update_kwargs(_basic_submitted, profile), **update_data}

   换成

       update_data = {**update_data, **to_update_kwargs(_basic_submitted, profile)}

(别名反过来压过真列名),原有 19 条判据**照样全绿**。
注释挡不住任何东西;「我做了」和「坏了会出声」是两件事。

🔴 为什么必须走**真 handler**:那条优先级住在 PUT 里的 dict 合并,
   **不在** `services/writing_basics_contract` 里。
   只调 `to_update_kwargs` 的判据永远看不见它 —— 那正是原来那 19 条全绿的原因。
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.writing_basics_2026_09_16.conftest import PID_PREFIX, conn  # noqa: E402

from db.profile_db import get_profile                                  # noqa: E402
from services.writing_basics_contract import from_profile              # noqa: E402


def _new_profile(name="220c2' 优先级") -> str:
    pid = PID_PREFIX + uuid.uuid4().hex[:10]
    c = conn()
    try:
        c.cursor().execute(
            "INSERT INTO client_profiles (id, name) VALUES (%s, %s)", (pid, name))
    finally:
        c.close()
    return pid


def _put(pid: str, payload: dict, monkeypatch):
    """走**真的** PUT handler(`api_update_profile`),只把 RBAC 换成放行。

    🔴 不用 TestClient:那要拉起整个 app 和鉴权链,而本条要测的只是
       handler 函数体内的那一次 dict 合并。把 Request 换成一个占位对象、
       把 `require_brand_access` 换成 no-op,被测的那段逻辑一行没绕过。
    """
    import api.profile_api as papi

    monkeypatch.setattr(papi, "get_profile", get_profile, raising=False)
    import auth.brand_access as ba
    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)

    data = papi.ProfileUpdate(**payload)
    fake_request = types.SimpleNamespace(state=types.SimpleNamespace())
    return asyncio.run(papi.api_update_profile(pid, data, fake_request))


def test_real_column_wins_when_both_are_sent(monkeypatch):
    """🔴 同一请求同时传别名和真列名 ⇒ **以真列名为准**。

    方向本身是可以商量的(真列名是既有契约,客户档案页一直用它);
    **没得商量的是「必须定死一个方向、并且有东西守着它」** ——
    否则写作大厅和客户档案页会互相覆盖,而没有任何东西看得出来。
    """
    pid = _new_profile()
    _put(pid, {"forbidden_notes": "A-来自别名", "brand_constraints": "B-来自真列"},
         monkeypatch)

    got = from_profile(get_profile(pid))
    assert got["forbidden_notes"] == "B-来自真列", (
        "同传时读回 %r —— 别名压过了真列名,优先级反了" % got["forbidden_notes"])


def test_alias_alone_still_lands(monkeypatch):
    """🔴 反向臂:只传别名时必须**真的写进去**。

    没有这一臂的话,上一条在「别名根本没生效」时也会绿 ——
    那时读回的当然是真列名那份,而原因完全不同。
    一条只在「另一边赢」时成立的断言,不能证明优先级,只能证明别名没用。
    """
    pid = _new_profile()
    _put(pid, {"forbidden_notes": "A-来自别名"}, monkeypatch)

    got = from_profile(get_profile(pid))
    assert got["forbidden_notes"] == "A-来自别名", (
        "只传别名却读回 %r —— 别名这条路没通,上一条判据因此没有区分力"
        % got["forbidden_notes"])


def test_real_column_alone_still_lands(monkeypatch):
    """第二条反向臂:只传真列名也要写进去(否则「真列名赢」可能只是它恰好没被动)。"""
    pid = _new_profile()
    _put(pid, {"brand_constraints": "B-来自真列"}, monkeypatch)

    got = from_profile(get_profile(pid))
    assert got["forbidden_notes"] == "B-来自真列", (
        "只传真列名却读回 %r" % got["forbidden_notes"])


def test_precedence_holds_for_every_aliased_field(monkeypatch):
    """🔴 四个「别名 → 真列」的字段**逐个**验,不是只验 forbidden_notes 一个。

    只验一个的话,别的字段哪天接错方向不会有任何提示 ——
    工单点名的那个实例不是缺陷类本身。
    """
    from services.writing_basics_contract import COLUMN_FOR

    aliased = {f: c for f, c in COLUMN_FOR.items() if f != c}
    assert aliased, "映射里没有「别名 ≠ 列名」的字段 —— 这条锁的参照物没了,先核对象"

    for field, column in sorted(aliased.items()):
        pid = _new_profile()
        _put(pid, {field: "别名值-%s" % field, column: "真列值-%s" % column}, monkeypatch)
        got = from_profile(get_profile(pid))
        assert got[field] == "真列值-%s" % column, (
            "%s/%s 同传时读回 %r —— 这个字段的优先级方向与其它几个不一致"
            % (field, column, got[field]))


def test_the_merge_order_is_pinned_in_the_source(monkeypatch):
    """兜底的**结构锁**:合并次序写死在源码里。

    行为锁(上面几条)是主锁;这一条防的是「合并被挪走/改写成别的形式」之后
    行为锁恰好还成立的情况。两条机制不同,一起才盖得住。
    """
    from tests._shared.source_slice import code_only, function_body

    body = code_only(function_body("api/profile_api.py", "api_update_profile"))
    assert "{**to_update_kwargs(_basic_submitted, profile), **update_data}" in body, (
        "PUT 里的合并次序变了 —— 真列名必须在**后**(后者覆盖前者)")
    assert "{**update_data, **to_update_kwargs(" not in body, (
        "出现了反过来的合并次序 —— 别名会压过真列名")
