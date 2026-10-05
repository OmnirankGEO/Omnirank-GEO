"""[WO-KYB-ROUTING D0-a] 目录接口字段白名单 —— 行为锁。

工单 §2.3 的要求逐条落成断言,每条"必须命中"配一条"必须不命中"。
核心一条:**断言的是键集合全等,不是"provider 不在里面"这种单点**
—— 单点断言下次 ADD COLUMN 照样漏,那正是本 bug 的成因型。
"""
import os

import pytest

from ddl_columns import declared_columns

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# 生产实测(工单 §2.1,QA 号 113 真实登录调接口):/media 47 键 · /wemedia 30 键。
# mhz_short_video 工单没测,36 是仓库 DDL 推导值。
_DECLARED_TOTALS = {"mhz_media": 47, "mhz_wemedia": 30, "mhz_short_video": 36}

# (表名, list 函数名, 白名单常量名, 该表应被挡掉的内部列)
_CASES = [
    (
        "mhz_media", "list_media", "MEDIA_PUBLIC_COLUMNS",
        {"provider", "provider_media_id", "source_domain", "hidden_by_dedupe",
         "platform_cost_cents", "wholesale_cents", "wholesale_points"},
    ),
    (
        "mhz_wemedia", "list_wemedia", "WEMEDIA_PUBLIC_COLUMNS",
        {"provider", "provider_media_id", "source_domain", "hidden_by_dedupe"},
    ),
    (
        "mhz_short_video", "list_short_video", "SHORT_VIDEO_PUBLIC_COLUMNS",
        {"provider", "provider_media_id", "source_domain"},
    ),
]

# 每张表上"用户本来就该看见"的字段 —— 用来证明我们没有把白名单删过头。
_MUST_KEEP = {
    "mhz_media": ("id", "media_name", "price", "area"),
    "mhz_wemedia": ("id", "toutiao_name", "price", "province"),
    "mhz_short_video": ("id", "media_name", "price", "platform"),
}


def _fake_row(columns):
    """给每个列一个可区分的值,方便断言拿到的是真投影而不是空壳。"""
    return {c: f"v::{c}" for c in columns}


def _run(mhz_db, wire, table, fn_name, schema_columns=None, rows=None):
    declared = sorted(declared_columns(_ROOT, table))
    schema = list(schema_columns) if schema_columns is not None else declared
    rows = rows if rows is not None else [_fake_row(schema)]
    cur = wire(schema, rows)
    result = getattr(mhz_db, fn_name)(page=1, limit=20)
    return cur, result


# ---------- 1. 键集合全等(不是子集) ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_response_keys_equal_whitelist(mhz_db, wire, table, fn_name, const_name, internal):
    whitelist = set(getattr(mhz_db, const_name))
    _cur, result = _run(mhz_db, wire, table, fn_name)
    assert result["media"], "替身没返回任何行,后面的键断言会恒真"
    got = set(result["media"][0].keys())
    assert got == whitelist, (
        f"{table} 返回键集合与白名单不等 · 多出={sorted(got - whitelist)} "
        f"少了={sorted(whitelist - got)}"
    )


# ---------- 2. 必须不命中:内部列一个都不许出现 ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_internal_columns_absent_from_response_and_sql(
    mhz_db, wire, table, fn_name, const_name, internal
):
    cur, result = _run(mhz_db, wire, table, fn_name)
    got = set(result["media"][0].keys())
    leaked = sorted(got & internal)
    assert not leaked, f"{table} 接口仍在返回内部字段:{leaked}"

    # SQL 文本里也不许出现(防"选出来再 pop 掉"那种黑名单写法)
    select_clause = cur.main_query.split(" FROM ")[0]
    for col in sorted(internal):
        assert f'"{col}"' not in select_clause, f"{table} 的 SELECT 列表里仍有 {col}"

    # 🔴 反向对照:上面那个"不在 SQL 里"的断言必须有判别力 ——
    # 换成一个公开列,它必须真的在 SQL 里。否则说明我在比一个恒不命中的东西。
    public_sample = getattr(mhz_db, const_name)[0]
    assert f'"{public_sample}"' in select_clause, (
        f"公开列 {public_sample} 都不在 SELECT 里 → 上面的否定断言没有判别力"
    )


# ---------- 3. 必须命中:正常字段不许被误删 ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_normal_fields_survive(mhz_db, wire, table, fn_name, const_name, internal):
    _cur, result = _run(mhz_db, wire, table, fn_name)
    row = result["media"][0]
    for col in _MUST_KEEP[table]:
        assert col in row, f"{table} 把用户要看的 {col} 也删了"
        assert row[col] == f"v::{col}", f"{table}.{col} 取到的不是真值"


# ---------- 4. 新加列不许自动泄漏(反黑名单 · 本 bug 的成因型) ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_future_column_does_not_leak(mhz_db, wire, table, fn_name, const_name, internal):
    """模拟下一次 `ALTER TABLE ADD COLUMN supplier_cost_2099`。

    白名单实现下它自动不出现;黑名单实现下它会直接漏出去。
    """
    schema = sorted(declared_columns(_ROOT, table)) + ["supplier_cost_2099"]
    _cur, result = _run(mhz_db, wire, table, fn_name, schema_columns=schema)
    assert "supplier_cost_2099" not in result["media"][0], (
        f"{table} 新加的列直接漏进了接口 —— 这是黑名单行为,不是白名单"
    )


