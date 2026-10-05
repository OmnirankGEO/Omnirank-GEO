"""自媒体推荐确定性锁(2026-07-29 Deploy-CTO)。

生产实测:`_match_wemedia` 同参数连跑 5 次得到 **5 个不同结果**。
根因不是"缺 ORDER BY"—— ORDER BY 在,但**不是全序**:

    ORDER BY (price / GREATEST(fans_num, 1)) ASC, fans_num DESC

生产 `mhz_wemedia` 里该排序键的并列规模(实测):

    (4.000000, 1) → 10,345 行 · (7.000000, 1) → 3,814 行 · (0.003000, 1000) → 2,780 行

上万行完全并列,`LIMIT` 从中任取,PostgreSQL 对并列行不保证顺序。
修法 = 三处 ORDER BY 末尾各加 `, id ASC`(主键,非空唯一)。

🔴 本锁是**行为级**的:不 grep 源码里有没有 "id ASC"(换个写法就绕过去了),
而是喂并列到底的假数据、真调 `_match_wemedia`、断言多次结果逐字节一致。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import placement_service as ps  # noqa: E402


# --- 全并列假数据:排序键两列完全相同,只有 id 不同 ------------------------
# 顺序**故意打乱**,模拟 PG 对并列行的任意返回顺序。
#: 列名照真实 SELECT 取:
#:   SELECT id, toutiao_name as media_name, platform, industry, fans_num, price,
#:          our_price_yuan, our_price_points FROM mhz_wemedia
#: 每行 platform 各不相同 —— 代码有"每平台最多 2 个"的配额,同平台会被挤掉,
#: 那样并列顺序就观察不到了。
_TIED_ROWS = [
    {"id": i, "media_name": f"媒体{i}", "platform": f"平台{i}", "industry": "家居",
     "fans_num": 1, "price": 4, "our_price_yuan": 4, "our_price_points": 520}
    for i in (907, 101, 555, 203, 809, 404, 706, 302, 608, 150)
]


class _FakeCursor:
    """按 SQL 里出现的 ORDER BY 决定返回顺序 —— 模拟真实 DB 的并列行为。

    * SQL 含 ``id ASC``      → 按 id 升序稳定返回(真实 DB 的确定性行为);
    * SQL 不含 ``id ASC``    → 每次调用轮转一格,模拟并列行的任意顺序。
    """

    _rotation = 0

    def __init__(self):
        self._rows: list = []

    def execute(self, sql, params=None):
        limit = 8
        if params:
            for p in params:
                if isinstance(p, int) and p > 0:
                    limit = p
        rows = list(_TIED_ROWS)
        if "id ASC" in sql:
            rows.sort(key=lambda r: r["id"])
        else:
            type(self)._rotation = (type(self)._rotation + 3) % len(rows)
            k = type(self)._rotation
            rows = rows[k:] + rows[:k]
        self._rows = rows[:limit]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        return None


class _FakeConn:
    def cursor(self, *a, **kw):
        return _FakeCursor()

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """🔴 `_match_wemedia` 是**函数内** `from db.connection import get_connection`,
    所以必须 patch `db.connection.get_connection` 本身 —— patch
    `placement_service.get_db` 不生效(模块里根本没这个名字,会真连库)。
    """
    import db.connection as dbconn

    _FakeCursor._rotation = 0
    monkeypatch.setattr(dbconn, "get_connection", lambda *a, **kw: _FakeConn())


def _ids(result) -> list[int]:
    """抽出返回记录的媒体 ID。

    🔴 返回项的键是 **`media_id`**(不是 `id`)—— 取错键会得到空列表,
    而空列表在"多次结果一致"这类断言下**恒真**,是典型假绿。
    """
    out: list[int] = []
    if isinstance(result, dict):
        for value in result.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("media_id") is not None:
                        out.append(int(item["media_id"]))
    return out


def _signature(result) -> str:
    return hashlib.md5("|".join(str(i) for i in _ids(result)).encode()).hexdigest()


def test_match_wemedia_is_deterministic_across_repeated_calls():
    """🔴 同参数连跑 5 次,返回的媒体 ID 序列必须完全一致。

    变异(去掉三处 ORDER BY 末尾的 `, id ASC`)→ 本锁转红。
    """
    signatures = {
        _signature(ps._match_wemedia([], [], "家居", 8, raw_industry="全屋定制",
                                     media_balance_enabled=False))
        for _ in range(5)
    }
    assert len(signatures) == 1, (
        f"同参数 5 次跑出 {len(signatures)} 种结果 —— 排序键存在并列且无 tiebreak;"
        "三处 ORDER BY 末尾需加 `, id ASC`"
    )


def test_match_wemedia_sql_carries_total_order_tiebreak():
    """并列到底的数据下,结果必须落在 id 升序的确定性序列上(不只是"稳定")。

    只断言"5 次一样"不够 —— 一个恒返空列表的实现也能满足。
    本锁额外要求结果非空且确实按 id 升序取前 N。
    """
    ids = _ids(ps._match_wemedia([], [], "家居", 8, raw_industry="全屋定制",
                                 media_balance_enabled=False))
    assert ids, "结果不应为空(空列表也能通过'多次一致',必须排除)"
    assert ids == sorted(ids), f"并列行未按 id 升序取,实际 = {ids}"
