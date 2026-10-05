# -*- coding: utf-8 -*-
"""WO_267 追加一笔(A 09-23 提):`/api/history` 与 `GET /api/my-clients/{id}` 也附大类 key / 中文名。

前端(A · 2721da3ab)写成「有 `industry_category_key` 就按 key 选中」。这两个端点原来只回
`industry_category` 原值,于是存量旧名(房产家居…)对不上字典名:历史筛选只落「所有行业」,
客户详情的下拉显示「自动判断」。

一格判据,两个端点共用同一张存量形状表(第 2 格 `客户详情` 要库):
  · 存量旧名 ⇒ 翻成新 key + 大类中文名;
  · 新 key ⇒ key 原样,名字取字典;
  · 认不出 ⇒ key=None、name=原值(对照臂:不许被「猜」进某个大类);
  · 空 ⇒ 两个都 None。
原字段 `industry_category` 一律原样保留(只增不改)。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

#: (库里存的原值, 期望 key, 期望 name)。前两行是被修的形状,后两行是对照臂。
SHAPES = [
    ("房产家居", "real_estate", "房地产"),
    ("new_energy", "new_energy", "新能源"),
    ("认不出的值xyz", None, "认不出的值xyz"),
    (None, None, None),
]


class _Req:
    def __init__(self, user):
        self.state = type("S", (), {"user": user, "organization_identity": None})()


def _history_rows(monkeypatch):
    import auth.brand_access as brand_access
    import db.diagnosis_db as diagnosis_db
    import server

    stored = [{"id": i + 1, "brand_id": 7, "industry_category": raw}
              for i, (raw, _k, _n) in enumerate(SHAPES)]
    monkeypatch.setattr(server, "list_diagnoses", lambda **kw: [dict(r) for r in stored])
    monkeypatch.setattr(brand_access, "get_user_brand_filter", lambda request: None)
    monkeypatch.setattr(diagnosis_db, "count_diagnoses", lambda **kw: len(stored))
    out = server.get_history(request=_Req({"user_id": 1, "is_admin": True}))
    assert "data" in out, out
    return out["data"]


def _client_detail_rows():
    from api.brand_api import get_client_detail
    from db.connection import get_connection

    tag = uuid.uuid4().hex[:8]
    conn = get_connection()
    ids: list[int] = []
    try:
        cur = conn.cursor()
        for raw, _k, _n in SHAPES:
            cur.execute(
                "INSERT INTO brands (name, owner_user_id, industry_category) "
                "VALUES (%s, %s, %s) RETURNING id",
                (f"wo267读侧判据_{tag}_{len(ids)}", 1, raw))
            ids.append(cur.fetchone()["id"])
        conn.commit()
        rows = []
        for bid in ids:
            out = asyncio.run(get_client_detail(bid, _Req({"user_id": 1, "is_admin": True})))
            rows.append(out["brand"])
        return rows
    finally:
        cur = conn.cursor()
        cur.execute("DELETE FROM brands WHERE id = ANY(%s)", (ids,))
        conn.commit()
        conn.close()


@pytest.mark.parametrize("surface", ["history", "client_detail"])
def test_read_side_endpoints_carry_category_key_and_name(surface, monkeypatch):
    rows = _history_rows(monkeypatch) if surface == "history" else _client_detail_rows()
    assert len(rows) == len(SHAPES), "分母自证:四种存量形状都得回来,少一行后面的断言没意义"
    for row, (raw, key, name) in zip(rows, SHAPES):
        assert row["industry_category"] == raw, "原字段必须原样保留(只增不改)"
        # 🔴 用 `in` 先断言键在:对照臂期望 None,`row.get()` 在「压根没附字段」时也返回 None ——
        #    那两行会空转成绿。键必须在,值再比。
        assert "industry_category_key" in row and "industry_category_name" in row, (surface, row)
        assert row["industry_category_key"] == key, (surface, raw, row)
        assert row["industry_category_name"] == name, (surface, raw, row)
