"""改名冲突检查按 (name, owner_user_id) · 判据锁(WO 快修 2026-08-08)

缺陷:`api/brand_api.py` 的 onboarding 预查写的是
`SELECT id FROM brands WHERE name = %s AND id <> %s` —— **全局、且不看 is_deleted**,
而库约束是 `brands_name_owner_key UNIQUE (name, owner_user_id) WHERE is_deleted = false`。
应用层比库严格 → **新用户填一个别人家用过的品牌名就被静默挡掉**:
名字更新被跳过、接口仍返回 success,用户看到的品牌名不是自己填的那个。

🔴 判据打在**真库真约束**上:用真 PG 建一张与生产**逐字相同**的 `brands`
(含那个 partial unique index),再跑真实的冲突判据 SQL ——
只测"函数返回了什么"证明不了应用层与库对齐。
"""
from __future__ import annotations

import re

import pytest
from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块

# 与生产 `\d brands` 实查一致(2026-08-08):唯一索引是 partial 的,只管活行。
# 🔴 测试库里 `brands` 可能已被别的包建过(实测缺 `brand_type`),
#    所以不能只写 CREATE TABLE IF NOT EXISTS —— 那是空操作,列还是缺的。
#    改成"补齐到生产形态"(additive,同仓里 `_safe_add_column` 的惯例),
#    并且**先删掉全局唯一那一条**再建 partial 那一条 ——
#    否则这套用例测的就不是生产那个约束了(判据打偏的经典形状)。
#
# 🔴🔴 顺带被这个夹具抓出来一件事(已写进交付说明):**新建的库会长出
#    `brands_name_key UNIQUE (name)` 这个全局约束** —— 生产上它已经被迁移删掉了
#    (2026-08-08 只读实查:brands 只有 brands_pkey 与 brands_name_owner_key),
#    但应用的建表兜底还在造它。所以这里用 DROP CONSTRAINT(不是 DROP INDEX ——
#    约束背后的索引删不掉,会报 DependentObjectsStillExist,我第一版就是这么挂的)。
BRANDS_DDL = """
-- [R5 ⑤ 批2] brands 建表/补列由 ensure_brands_schema()（生产 SSOT 出口）在本段之前完成。
--   下面「先造再删 brands_name_key」那几句是本文件的**被测对象**，原样保留。
-- 🔴 先造再删:throwaway PG 是**持久**的,跑过一次之后这个约束就已经没了,
--    只写 DROP 的话"删这一步"在第二次跑就是空操作(变异 M07 第一版因此存活)。
--    造一次再删,这一步才始终有意义。
ALTER TABLE brands DROP CONSTRAINT IF EXISTS brands_name_key;
DELETE FROM brands;
ALTER TABLE brands ADD CONSTRAINT brands_name_key UNIQUE (name);
ALTER TABLE brands DROP CONSTRAINT IF EXISTS brands_name_key;
DROP INDEX IF EXISTS brands_name_owner_key;
CREATE UNIQUE INDEX brands_name_owner_key
    ON brands (name, owner_user_id) WHERE (is_deleted = false);
"""

def _app_conflict_sql() -> str:
    """把 `api/brand_api.py` 里**真正在用**的那条预查抽出来跑。

    🔴 第一版我在测试里抄了一份同样的 SQL 当常量 —— 那样改坏源码测试照样绿
    (变异 M05「去掉 id <> 自己」当场存活)。判据必须绑在**真出口**上。
    """
    with open("api/brand_api.py", encoding="utf-8") as handle:
        raw = handle.read()
    m = re.search(
        # 末行带逗号(`"… LIMIT 1",`),所以逗号要写成可选 —— 少这一个 `,?`
        # 整条抽不出来,而 assert 会当场炸(刻意不静默回落到抄的一份)。
        r'cur\.execute\(\s*\n((?:\s*"[^"]*",?\s*\n)+)\s*\(name, \w+, brand_id\),',
        raw,
    )
    assert m, "抽不出应用里的预查语句 —— 判据锚点失效,先修判据"
    return "".join(re.findall(r'"([^"]*)"', m.group(1)))


