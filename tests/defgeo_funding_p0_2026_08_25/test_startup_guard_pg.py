"""【A-1 · 迁移 050 漏跑必须响亮】web/cron 启动期 fail-closed 反查真的会拦。

为什么这条非有不可(它是撕锁 S08 存活逼出来的)
----------------------------------------------
迁移 050 的「漏跑后果**刻意做成响亮**」这句话,唯一的兑现处是
``services.startup_schema_guards.verify_diagnosis_schema_fail_closed`` 的关键列集合。
把 ``payer_user_id`` 从那个集合里拿掉,本包此前 **37 条判据一条都不红** ——
因为没有任何一条真的调过那个函数。

而漏跑的静默后果很贵:列不在 → ``run.get("payer_user_id")`` 取 None →
回落 owner → 平台承担腿又变回「拿租户 id 去结算平台的冻结」,
也就是本单要修的那个 bug 原样复活,且**没有任何判据会红**。

判据形态:**真库两臂**
  · 缺列臂:一个只灌了生产 dump(08-19,那时还没有 050)的库 → 必须抛,
    而且异常里必须点名 ``payer_user_id``(不点名就分不清是被别的缺列拦下的);
  · 有列臂:本 session 那个已重放全部迁移的库 → 必须放行。
只验前者会漏掉「恒抛」的坏守卫,只验后者会漏掉「恒放行」的坏守卫。
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.defgeo_funding_p0_2026_08_25 import conftest as CT

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_without_the_column():
    """只灌生产 dump、**不跑迁移** 的一次性库(= 050 漏跑的真实形态)。"""
    if not CT.PROD_SCHEMA.is_file():
        pytest.skip("缺生产 schema 夹具 " + str(CT.PROD_SCHEMA))
    name = "defgeo_p0fix_nomig_" + uuid.uuid4().hex[:6] + "_test"
    assert "defgeo" in name and "test" in name, "unsafe test db name"
    import psycopg2
    admin = psycopg2.connect(CT._admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = CT.ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    conn = CT._conn(dsn)
    CT._load_prod_schema(conn)
    conn.cursor().execute("SET search_path = public")
    # 自证这个库**确实**缺列 —— 夹具没造对的话下面那条会因为错误的原因绿。
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='diagnosis_runs' "
                "AND column_name='payer_user_id'")
    assert cur.fetchone() is None, "夹具库里居然已经有 payer_user_id —— 缺列臂不成立"
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(CT._admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def _run_guard(dsn):
    from services.startup_schema_guards import verify_diagnosis_schema_fail_closed

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        verify_diagnosis_schema_fail_closed()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def test_guard_refuses_to_start_when_the_payer_column_is_missing(db_without_the_column):
    with pytest.raises(RuntimeError) as err:
        _run_guard(db_without_the_column)
    assert "payer_user_id" in str(err.value), (
        "守卫抛了,但抛的不是缺 payer_user_id(而是别的原因)—— "
        "这条判据没有在守迁移 050:%s" % err.value)


def test_guard_lets_a_migrated_database_start(migrated_dsn, live_server):
    """配对的必须不命中:列在的库必须放行。

    少了它,一个「恒抛」的守卫也能让上面那条绿 —— 而恒抛的守卫等于 fleet 永远起不来。
    """
    _ = live_server
    _run_guard(migrated_dsn)