# ---------- 5. 白名单里的列在库里不存在 → 不许炸 ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_missing_declared_column_is_tolerated(
    mhz_db, wire, table, fn_name, const_name, internal
):
    """本仓对 mhz_media 有两份互斥 DDL,全新库只会落其中一份。

    白名单跟库内真实列求交(to_regclass),就是为了这种库不炸(而不是 500)。
    """
    whitelist = list(getattr(mhz_db, const_name))
    dropped = whitelist[-1]
    schema = [c for c in sorted(declared_columns(_ROOT, table)) if c != dropped]
    _cur, result = _run(mhz_db, wire, table, fn_name, schema_columns=schema)
    got = set(result["media"][0].keys())
    assert got == set(whitelist) - {dropped}
    assert dropped not in got


# ---------- 6. 交集为空 → 宁可炸,也不许退回 SELECT * ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_empty_intersection_raises_instead_of_select_star(
    mhz_db, wire, table, fn_name, const_name, internal
):
    cur = wire(["totally_unrelated_col"], [{"totally_unrelated_col": 1}])
    with pytest.raises(RuntimeError, match="D0-a"):
        getattr(mhz_db, fn_name)(page=1, limit=20)
    # 而且不能是"先跑了 SELECT * 再抛" —— 主查询压根不该发出去
    mains = [
        sql for sql, _ in cur.executed
        if "pg_attribute" not in sql and not sql.strip().upper().startswith("SELECT COUNT(")
    ]
    assert not mains, f"交集为空时仍然发了主查询:{mains}"


# ---------- 7. 结构断言:白名单与内部列集不许有交集 ----------

def test_whitelists_disjoint_from_internal_set(mhz_db):
    internal = mhz_db._INTERNAL_ONLY_MEDIA_COLUMNS
    assert internal, "内部列集是空的 → 下面的断言恒真"
    for _table, _fn, const_name, _per_table in _CASES:
        overlap = set(getattr(mhz_db, const_name)) & internal
        assert not overlap, f"{const_name} 里混进了内部列:{sorted(overlap)}"


@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_per_table_internal_set_is_covered(mhz_db, table, fn_name, const_name, internal):
    """每张表该挡的列,必须都在全局内部列集里登记过。"""
    missing = internal - set(mhz_db._INTERNAL_ONLY_MEDIA_COLUMNS)
    assert not missing, f"{table} 要挡的 {sorted(missing)} 没登记进 _INTERNAL_ONLY_MEDIA_COLUMNS"


# ---------- 8. 白名单必须来自真实 DDL(抓 typo) ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_whitelist_is_subset_of_repo_ddl(mhz_db, table, fn_name, const_name, internal):
    declared = declared_columns(_ROOT, table)
    assert declared, f"{table} 的 DDL 一列都没解析出来 → 本断言恒真,解析器坏了"
    unknown = set(getattr(mhz_db, const_name)) - declared
    assert not unknown, (
        f"{const_name} 里有仓库 DDL 从未声明过的列(多半是 typo):{sorted(unknown)}"
    )


# ---------- 9. 列数守恒:白名单 + 内部列 = 全部声明列 ----------

@pytest.mark.parametrize("table,fn_name,const_name,internal", _CASES)
def test_column_accounting_is_complete(mhz_db, table, fn_name, const_name, internal):
    """新加一列时这条会红 —— 逼着加列的人明确回答"这列该不该对外"。

    这就是把「反黑名单」从一句要求变成一个会自己报错的东西。
    """
    declared = declared_columns(_ROOT, table)
    assert len(declared) == _DECLARED_TOTALS[table], (
        f"{table} 声明列数 {len(declared)} != 基线 {_DECLARED_TOTALS[table]}。"
        f"若是新加了列:请判断它该公开还是内部,更新 {const_name} 或 "
        f"_INTERNAL_ONLY_MEDIA_COLUMNS,再改这里的基线。"
    )
    accounted = set(getattr(mhz_db, const_name)) | internal
    assert accounted == declared, (
        f"{table} 有列既不在白名单也没登记为内部:{sorted(declared - accounted)}"
    )


# ---------- 10. 缓存不许串表 ----------

def test_column_cache_is_keyed_per_table(mhz_db, wire):
    media_declared = sorted(declared_columns(_ROOT, "mhz_media"))
    _cur, _r = _run(mhz_db, wire, "mhz_media", "list_media")
    assert set(mhz_db._PUBLIC_COLUMN_CACHE) == {"mhz_media"}

    _cur2, r2 = _run(mhz_db, wire, "mhz_wemedia", "list_wemedia")
    assert set(mhz_db._PUBLIC_COLUMN_CACHE) == {"mhz_media", "mhz_wemedia"}
    # 自媒体拿到的必须是自媒体的白名单,不是被软文的缓存污染
    assert set(r2["media"][0].keys()) == set(mhz_db.WEMEDIA_PUBLIC_COLUMNS)
    assert "toutiao_name" in r2["media"][0]
    assert "portal_media" not in r2["media"][0]
    assert "portal_media" in media_declared  # 反向对照:这列确实是软文独有的
