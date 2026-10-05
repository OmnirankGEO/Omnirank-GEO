# -*- coding: utf-8 -*-
"""WO_220-c2'' · 两个端点必须回**同一份** writing_basics。

🔴 这个文件为什么存在:c2 交付时我只把 `writing_basics` 挂在
   `GET /api/profiles/{id}` 上,而写作大厅读的是
   `GET /api/my-clients/{brand_id}`(`api/brand_api.py` 的 `get_client_detail`)——
   **c2 上线后 products_services / proof_cases 仍然永远是空的**。

   我那 24 条判据一条都没红,因为它们全在验「做事方」(profiles 端点与契约),
   **没有一条去问被服务方**:前端实际调的那个接口到底回没回。
   是 A 的端到端第一发逮到的。

   ⇒ 判据里至少要有一条,钉的是**前端真正调用的那个面**。
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

from db.profile_db import get_profile, update_profile                  # noqa: E402
from services.writing_basics_contract import (                          # noqa: E402
    BASIC_FIELDS, to_update_kwargs)

SAMPLES = {
    "business_summary": "我们做租车、代驾,还有商务接送,不承诺最低价",
    "target_customers": "本地中小企业主、需要长期用车的公司,以及机场接送客户",
    "products_services": "日租、月租、带司机商务接送,含保险",
    "key_selling_points": "车龄新、24 小时响应,可开专票",
    "proof_cases": "某租车公司,3 个月询盘翻倍、成本降 20%",
    "forbidden_notes": "不许说最便宜、不许承诺一定能上榜,不提竞品名",
}

#: 本包自己的品牌 id 段,清理只认它。
BRAND_ID = 990220


def _seed(with_values: bool):
    """建一个 brand + 挂在它下面的档案。返回 (brand_id, profile_id)。"""
    pid = PID_PREFIX + uuid.uuid4().hex[:10]
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM client_profiles WHERE brand_id = %s", (BRAND_ID,))
        cur.execute("DELETE FROM brands WHERE id = %s", (BRAND_ID,))
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id) VALUES (%s, %s, %s)",
            (BRAND_ID, "c2'' 双端点 %s" % pid[-6:], 1))
        cur.execute(
            "INSERT INTO client_profiles (id, name, brand_id) VALUES (%s, %s, %s)",
            (pid, "c2'' 档案", BRAND_ID))
    finally:
        c.close()
    if with_values:
        assert update_profile(pid, **to_update_kwargs(SAMPLES, get_profile(pid)))
    return BRAND_ID, pid


def _my_clients_detail(brand_id: int, monkeypatch):
    """走**真的** `get_client_detail`(前端实际调的那个),只放行 RBAC。"""
    import api.brand_api as bapi
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)
    monkeypatch.setattr(bapi, "_get_user",
                        lambda *a, **k: {"user_id": 1, "is_admin": True})
    req = types.SimpleNamespace(state=types.SimpleNamespace(organization_identity=None))
    return asyncio.run(bapi.get_client_detail(brand_id, req))


def test_my_clients_detail_carries_writing_basics(monkeypatch):
    """🔴 前端真正调的那个端点必须回 writing_basics —— 这条是本次缺口的正面。"""
    brand_id, _pid = _seed(with_values=True)
    resp = _my_clients_detail(brand_id, monkeypatch)
    profile = resp.get("profile")
    assert profile is not None, "my-clients 详情没回 profile —— 先核对象"
    assert "writing_basics" in profile, (
        "my-clients 详情的 profile 里没有 writing_basics —— "
        "写作大厅读的就是这个面,它空着等于本单白做")
    for field in BASIC_FIELDS:
        assert profile["writing_basics"][field] == SAMPLES[field], (
            "%s 在 my-clients 这一面读回 %r" % (field, profile["writing_basics"][field]))


def test_my_clients_detail_on_an_old_profile_reads_six_empty_strings(monkeypatch):
    """老档案(建于迁移之前,六个位置都空)在**这一面**也要是六个空串,不许抛。"""
    brand_id, _pid = _seed(with_values=False)
    wb = _my_clients_detail(brand_id, monkeypatch)["profile"]["writing_basics"]
    assert set(wb) == set(BASIC_FIELDS)
    assert all(v == "" for v in wb.values()), wb


def test_my_clients_reuses_the_shared_mapping_instead_of_rolling_its_own(monkeypatch):
    """🔴 结构锁:`brand_api` 必须**调用共用映射**,不许自己拼一份。

    自己拼的话,今天的行为锁照样绿(值碰巧一样),
    而加字段那天两边会分叉 —— 行为锁那时才红,已经晚了。
    """
    from tests._shared.source_slice import code_only, function_body

    body = code_only(function_body("api/brand_api.py", "get_client_detail"))
    assert "writing_basics_contract" in body, (
        "brand_api 没 import 共用映射 —— 多半是自己拼了一份")
    assert 'profile_out["writing_basics"]' in body, "brand_api 没挂 writing_basics"
    # 自己拼的形状:直接点名某个真列 / 字面量键名堆在一起
    for hand_rolled in ('"target_users"', '"selling_points"', '"brand_constraints"'):
        assert hand_rolled not in body, (
            "get_client_detail 里出现了手写的列名 %s —— 像是在自己拼那六个字段"
            % hand_rolled)


def test_cleanup_marker_rows_are_gone():
    """本包自己造的 brand 行跑完要清掉(别人的行一律不碰)。"""
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM client_profiles WHERE brand_id = %s", (BRAND_ID,))
        cur.execute("DELETE FROM brands WHERE id = %s", (BRAND_ID,))
        cur.execute("SELECT count(*) AS n FROM brands WHERE id = %s", (BRAND_ID,))
        assert dict(cur.fetchone())["n"] == 0
    finally:
        c.close()