#: 应用真出口那条。模块加载时抽一次,抽不到直接炸(不许静默回落到抄的一份)。
FIXED_CONFLICT_SQL = _app_conflict_sql()
#: 修复**前**那条(全局 + 不看 is_deleted)。留着做反向对照:
#: 同一组数据上它必须给出**不同**答案,否则这套用例没有判别力。
LEGACY_CONFLICT_SQL = "SELECT id FROM brands WHERE name = %s AND id <> %s LIMIT 1"


@pytest.fixture
def brands_db():
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        # [R5 ⑤ 批2 2026-08-21] brands 走生产 SSOT 出口（手搓版比生产窄）。
        #   只换 brands 一张表的 DDL 来源，本文件其余业务表一概不动 —— 批 1 实测证伪过
        #   「夹具改跑整个 init_db」：把 150 张表拖进只要十来张表的夹具，
        #   174 passed/0 failed 变 138 passed/34 failed、194s 变 589s。
        #   手搓版 6 列，生产 32 列。
        #   🔴 必须排在 BRANDS_DDL 之前：那段要 DROP/ADD brands_name_key，表得先在。
        ensure_brands_schema(cur)
        cur.execute(BRANDS_DDL)
        cur.execute("DELETE FROM brands")
    yield
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM brands")


def _seed(rows):
    from db.connection import get_db

    ids = []
    with get_db() as conn:
        cur = conn.cursor()
        for name, owner, deleted in rows:
            cur.execute(
                "INSERT INTO brands (name, owner_user_id, brand_type, is_deleted) "
                "VALUES (%s, %s, 'self', %s) RETURNING id",
                (name, owner, deleted),
            )
            ids.append(int(cur.fetchone()["id"]))
    return ids


def _conflict(sql, params):
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return None if row is None else int(row["id"])


# ══════════════════════════════════════════════════════════════════════════
# §1 判据本体 —— 与库约束逐字对齐
# ══════════════════════════════════════════════════════════════════════════

def test_same_name_different_owner_is_not_a_conflict(brands_db):
    """【必须命中】跨 owner 同名是合法的 —— 不许再挡新用户。

    生产实证:「全域上榜(深圳)科技有限公司」7 行 / 7 个 owner,
    QZQZ 三行 / 三个 owner。不同代理服务同一家企业本来就会同名。
    """
    other_id, mine_id = _seed([("QZQZ 美学定制", 15, False), ("我的旧名", 24, False)])
    assert _conflict(FIXED_CONFLICT_SQL, ("QZQZ 美学定制", 24, mine_id)) is None

    # 反向对照:旧判据在**同一组数据**上会挡下来 —— 证明这条用例真的打到了改动点
    assert _conflict(LEGACY_CONFLICT_SQL, ("QZQZ 美学定制", mine_id)) == other_id


def test_same_name_same_owner_is_still_a_conflict(brands_db):
    """【必须不命中】同一个 owner 下同名**仍然**要挡 —— 库约束会真的报错。

    没有这条,"把预查删掉"这种实现会把上面那条拿满分,而那会让 UPDATE 撞
    unique index 抛 500(注释里写明不能用 SAVEPOINT 兜)。
    """
    other_id, mine_id = _seed([("同一个人的旧品牌", 24, False), ("我的旧名", 24, False)])
    assert _conflict(FIXED_CONFLICT_SQL, ("同一个人的旧品牌", 24, mine_id)) == other_id


def test_soft_deleted_row_is_not_a_conflict(brands_db):
    """【必须命中】软删的行不算冲突 —— partial index 本来就不管它。

    旧判据不看 `is_deleted`,所以一个用户把品牌删了再用回同一个名字会被自己挡住。
    """
    _dead, mine_id = _seed([("删掉的名字", 24, True), ("我的旧名", 24, False)])
    assert _conflict(FIXED_CONFLICT_SQL, ("删掉的名字", 24, mine_id)) is None
    # 反向对照:旧判据会把软删行也算成冲突
    assert _conflict(LEGACY_CONFLICT_SQL, ("删掉的名字", mine_id)) == _dead


