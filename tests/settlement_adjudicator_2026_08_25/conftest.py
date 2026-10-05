"""复用 P0-3c 的运行时台架 —— 真 PG + 真 import server + 真资金原语。

不另起一套:同一个台架两处维护必有一处漂(本仓老教训)。
P0-3c 的 `live_dsn` 用 `os.environ` 做**进程级**单例(不是模块级变量 —— pytest 会把
同一个 conftest 加载成两个模块对象,模块级 dict 等于没做单例),所以这里 re-export
是安全的:三个包合跑仍然只建一个库。
"""
from tests.p03c_org_guards_2026_08_25.conftest import (  # noqa: F401
    db,
    live_dsn,
    live_server,
)

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


@pytest.fixture(autouse=True)
def _isolate_adjudicator_world(live_server, live_dsn):
    """逐用例复位。

    🔴 必须有:`run_diagnosis_sweep()` 处理的是**库里所有** settlement_manual 单,
    不只是本用例造的那一单。上个用例的残留会:
      ① 被重复裁定,污染本用例的断言;
      ② 往裁定表里插入不同 failure_cause 的行 —— 而「同因**连续** ≥N」是全局连续性,
         被插一行别的原因就断了 ⇒ 那条判据会随执行顺序时红时绿(比恒绿更难查)。
    候选按 status_changed_at ASC 取,残留单**排在本用例的新单前面**,所以一定先被处理。

    🔴 依赖 `live_server` 而不只是 `live_dsn`:049 表由 `init_db` 重放迁移清单建出来,
    而 `init_db` 是 `import server` 时跑的。只要 `live_dsn`(灌 schema dump)的话,
    不碰 server 的那几条契约判据会在这里 UndefinedTable —— 与 P0-3c 那次
    "org 就绪门要求 init_db 在**那个库**上跑过" 是同一个形状。
    """
    c = psycopg2.connect(live_dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    cur = c.cursor()
    # 裁定表没有 FK(刻意:run 行被清理时审计轨要留存),可以直接清。
    cur.execute("DELETE FROM diagnosis_settlement_adjudications")
    # 残留的转人工单挪出候选集(不删 —— 删 run 行会撞 diagnosis_refund_records 的 FK)。
    cur.execute("UPDATE diagnosis_runs SET run_status='committed' "
                "WHERE run_status IN ('settlement_manual','commit_pending','release_pending')")
    c.close()
    yield
