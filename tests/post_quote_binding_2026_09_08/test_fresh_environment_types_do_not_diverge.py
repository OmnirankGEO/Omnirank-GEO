"""#150 §3.2 返修 · 055 不许把 `quote_id` 抢过来。

## 为什么需要这一条

我第一版的 055 把 `quote_id INTEGER` 也加了。在**生产 schema** 上那是空操作
(列已存在于 034 建的 bigint),所以打生产 dump 的那一组**看不见**它 ——
把它加回去,那一组照样全绿。毒下成了、却没有可观测后果。

危害在**别的环境**:任何还没有 034 那一列的库上,`ADD COLUMN quote_id INTEGER`
会真的建出 integer 列,与生产的 bigint 分叉,而分叉那天没有任何东西会说话。

## ⚠️ 我原本想写的行为臂写不成,理由记在这里

原计划是「空库跑 017→034→055,断言 quote_id 是 bigint」。实测**跑不成**:

    干净库跑 manifest ⇒ `db/migration_034_geo_image_note_contract_2026_08_17.sql`
    **失败**:`relation "publish_idempotency_keys" does not exist`

也就是说**全新环境里根本没有 `quote_id`**(034 半途而废),
生产上有它是因为生产是逐步演进过来的。

🔴 我**不**把这个状态写成断言 —— 那会变成「锁住一个坏状态」,
   等 034 被修好的那天它反而变红,拦住正确的修复。
   这是一处**既有环境缺陷**,已单独报给 Review,不在本单范围。

⇒ 所以这里只留**结构臂**:055 的 DDL 里根本不该出现 `quote_id`。
   它是 P-B 那发毒唯一能被观测到的地方。
"""

from __future__ import annotations

import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_055_geo_douyin_post_quote_binding_2026_09_08.sql"


def test_055_does_not_declare_quote_id():
    """🔴 055 只管 `confirmed_keyword_id`;`quote_id` 归 034。

    只剥注释后扫 **DDL 语句** —— 迁移头**解释**了为什么不加它,
    那是注释不是 DDL(连注释一起扫会把解释算成实现,#139 栽过同一个坑)。
    """
    raw = MIGRATION.read_text(encoding="utf-8")
    ddl = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
    assert "quote_id" not in ddl.lower(), (
        "055 的 DDL 里出现了 quote_id —— 它归 034,本迁移不该碰:\n%s" % ddl)


def test_055_does_not_recreate_the_existing_quote_index():
    """🔴 也不许重建同名的报价索引。

    生产上 `idx_geo_douyin_posts_quote` 已存在且定义**不同**
    (`(quote_id, contract_revision_id, batch_item_ordinal)
      WHERE quote_id IS NOT NULL AND deleted_at IS NULL`)。
    `CREATE INDEX IF NOT EXISTS` 只按**名字**判存,同名不同定义**静默跳过** ——
    写了也建不出来,只会让下一个读的人以为建了。
    """
    raw = MIGRATION.read_text(encoding="utf-8")
    ddl = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
    assert "idx_geo_douyin_posts_quote" not in ddl, (
        "055 又去建同名报价索引了 —— 按名判存会静默跳过,建不出来还让人以为建了")


def test_prestart_bootstraps_runtime_tables_before_running_migrations():
    """🔴 [#156] prestart 必须在**跑 manifest 之前**引导运行时建的基础表。

    实测(2026-09-08):`migration_034` 直接 `ALTER` 一批由**运行时代码**
    (`db/meijiehezi_db.init_mhz_tables`)建的表。干净库上 manifest 先跑 ⇒ 整支失败。
    prestart 里本来就有同一种做法(`import db.diagnosis_db` 那行,注释写明
    是给灾备/新库用的),本条把 mhz 那份钉在同一位置。

    ⚠️ 这条锁的是**顺序**,不是「干净库能建起来」——后者今天做不到
       (prestart 在更早的 `users` 就 fail-fast 中止),那是 #156 台账里更大的一项,
       不在这条判据的射程内。把做不到的事写成断言,等于锁住一个坏状态。
    """
    src = io.open(REPO / "scripts" / "prestart.py", encoding="utf-8").read()
    boot = src.find("init_mhz_tables()")
    loop = src.find("for rel in MIGRATIONS")
    assert boot != -1, "prestart 没有引导 mhz 基础 schema —— 034 在干净库上必失败"
    assert loop != -1, "找不到 manifest 循环 —— 分母塌了,不是通过"
    assert boot < loop, (
        "引导排在 manifest 循环**之后** ⇒ 等于没引导(%d vs %d)" % (boot, loop))


def test_055_still_creates_the_keyword_index():
    """正样本臂:词维度索引**要**建 —— 证上面两条不是把 DDL 全删了才绿的。"""
    raw = MIGRATION.read_text(encoding="utf-8")
    ddl = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--"))
    assert "idx_geo_douyin_posts_confirmed_keyword" in ddl
    assert "confirmed_keyword_id INTEGER" in ddl
