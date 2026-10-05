"""#159 · 干净库能不能从「引导 + manifest」建起来。

## 这个包今天**做不到全绿**,而那是刻意的

实测(#156 census,`WO156_CLEAN_DB_MIGRATION_CENSUS_C_2026-09-08.txt`,
sha256 `35bc4df9…`):全新空库跑 manifest **首跑 63 支失败 / 二跑 28 支**。
差额 35 支是首跑期间**运行时代码**把表建了出来 ——
所以「跑两遍就行」不是修复,是掩盖:它把建库变成一个**依赖执行次数**的过程,
而次数不是契约。

## 🔴 为什么用 `xfail(strict=True)` 而不是「先不进分母」

我原方案写的是「本包先不进 gate9 分母,项目做完再登记」。
Review 订正(2026-09-08),采纳:

> **一个不存在的判据,和一个恒红的判据一样没人看。**

`xfail(strict=True)` 三个性质刚好齐:

  · **今天**:xfailed —— 不红,不训练人忽略;
  · **修好那天**:XPASS 在 strict 下**判红**,逼人回来摘标记 ——
    标记自己会退休,不需要谁记得;
  · **gate9 照常登记**:包在盘上就必须登记,否则 `DISK_PACKAGES_COUNT` 撞。

⚠️ 「修好那天变红」在这里是**设计目的**,不是缺陷。
   与「锁住坏状态」的区别在于:前者红在**该退休的标记**上,
   后者红在**正确的修复**上。

## 引导从哪来

优先读 `db.bootstrap_manifest.BASE_SCHEMA_BOOTSTRAP`(#159 要建的那份有序注册表);
它还不存在时,回落到 prestart 今天**硬编码**的那两个 ——
这样注册表落地时本文件**不用改**,只会从 xfail 变 XPASS。
"""

from __future__ import annotations

import importlib
import io
import os
import pathlib
import re
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
DSN = os.getenv("TEST_DATABASE_URL")

#: 注册表还没建时的回落 —— 与 `scripts/prestart.py` 今天硬编码的那两个一致。
_FALLBACK_BOOTSTRAP = ("db.diagnosis_db", "db.meijiehezi_db")


def _bootstrap_modules() -> tuple:
    try:
        mod = importlib.import_module("db.bootstrap_manifest")
        return tuple(getattr(mod, "BASE_SCHEMA_BOOTSTRAP", ()) or ())
    except Exception:            # noqa: BLE001 注册表还没建
        return _FALLBACK_BOOTSTRAP


def _migrations() -> list:
    from db.migration_manifest import MIGRATIONS
    return list(MIGRATIONS)