def test_self_row_is_never_its_own_conflict(brands_db):
    """【必须命中】改成自己现在的名字不算冲突(`id <> %s` 那一半没被我改坏)。"""
    (mine_id,) = _seed([("我的名字", 24, False)])
    assert _conflict(FIXED_CONFLICT_SQL, ("我的名字", 24, mine_id)) is None


def test_the_db_constraint_really_is_per_owner_and_live_only(brands_db):
    """【必须命中 · 元判据】库约束就是 `(name, owner_user_id) WHERE is_deleted=false`。

    应用层判据是照着库约束抄的 —— 所以要有一条锁盯住"库约束没变"。
    库哪天改成全局唯一,这条转红,提醒应用层跟着改回去。
    """
    import psycopg2

    from db.connection import get_db

    _seed([("撞名测试", 24, False)])
    # 同 owner 同名 → 必须被库拒
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO brands (name, owner_user_id, is_deleted) VALUES (%s, %s, false)",
                ("撞名测试", 24),
            )
    # 换 owner → 库必须放行(否则"跨 owner 合法"这个前提就不成立)
    assert _seed([("撞名测试", 15, False)])
    # 软删行同名 → 库也放行
    assert _seed([("撞名测试", 24, True)])


# ══════════════════════════════════════════════════════════════════════════
# §2 接线 —— 源码里那条 SQL 必须真的换了
# ══════════════════════════════════════════════════════════════════════════

def _src(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    return "\n".join(l for l in raw.split("\n") if not l.strip().startswith("#"))


def test_brand_api_conflict_query_is_scoped(brands_db):
    """【必须命中 · 打在接线上】`api/brand_api.py` 的预查带上了 owner 与 is_deleted。"""
    code = _src("api/brand_api.py")
    m = re.search(
        r'"SELECT id FROM brands "\s*\n\s*"WHERE name = %s([^"]*)"', code
    )
    assert m, "找不到预查语句 —— 判据锚点失效,先修判据"
    where = m.group(1)
    assert "owner_user_id = %s" in where, "必须按 owner 收窄"
    assert "is_deleted = false" in where, "必须只看活行(与 partial index 逐字一致)"


def test_brand_api_no_longer_claims_a_global_unique_constraint(brands_db):
    """【必须不命中】那句错注释("name 有全局唯一约束")不许留着。

    它是这个 bug 的成因 —— 下一个人照着它写还会再错一次。
    """
    with open("api/brand_api.py", encoding="utf-8") as handle:
        raw = handle.read()
    # 🔴 判据只看**断言句**本身,不看本包自己解释它为什么错的那段话
    #    (第一版我在代码注释里逐字复述了原句 → 这条锁恒假,当场被自己抓到)。
    assert "name 有全局唯一约束，" not in raw and "name 有全局唯一约束,先预查" not in raw


def test_owner_is_passed_not_hardcoded(brands_db):
    """【必须命中】owner 参数用的是当前登录用户 `user_id`,不是写死的数字。"""
    code = _src("api/brand_api.py")
    m = re.search(r'\(name, (\w+), brand_id\),', code)
    assert m and m.group(1) == "user_id", f"owner 参数应为 user_id,实际 {m and m.group(1)}"


def test_conflict_result_really_comes_from_the_query(brands_db):
    """【必须命中 · 接线】`name_conflict` 必须来自 `cur.fetchone()`,
    且改名 UPDATE 必须在 `if not name_conflict:` 之下。

    把预查结果写死成 None 等于把冲突保护整个删掉 —— 同 owner 重名时
    UPDATE 会撞 unique index 抛 500(源码注释写明这里不能用 SAVEPOINT 兜)。
    库层判据抓不到这一条(它测的是 SQL 本身),所以这里用接线判据补。
    """
    code = _src("api/brand_api.py")
    m = re.search(
        r'name_conflict = (.+)\n\s*if not name_conflict:\n\s*cur\.execute\(\n'
        r'\s*"UPDATE brands SET name = %s',
        code,
    )
    assert m, "预查 → 判空 → UPDATE 这条顺序被改了"
    assert m.group(1).strip() == "cur.fetchone()", (
        f"name_conflict 必须来自 fetchone(),实际 {m.group(1).strip()}"
    )
