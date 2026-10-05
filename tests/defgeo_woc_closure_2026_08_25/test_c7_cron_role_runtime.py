"""C-7(2) · 发布 worker 的**启动归属**:cron 是独立角色容器。

═══════════════════════════════════════════════════════════════════════
🔴 先证伪:Codex P1-15 的后半句在 `1c7839edc` 上**不成立**
═══════════════════════════════════════════════════════════════════════
Codex 写「发布入口可冻结,但主 scheduler 未确保启动发布 worker」。
实核 `api/scheduler.py::register_v32_core_tasks`(无条件段)里那一组四条
`add_job` 已经包含 `defgeo_publish_dispatch` →
`publish_worker:dispatch_pending_sync`,而且是 **fail-closed** 注册
(注册失败直接 `raise`,拒绝带病启动 cron)。
`tests/defensive_geo_pkge_2026_08_24/test_wiring_lock_runtime.py` 也已经有
"从调度摘掉 → 红"的运行期锁。**那一半是包E 修过的,不是缺口。**

═══════════════════════════════════════════════════════════════════════
🔴 真正缺的那一格:**没有任何判据钉住"谁来注册"**
═══════════════════════════════════════════════════════════════════════
既有的运行期锁是直接调 `register_v32_core_tasks()`,它证明的是
"这个函数会注册那四条";它证明不了:

  · web 角色**不会**注册(摘掉 `if _IS_CRON_ROLE:` ⇒ 4 个 web worker 各跑一遍
    cron,本仓 2026-07-13 记过的 4× 病根原样复活);
  · cron 角色**会**注册(把那一段从 server.py 导入期挪走 ⇒ 生产上一条都不注册,
    症状是客户确认发布、算力冻着、没有任何东西接手 —— 终审 P0-1 复发)。

两个方向都要打,而且必须是**运行期**:AST 杀不掉 `if False:` / 早 return /
把整块搬进一个没人调的函数。所以这里在**子进程里真 import server.py**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import psycopg2
import pytest

from tests.defgeo_woc_closure_2026_08_25.conftest import (
    EXACT_THROWAWAY_URL, PROD_SCHEMA, ROOT,
)

#: 角色探针自己的库。**不复用**本包主库:探针要跑全量 manifest 迁移,
#: 会把主库的 schema 改成另一副样子,后面的判据就不在同一个分母上了。
ROLE_DB = "geo_defgeo_woc_role_test"

#: 必须由 cron 角色注册、且**动状态/动钱**的那几条。分母写在这里、
#: 由下面那条判据与 `pkge` 的 EXPECTED_JOBS 机械对账,不手抄两份。
REQUIRED_CRON_JOBS = (
    "defgeo_publish_dispatch",
    "defgeo_publish_reconcile",
    "defgeo_activation_materialize",
    "defgeo_run_execute",
)


def _admin_dsn() -> str:
    return EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/postgres"


def _role_dsn() -> str:
    return EXACT_THROWAWAY_URL.rsplit("/", 1)[0] + "/" + ROLE_DB


def _probe(role: str, dsn: str) -> dict:
    env = dict(os.environ)
    env["ROLE"] = role
    env["DATABASE_URL"] = dsn
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("TEST_DATABASE_URL", None)                    # 别让本包 conftest 的闸串进去
    proc = subprocess.run(
        [sys.executable, "-m", "tests.defgeo_woc_closure_2026_08_25._role_probe"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    marker = "PROBE_RESULT "
    for line in (proc.stdout or "").splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise AssertionError(
        f"ROLE={role} 探针没起来(rc={proc.returncode})\n"
        f"--- stdout tail ---\n{(proc.stdout or '')[-1500:]}\n"
        f"--- stderr tail ---\n{(proc.stderr or '')[-2500:]}")


# ══════════════════════════════════════════════════════════════════════════
def test_c7_50_both_arms_actually_started(probes) -> None:
    """🔴 活性前置:**两臂都要真的起来**。

    只要有一臂是因为起不来而"没注册",下面那条"web 不注册"就变成了
    一句空话 —— 本仓铁律:A/B 两臂都红 ⇒ 记「没验」不是「无新增红」。
    """
    assert probes["web"]["role"] == "web", probes["web"]
    assert probes["cron"]["role"] == "cron", probes["cron"]
    assert probes["cron"]["jobs"], "cron 臂一条 job 都没注册 —— 它其实没起来"


def test_c7_51_web_role_registers_nothing(probes) -> None:
    """🔴 web 角色**跳过全部 cron 注册**。

    摘掉 `server.py` 里那道 `if _IS_CRON_ROLE:` ⇒ 4 个 web worker 各注册一遍
    ⇒ 本条红。这正是 2026-07-13 记过的 4× 病根(含计费 job)。
    """
    web = probes["web"]
    assert web["isCronRole"] is False, web
    assert web["registerCalls"] == 0, (
        f"web 角色调了 register_v32_core_tasks {web['registerCalls']} 次 —— "
        "cron 会在每个 web worker 上各跑一遍")
    assert web["jobs"] == {}, web["jobs"]


def test_c7_52_cron_role_owns_the_registration(probes) -> None:
    """🔴 cron 角色**确实**注册,而且发布 worker 就在里面。

    把那一段从 server.py 导入期挪走(或包进一个没人调的函数)⇒ 本条红:
    生产上一条都不注册,症状是客户确认发布、算力冻着、没有任何东西接手。
    """
    cron = probes["cron"]
    assert cron["isCronRole"] is True, cron
    assert cron["registerCalls"] == 1, cron
    missing = [j for j in REQUIRED_CRON_JOBS if j not in cron["jobs"]]
    assert not missing, f"cron 角色没注册这些推进器:{missing}(实注册 {sorted(cron['jobs'])})"


def test_c7_53_publish_worker_is_bound_to_the_real_function(probes) -> None:
    """绑的是**那个函数**,不是别的(比如只读告警)。

    只判"job 在不在"抓不到绑错函数:id 还在,而它跑的是另一段代码。
    """
    jobs = probes["cron"]["jobs"]
    assert jobs["defgeo_publish_dispatch"] == (
        "services.defensive_geo.publish.publish_worker:dispatch_pending_sync"), jobs
    assert jobs["defgeo_publish_reconcile"] == (
        "services.defensive_geo.publish.publish_worker:reconcile_tick_sync"), jobs
    assert jobs["defgeo_activation_materialize"] == (
        "services.defensive_geo.activation_materializer:materialize_pending_sync"), jobs


def test_c7_54_required_jobs_denominator_matches_the_package_lock(probes) -> None:
    """分母对账:本文件这份清单 ⊆ 包E 那份 ``EXPECTED_JOBS`` + 诊断执行器。

    两份手抄清单迟早分叉,而分叉的那一格不会让任何判据变红。
    这条把它们绑在一起:包E 加一条推进器而这里没跟上,立刻红。
    """
    from tests.defensive_geo_pkge_2026_08_24.test_wiring_lock_runtime import (
        EXPECTED_JOBS,
    )

    advancers = {jid for jid, (_m, _f, fail_closed) in EXPECTED_JOBS.items() if fail_closed}
    # 诊断执行器不在包E 的表里(它是门三 G9 的),显式并进来。
    expected = advancers | {"defgeo_run_execute"}
    assert set(REQUIRED_CRON_JOBS) == expected, (
        f"分母漂移:本文件 {sorted(REQUIRED_CRON_JOBS)} vs 包E+G9 {sorted(expected)}")
