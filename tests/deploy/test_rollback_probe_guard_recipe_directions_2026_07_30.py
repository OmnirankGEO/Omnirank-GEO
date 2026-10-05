"""锁:回滚能力探针**锁场景构造配方**的判别力 —— 两个方向都要造(工单 §4.2.5)。

`test_rollback_capability_probe_2026_07_30.py` 里的 stub 用的是罐头标记,
验的是**探针自己**的五态/落痕/位置。它验不到「真守卫遇到真不符会不会红」——
那正是锁 1 / 锁 3 场景的地基。地基塌了,那两条锁就变成"造了个假红看它报警"。

本文件用**真 SQL + 真 `schema_status()`** 钉住地基,三个场景:

| 场景 | 含义 | 期望 |
|---|---|---|
| `expected ⊋ actual` | 代码多要一个值 = **漏跑迁移**那一侧 | 白名单 blocker 出现 |
| `actual ⊋ expected` | 库多允许一个值 = **迁移超前**那一侧 | 白名单 blocker 出现 |
| 反向对照(不注入) | 两边一致 | 白名单 blocker **不出现** |

🔴 **为什么两个方向都要造**:今天的比对是
`dealer_inventory_resale.py:1803 actual_values != set(expected_values)` = **精确相等**,
两侧都硬拦。但这两侧在 SSOT §6.1 第 16 条里**危险度不对等** —— 将来若把守卫改成
单向包含(只拦"代码要的库里没有"),只造一个方向的话,锁会集体保持绿而没人发现覆盖面掉了。
造了两条,那次改动会让其中恰好一条转红 = 可见。

🔴 **判别力自证**:第三个场景是前两个的反向对照。没有它,这两条锁可能只是
"库里缺一堆表所以什么都红"的恒真判据 —— 事实上本夹具**故意只建两张表**,
`schema_status()` 会报一堆缺表 blocker,所以断言必须精确到
`invalid_legacy_check_values` 这个字段,不能拿 `ready` 当判据。

生产侧同一配方的只读实证(2026-07-30,`omnirank-release:3b7a7ecb` 一次性容器):
两个方向都 `exit 1` + `[DealerResale SchemaCheck fail-closed] 旧流水 CHECK 白名单不完整或被篡改
agent_inventory_transactions_type_check`,不注入 → `exit 0` + 六道 key 全返回。
"""
import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

from services.dealer_inventory_resale import (
    LEGACY_CUSTOMER_CREDIT_SOURCES,
    LEGACY_INVENTORY_TRANSACTION_TYPES,
    schema_status,
)

WHITELIST_FIELD = "invalid_legacy_check_values"
INVENTORY_CHECK = "agent_inventory_transactions_type_check"


def _quoted(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


@pytest.fixture()
def isolated_cursor():
    """独立 schema + `search_path` —— `schema_status()` 的每条查询都以 `current_schema()`
    为基准,所以换 schema 就等于换了一个干净的库,不会碰到共享测试库里别人的表形状
    (`CREATE TABLE IF NOT EXISTS` 在非空库里会静默拿到别人的列,这里刻意规避)。"""
    url = os.environ.get("RBPROBE_PG_URL") or os.environ["TEST_DATABASE_URL"]
    schema = "rbprobe_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}"')
    try:
        yield cur, schema
    finally:
        try:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            conn.close()


def _build_legacy_checks(cur, *, inventory_values, credit_values):
    cur.execute(
        "CREATE TABLE agent_inventory_transactions ("
        " id BIGSERIAL PRIMARY KEY, type TEXT NOT NULL,"
        f" CONSTRAINT {INVENTORY_CHECK} CHECK (type IN ({_quoted(inventory_values)})))"
    )
    cur.execute(
        "CREATE TABLE customer_credit_transactions ("
        " id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL,"
        " CONSTRAINT customer_credit_transactions_source_check"
        f" CHECK (source IN ({_quoted(credit_values)})))"
    )


def _whitelist_blockers(cur):
    status = schema_status(cur)
    # 🔴 精确到这个字段:本夹具只建两张表,`ready` 恒为 False(一堆缺表 blocker),
    #    拿 `ready` 当判据就成了恒真判据。
    assert status["blockers"], "夹具没报任何 blocker → 说明 schema_status 根本没跑到,断言会变空"
    return status[WHITELIST_FIELD]


def test_reverse_control_matching_whitelists_produce_no_blocker(isolated_cursor):
    """反向对照:两边逐值一致 → 白名单 blocker 必须缺席。

    没有这一条,下面两条方向锁可能只是"库里什么都没有所以什么都红"。
    """
    cur, _schema = isolated_cursor
    _build_legacy_checks(
        cur,
        inventory_values=LEGACY_INVENTORY_TRANSACTION_TYPES,
        credit_values=LEGACY_CUSTOMER_CREDIT_SOURCES,
    )

    assert _whitelist_blockers(cur) == [], "两边一致却报白名单不符 → 配方没有判别力"


def test_direction_expected_superset_of_actual_is_caught(isolated_cursor):
    """方向 1 `expected ⊋ actual`:代码多要一个值(= 该跑的迁移没跑)→ 必须红。"""
    cur, _schema = isolated_cursor
    _build_legacy_checks(
        cur,
        inventory_values=set(LEGACY_INVENTORY_TRANSACTION_TYPES) - {"manufacturer_origin_in"},
        credit_values=LEGACY_CUSTOMER_CREDIT_SOURCES,
    )

    assert _whitelist_blockers(cur) == [INVENTORY_CHECK]


def test_direction_actual_superset_of_expected_is_caught(isolated_cursor):
    """方向 2 `actual ⊋ expected`:库多允许一个值(= 迁移超前于代码)→ 必须红。

    危险度与方向 1 不对等;将来若守卫改成单向包含,只会是这一条转红。
    """
    cur, _schema = isolated_cursor
    _build_legacy_checks(
        cur,
        inventory_values=set(LEGACY_INVENTORY_TRANSACTION_TYPES) | {"__probe_bogus_type__"},
        credit_values=LEGACY_CUSTOMER_CREDIT_SOURCES,
    )

    assert _whitelist_blockers(cur) == [INVENTORY_CHECK]


def test_comparison_is_exact_equality_today_not_one_way_containment():
    """把"今天是精确相等"这件事钉成断言,而不是留在注释里。

    上面两条方向锁**同时**为红,只在精确相等语义下成立。哪天它变成单向包含,
    这条断言会连同那一条方向锁一起转红,而不是静悄悄放宽。
    """
    import inspect

    source = inspect.getsource(schema_status)
    assert "actual_values != set(expected_values)" in source, (
        "白名单比对已不是精确相等 → 两条方向锁里有一条会失去判别力,先重估覆盖面"
    )