def _fresh_db():
    name = "cleanboot_%s" % uuid.uuid4().hex[:8]
    root = DSN.rsplit("/", 1)[0]
    admin = psycopg2.connect(root + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    return name, root + "/" + name


def _drop(name):
    root = DSN.rsplit("/", 1)[0]
    admin = psycopg2.connect(root + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
    admin.close()


def _apply_all(dsn) -> list:
    """在**子进程**里跑引导 + 全量 manifest,返回失败清单。

    🔴 必须子进程:引导就是 `import db.xxx`,在本进程里做会污染后续用例
       (而且这些模块在 import 期就连库 —— 换了 DSN 也拉不回来)。
    """
    import json
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent('''
        import importlib, json, os, sys, pathlib
        sys.path.insert(0, %r)
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"] = %r
        failures = []
        for mod in %r:
            try:
                importlib.import_module(mod)
            except Exception as e:
                failures.append("bootstrap:" + mod + " - " + str(e)[:160])
        from db.connection import get_connection
        from db.migration_manifest import MIGRATIONS
        root = pathlib.Path(%r)
        conn = get_connection(); conn.autocommit = True
        cur = conn.cursor()
        for rel in MIGRATIONS:
            p = root / rel
            if not p.is_file():
                failures.append(rel + " - 文件缺失"); continue
            try:
                cur.execute(p.read_text(encoding="utf-8"))
            except Exception as e:
                failures.append(rel + " - " + str(e).splitlines()[0][:160])
        conn.close()
        print("@@JSON@@" + json.dumps(failures, ensure_ascii=False))
    ''') % (str(REPO), dsn, list(_bootstrap_modules()), str(REPO))

    env = dict(os.environ, TEST_DATABASE_URL=dsn, DATABASE_URL=dsn,
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    m = re.search(r"@@JSON@@(.*)", r.stdout or "")
    if not m:
        # 🔴 拿不到读数 ≠ 没问题:让它响亮地失败,而不是当成 0 个失败。
        raise AssertionError("子进程没有吐出读数(stderr 尾部):\n%s"
                             % (r.stderr or "")[-800:])
    return json.loads(m.group(1))


@pytest.fixture
def fresh():
    if not DSN:
        pytest.skip("需要 TEST_DATABASE_URL(缺则本组未验证)")
    name, dsn = _fresh_db()
    try:
        yield dsn
    finally:
        _drop(name)


# ══════════════════════════════════ 主判据(今天 xfail)

@pytest.mark.xfail(strict=True,
                   reason="#159 未完成:干净库还建不起来(census 首跑 63 / 二跑 28)")
def test_a_clean_database_builds_with_zero_failures(fresh):
    """🔴 空库 → 引导 → 全量 manifest ⇒ **零失败**。

    这是 #159 的验收标准本身。今天必然 xfail;
    项目做完那天它会 XPASS,而 `strict=True` 会把 XPASS 判红 ——
    **逼人回来摘掉这个标记**,标记自己退休。
    """
    failures = _apply_all(fresh)
    assert not failures, "干净库上有 %d 支迁移失败:\n%s" % (
        len(failures), "\n".join(failures[:20]))


@pytest.mark.xfail(strict=True, reason="#159 未完成:首跑与二跑读数不同")
def test_the_first_run_equals_the_second_run(fresh):
    """🔴 同一空库连跑两次,失败集**逐项相同**(且为空)。

    这条专门钉死「跑两遍才行」那条捷径:
    首跑 63 / 二跑 28 的差额是**运行时代码在首跑期间把表建了出来**,
    不是迁移变好了。把建库变成依赖执行次数的过程,次数不是契约。
    """
    first = _apply_all(fresh)
    second = _apply_all(fresh)
    assert first == second == [], (
        "首跑 %d 支 / 二跑 %d 支 —— 差额是运行时代码补上的,不是迁移自愈"
        % (len(first), len(second)))


# ══════════════════════════════════ 今天就该绿的

def test_the_bootstrap_list_is_ordered_and_explicit():
    """引导清单是**有序显式**的列表(注册表落地前回落到硬编码那两个)。

    今天回落值就是 prestart 里那两行;注册表建好后本条自动改读它,
    **本文件不用改**。
    """
    mods = _bootstrap_modules()
    assert mods, "引导清单为空 —— 干净库上什么都建不起来"
    assert list(mods) == list(dict.fromkeys(mods)), "引导清单有重复项:%r" % (mods,)
    for m in mods:
        assert m.startswith("db."), "引导项应是 db 下的模块名:%r" % m


@pytest.mark.xfail(strict=True,
                   reason="#159 未完成:引导与迁移已在大量重复建表(实测 18+ 张)")
def test_bootstrap_modules_and_migrations_do_not_both_create_the_same_table():
    """🔴 引导与迁移**不许对同一张表各建一次**。

    同一张表两份定义,是比顺序更糟的病:它们会**静默漂开**
    (#156 我第一版就往迁移里复制了建表语句,正是这个错)。

    ⚠️ 只查**迁移里显式 CREATE TABLE 的表名**与**引导模块里 CREATE TABLE 的表名**
       的交集;查得到交集就说明有人抄了一份。
    """
    def _created(text):
        # 🔴 `IF NOT EXISTS` 那段用**非捕获且必须后接表名**的写法,
        #    否则遇到 `CREATE TABLE IF NOT EXISTS %s`(格式化占位符)
        #    正则会回溯成「表名 = IF」——我第一版就捕到了一个假表名 'IF'。
        names = set(re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?"
            r"([a-z_][a-z0-9_]*)",
            text, flags=re.I))
        return {n for n in names if n.lower() not in {"if", "not", "exists"}}

    boot_tables = set()
    for mod in _bootstrap_modules():
        path = REPO / (mod.replace(".", "/") + ".py")
        if path.is_file():
            boot_tables |= _created(io.open(path, encoding="utf-8").read())

    mig_tables = set()
    for rel in _migrations():
        p = REPO / rel
        if p.is_file():
            mig_tables |= _created(io.open(p, encoding="utf-8", errors="ignore").read())

    overlap = boot_tables & mig_tables
    assert not overlap, (
        "这些表**引导和迁移各建了一次**,定义会静默漂开:%s"
        % sorted(overlap)[:20])


def test_the_census_evidence_is_on_record():
    """债务台账在,且记的是实测数字。

    ⚠️ 它是外部证据文件(不在仓内),缺了就 skip ——
       **skip 不是通过**,只是说明这台机器上没有那份证据。
    """
    census = pathlib.Path(
        r"C:/AI-Test/WO156_CLEAN_DB_MIGRATION_CENSUS_C_2026-09-08.txt")
    if not census.is_file():
        pytest.skip("census 不在本机(它是交付物,不在仓内)")
    text = census.read_text(encoding="utf-8", errors="ignore")
    assert "首跑失败 63 支" in text and "二跑仍失败 28 支" in text
