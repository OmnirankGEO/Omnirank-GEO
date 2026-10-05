"""#96 · 提交者能查到自己反馈的状态,且**看不到内部备注**。

改前:能提交、能去重、能结单 —— 但结单之后 `user_notifications` 一条不写、
提交者也没有任何查询出口。她提完就再也看不到下文,只能当石沉大海。

🔴 本单只补**状态出口**。「公开回复」需要一列独立字段(表上只有 `admin_note`,
   那是**内部备注**),没有加 —— 拿内部备注冒充回复是内部口径外泄,
   而且它长得就像一个正常文本字段,**不会有任何东西报警**。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import types
import uuid

import psycopg2
import psycopg2.extras
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ME, OTHER = 970101, 970102


def _req(uid):
    return types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": uid, "is_admin": False}))


@pytest.fixture
def world(db):
    tag = uuid.uuid4().hex[:8]
    made = {}
    with db.cursor() as cur:
        for uid in (ME, OTHER):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name, is_active) "
                "VALUES (%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
                (uid, f"wo96_{uid}_{tag}", f"WO96-{uid}"))
            cur.execute(
                "INSERT INTO faq_feedback (client_id, message, user_id, status, admin_note) "
                "VALUES (%s,%s,%s,'done',%s) RETURNING id",
                (f"c_{tag}_{uid}", f"反馈内容 {uid}", uid, f"内部备注-不该外泄-{uid}"))
            made[uid] = int(cur.fetchone()["id"])
    db.commit()
    yield made
    with db.cursor() as cur:
        cur.execute("DELETE FROM faq_feedback WHERE id = ANY(%s)", (list(made.values()),))
        cur.execute("DELETE FROM users WHERE id = ANY(%s)", ([ME, OTHER],))
    db.commit()


def test_fixture_really_wrote_an_internal_note(db, world):
    """🔴 分母自证:库里确实有内部备注。

    这一条为空时,下面「响应里没有 admin_note」就**不可解读** ——
    它可能只是因为根本没有备注可泄。
    """
    with db.cursor() as cur:
        cur.execute("SELECT admin_note FROM faq_feedback WHERE id=%s", (world[ME],))
        assert cur.fetchone()["admin_note"].startswith("内部备注")


def test_submitter_sees_own_feedback_with_status(world):
    from api.faq_api import api_my_feedback
    out = asyncio.run(api_my_feedback(_req(ME)))
    ids = {int(x["id"]) for x in out["items"]}
    assert world[ME] in ids, out
    row = next(x for x in out["items"] if int(x["id"]) == world[ME])
    assert row["status"] == "done", row


def test_response_never_carries_the_internal_note(world):
    """🔴 `admin_note` 一个字都不许出现在响应里。

    断言打在**整个响应的键集合**上,不是「那一行没有这个键」——
    将来有人在别处补一个字段,按行断言不会红。
    """
    from api.faq_api import api_my_feedback
    out = asyncio.run(api_my_feedback(_req(ME)))
    assert out["items"], "没数据 ⇒ 这条什么都没验"
    for row in out["items"]:
        assert "admin_note" not in row, row
    assert "内部备注" not in str(out), "内部备注文本泄漏进了响应"


def test_other_users_feedback_is_not_returned(world):
    """🔁 归属臂:别人的反馈不可见。

    没有它,上面两条全绿也可能是因为**返回了所有人的反馈**(越权),
    而那比原缺陷严重得多 —— 两者在「我能看到自己的」这个读数上完全同形。
    """
    from api.faq_api import api_my_feedback
    out = asyncio.run(api_my_feedback(_req(ME)))
    ids = {int(x["id"]) for x in out["items"]}
    assert world[OTHER] not in ids, "看到了别人的反馈"


def test_ownership_is_filtered_in_where_not_after_the_fact():
    """🔴 归属过滤必须在 WHERE 里,不是查出来再比。

    查出来再比的代码,漏一个 return 就把别人的反馈发出去了 ——
    而那一天不会有任何判据变红(除非有人恰好写了上面那条归属臂)。
    """
    src = (ROOT / "db" / "faq_db.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "list_feedback_for_user")
    # 🔴 只取函数体里的**字符串常量**(即 SQL 本身),不扫整段源码:
    #    docstring 里正解释「为什么不返回 admin_note」,按整段扫会被自己的散文打红
    #    —— 今天第三次踩同一个坑(#72 检测器、#78 文案门,这是第三次)。
    sqls = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value]
    assert sqls, "函数里没找到 SQL —— 分母为空,这条什么都没验"
    joined = chr(10).join(sqls)   # 用 chr(10),不写转义字面量
    assert "WHERE f.user_id = %s" in joined, "归属没进 WHERE"
    assert "admin_note" not in joined, "SELECT 里选了内部备注"


def test_unauthenticated_is_rejected():
    from fastapi import HTTPException
    from api.faq_api import api_my_feedback
    req = types.SimpleNamespace(state=types.SimpleNamespace(user=None))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api_my_feedback(req))
    assert ei.value.status_code == 401
