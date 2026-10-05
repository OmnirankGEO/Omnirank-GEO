#!/usr/bin/env python
"""[门9 ①] 合流尖全分母基线 —— **七包 + 新增三包**,自有冷建库,collect **实收**报数。

为什么要单独一个脚本(而不是复用 extsel 的 ``run_family``)
--------------------------------------------------------
extsel 那套是**变异**用的:它只关心"红集"与"绿数",collect 数从来没进过账。
门9 要的是「分母本身可核」——**收了多少条**、跑了多少条、两个数对不对得上。
"收了 1311 跑了 1310 skip 1" 与 "收了 1311 跑了 900" 在旧口径下长得一模一样。

三条自证(每一条都是实测踩出来的,不是设计洁癖)
------------------------------------------------
① **分母机械枚举**:三个来源(extsel FULL_DENOMINATOR / e3 SUITES /
   E2 合流带进 tests/ 的目录)求并集,不手抄清单。手抄漏一个不会让任何判据变红。
② **环境变量 census 走 AST,不走正则**:``defgeo_funding_p0`` 的
   ``os.getenv(\\n "DEFGEO_P0FIX_TEST_DSN", ...)`` 是**跨行**的,单行正则扫不到 ——
   我第一版就是这么漏的。漏掉一个 DSN 变量 = 那个包**静默**回落到共享默认库,
   而它照样全绿 ⇒ "自有冷建库"这句话当场变成假的。
③ **库活性反证**:跑完在**我自己的库**里数表。就地建库的包如果偷偷回落到共享库,
   我的库会是空的 —— 这是"回落"唯一会留下的可观测痕迹。
   ``pkgf`` / ``w4`` 读的是 ``X or DEFAULT_THROWAWAY_URL``,是本轮最危险的两个。
   每 session 现建库的两个包(funding_p0 / xiaobang)建完就 DROP,数表数不到,
   改用**服务端 DDL 日志**取证(容器 ``log_statement='ddl'``)。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import hashlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 🔴 [V7-B] 本文件也会被 importlib 按路径加载(v2 runner 的 `_B`),
#    那时 sys.path[0] 不是 scripts/ —— 实测 `import mutation_replay_common`
#    直接 ModuleNotFoundError。所以自己把所在目录也插进去。
sys.path.insert(0, str(Path(__file__).resolve().parent))
OUT = ROOT / ".gate9"

#: 我自己的一次性 PG16 容器 —— 现值,也是**不设 env 时的默认值**。
#: 🔴 这两个默认值一变就是一次行为改变:`test_mutation_gate9_transport_contract`
#:    把它们逐字钉住,必须过一次人。
DEFAULT_BASE_DSN = "postgresql://geo_admin:testpw@localhost:55488"
DEFAULT_PG_CONTAINER = "gate9-pg"

#: host 落在这几个名字上时,5432 就是**宿主上暴露的生产端口**。
#: 🔴 不能一刀切"含 5432 就停":容器内部网里 PG 本来就听 5432 ——
#:    Codex 那轮的容器内 DSN 逐字是
#:    `postgresql://geo_admin:testpw@codex-fixoffix2-den-pg312-…:5432`。
#:    一刀切会把要用的 harness 一起挡掉(规则的适用域 < 使用域,又一次)。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})

#: 生产特征词:命中即停。`omnirank` 覆盖 `omnirank-db` 容器名。
_PROD_MARKERS = ("geo_agentscope", "omnirank")


def assert_transport_is_not_production(dsn: str) -> None:
    """[V5-B · Review 照准] BASE 可配之后的三条停机栓。

    🔴 为什么参数化必须同笔加栓:``_assert_latch_armed`` 里 ADMIN DSN 那条是
       ``dsn.startswith(BASE + "/")`` —— 它**跟着 BASE 走**。BASE 写死时
       "我自己的容器"这句话是常量;BASE 可配之后,这句话由环境说了算。
       一个打错的 env 就能让 15 包判据连上生产,而 ADMIN 栓会照样放行。
    """
    import urllib.parse as _up

    low = dsn.lower()
    hit = [m for m in _PROD_MARKERS if m in low]
    if hit:
        raise SystemExit(
            f"🔴 传输面指向**生产**特征({hit}):{dsn}\n"
            "   gate9 只许跑在一次性容器上。停机。")
    try:
        parts = _up.urlsplit(dsn)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError as e:                      # 端口不是数字之类
        raise SystemExit(f"🔴 传输面 DSN 解析不了:{dsn}({e})") from None
    # 没写端口 ⇒ PostgreSQL 默认就是 5432,与显式写 5432 同罪。
    if host in _LOCAL_HOSTS and (port is None or port == 5432):
        raise SystemExit(
            f"🔴 传输面指向**宿主上的 5432**:{dsn}\n"
            "   那是生产实例暴露的端口(没写端口时 PG 默认也是 5432)。\n"
            "   一次性容器请用 55xxx;容器内部网里用容器名做 host,那时 5432 是允许的。")


def _resolve_base_dsn() -> str:
    raw = (os.environ.get("GATE9_BASE_DSN") or "").strip() or DEFAULT_BASE_DSN
    raw = raw.rstrip("/")
    assert_transport_is_not_production(raw)
    return raw


def _resolve_pg_container() -> str:
    return (os.environ.get("GATE9_PG_CONTAINER") or "").strip() or DEFAULT_PG_CONTAINER


def _resolve_pg_log_file():
    """DDL 活性日志读哪儿:不设 ⇒ 走 ``docker logs``(原路,逐字节不变)。

    🔴 [V5-B · Review 照准] 加这个口子是为了**把时效边界拿回被测方手里**。
       容器 harness 给 ``docker`` 装的 shim 全文只有两行::

           #!/bin/sh
           cat /pglogs/postgresql-round2.log

       它**不看任何参数** —— 既不看容器名,也不看 ``--since``。而 ``liveness``
       里那句注释逐字写着「``--since`` 是唯一的时效边界,没有它这条臂第二轮起
       永久为真」。于是前手专门加的边界在容器轮里是**关着的**,runner 无从知道。
       上一轮结论仍成立,靠的是「PG 容器当轮现建、日志里本来只有这一轮」——
       **布置的巧合,不是机制**。

       替身**少接**一个参数比替身**多做**一件事更隐蔽:不报错、不缺字段、
       不改格式,调用方拿到的形状完全正确,只是它以为施加的约束没有施加。
    """
    raw = (os.environ.get("GATE9_PG_LOG_FILE") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_file():
        raise SystemExit(
            f"🔴 GATE9_PG_LOG_FILE 指的文件不在:{p}\n"
            "   不许静默当成「零 DDL 行」—— 零 DDL 行正是「这个包没跑在自己库上」的"
            "观测形态,两者不能同形。")
    return p


#: PG 日志行首(``log_line_prefix = '%m [%p] '``)。真机取样:
#: ``2026-08-28 20:22:18.088 UTC [218030] LOG:  statement: CREATE DATABASE "…"``
_PG_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? +(\S+) ")

#: 只认这些等价于 UTC 的写法。别的时区缩写**有歧义**,猜一个 = 悄悄挪动边界。
_UTC_TZ_NAMES = frozenset({"UTC", "GMT", "Z", "UCT", "Universal", "Zulu", "Etc/UTC"})


def parse_pg_log_ts(line: str):
    """取一行 PG 日志的时间戳(UTC);**续行**(无前缀)返回 ``None``。"""
    import datetime as _dt

    m = _PG_LOG_TS.match(line)
    if not m:
        return None
    tz = m.group(2)
    if tz not in _UTC_TZ_NAMES:
        raise SystemExit(
            f"🔴 PG 日志时区不是 UTC 而是 {tz!r} —— 时区缩写有歧义,"
            "猜一个等于悄悄挪动本轮的时效边界。\n"
            "   请把该容器的 log_timezone 设成 UTC 再跑。原行:" + line[:120])
    return _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_dt.timezone.utc)


def filter_log_since(text: str, since: str | None) -> str:
    """把日志按 ``since`` 裁回本轮 —— ``docker logs --since`` 在文件路径上的等价物。

    续行(没有时间戳前缀)**继承上一行**的时间戳:多行语句不许被拦腰截断。
    """
    import datetime as _dt

    if not since:
        return text
    try:
        cut = _dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc)
    except ValueError:
        raise SystemExit(
            f"🔴 since 解析不了:{since!r} —— 本轮锚是 runner 自己生成的 "
            "%Y-%m-%dT%H:%M:%SZ,对不上就说明上游改了格式,停机") from None
    keep, cur = [], None
    for line in text.splitlines(keepends=True):
        ts = parse_pg_log_ts(line)
        if ts is not None:
            cur = ts
        if cur is not None and cur >= cut:
            keep.append(line)
    return "".join(keep)


def transport_log_source() -> str:
    """DDL 活性证据的**真实**来源 —— 进证据用。"""
    return f"file:{PG_LOG_FILE}" if PG_LOG_FILE else f"docker:{PG_CONTAINER}"


def print_transport_identity(identity: dict) -> dict:
    """把**解析后**的传输面打进证据。

    证据里只有"跑过了"而没有"跑在哪个库上",等于缺了传输面那一栏 ——
    上一轮容器 harness 正是靠一份手工 patch 换掉这两个值的,
    换成 env 之后更要在读数里写明它到底解析成了什么、从哪来。
    """
    src_b = "GATE9_BASE_DSN" if (os.environ.get("GATE9_BASE_DSN") or "").strip() \
        else "默认值(未设 GATE9_BASE_DSN)"
    src_c = "GATE9_PG_CONTAINER" if (os.environ.get("GATE9_PG_CONTAINER") or "").strip() \
        else "默认值(未设 GATE9_PG_CONTAINER)"
    # 🔴 [2026-08-30] 这里原来自己调 repo_identity() —— 于是同一轮里
    #    log 与产物各取一个 started_at,实测差 **27 分钟**
    #    (log 07:02:41Z / 产物 07:29:26Z)。字段名叫 started_at,记的却是
    #    「payload 写盘那一刻」;同一份证据里两个时间戳互相打架。
    #    身份**只在入口算一次**,print 与 payload 共用同一个对象。
    _id = identity
    print(f"── ⓪ 树身份 ── tip={_id['tip']}")
    print(f"               tree={_id['tree']}  ·  镜像={_id['image_id'] or '(宿主/未提供)'}"
          f"  ·  起跑 {_id['started_at']}")
    print(f"── ⓪ 传输面 ── BASE={BASE}  ← {src_b}")
    print(f"               PG 容器={PG_CONTAINER}  ← {src_c}")
    print(f"               DDL 活性日志来源={transport_log_source()}"
          + ("  (Python 侧按 --since 裁回本轮)" if PG_LOG_FILE else ""))
    # 🔴 **返回它实际打印的那一份** —— 硬门据此与产物做整份比。
    #    只比 tip 不够:实测的 bug 正是 tip 相同而 started_at 被覆盖
    #    (log 07:02:41Z / 产物 07:29:26Z)。自打毒验过:只比 tip 时那一形**存活**。
    return dict(identity)


BASE = _resolve_base_dsn()
PG_CONTAINER = _resolve_pg_container()
PG_LOG_FILE = _resolve_pg_log_file()

#: 每个包要落哪些环境变量。**键是包目录,值是 env → DSN**。
#: 🔴 这张表不许凭印象写:下面 ``_env_census`` 会用 AST 把每个包真读的
#:    DSN 类变量枚举出来,少一个就停机。
ENV_PLAN: dict[str, dict[str, str]] = {
    "tests/defgeo_funding_p0_2026_08_25": {
        # ADMIN_DSN —— 本包每 session 现建 defgeo_p0fix_<hex>_test 再 DROP。
        "DEFGEO_P0FIX_TEST_DSN": f"{BASE}/defgeo_p0fix_g9_test",
        "TEST_DATABASE_URL": f"{BASE}/defgeo_p0fix_g9_test",
    },
    "tests/defensive_geo_2026_08_21": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_g9a_test"},
    "tests/defgeo_woc_closure_2026_08_25": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_woc_g9_test"},
    "tests/xiaobang_execute_2026_08_20": {
        # 本包也是每 module 现建 xbexec_<hex>_test 再 DROP。
        "TEST_DATABASE_URL": f"{BASE}/xbexec_g9_seed_test"},
    "tests/defgeo_wob_publish_funding_2026_08_25": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_wob_g9_test"},
    "tests/defensive_geo_pkge_2026_08_24": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_pkge_g9_test"},
    "tests/defensive_geo_w3_2026_08_21": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_w3c_g9_test"},
    "tests/defensive_geo_w2_2026_08_21": {
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_w2_g9_test"},
    "tests/defgeo_e3_2026_08_26": {
        "E3_TEST_DATABASE_URL": f"{BASE}/geo_e3_g9_ledger_test",
        "TEST_DATABASE_URL": f"{BASE}/geo_e3_g9_ledger_test"},
    "tests/defensive_geo_pkgf_2026_08_23": {
        # 🔴 只认自己这个变量,不认 TEST_DATABASE_URL;不设就静默回落共享库。
        "DEFGEO_PKGF_TEST_DB_URL": f"{BASE}/geo_defgeo_pkgf_g9_test",
        # 🔴 根 tests/conftest.py 无条件要 TEST_DATABASE_URL,不设直接 ImportError。
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_pkgf_g9_test"},
    # 🔴 [Review 令① · 2026-08-27] 收编 —— MUT-EXTE2-04 的杀手判据长在这儿。
    #    两个包都是**现建现删**(每 module 建 p03_<hex>_test / p03c_<hex>_test),
    #    所以只需要把 ADMIN_DSN 指到我的容器,活性靠 DDL 日志取证。
    #    ⚠️ 实测:p03 与 p03c **合跑同一个 pytest 进程会齐炸 20 个 setup error**
    #    (p03c conftest 自陈:同名 fixture 被 re-export ⇒ 建两个库 ⇒ 第二个库
    #     migration_ok=false ⇒ org 就绪门 503)。本分母是**一包一进程**,不受影响;
    #    这条写在这里是留给下一个想把它们并成一条命令的人。
    # 🔴 [Review 令 · 台账「2026-08-28 A V3-A 验收合流」] 显式收编。
    #    变量名不是猜的,从该包 conftest 读:DEFGEO_V3A_TEST_DSN 是 **ADMIN DSN**
    #    (连 postgres 库),包内每 module 现建 defgeo_v3a_<tag>_<hex>_test 再 DROP。
    # 🔴 [Review 令 · 台账「08-28 A V4-A 合流」] 收编。变量名从该包 conftest 读:
    #    DEFGEO_V4A_TEST_DSN 是 **ADMIN DSN**(连 postgres 建/删库),
    #    包内现建 defgeo_v4a_<tag>_<hex>_test ⇒ MINTING_DB + ADMIN 栏安全栓。
    "tests/defgeo_v4a_2026_08_28": {
        "DEFGEO_V4A_TEST_DSN": f"{BASE}/postgres",
        "TEST_DATABASE_URL": f"{BASE}/defgeo_v4a_g9_test"},
    # 🔴 [Review 补令 · 2026-08-29] A 的 V5-A 包收编。变量名不是猜的,
    #    从该包 conftest 读:DEFGEO_V5A_TEST_DSN 是 **ADMIN DSN**
    #    (默认值 `…@localhost:55495/postgres`,包内 CREATE/DROP DATABASE),
    #    每 module 现建 defgeo_v5a_<tag>_<hex>_test ⇒ MINTING_DB + ADMIN 栏安全栓。
    #    AST census 实测该包只读这一个 DSN 变量;TEST_DATABASE_URL 是根 conftest 无条件要的。
    # 🔴 [08-29] 纯静态包,但根 conftest **无条件**要 TEST_DATABASE_URL
    #    (不给直接 ImportError)。它只校验库名、不主动连 —— 实测在一个不存在的
    #    库上 15 passed。所以给库名、不给活性归属(见 NO_DB_PACKAGES)。
    "tests/defgeo_v5b_2026_08_29": {
        "TEST_DATABASE_URL": f"{BASE}/defgeo_v5b_g9_test"},
    "tests/defgeo_v5a_2026_08_28": {
        "DEFGEO_V5A_TEST_DSN": f"{BASE}/postgres",
        "TEST_DATABASE_URL": f"{BASE}/defgeo_v5a_g9_test"},
    "tests/defgeo_v3a_2026_08_28": {
        "DEFGEO_V3A_TEST_DSN": f"{BASE}/postgres",
        "TEST_DATABASE_URL": f"{BASE}/defgeo_v3a_g9_test"},
    # 🔴 [V7-C · 2026-08-30] A 的 gate8 判据包,Review 裁定收编(17 -> 18)。
    #    变量名从该包 conftest 实读:`DEFGEO_GATE8_TEST_DSN` 是 **ADMIN DSN**
    #    (默认值就连 /postgres),按 `CREATE DATABASE … TEMPLATE` 建
    #    `defgeo_gate8_<tag>_<hex>_test` ⇒ 归 MINTING_DB。
    #    🔴 **故意不给 TEST_DATABASE_URL**:那把库没有任何人会创建,
    #    在计划里写一个永不存在的库 = 往 ENV_PLAN 里写一句假话,
    #    而且会让这个包同时挂着 DSN 与铸库前缀、活性归属含糊。
    #    它的祖先 conftest 问题改用 EXTRA_PYTEST_ARGS 的 --confcutdir 解,见那里。
    "tests/defgeo_gate8_2026_08_30": {
        "DEFGEO_GATE8_TEST_DSN": f"{BASE}/postgres"},
    "tests/p03_settlement_2026_08_24": {
        "P03_TEST_DSN": f"{BASE}/p03_g9c_test",
        "TEST_DATABASE_URL": f"{BASE}/p03_g9c_test"},
    "tests/p03c_org_guards_2026_08_25": {
        "P03C_TEST_DSN": f"{BASE}/p03c_g9c_test",
        "TEST_DATABASE_URL": f"{BASE}/p03c_g9c_test"},
    "tests/defensive_geo_w4_2026_08_22": {
        "DEFGEO_W4_TEST_DB_URL": f"{BASE}/geo_defgeo_w4_g9_test",
        # 🔴 census 抓出来的:E3 带进来的 test_v2_monitoring_http_pg.py 走**另一把库**,
        #    只认 DEFGEO_W4_HTTP_DB_URL。不落值它就自己在共享 55475 上建库跑,
        #    整包照样全绿 —— "自有冷建库"会**恰好在 E3 新增的那条判据上**变成假话。
        "DEFGEO_W4_HTTP_DB_URL": f"{BASE}/geo_defgeo_w4_http_g9_test",
        "TEST_DATABASE_URL": f"{BASE}/geo_defgeo_w4_g9_test"},
    # 🔴 [P0 2026-09-01] 迁移重放床。它**只从 TEST_DATABASE_URL 取服务器地址**,
    #    再连同一台机的 postgres 库 CREATE/DROP `migreplay_*_test` ⇒ 归 MINTING_DB。
    #    不另设 ADMIN DSN 变量:包内 `_server_base()` 把路径段剥掉复用同一 netloc,
    #    AST 实测该包读的 DSN 变量只有这一个(写两个 = 活性归属含糊)。
    "tests/defgeo_migration_replay_2026_09_01": {
        "TEST_DATABASE_URL": f"{BASE}/migreplay_g9_test"},
    # 🔴🔴 [第二趟车 2026-09-02] 这个包的 conftest 会 `DROP SCHEMA public CASCADE`
    #    —— 它**必须**拿到一把专属库,任何一个变量落到共享库都是清空别人。
    #    变量名从该包 conftest 实读:`DIAGBRAND_TEST_DB_URL`(:60),
    #    默认值 `…@localhost:55498/geo_diagbrand_test`;它自带安全栓,
    #    库名必须同时含 `diagbrand` 与 `test`(:18),下面这把名字两项都满足。
    #    🔴 还要 `TEST_DATABASE_URL`:根 conftest 无条件要它,**并且** `env_for()`
    #    会拿它把 `DATABASE_URL` 一起钉死(:1354-1357)—— 被测的
    #    `get_or_create_brand` 自己开连接、不收 cursor,读的正是 `DATABASE_URL`。
    #    两个变量指**同一把库**,不许分叉。
    "tests/diag_brand_attribution_2026_08_31": {
        "DIAGBRAND_TEST_DB_URL": f"{BASE}/geo_diagbrand_g9_test",
        "TEST_DATABASE_URL": f"{BASE}/geo_diagbrand_g9_test"},
    # 🔴 [第二趟车 2026-09-02] 纯逻辑包:替身 Redis(monkeypatch),不连真 Redis 也不碰库。
    #    机械核过:psycopg2 / DATABASE_URL / connect( 全零命中,且它没有自带 conftest。
    #    给库名只是因为**根 conftest 无条件要** `TEST_DATABASE_URL`(不给直接 ImportError);
    #    给名不给活性归属 —— 见 NO_DB_PACKAGES。
    "tests/redis_degrade_2026_09_02": {
        "TEST_DATABASE_URL": f"{BASE}/redis_degrade_g9_test"},
    # 🔴 [第四趟合车 2026-09-02] 两个纯逻辑包。**实测**不是采信自报:
    #    在一个**不存在的库**(`…@127.0.0.1:1/rv_no_db_test`)上跑 ⇒ 17 / 21 passed;
    #    反向对照 `diag_brand_attribution` 在同一把库上当场红(安全栓拒绝)⇒ 探针不恒真。
    #    机械核过:两包**零 DSN 变量、零自有 conftest**。给库名只因根 conftest 无条件要它。
    "tests/defgeo_flat_pricing_2026_09_02": {
        "TEST_DATABASE_URL": f"{BASE}/flat_pricing_g9_test"},
    "tests/plan_identity_copy_2026_09_02": {
        "TEST_DATABASE_URL": f"{BASE}/plan_identity_g9_test"},
}

#: 跑完在**我的**库里数表(> 0 即证明这个包真的建在我这儿)。
#: 每 session 现建现删的两个包不在此列 —— 它们靠 DDL 日志取证。
INPLACE_DB: dict[str, tuple[str, ...]] = {
    "tests/defensive_geo_2026_08_21": ("geo_defgeo_g9a_test",),
    "tests/defgeo_woc_closure_2026_08_25": ("geo_defgeo_woc_g9_test",),
    "tests/defgeo_wob_publish_funding_2026_08_25": ("geo_defgeo_wob_g9_test",),
    "tests/defensive_geo_pkge_2026_08_24": ("geo_defgeo_pkge_g9_test",),
    "tests/defensive_geo_w3_2026_08_21": ("geo_defgeo_w3c_g9_test",),
    "tests/defensive_geo_w2_2026_08_21": ("geo_defgeo_w2_g9_test",),
    "tests/defgeo_e3_2026_08_26": ("geo_e3_g9_ledger_test", "geo_defgeo_e3_cold_test"),
    "tests/defensive_geo_pkgf_2026_08_23": ("geo_defgeo_pkgf_g9_test",),
    "tests/defensive_geo_w4_2026_08_22": ("geo_defgeo_w4_g9_test", "geo_defgeo_w4_http_g9_test"),
    # 🔴 [第二趟车] 它 DROP SCHEMA public + 重建 `brands` 等表 ⇒ 跑完表数 > 0,
    #    属**就地**而非现建现删,所以归这一类不归 MINTING_DB。
    "tests/diag_brand_attribution_2026_08_31": ("geo_diagbrand_g9_test",),
}

#: 现建现删 ⇒ 数表数不到,看服务端 DDL 日志里的 ``CREATE DATABASE "<前缀>…"``。
MINTING_DB: dict[str, str] = {
    "tests/defgeo_funding_p0_2026_08_25": "defgeo_p0fix_",
    "tests/xiaobang_execute_2026_08_20": "xbexec_",
    "tests/p03_settlement_2026_08_24": "p03_",
    "tests/p03c_org_guards_2026_08_25": "p03c_",
    "tests/defgeo_v3a_2026_08_28": "defgeo_v3a_",
    "tests/defgeo_v4a_2026_08_28": "defgeo_v4a_",
    "tests/defgeo_v5a_2026_08_28": "defgeo_v5a_",
    # 🔴 [V7-C] 实读 conftest 的 `_safe_name`:`defgeo_gate8_%s_%s_test`。
    "tests/defgeo_gate8_2026_08_30": "defgeo_gate8_",
    # 🔴 [P0 2026-09-01] 三张床各现建现删一把库:migreplay_bed_a_test /
    #    migreplay_bed_b_test / migreplay_bed_oid_test(前缀实读自本包源码)。
    "tests/defgeo_migration_replay_2026_09_01": "migreplay_",
}

#: 🔴 [V6-B-final · Review 裁定 2026-08-29] 第三类归属:**根本不碰库**的纯静态判据包。
#:    起因:合流带进 tests/defgeo_v5b_2026_08_29(15 条),机械查过它
#:    DSN 变量 0 / DROP SCHEMA 文件 0 / 夹具 schema 变量 0 / 无建库前缀。
#:    而「每个包都要有活性归属」那道闸当初的**适用域只覆盖会碰库的包** ——
#:    对纯静态包只有两条死路:编一个 INPLACE_DB(那把库永远 0 张表 ⇒ 活性臂恒红),
#:    或把它排除在分母外(那 15 条判据的红就没有地方会出现)。所以立第三类。
#:    🔴 它极易变成垃圾桶,所以:逐条写理由 + 钉大小 + 正样本
#:    (往里塞一个**真碰库**的包必须红)+ 三类两两互斥且并集 == 分母。
NO_DB_PACKAGES: dict[str, str] = {
    "tests/defgeo_v5b_2026_08_29":
        "纯静态判据(nextAction 覆盖 census + .gitattributes 窄豁免作用域),"
        "AST 实测:DSN 变量 0 · DROP SCHEMA 0 · 夹具 schema 变量 0;"
        "在一个**不存在的库**上实跑 15 passed —— 它不碰库",
    # 🔴 [第二趟车 2026-09-02] 第二个纯静态包。机械核过:
    #    `psycopg2` / `DATABASE_URL` / `connect(` 在该包源码里**零命中**;
    #    Redis 也是 monkeypatch 装的替身(`_install()` 注释原话「不碰真 Redis」),
    #    所以它既不需要容器里的 PG,也不需要真 Redis ⇒ 容器段即可,不用宿主运输。
    "tests/redis_degrade_2026_09_02":
        "纯逻辑判据(cache/redis_client 熔断)· 替身 Redis 不连真服务;"
        "AST/grep 实测:psycopg2 0 · DATABASE_URL 0 · connect( 0 · 无自带 conftest",
    "tests/defgeo_flat_pricing_2026_09_02":
        "纯逻辑判据(按题计价)· 实测在不存在的库上 17 passed;DSN 变量 0 · 自有 conftest 0",
    "tests/plan_identity_copy_2026_09_02":
        "纯逻辑判据(题单身份文案)· 实测在不存在的库上 21 passed;DSN 变量 0 · 自有 conftest 0",
}
NO_DB_PACKAGES_SIZE = 4

#: 非 DSN 但**决定判据能不能跑**的夹具文件路径变量。
#: 🔴 [V6-B · Codex P1-NEW-1] 这里原来只有 2 项、手写,漏了 P03 / P03C ——
#:    而那两包 conftest 的默认值是 **Windows 绝对路径**,容器里必不存在 ⇒ 整包 skip。
#:    漏掉的那一项不会让任何判据变红,它只让两个包静默消失。
#:    现在这份清单是**冻结对照**,真分母由 `schema_fixture_envs()` 从源码枚举,
#:    两向对账(判据 s02)。
SCHEMA_PATH_ENVS = ("DEFGEO_P0FIX_PROD_SCHEMA_SQL", "GEOIMG_PROD_SCHEMA_SQL",
                    "P03C_PROD_SCHEMA_SQL", "P03_PROD_SCHEMA_SQL")


def schema_fixture_envs(root, targets) -> set:
    """机械枚举:目标包里读的、**指向 .sql 夹具**的环境变量。

    判定两条任一:默认值以 ``.sql`` 结尾,或变量名含 ``SCHEMA``。
    读文件用 ``utf-8-sig``,解析失败**数出来并停机**(静默 continue = 分母洞)。
    """
    import ast as _ast

    found, broken = set(), []
    for t in targets:
        for f in sorted((Path(root) / t).rglob("*.py")):
            try:
                tree = _ast.parse(f.read_text(encoding="utf-8-sig"))
            except (SyntaxError, ValueError) as e:
                broken.append(f"{t}/{f.name}: {e}")
                continue
            for node in _ast.walk(tree):
                name = default = None
                if (isinstance(node, _ast.Call) and node.args
                        and (getattr(node.func, "attr", None)
                             or getattr(node.func, "id", None)) in ("getenv", "get")
                        and isinstance(node.args[0], _ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    name = node.args[0].value
                    if len(node.args) > 1 and isinstance(node.args[1], _ast.Constant):
                        default = node.args[1].value
                elif (isinstance(node, _ast.Subscript)
                      and isinstance(node.slice, _ast.Constant)
                      and isinstance(node.slice.value, str)
                      and getattr(node.value, "attr", None) == "environ"):
                    name = node.slice.value
                if not name:
                    continue
                if "SCHEMA" in name or (isinstance(default, str)
                                        and default.lower().endswith(".sql")):
                    found.add(name)
    if broken:
        raise SystemExit("🔴 夹具 env 枚举的**分母有洞**(这些文件解析不了):"
                         + "; ".join(broken[:5]))
    return found


def assert_schema_fixtures(env, root, targets) -> dict:
    """每个夹具 env 都必须**显式落值**、文件真在、并把 sha256 写进 provenance。

    🔴 为什么不许"用默认值":那些默认值是**写死的宿主绝对路径**。
       在容器里它们不存在 ⇒ conftest ``skipif`` ⇒ 整包 skip ⇒ 六数好看、rc 0,
       而那一包的判据一条都没跑。Codex 反例:撤掉 ``P03_PROD_SCHEMA_SQL`` 之后
       ``--only p03_settlement`` 出 62 collect / 30 skip / rc 0。
    """
    import hashlib

    names = schema_fixture_envs(root, targets)
    prov, missing, gone = {}, [], []
    for n in sorted(names):
        v = (env.get(n) or "").strip()
        if not v:
            missing.append(n)
            continue
        f = Path(v)
        if not f.is_file():
            gone.append(f"{n}={v}(文件不存在)")
            continue
        prov[n] = {"path": str(f), "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                   "bytes": f.stat().st_size}
    if missing or gone:
        _nl = chr(10)
        raise SystemExit(
            "🔴 夹具 schema 变量没落实,**一个包都不跑**(零官方产物):" + _nl
            + (f"   未显式设值:{missing}" + _nl if missing else "")
            + (f"   设了但不存在(容器里那些宿主绝对路径正是这一形):{gone}" + _nl
               if gone else "")
            + "   默认值是宿主绝对路径,容器里必不存在 ⇒ conftest skipif ⇒ 整包 skip"
            " ⇒ 六数好看而判据一条没跑。")
    print(f"   ✅ 夹具 schema {len(prov)} 个全部显式落值且文件在:"
          + " · ".join(f"{k}={v['sha256'][:12]}" for k, v in sorted(prov.items())))
    return prov


#: 允许出现的 skip —— 按 **nodeid 精确**,不按数量。
#: 🔴 按数量放行等于"任意一条都行":缺 schema 那一轮 p03 整包 30 条 skip,
#:    数量闸只要写着「允许 1 条」也拦不住"换了一条"。
EXPECTED_SKIP_NODEIDS = frozenset({
    "tests/defensive_geo_w3_2026_08_21/test_wp6_structural_anchors.py"
    "::test_zero_diff_check_has_discriminating_power",
})


#: 🔴 [fof8 · Review 裁定 2026-08-31] 跑判据的 **runner 版本**也要绑进产物。
#:    由来是一次实证:我清 staging 时把 bind 进容器的 `pytestlib/` 删了
#:    (镜像里没有 pytest),重建时才发现**证据从来没记过 runner 版本** ——
#:    尖 / 树 / 镜像 id / python / platform 都绑了,唯独「用哪个 pytest 跑的」没绑。
#:    这是「产物绑尖」纪律的又一根轴:同一棵树、同一个镜像,换个 runner 版本
#:    收集数与行为都可能变,而产物说不出自己是哪一个跑的。
#: 🔴 取不到就**停机**,不写 None:「没记录」与「记了个空」在读表人眼里一样,
#:    而后者会让缺口看起来像是被覆盖过。
_RUNNER_MODULES = ("pytest", "pytest_asyncio")


def runner_versions() -> dict:
    import importlib

    out: dict[str, str] = {}
    bad: list[str] = []
    for name in _RUNNER_MODULES:
        try:
            v = getattr(importlib.import_module(name), "__version__", None)
        except Exception as e:                       # noqa: BLE001
            v, e_ = None, e
            bad.append(f"{name}(import 失败:{type(e_).__name__})")
            continue
        if not isinstance(v, str) or not v.strip():
            bad.append(f"{name}(取不到 __version__)")
        else:
            out[name] = v
    if bad:
        raise SystemExit(
            "🔴 取不到 runner 版本:" + " · ".join(bad) + chr(10) +
            "   产物不绑 runner = 说不出这批读数是哪个 pytest 跑出来的,停机。")
    return out


def repo_identity(transport: str = "container") -> dict:
    """本轮的**树身份** —— 产物必须自带,不许靠外部记录。

    🔴 [Codex fof5 前置 · Review 令 2026-08-29] 实证:两个**不同的尖**
       (`d22a567f7` 与 `643bb6e7a`)跑出的 `baseline.json` **sha256 逐字节相同**
       —— 因为它顶层九个键里没有任何随尖变化的东西(sha/tip/head/commit/rev 一个没有),
       log 里也 0 次出现本轮尖。于是 17 包那份证据**单独拿出来说不出它出自哪棵树**。
       对照:27 发产物每条都绑 `tip`(门③),而 gate9 的基线产物一直没绑。
       同一条纪律,我给一半做了、另一半漏了。
    """
    import datetime as _dt

    def _rev(spec: str) -> str:
        r = _sh(["git", "rev-parse", spec])
        if r.returncode != 0:
            raise SystemExit(
                f"🔴 取不到树身份 `git rev-parse {spec}`(rc={r.returncode}):"
                f"{r.stderr.strip()[:200]}" + chr(10) +
                "   产物不绑尖 = 证据说不出自己出自哪棵树,停机。")
        return r.stdout.strip()

    image = (os.environ.get("GATE9_IMAGE_ID") or "").strip() or None
    in_container = Path("/.dockerenv").exists()
    # 🔴 [V9-B · Review 裁定 2026-08-31] 运输分裂之后,identity 必须**自己说清
    #    自己是哪一段**,并且这句话要能被反证:
    #      · 声称 host 却在容器里  ⇒ 停机(不然「宿主段」可以在容器里伪造);
    #      · 声称 container 却不在容器里 ⇒ 停机(同理,反向)。
    #    只记一个字符串而不反证,等于让产物自报身份 —— 自报的不是证据。
    if transport not in ("container", "host"):
        raise SystemExit(f"🔴 transport={transport!r} 不是已知两段之一")
    if transport == "host" and in_container:
        raise SystemExit(
            "🔴 声称跑在宿主(transport=host)却检测到 /.dockerenv —— "
            "宿主段必须真的在宿主跑,否则它证不到「原生运行面」这件事。")
    if transport == "container" and not in_container:
        raise SystemExit(
            "🔴 声称跑在容器(transport=container)却没有 /.dockerenv —— "
            "两段运输的身份不许互相冒充。")
    if image is None and in_container:
        raise SystemExit(
            "🔴 在容器里跑却没给 GATE9_IMAGE_ID —— 镜像身份只能**带外**传入"
            "(容器无法可靠自报镜像 digest),不给就等于这份证据没有环境身份。")
    return {"tip": _rev("HEAD"), "tree": _rev("HEAD^{tree}"), "image_id": image,
            "transport": transport,
            # 宿主段没有镜像 digest 可绑,环境身份改由这两样承担。
            # 两段都记(一个形状),免得「段①有段②没有」变成第二种记录格式。
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            #: 跑判据的 runner 版本(见 runner_versions 的理由)
            "runner": runner_versions(),
            "started_at": _dt.datetime.now(_dt.timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ")}


def assert_identity_recorded(payload: dict, expect_tip: str | None = None,
                             expect_identity: dict | None = None) -> None:
    """硬门:产物里必须有可核的树身份;并与**打印出去的那一份逐字段相同**。

    🔴 只比 `tip` 不够 —— 实测那个 bug 正是 tip 相同而 `started_at` 被覆盖:
       log 记 07:02:41Z、产物记 07:29:26Z,同一份证据里两个时间戳打架。
       自打毒证过:只比 tip 时,「写盘前把 started_at 改成此刻」那一形**存活**。
    """
    ident = (payload or {}).get("identity") or {}
    for k in ("tip", "tree"):
        if not ident.get(k):
            raise SystemExit(
                f"🔴 产物里没有 identity.{k} —— 它说不出自己出自哪棵树,"
                "这样的读数不许当证据(实测:两个不同的尖曾跑出逐字节相同的 baseline.json)")
    if expect_tip and ident["tip"] != expect_tip:
        raise SystemExit(
            f"🔴 产物 identity.tip={ident['tip'][:12]} ≠ 本轮 HEAD={expect_tip[:12]}")
    if expect_identity is not None and dict(ident) != dict(expect_identity):
        diff = sorted(k for k in set(ident) | set(expect_identity)
                      if ident.get(k) != expect_identity.get(k))
        raise SystemExit(
            f"🔴 产物里的 identity 与**打印出去的**那一份不一致,分歧字段:{diff}"
            f" —— 同一轮证据自相矛盾(产物 {[ident.get(k) for k in diff]} vs "
            f"打印 {[expect_identity.get(k) for k in diff]})")


def baseline_rc(broken, live_bad, red, skip_bad) -> int:
    """本轮最终裁决 —— **抽成纯函数好让「它到底进不进退出码」能被判据直接打**。

    🔴 [2026-08-29 · Review 毒③ 存活] 上一版这一段内联在 `_main_locked` 里,
       判据只打了 `unexpected_skips()` 这个函数**有没有牙**,没打「它的结果
       进不进最终裁决」。Review 把 `skip_bad` 从 `if` 条件里拿掉,
       209 条判据**全绿存活** —— 因为那条「结构锁」是**裸串**:
       `assert "unexpected_skips" in body`,而赋值行还在、位置也还在 `return 0` 之前。

       这正是我自己记过、且本轮还在给别人挑的病:**验标记 ≠ 验接线**。
       同一谓词函数有牙、接线没人验;而 Codex P1-NEW-1 的正中心恰恰是
       「skip 不在退出条件里」—— 我补的判据绕开了那个正中心。
    """
    return 1 if (broken or live_bad or red or skip_bad) else 0


def unexpected_skips(recs) -> list:
    """白名单之外的 skip,逐条点名。"""
    out = []
    for r in recs:
        for nid in (r.get("skipped_nodes") or ()):
            if nid not in EXPECTED_SKIP_NODEIDS:
                out.append(f"{r.get('target')} :: {nid}")
    return out


def liveness_from_logs(logs: str, targets) -> list:
    """[V6-B · B-3 / B-5] DDL 日志这一臂,抽成**纯函数**好让它自己被判据打。

    两处改:
      · 只核**本轮选中**的 minting 包(``--only`` 时不许要求没选的包也建库);
      · 一条 ``CREATE DATABASE`` 都没有 ⇒ **bad 非空**。
        「探针不可信」是更坏的消息,不是更好的 —— 它原来和"一切正常"共用返回值。
    """
    expected = {t: p for t, p in MINTING_DB.items() if t in set(targets)}
    if not expected:
        return []
    bad = []
    if not re.findall(r'CREATE DATABASE "[^"]+"', logs):
        return [f"__PROBE__::整份日志一条 CREATE DATABASE 都没有 —— 判**探针不可信**"
                f"(应有 {len(expected)} 个现建现删包留痕);"
                f"先查 log_statement 是否为 ddl、日志来源是否 {transport_log_source()}"]
    for t, prefix in sorted(expected.items()):
        if not re.findall(rf'CREATE DATABASE "{prefix}[0-9a-z_]+"', logs):
            bad.append(f"{t}::{prefix}…(本轮日志里 0 条)")
    return bad

#: census 认定「与库/传输无关」的变量 —— **每一条都要带理由**,不许当垃圾桶。
#:
#: 🔴 [V9-B] 从 set 改成 dict:理由过去写在注释里,而注释不进任何判据。
#:    「带理由」要能被机器核,否则它只是一句我对自己的承诺 ——
#:    `NO_DB_PACKAGES` 已经因为同一个毛病被钉过大小,这里一并按同一套办。
NON_DSN_ENVS: dict[str, str] = {
    "PLATFORM_DIRECT_SERVICE_USER_ID": "业务身份,不指库也不指传输",
    "DEFGEO_W3_HTTP_UNBLOCK": "真 HTTP 开关,布尔值",
    "DEFGEO_P0FIX_SESSION_DSN_INPROC": "进程内单例握手,值由 conftest 自己写",
    "P03C_SESSION_DSN_INPROC": "同上,进程内单例握手",
    "DEFGEO_P0FIX_SESSION_DB_BUILDS_INPROC": "进程内建库计数,不是连接串",
    "DEFGEO_PKGE_KEEP_DB": "调试开关:跑完保留库,布尔值",
    # 🔴 不是 DSN,是根 conftest 的**安全栓旁路开关**(库名不含 test / 含 prod 时
    #    唯一的放行口)。它不该被「落值」,而该被**反向自证从未设过** ——
    #    见 `_assert_latch_armed`。
    "ALLOW_NONTEST_DB": "安全栓旁路开关;由 _assert_latch_armed 反向自证从未设过",
}

#: 钉大小 —— 豁免名单最容易变成垃圾桶。数变了必须有人解释。
NON_DSN_ENVS_SIZE = 7


#: 🔴 **ADMIN DSN 白名单**(显式登记,不是通配)。
#:
#:    这些变量指的是"管理入口" —— 直连 ``postgres`` 去 CREATE/DROP 每 module 的
#:    临时库,判据**不在**那把库上跑。所以「库名必须含 test」那条栓对它们不适用:
#:    它是给**判据 DSN** 写的,套到 ADMIN DSN 上就是**适用域比使用域小**
#:    (2026-08-28 收编 v3a 时被自己这条栓判红,才发现)。
#:
#:    但不能就此放行 —— 换两条更贴的:
#:      ① 库名恰为 ``postgres``(不许拿一把有数据的库当管理入口);
#:      ② host:port 必须是**我自己的容器**(不许拿别人的实例当管理入口)。
ADMIN_DSN_ENVS = frozenset({
    "DEFGEO_V3A_TEST_DSN",      # A 的 V3-A 包:conftest 直连它建/删临时库
    "DEFGEO_V4A_TEST_DSN",      # A 的 V4-A 包:同形
    "DEFGEO_V5A_TEST_DSN",      # A 的 V5-A 包:同形(conftest 默认值就连 /postgres)
    "DEFGEO_GATE8_TEST_DSN",    # A 的 gate8 包:同形(实读 conftest:连 /postgres 建/删库)
})


def _assert_latch_armed() -> None:
    """反向自证:安全栓必须是**armed** 的。

    根 tests/conftest.py 的库名栓(不含 ``test`` / 含 ``prod`` 即拒跑)只有一个
    放行口 —— ``ALLOW_NONTEST_DB``。这一轮如果它被设过(哪怕是别的窗口留在
    环境里的),那道栓就是关着的,而"栓关着"与"栓开着且库名恰好合法"跑出来
    一模一样。所以这里既查环境,也查我自己的计划表没偷偷落它。
    """
    if os.environ.get("ALLOW_NONTEST_DB"):
        raise SystemExit(
            "🔴 环境里设了 ALLOW_NONTEST_DB —— 根 conftest 的库名安全栓被旁路了。"
            "这一轮的库名合法性**没有任何东西在守**,停机。")
    leaked = sorted(k for plan in ENV_PLAN.values() for k in plan
                    if k == "ALLOW_NONTEST_DB")
    if leaked:
        raise SystemExit("🔴 计划表里出现了 ALLOW_NONTEST_DB —— 不许由本脚本放行")
    # 正向:每个计划里的库名都得自己过那道栓(不靠旁路)。
    bad, n_crit, n_admin = [], 0, 0
    for t, plan in ENV_PLAN.items():
        for k, dsn in plan.items():
            name = dsn.rsplit("/", 1)[-1].lower()
            if k in ADMIN_DSN_ENVS:
                # ADMIN DSN:两条更贴的栓(见 ADMIN_DSN_ENVS 的注释)。
                n_admin += 1
                if name != "postgres":
                    bad.append(f"{t}::{k}(ADMIN)库名={name} —— 管理入口只许连 postgres")
                if not dsn.startswith(BASE + "/"):
                    bad.append(f"{t}::{k}(ADMIN)不在我的容器上:{dsn}")
                continue
            n_crit += 1
            if "prod" in name or "test" not in name:
                bad.append(f"{t}::{k}={name}")
    if bad:
        raise SystemExit(f"🔴 这些 DSN 过不了安全栓:{bad}")
    print(f"   ✅ 安全栓 armed:ALLOW_NONTEST_DB 未设;判据 DSN {n_crit} 条库名自己过栓;"
          f"ADMIN DSN {n_admin} 条恰连本容器 postgres")

# 🔴 [V9-B · Review 裁定 2026-08-31] 扩到 REDIS 与 *_PG_URL / *_PG_TEST_URL 形。
#    实测依据:全 tests/ 用本文件自己的 _env_reads 扫,连接类候选 33 个里有 7 个
#    落在旧正则视野之外,其中 **5 个是 Postgres URL** —— 正是 env_census 存在
#    理由本身的形状(读了却没落值 ⇒ 静默回落共享库 ⇒ 照样全绿)。
#    🔴 如实记:这 7 个变量当前**都在 18 包分母之外**的包里,所以本轮实测
#    「新入视野 0 · 未计划 0」。这一改是前瞻性的:等那些包进分母时闸才生效,
#    而不是它今天挡下了什么。
_DSNISH = re.compile(r"DSN|DATABASE|DB_URL|_DB$|TEST_DB|REDIS|_PG_URL$|_PG_TEST_URL$")

#: 外选终单 V2(27 发)预期红集**实测反查**到的包 → 哪一发要它。
#: 用 ``grep -rl "def <判据名>" tests/`` 得到,不是凭印象填。
_V2_EXPECT_PACKAGES: dict[str, str] = {
    "tests/defensive_geo_w2_2026_08_21":
        "MUT-EXTE3-11 · test_e4_confirm_lands_every_irreproducible_acceptance_fact",
    "tests/defensive_geo_pkgf_2026_08_23":
        "MUT-EXTE3-04 · test_retry_reservation_opens_a_new_attempt_through_the_live_chain",
    "tests/defensive_geo_w4_2026_08_22":
        "MUT-EXTE3-14 · test_progress_on_a_task_without_a_v2_plan_is_typed_not_a_zero",
    "tests/defgeo_woc_closure_2026_08_25":
        "MUT-EXTE3-06/07 · test_e2_01/02/22 · MUT-EXTE2-05 分母外注",
    # 🔴 [Review 令① 收编] 这一条是**实测**反查来的,不是预测:
    #    MUT-EXTE2-04 在 11 包里存活,决定性双臂坐实杀手判据在这个包
    #    (臂A 32 passed 证明真跑非 skip / 臂B 2 failed)。分母洞的处方
    #    是补分母 —— 补法就是把它登记在这张表里,让 derive_targets 求并。
    "tests/p03_settlement_2026_08_24":
        "MUT-EXTE2-04 · test_multi_pool_with_snapshot_settles_per_pool_in_the_frozen_order",
    "tests/p03c_org_guards_2026_08_25":
        "Review 令①点名同族(org 臂);本轮 27 发无预期红落此包,收编是为分母完整",
}


# ══════════════════════════════════════════════════════════════════════════
# ①.5 分母集:清单化 + 钉大小   [Review 令 · 2026-08-27]
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 **源仍然是机械推导**(``derive_targets`` 的四来源求并)。下面这张清单是
#: **锁**,不是源 —— 它唯一的作用是"分母悄悄变了的时候有人会知道"。
#:
#: 为什么非要它:上一轮的分母是 ``tuple(sorted(ENV_PLAN))``,一个推导值。
#: 推导值最危险的性质是**跟着输入悄悄变**,而分母少一个包
#: **不会让任何判据变红** —— 它只会把某一发从「杀」翻成「存活」,
#: 再被当成「判据洞」上报。方向完全反了:派出去的活会是
#: "写一条**已经存在**的判据"(MUT-EXTE2-04 就是这个形状)。
#:
#: 🔴 A/C 合流后**必然**踩这道锁(tests/ 会多出包)。那正是它的用处:
#:    逼一次**显式决定**「新包进不进分母」,而不是让 11 发复放沿用旧分母。
#:    改它要连着改 ``GATE9_DENOMINATOR_SIZE``,并在 commit 里写清楚**为什么**。
GATE9_DENOMINATOR: tuple[str, ...] = (
    "tests/defensive_geo_2026_08_21",
    "tests/defensive_geo_pkge_2026_08_24",
    "tests/defensive_geo_pkgf_2026_08_23",
    "tests/defensive_geo_w2_2026_08_21",
    "tests/defensive_geo_w3_2026_08_21",
    "tests/defensive_geo_w4_2026_08_22",
    "tests/defgeo_e3_2026_08_26",
    "tests/defgeo_funding_p0_2026_08_25",
    "tests/defgeo_wob_publish_funding_2026_08_25",
    "tests/defgeo_woc_closure_2026_08_25",
    # 🔴 [2026-08-28] A 的 V3-A 判据包,Review 令显式收编(台账同上)。
    "tests/defgeo_v3a_2026_08_28",
    # 🔴 [08-28] A 的 V4-A 判据包,Review 令显式收编(台账同上)。
    "tests/defgeo_v4a_2026_08_28",
    # 🔴 [08-29] A 的 V5-A 判据包(79 条,含 4 个 PG 文件与 x1 幻列判据),
    #    Review 补令显式收编 —— 合流尖带进了这个包而分母还停在 15。
    "tests/defgeo_v5a_2026_08_28",
    # 🔴 [08-29] A 的 V6-A 判据包(15 条,纯静态无库),Review 裁定收编。
    "tests/defgeo_v5b_2026_08_29",
    # 🔴 [Review 令① · 2026-08-27] 收编:分母洞 MUT-EXTE2-04 的处方是补分母。
    "tests/p03_settlement_2026_08_24",
    "tests/p03c_org_guards_2026_08_25",
    "tests/xiaobang_execute_2026_08_20",
    # 🔴 [V7-C · 2026-08-30] gate8 合流带进 tests/defgeo_gate8_2026_08_30(4 条,真碰库)。
    #    收编理由:分母外的包**杀不了任何一发** —— 只有它能抓住的变异会读成「存活」。
    #    这正是当初收编 p03_settlement 的处方(MUT-EXTE2-04 那个分母洞)。
    "tests/defgeo_gate8_2026_08_30",
    "tests/defgeo_migration_replay_2026_09_01",
    "tests/diag_brand_attribution_2026_08_31",
    "tests/redis_degrade_2026_09_02",
    "tests/defgeo_flat_pricing_2026_09_02",
    "tests/plan_identity_copy_2026_09_02",
)

#: 钉大小 —— 与清单分开写,防"顺手加一行"式的静默扩容/缩容。
#: 🔴 11 -> 13(2026-08-27 Review 令①收编 p03_settlement / p03c)。
#: 🔴 13 -> 14(2026-08-28 A V3-A 验收合流,显式收编 tests/defgeo_v3a_2026_08_28)。
#: 🔴 14 -> 15(08-28 A V4-A 合流,显式收编 tests/defgeo_v4a_2026_08_28)。
#: 🔴 15 -> 16(08-29 A V5-A 合流,Review 补令收编 tests/defgeo_v5a_2026_08_28)。
#: 🔴 16 -> 17(08-29 最终合流带进 tests/defgeo_v5b_2026_08_29,Review 裁定收编;
#:    它是第一个**纯静态无库**的包,见 NO_DB_PACKAGES)。
#: 🔴 17 -> 18(08-30 gate8 合流带进 tests/defgeo_gate8_2026_08_30,Review 裁定收编)。
#: 🔴 18 -> 19(09-01 P0 迁移重放床,Review 令收编 —— 「首次部署过、还原副本上重放必炸」
#:    这一格此前不在任何分母里:18 包 / 门八 / idxguard 都没模拟「在还原副本上重放」)。
#: 🔴 19 -> 21(09-02 第二趟车带进 diag_brand_attribution 与 redis_degrade,
#:    Review 裁定收编 —— 上一趟它们在盘上但不在这条链上,故记域外待裁)。
#: 🔴 21 -> 23(09-02 第四趟合车带进 defgeo_flat_pricing / plan_identity_copy,
#:    Review 裁定收编;两者都是独立判据包,不长任何一发的预期红)。
GATE9_DENOMINATOR_SIZE = 23


#: 🔴 [V9-B · Review 裁定 2026-08-31] **运输分裂**:分母里有包不在容器里跑。
#:
#: 由来(实测,不是设计选择):`tests/defgeo_gate8_2026_08_30` 在 fof7 尖上只有
#: 4 条无 docker 依赖的判据、容器内全绿;A 的 `8a6b204a2` 给既有判据加上了
#: **真 Redis** 夹具(共享守卫 `tests/_shared/progress_redis_guard.py`)。
#: 那组守卫要两样东西:① 目标必须是 **loopback** 发布端口;② 用 `docker inspect`
#: 核一次性容器的标签与端口映射。而本跑批是「容器内 + `--internal` 网络」——
#: 容器里没有 docker 二进制,兄弟容器对测试进程也不是 loopback。
#: 实测(冻在 `findings/gate8_container_transport_incompatible.txt`):
#: 容器内 29 collected / 15 passed / **14 error**(实测于尖 fb1f82ee9;
#: 之后 A 又加了判据文件,数会涨 —— 这里记的是**那一次测量**,不是当前值。
#: 实测数不绑尖 = 散文版的「产物不绑尖」,下一个人会拿它当现值去核然后困惑),签名逐字
#: `FileNotFoundError: [Errno 2] No such file or directory: 'docker'`。
#:
#: 🔴 处置**不是** `--ignore` 掉那两个文件:被 ignore 的恰是本班最新生产修复的判据,
#: 发车证据在最终尖上就没人跑它们。「可见地缩」补不了这个洞。
#: 裁定 = **整包换一条运输**(宿主),不拆文件 —— 同包拆两种运输 = 同包两个真相。
TRANSPORT: dict[str, str] = {
    "tests/defgeo_gate8_2026_08_30":
        "host · 守卫要 loopback 发布端口 + docker inspect 核一次性容器标签;"
        "容器内必然 14 error(读数冻在 findings/)。整包移出,不拆文件。"
        "段② 用本脚本 `--host-segment` 在宿主跑(同一套谓词,两段运输),"
        "identity 记 transport/python/platform 并反证不在容器里。",
}

#: 钉大小 —— 运输标注是最容易被静默扩用的东西:
#: 「这个包在我这跑不绿」永远有下一个候选,而每多标一个,容器段的分母就少一个。
TRANSPORT_HOST_SIZE = 1


def host_targets(transport: dict | None = None) -> tuple[str, ...]:
    """标了 `host` 的包 —— 容器段**不跑**它们,由宿主段覆盖。"""
    t = TRANSPORT if transport is None else transport
    return tuple(sorted(k for k, v in t.items() if str(v).startswith("host")))


def container_targets(denominator=None, transport: dict | None = None) -> tuple[str, ...]:
    """容器段真正要跑的包 = 冻结分母 − 宿主运输的包。

    🔴 返回值**不是**新的分母:分母仍是 18(两段合起来要覆盖满),
    这里只回答「这一段跑哪些」。两者混成一个名字,就会有人拿 17 当分母去比。
    """
    d = tuple(GATE9_DENOMINATOR) if denominator is None else tuple(denominator)
    host = set(host_targets(transport))
    return tuple(t for t in d if t not in host)

#: ``tests/`` 下目录全集(去掉 ``__pycache__``)的**数目 + 指纹**。
#: 钉它不是为了限制别人加包,是为了让"仓里多了/少了一个测试包"这件事
#: **必须被一个人看见** —— 新包极可能正是某一发预期红集的家。
#: 🔴 106 -> 107(08-29 合流带进 tests/defgeo_v5a_2026_08_28)。这条锁不是错误,
#:    是**要求做一次决定**:新包是不是某一发预期红集的家?进分母还是记域外?
#:    决定 = 进分母(见 REVIEW_MANDATED_PACKAGES 的台账出处),同批更新这两个值。
#: 🔴 107 -> 108(08-29 最终合流带进 tests/defgeo_v5b_2026_08_29)。这一次这道锁
#:    **按设计拦停了整轮**(三条臂全停、零产物),我把它当「要求做一次决定」处理。
#: 🔴 108 -> 109(08-30 gate8 合流带进 tests/defgeo_gate8_2026_08_30)。这道锁又一次
#:    **按设计要求做一次决定**。反向对照已做:把 gate8 从名单里去掉重算,
#:    指纹逐字回到旧值 afad4d88… ⇒ 差集**恰是这一个包**,没有别的悄悄变化。
#: 🔴 109 -> 110(08-31 合入 A 的 29673664f,带进 `tests/_shared/`)。
#:    决定 = **记域外,不进分母**。依据是机械查的,不是印象:
#:      · `tests/_shared/` 里 **0 个 test_ 文件**(只有 progress_redis_guard.py 一个共享 helper)
#:      · 它不是任何一发预期红集的家 —— 而这道锁的设问就是「新包是不是某一发预期红的家」
#:    反向对照已做:把 `_shared` 从名单去掉重算,指纹**逐字回到** 4c0766fa…
#:    ⇒ 差集恰是这一个目录,没有别的悄悄变化。
#:    ⚠️ 顺带纠一次我自己的读数:`ls -d tests/*/` 数出来是 111,因为它把
#:    `__pycache__` 算进去了;**以本函数的口径(iterdir 且排除 __pycache__)为准**。
#: 🔴 110 -> 111(2026-09-01 P0 迁移重放班):唯一新增 tests/defgeo_migration_replay_2026_09_01,
#:    Review 令收编进分母(18 -> 19)。反查过:去掉这一个名字,指纹**逐字回到** 110983c6…,
#:    ⇒ 差集恰是它,没有别的悄悄变化。
#:    ⚠️ 切到本车分支时踩到一格,记下来:普查按 `p.is_dir()` 计数,而 `git checkout`
#:    **删不掉带未跟踪文件的目录**。别的链上的包被切走后,只要留下 `<pkg>/__pycache__`,
#:    那个名字就仍在普查里 —— 本轮 111 里就一度有一个是这样的**空壳**(我自己那个包,
#:    cherry-pick 之后才变成真包)。⇒ 数对了不等于树对了。
#:    处置已记 post-train:普查改按「目录内含 >=1 个被 git 跟踪的 .py」计。
#: 🔴 111 -> 113(09-02 第二趟车):新增 diag_brand_attribution / redis_degrade 两包,
#:    两包**同批进分母**(19 -> 21),没有域外项。反查过:去掉这两个名字,
#:    指纹逐字回到 bdf2f8dd…(= 上一趟车的值),差集恰是这两个。
#:    起跑前核过三个包都有**真跟踪的 .py**(3 / 2 / 4),不是 __pycache__ 撑的空壳。
#: 🔴 113 -> 115(09-02 第四趟合车)。反查过:去掉 defgeo_flat_pricing / plan_identity_copy
#:    两个名字,数目与指纹**逐字回到** 113 / 6fa897bea228ac30 ⇒ 差集恰是这两个。
#:    两包都有真跟踪的 .py(1 / 2 个),不是 __pycache__ 撑的空壳。
#: 🔴 119 -> 121(09-04 第二轮 · rebase 到 C 的 238beeb11 之后)。反查过:去掉这两个名字,
#:    数目与指纹**逐字回到** 119 / a1d15d1a76bda5a0 ⇒ 差集恰是这两个。归属:
#:      · `defgeo_starlette_httpexc_2026_09_04` ← C **#30** `972d31027`
#:      · `defgeo_idempotency_key_2026_09_04`   ← C **#31** `238beeb11`
#:    Review 签完 C 那两班**之后**才跑签前脚本,`ea_12` 读到 121 vs 119 ——
#:    **今日第三次同病**(加包时没人做「登记」这个决定)。
#:    这正是 #48 那条报文改进(盘上多了 / 少了 + 点名)派上用场的场合:
#:    它直接说出是哪两个包,不用谁去重跑一遍普查再对。
#:
#: 🔴 115 -> 119(09-04 收编四个包)。反查过:去掉这四个名字,数目与指纹**逐字回到**
#:    115 / b3d3a1d8589aa1c8 ⇒ 差集恰是这四个,没有别的包被同时加删。归属:
#:      · `plan_error_upstream_2026_09_03`   ← `ece4913b9` C14 第七班(题单混侧黄框)
#:      · `report_metrics_quote_keys_2026_09_03` ← C14 **第八班** `9f7ced35e`(rebase 后并入)
#:      · `preview_pricing_rule_2026_09_02`  ← `e0692ea66` C14(题单响应带计价规则)
#:      · `wob39_collection_scope_2026_09_03`← `858f61912` 窗口B #39(本仪器层改动)
#:    前两个是**别人的交付物**,由窗口B 代登记(Review 2026-09-04 裁定:登记是仪器层
#:    动作,不是交付物变更)。⚠️ 这个计数在 #39 动手前**已经欠了 2** —— 第七班加包时
#:    没跑 `assert_denominator_frozen`,所以是「欠 2」变「欠 3」,不是我一个人造成的。
#: 🔴 [2026-09-04 · #48] **显式名单是 SSOT**,COUNT / SHA256 由它派生。
#:
#:    改这张表的**真理由**(立卡时我写的「两个常量会互相漂开」是错的 —— 核过:
#:    只改 COUNT 不改 SHA256,hash 断言会红,不会静默漂):
#:      ① hash 说不出「差的是哪个」。失败只能报「指纹 4f8aee70 != 钉的 a1d15d1a」,
#:         而有名单就能报「多了 ['pkg_new'] / 少了 []」。
#:         `_census_diag` 只补上了**未跟踪**那一类;一个正经的、被 git 跟踪的包
#:         被加或被删,它一个字也说不出 —— 而那最常发生。
#:      ② **让登记提交可核**。改之前一次登记的 diff 是两个数字 + 两串哈希,
#:         复核的人看不出加的是哪个包,只能自己跑一遍普查再对。2026-09-04 的
#:         `61fee9119` 就是这样:Review 只能靠 commit 注释里我写的归属去核,
#:         而**注释是散文,不是机器可核的东西**。现在 diff 自己说出答案:
#:             + "report_metrics_quote_keys_2026_09_03",
#:
#:    🔴 **登记语义:加一个判据包 = 两处显式决定** —— 建目录 + 在本表加一行。
#:       漏掉后一处,`test_ea_12` 必须红且报文点名。这是**故意的**:
#:       登记本来就该是一次决定,不是「跑个脚本把数字改了」。
#:       2026-09-04 那笔欠 3 个包的账,正是因为加包时没人做这个决定。
#:
#:    顺序必须 sorted、不许重复 —— 指纹是 sha256(chr(10).join(本表))。
#:    本表**只管目录**;`tests/` 顶层那些散文件是另一个轴,不进这里(Review 2026-09-04 定)。
DISK_PACKAGES: tuple[str, ...] = (
    "_shared",
    "admin_user_governance",
    "advisor_ownership_2026_08_04",
    "agent_loop",
    "ai_ops",
    "ai_surface_monitoring",
    "api",
    "article_self_report_2026_08_19",
    "bh75_admin_silent_failures_2026_09_05",
    "brand_test_visibility_2026_09_05",
    "clean_db_bootstrap_2026_09_08",
    "closeout_2026_07_30",
    "custfb_2026_08_09",
    "db_bootstrap_r4",
    "db_bootstrap_r5",
    "dealer_inventory_resale",
    "defensive_geo_2026_08_21",
    "defensive_geo_pkge_2026_08_24",
    "defensive_geo_pkgf_2026_08_23",
    "defensive_geo_pkgh_2026_08_23",
    "defensive_geo_w2_2026_08_21",
    "defensive_geo_w3_2026_08_21",
    "defensive_geo_w4_2026_08_22",
    "defgeo_dispatch_terminal_2026_09_08",
    "defgeo_e3_2026_08_26",
    "defgeo_flat_pricing_2026_09_02",
    "defgeo_funding_p0_2026_08_25",
    "defgeo_gate8_2026_08_30",
    "defgeo_idempotency_key_2026_09_04",
    "defgeo_migration_replay_2026_09_01",
    "defgeo_starlette_httpexc_2026_09_04",
    "defgeo_v3a_2026_08_28",
    "defgeo_v4a_2026_08_28",
    "defgeo_v5a_2026_08_28",
    "defgeo_v5b_2026_08_29",
    "defgeo_wob_publish_funding_2026_08_25",
    "defgeo_woc_closure_2026_08_25",
    "delist_sku_2026_08_10",
    "demo_snapshot_serialization_2026_09_07",
    "deploy",
    "diag_brand_attribution_2026_08_31",
    "engine_search_fidelity",
    "fixtures",
    "flywheel_binding_liveness_2026_08_15",
    "flywheel_close_loop",
    "flywheel_integration",
    "frontend_privacy_final",
    "gap_plan_2026_08_08",
    "gap_plan_migration_2026_08_08",
    "generator_input_parity_2026_09_07",
    "geo_douyin_publish_converge_2026_09_08",
    "geo_image_note_2026_08_17",
    "geo_monitoring_content_loop",
    "geo_observation",
    "geo_observation_analytics",
    "geo_observation_integration",
    "governance_header_latin1_2026_08_06",
    "hotfix_delivery_plan_2026_08_20",
    "hotfix_to_thread_2026_08_20",
    "integration",
    "inventory_distribution_chain_2026_08_12",
    "inventory_pricing_admin",
    "inventory_relation_model_2026_08_13",
    "kyb_catalog_governance_2026_08_10",
    "kyb_d0a_2026_08_04",
    "kyb_d0b_2026_08_05",
    "kyb_d1_2026_08_04",
    "kyb_region_remark_2026_08_14",
    "legal",
    "lineage_report_timeout_2026_08_16",
    "login_gate_pg",
    "manual_keyword_parity_2026_08_16",
    "marketing_content_center",
    "mcidor_2026_08_10",
    "mhz_payload_shape_2026_08_04",
    "mobile_pay_resume_2026_09_10",
    "monitoring_optin_2026_08_15",
    "native_dialog_sweep_2026_08_06",
    "order_items_idor_2026_08_04",
    "order_remark_p0_2026_08_10",
    "organization_internal_seats",
    "orphanmon_2026_08_10",
    "p03_settlement_2026_08_24",
    "p03c_org_guards_2026_08_25",
    "performance",
    "plan_error_upstream_2026_09_03",
    "plan_identity_copy_2026_09_02",
    "platform_covered_2026_08_16",
    "platform_domain_match_2026_08_15",
    "platform_domain_rawurl_2026_08_16",
    "platform_domain_stem_2026_08_16",
    "portal_notify_2026_07_29",
    "post_quote_binding_2026_09_08",
    "prestart",
    "prestart_lock_timeout_2026_08_16",
    "preview_pricing_rule_2026_09_02",
    "pricing_quote_wiring",
    "pricing_ssot",
    "production_quote_2026_09_08",
    "pubfilter_industry_2026_08_08",
    "publish_center_p1",
    "publish_center_scope_2026_07_30",
    "publish_dispatch_industry_2026_08_17",
    "publish_zombie_2026_08_04",
    "pubrec_packages_2026_08_09",
    "question_origin_2026_09_08",
    "question_origin_report_2026_09_08",
    "quote_media_mix_2026_08_12",
    "quotegeo_2026_08_10",
    "redis_degrade_2026_09_02",
    "refusal_observability_2026_09_07",
    "registration_origin_2026_08_06",
    "regression",
    "report_metrics_quote_keys_2026_09_03",
    "research_monitor",
    "response_model_contract",
    "rollback_035_2026_08_20",
    "security",
    "settlement_adjudicator_2026_08_25",
    "settlement_manual_ux_2026_08_25",
    "settlement_notifications",
    "suggest_questions_framing_2026_09_05",
    "svideo_gate_2026_07_30",
    "svideo_relay_2026_08_01",
    "svideo_tails_2026_08_02",
    "system_kb",
    "v35_refund_single_ledger_2026_08_17",
    "v35_unspent_fifo_2026_08_17",
    "v3_3_1",
    "vocab_convergence_2026_08_09",
    "wob39_collection_scope_2026_09_03",
    "xiaobang_execute_2026_08_20",
    "xiaobang_solution_first_2026_08_21",
    "xiaobang_unified_2026_08_10",
    "xiaobang_vnext_2026_08_18",
    "xunhupay_query_guard_2026_09_10",
)
DISK_PACKAGES_COUNT = len(DISK_PACKAGES)
DISK_PACKAGES_SHA256 = hashlib.sha256(
    chr(10).join(DISK_PACKAGES).encode("utf-8")).hexdigest()


def assert_denominator_frozen(targets=None, *, plan=None, expect=None,
                              inplace=None, minting=None, nodb=None, disk_root=None,
                              frozen=None, size=None,
                              disk_count=None, disk_sha=None) -> None:
    """五道核对,任一不过 ⇒ 停机。参数全可注入 —— 正样本自证要用。"""
    frozen = tuple(GATE9_DENOMINATOR) if frozen is None else tuple(frozen)
    size = GATE9_DENOMINATOR_SIZE if size is None else size
    plan = ENV_PLAN if plan is None else plan
    expect = _V2_EXPECT_PACKAGES if expect is None else expect
    inplace = INPLACE_DB if inplace is None else inplace
    minting = MINTING_DB if minting is None else minting
    disk_root = (ROOT / "tests") if disk_root is None else Path(disk_root)
    disk_count = DISK_PACKAGES_COUNT if disk_count is None else disk_count
    disk_sha = DISK_PACKAGES_SHA256 if disk_sha is None else disk_sha

    # A 钉大小 + 无重复(重复会让 len 对上而集合小一个)
    if len(frozen) != size:
        raise SystemExit(
            f"🔴 分母清单 {len(frozen)} 项 ≠ 钉死的 {size} 项 —— "
            "分母变了。要么改错了,要么该改锁但没改。停机。")
    if len(set(frozen)) != len(frozen):
        dup = sorted({x for x in frozen if list(frozen).count(x) > 1})
        raise SystemExit(f"🔴 分母清单有重复项:{dup} —— 大小对得上但集合小一个")

    # B 清单 == ENV_PLAN 的键(没有 env 计划的包 = 跑在别人的库上)
    a, b = set(frozen), set(plan)
    if a != b:
        raise SystemExit(
            f"🔴 分母清单与 ENV_PLAN 不一致:清单多 {sorted(a - b)} · "
            f"ENV_PLAN 多 {sorted(b - a)}")

    # B2 清单 == **本轮真推导出来的** targets(四来源求并的结果)
    if targets is not None:
        c = set(targets)
        if a != c:
            raise SystemExit(
                f"🔴 分母清单与本轮机械推导不一致:清单多 {sorted(a - c)} · "
                f"推导多 {sorted(c - a)} —— 推导才是源,先想清楚是谁变了")

    # C 预期红集反查到的包必须都在分母里(执行令①那条,机械化)
    miss = sorted(set(expect) - a)
    if miss:
        raise SystemExit(
            f"🔴 这些包是某一发**预期红集**的家,却不在分母里:{miss} —— "
            "缺包会把那一发**误记存活**")

    # D 每个包都要有活性归属:要么在盘数表,要么靠 DDL 日志。
    #    两边都不挂 ⇒ "它真的跑在我的库上"这句话没有任何东西在证。
    # 🔴 [V6-B-final] 第三类:纯静态无库包显式豁免,但**互斥 + 并集**必须成立,
    #    否则它就是垃圾桶 —— 把一个真碰库的包丢进去就能让活性臂闭嘴。
    nodb_map = NO_DB_PACKAGES if nodb is None else nodb
    nodb = set(nodb_map)
    if len(nodb_map) != NO_DB_PACKAGES_SIZE:
        raise SystemExit(
            f"🔴 NO_DB_PACKAGES 大小 {len(nodb_map)} ≠ 钉死的 "
            f"{NO_DB_PACKAGES_SIZE} —— 新增「不碰库」必须过一次人")
    if any(not why.strip() for why in nodb_map.values()):
        raise SystemExit("🔴 NO_DB_PACKAGES 有条目没写理由")
    overlap = ((nodb & set(inplace)) | (nodb & set(minting))
               | (set(inplace) & set(minting)))
    if overlap:
        raise SystemExit(
            f"🔴 三类活性归属出现重叠:{sorted(overlap)} —— "
            "一个包只能属于一类,重叠等于两处各说各话")
    union = set(inplace) | set(minting) | nodb
    if union != a:
        raise SystemExit(
            f"🔴 三类并集 ≠ 分母:分母多出 {sorted(a - union)} · "
            f"三类多出 {sorted(union - a)}")
    no_anchor = sorted(a - set(inplace) - set(minting) - nodb)
    if no_anchor:
        raise SystemExit(
            f"🔴 这些包没有活性归属(INPLACE_DB / MINTING_DB / NO_DB_PACKAGES 都不在):"
            f"{no_anchor} —— 它们回落共享库也不会被发现")

    # E 仓里 tests/ 全集的数目 + 指纹
    # 🔴 [#41] 口径改为派生:见 `disk_package_names()`。
    #    原写法 `iterdir() 且 name != "__pycache__"` 是**手写忽略名单**,
    #    冒出 `.pytest_cache` / `.ruff_cache` 之类就漏。
    names = disk_package_names(disk_root)
    now = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    if len(names) != disk_count or now != disk_sha:
        # 🔴 [#48] 报**差集**,不只报指纹。指纹说得出「不一样」,说不出「差的是哪个」——
        #    而「哪个」正是读的人下一步要做的决定所需要的唯一信息。
        extra = sorted(set(names) - set(DISK_PACKAGES))
        gone = sorted(set(DISK_PACKAGES) - set(names))
        raise SystemExit(
            f"🔴 tests/ 包集变了:盘上 {len(names)} 个(名单里 {disk_count})。\n"
            f"   盘上多了 {len(extra)} 个:{extra}\n"
            f"   盘上少了 {len(gone)} 个:{gone}\n"
            f"   (指纹 {now[:16]} vs 名单 {disk_sha[:16]})\n"
            "   这是**要求做一次决定**,不是错误:新包是不是某一发预期红集的家?"
            "进分母还是记域外?决定后在 DISK_PACKAGES 里加/删对应的行 ——"
            "COUNT 与 SHA256 由它派生,不用也不许手改。")

    print(f"   ✅ 分母锁:清单 {len(frozen)} 项 == ENV_PLAN == 本轮推导;"
          f"预期红集全在分母内;每包都有活性归属;tests/ 包集指纹未变")


#: 🔴 正样本⑤专用的"永远不可能在分母里"的包名。
#:    第一版这里写的是 tests/p03_settlement_2026_08_24 —— 它 2026-08-27 被收编进
#:    分母,于是那条正样本**当场恒真**、锁变装饰。是自证自己抓出来的
#:    (锚点过期 ⇒ 判据恒真,本仓老病)。改成合成名,并在用之前反证它真不在分母里。
_SELFTEST_ALIEN_PKG = "tests/__selftest_never_in_denominator__"


def disk_package_names(disk_root=None) -> list[str]:
    """`tests/` 下**算作判据包**的目录名 —— 单点实现,三个调用点共用。

    🔴 [2026-09-04 · #41] 计入条件 = 目录里存在**任何一个未被 git 忽略的文件**
       (tracked,或 untracked-but-not-ignored)。**派生的,不是手写忽略名单**。

    改口径前做过状态矩阵,四格都验过(种真目录实测,不是推理):

      A 未跟踪 + 有内容(毒 / 别的窗口的残留)   → **计入**
      B 只剩 `__pycache__`(缓存空壳)          → 不计
      C 空目录                                  → 不计
      D 正常判据包(有 tracked 文件)            → 计入

    🔴 **A 那格是这条规则的命门。** 原提案是「只数含 tracked 文件的目录」,
       四格里 A/B/C 一视同仁排除 —— 听起来干净,代价是**对未跟踪的污染彻底变盲**。
       2026-09-04 上午 `test_ea_12` 抓住另一个窗口种在本树里的
       `tests/rv_poison_pkg_2026_09_04`,靠的正是「未跟踪目录也计数」。
       按原提案改完,同样的毒下下去**计数纹丝不动,一声不吭** ——
       而这种降级**不会让任何判据变红**,所以没人会发现。

       A 与 B 的分界不在「跟没跟踪」,在**内容有没有被 ignore**:
       A 的内容未跟踪但**没被** ignore(是真东西),B 的内容**被** ignore(是缓存)。

    只跑两次 git(不是每个目录问一次),再与盘上真实目录名取交 ——
    `tests/` 顶层还有几百个散文件,它们的名字也会出现在路径第二段里。
    """
    disk_root = (ROOT / "tests") if disk_root is None else Path(disk_root)
    on_disk = {p.name for p in disk_root.iterdir() if p.is_dir()}

    def _seg1(args: list[str]) -> set:
        r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True)
        if r.returncode != 0:
            raise SystemExit(
                "🔴 disk_package_names: git " + " ".join(args) + " 退出 "
                + str(r.returncode) + " —— 普查依赖 git,拿不到就**停机**,"
                "不许静默返回空集合(空集合会让分母读成 0,而 0 与「目录都没了」同形)")
        out = r.stdout.decode("utf-8", "surrogateescape")
        return {l.split("/")[1] for l in out.splitlines() if l.count("/") >= 1}

    live = _seg1(["ls-files", "--", str(disk_root.name)])
    live |= _seg1(["ls-files", "--others", "--exclude-standard", "--", str(disk_root.name)])
    return sorted(on_disk & live)


def _disk_digest_with_extra(extra: str, disk_root=None) -> tuple[int, str]:
    """真名单 **多一个包** 之后的 (数目, 指纹) —— 只给正样本用,不参与裁定。"""
    disk_root = (ROOT / "tests") if disk_root is None else Path(disk_root)
    names = sorted(disk_package_names(disk_root) + [extra])
    joined = chr(10).join(names)
    return len(names), hashlib.sha256(joined.encode("utf-8")).hexdigest()


def selftest_denominator_lock() -> None:
    """🔴 正样本:七种破坏方式,每一种都必须让锁**真的报错**。

    锁不带正样本就只是一句"我检查过了" —— 没有任何东西在证明它会响。
    全在内存里做,不碰盘、不碰库,所以每轮都跑得起。
    """
    D = GATE9_DENOMINATOR
    assert _SELFTEST_ALIEN_PKG not in D, (
        "🔴 正样本⑤的合成包名进了分母 —— 换一个,别让样本恒真")
    cases = [
        ("① 分母少一个包", dict(frozen=D[:-1])),
        ("② 清单有重复(大小却对得上)", dict(frozen=D[:-1] + (D[0],))),
        ("③ ENV_PLAN 少一个包", dict(plan={k: v for k, v in list(ENV_PLAN.items())[1:]})),
        ("④ 本轮推导少一个包", dict(targets=list(D)[1:])),
        ("⑤ 预期红集指向分母外的包",
         dict(expect={**_V2_EXPECT_PACKAGES,
                      _SELFTEST_ALIEN_PKG: "自证正样本"})),
        ("⑥ 有包没有活性归属", dict(inplace={}, minting={})),
        # ⑦ 就是 A/C 合流那一幕:tests/ 多出一个包。这里从**真名单**算出
        #    "多一个包之后"的数目/指纹当钉值传进去 —— 谓词与真实场景
        #    逐字同一个(now != disk_sha),不是拿别的目录当替身。
        ("⑦ A/C 合流带进一个新包(tests/ 真多一个目录)",
         dict(disk_count=_disk_digest_with_extra("zz_ac_merge_new_pkg")[0],
              disk_sha=_disk_digest_with_extra("zz_ac_merge_new_pkg")[1])),
        ("⑧ 换一棵完全不同的目录树(兜底)", dict(disk_root=ROOT / "scripts")),
        # ⑨⑩ [V6-B-final] 第三类「纯静态无库」极易变成垃圾桶 —— 两向各钉一发。
        #    ⑨ 往里塞一个**真碰库**的包(它同时在 MINTING_DB 里)⇒ 重叠必红。
        #      大小仍是 1,所以它证的是**重叠闸**而不是大小闸。
        ("⑨ 把一个真碰库的包塞进 NO_DB_PACKAGES(重叠)",
         dict(nodb={"tests/defgeo_v3a_2026_08_28": "假理由:它其实每 module 现建库"})),
        #    ⑩ 反向:第三类空了 ⇒ 纯静态包没有归属 ⇒ 并集 ≠ 分母 ⇒ 必红。
        ("⑩ 第三类被清空(并集 ≠ 分母)", dict(nodb={})),
    ]
    for name, kw in cases:
        try:
            assert_denominator_frozen(**kw)
        except SystemExit:
            continue
        raise SystemExit(
            f"🔴 分母锁自证失败:破坏方式「{name}」没有让锁报错 —— "
            "这道锁是**装饰**,不是锁。停机。")
    print(f"   ✅ 分母锁正样本 {len(cases)}/{len(cases)}:{len(cases)} 种破坏方式全部被拦下")


#: 🔴 第五来源:**Review 裁定显式收编**的包(每条必带台账出处)。
#:
#:    前四个来源都是"从证据推出来的"(七包 / e3 SUITES / 合流 diff / 预期红反查)。
#:    但有一类包是**裁定进来的** —— 它不长任何一发的预期红,进分母是为完整性。
#:    这种包没有第五来源就只能硬塞进冻结清单,而那会让「清单 == 推导」当场不成立
#:    (2026-08-28 落 v3a 时我就是这么被自己的锁拦下的:**清单是锁,不是源**)。
REVIEW_MANDATED_PACKAGES: dict[str, str] = {
    "tests/defgeo_flat_pricing_2026_09_02":
        "台账「2026-09-02 第四趟合车」· Review 令收编(21 -> 23):第三趟计价判据包,前趟在 bd5fc709d 上有 14 条红臂;不长任何一发的预期红",
    "tests/plan_identity_copy_2026_09_02":
        "台账「2026-09-02 第四趟合车」· Review 令收编(21 -> 23):第五趟提示判据包,前趟在 bd5fc709d 上有 18 条红臂;不长任何一发的预期红",
    "tests/diag_brand_attribution_2026_08_31":
        "台账「2026-09-02 第二趟车 936 归属 + Redis 熔断」· Review 令收编(19 -> 21):"
        "11 条真 PG 判据;上一趟不在链上故记域外待裁,本趟随其三笔进来",
    "tests/redis_degrade_2026_09_02":
        "台账「2026-09-02 第二趟车」· Review 令收编(19 -> 21):"
        "5 条纯逻辑判据(Redis 熔断),零基础设施",
    "tests/defgeo_migration_replay_2026_09_01":
        "台账「2026-09-01 P0 迁移 readiness 渲染漂移」· Review 令收编(18 -> 19):"
        "两张重放床 + oid 终态信号 + 渲染文本静态锁 10 条。它不长任何一发的预期红,"
        "收编是因为它覆盖的那一格(还原副本上重放)此前**零覆盖**",
    "tests/defgeo_v3a_2026_08_28":
        "台账「2026-08-28 A V3-A 验收合流」· Review 令显式收编"
        "(4 判据文件 + 自带 conftest;不长预期红,收编为分母完整)",
    "tests/defgeo_v4a_2026_08_28":
        "台账「08-28 A V4-A 合流」· Review 令显式收编"
        "(16 条判据 + 自带 conftest;不长预期红,收编为分母完整)",
    "tests/defgeo_v5b_2026_08_29":
        "台账 REVIEW_CTO_STATE_2026-08-16.md(2026-08-29 节):「Review 裁定收编 "
        "tests/defgeo_v5b_2026_08_29(15 条,纯静态无库)进冻结分母 16→17:依据 = "
        "本台账『V6-A(A 称 V5-B)交付 … @ 466d87bc7 … Review 亲核:66 + 52 绿复现;"
        "毒接 request_budget_approval→/wallet → test_05 恰 1 红』及"
        "『A ⑤ 收口 45e958de6 … test_whitespace_attr_scope.py 5 条』」",
    "tests/defgeo_v5a_2026_08_28":
        "台账「V5-A 最终交付 … Review 亲核 79/79」· Review 补令(2026-08-29)显式收编"
        "(79 条判据,含 4 个 PG 文件与 x1 幻列判据 + 自带 conftest;"
        "不长本轮预期红,收编为分母完整 —— 合流尖带进了它而分母还停在 15)",
    "tests/defgeo_gate8_2026_08_30":
        "台账「门八 PASS @ 99ed9d03d」· Review 令 2026-08-30 显式收编(17→18):"
        "「收编 18 包、MINTING_DB 前缀 defgeo_gate8_、ADMIN_DSN_VARS 加项、108→109,"
        "配三类互斥+并集==分母+钉大小正样本判据」。"
        "包本身 = 4 条真碰库判据(付完钱到执行器领走之间的 queued 快照),"
        "自带 conftest 与专属 ADMIN DSN;不长本轮预期红,收编理由是"
        "**分母外的包杀不了任何一发** —— 只有它能抓住的变异会读成「存活」",
}


def _sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


# ══════════════════════════════════════════════════════════════════════════
# ① 分母:三来源机械求并
# ══════════════════════════════════════════════════════════════════════════
def derive_targets() -> tuple[list[str], dict[str, list[str]]]:
    prov: dict[str, list[str]] = {}

    import importlib.util

    def _load(name: str, rel: str):
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    ext = _load("_g9_extsel", "scripts/mutation_runner_extsel_2026_08_26.py")
    for target, _dsn, _extra in ext.FULL_DENOMINATOR:
        prov.setdefault(target, []).append("七包(extsel FULL_DENOMINATOR)")

    e3 = _load("_g9_e3", "scripts/mutation_runner_e3_2026_08_26.py")
    for _suite, (target, _dsn) in e3.SUITES.items():
        prov.setdefault(target, []).append("E3 runner SUITES")

    # E2 合流(4b3cdd886 → fed03b774)带进 tests/ 的目录
    r = _sh(["git", "diff", "--name-only", "4b3cdd886", "fed03b774", "--", "tests/"])
    if r.returncode != 0:
        raise SystemExit(f"🔴 取 E2 合流 diff 失败:{r.stderr}")
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            prov.setdefault(str(Path(line).parent).replace("\\", "/"), []).append("E2 合流 diff")

    # 🔴 第四个来源:**外选终单 V2 的预期红集反查**。
    #    Review 执行令①点名 pkgF/w4(EXTE3-04/14 的「恰 1 红」系于它们),
    #    按同一条逻辑机械反查下去,MUT-EXTE3-11 的那条红长在 **w2** 包里 ——
    #    它既不在七包也不在新增三包。缺一个包,那一发就会被**误记成存活**。
    #    这一条不是"再加一个包",是"分母必须由预期红集决定,不由印象决定"。
    for target, why in _V2_EXPECT_PACKAGES.items():
        prov.setdefault(target, []).append(f"V2 终单预期红反查({why})")

    # 第五来源:裁定收编(不长预期红,但 Review 令其入分母)。
    for target, why in REVIEW_MANDATED_PACKAGES.items():
        if not why:
            raise SystemExit(f"🔴 收编 {target} 没有台账出处 —— 不许无出处进分母")
        prov.setdefault(target, []).append(f"Review 裁定收编({why})")

    targets = sorted(prov)
    return targets, prov


# ══════════════════════════════════════════════════════════════════════════
# ② 环境变量 census(AST · 跨行也抓得到)
# ══════════════════════════════════════════════════════════════════════════
def _env_reads(py: Path) -> set[str]:
    """这个文件读了哪些环境变量名(字面量)。"""
    names: set[str] = set()
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        # os.getenv("X") / os.environ.get("X")
        if isinstance(node, ast.Call) and node.args:
            fn = node.func
            attr = getattr(fn, "attr", "")
            if attr in ("getenv", "get"):
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    src = ast.unparse(fn) if hasattr(ast, "unparse") else attr
                    if "environ" in src or "getenv" in src:
                        names.add(a0.value)
        # os.environ["X"]
        # 🔴 只算**读**：``os.environ["X"] = v`` 是**写**（conftest 给下游铺值），
        #    把它算成读会淹没真正的输入变量 —— 十个包里有七个都在写 DATABASE_URL。
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            src = ast.unparse(node.value) if hasattr(ast, "unparse") else ""
            if "environ" in src and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                names.add(node.slice.value)
    return names


def _env_writes(py: Path) -> set[str]:
    """这个文件**写**了哪些环境变量(``os.environ["X"] = ...``)。

    🔴 豁免必须机械可判。"我看了一眼觉得那是自己写的" 不是判据 ——
       这里要求 AST 真的在同一个包里找到写点,找不到就不给豁免。
    """
    names: set[str] = set()
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        targets_ = []
        if isinstance(node, ast.Assign):
            targets_ = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets_ = [node.target]
        for tgt in targets_:
            if isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant) \
                    and isinstance(tgt.slice.value, str):
                src = ast.unparse(tgt.value) if hasattr(ast, "unparse") else ""
                if "environ" in src:
                    names.add(tgt.slice.value)
        # os.environ.setdefault("X", ...)
        if isinstance(node, ast.Call) and node.args \
                and getattr(node.func, "attr", "") == "setdefault":
            src = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
            a0 = node.args[0]
            if "environ" in src and isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                names.add(a0.value)
    return names


def confcut_dir(target: str, extra=None) -> str | None:
    """从 `EXTRA_PYTEST_ARGS` 里解析出这个包的 `--confcutdir=<dir>`(没有就 None)。

    🔴 **不许手写第二张表** —— 作用域必须由「真正传给 pytest 的那串参数」决定。
    """
    extra = EXTRA_PYTEST_ARGS if extra is None else extra
    for a in extra.get(target, ()):
        if a.startswith("--confcutdir="):
            return a.split("=", 1)[1]
    return None


def conftest_chain(target: str, root=None, extra=None) -> list[Path]:
    """这一轮 pytest **真会加载**的祖先 conftest 链。

    🔴 [V7-C] 为什么不能一律往上收到仓根:
       `env_census` 的分母原本是「包自己的 *.py + 一路往上的 conftest」,
       出处是 2026-08-27 pkgf/w4 实测(根 `tests/conftest.py` 无条件要
       `TEST_DATABASE_URL`,漏了整包连 collect 都进不去)。那条理由**只在
       pytest 真会加载祖先时成立**。

       带 `--confcutdir=<pkg>` 之后 pytest **不再往上收** —— 此时仍把根
       conftest 算进分母,census 就会报「这个包会读 TEST_DATABASE_URL」,
       而那条路**根本不走**。实测:它当场把 gate8 判成「未计划的 DSN 读」并停机。
       结果不是安全,是**逼人往 ENV_PLAN 里塞一个永远用不到的变量**去消红 ——
       那正是「为了让锁闭嘴而往计划里写假话」。

       所以作用域跟着真参数走:confcutdir 之上的 conftest 一律不算。
    """
    root = ROOT if root is None else Path(root)
    d = root / target
    cut = confcut_dir(target, extra)
    stop = (root / cut).resolve() if cut else None
    out: list[Path] = []
    anc = d.parent
    while True:
        if stop is not None and not str(anc.resolve()).startswith(str(stop)):
            break
        cf = anc / "conftest.py"
        if cf.exists():
            out.append(cf)
        if anc == root:
            break
        anc = anc.parent
    return out


def env_census(targets: list[str]) -> None:
    print("\n── ② 环境变量 census(AST)──")
    bad: list[str] = []
    for t in targets:
        d = ROOT / t
        # 🔴 分母 = 包自己的 *.py **加上祖先 conftest 链**。
        #    只扫包自己会漏掉根 tests/conftest.py —— 它无条件要 TEST_DATABASE_URL,
        #    漏了整包连 collect 都进不去(2026-08-27 pkgf/w4 实测)。
        #    🔴 [V7-C] 但「祖先链」要按**真会加载的**算:带 --confcutdir 的包
        #    pytest 不往上收,把根 conftest 算进来会报一条根本不走的读取路径。
        files = list(sorted(d.rglob("*.py"))) + conftest_chain(t)
        read: set[str] = set()
        written: set[str] = set()
        for py in files:
            read |= _env_reads(py)
            written |= _env_writes(py)
        dsnish = {n for n in read if _DSNISH.search(n)} - set(NON_DSN_ENVS)
        planned = set(ENV_PLAN.get(t, {}))
        # 本包自己写过的 ⇒ 读回的是自己的值,不是外部输入,不构成"回落共享库"的口子。
        self_written = sorted(dsnish & written - planned)
        unplanned = sorted(dsnish - planned - written)
        print(f"   {t}")
        print(f"      读到 DSN 类:{sorted(dsnish) or '—'}")
        print(f"      已计划    :{sorted(planned) or '—'}")
        if self_written:
            print(f"      自写自读  :{self_written}(AST 在本包内找到写点 ⇒ 豁免)")
        if unplanned:
            print(f"      🔴 未计划 :{unplanned}")
            bad.append(f"{t}: {unplanned}")
        for e in sorted(read & set(SCHEMA_PATH_ENVS)):
            print(f"      夹具路径变量 {e}(用默认值)")
    if bad:
        raise SystemExit(
            "🔴 有包会读一个我没落值的 DSN 类变量 —— 它会**静默回落**到共享默认库,"
            f"而且照样全绿。停机:\n  " + "\n  ".join(bad))
    print("   ✅ 每个包读到的 DSN 类变量都已落值")


# ══════════════════════════════════════════════════════════════════════════
# ③ 跑
# ══════════════════════════════════════════════════════════════════════════
_COLLECTED = re.compile(r"(\d+)\s+tests?\s+collected")
_ERRORS_COLLECT = re.compile(r"(\d+)\s+errors?")


import mutation_replay_common as _RC   # noqa: E402  (红集 nodeid 单点)


#: 🔴 [V7-C · 2026-08-30] 逐包**额外 pytest 参数**。
#:
#:    起因(实测,不是推理):`tests/conftest.py` 在 import 期做两件事 ——
#:    缺 `TEST_DATABASE_URL` 直接 `RuntimeError`;并**硬写** `os.environ["DATABASE_URL"]`。
#:    而 gate8 包的 conftest 用的是 `os.environ.setdefault(...)`,祖先先跑完
#:    那句就不生效。三种组合实跑:
#:      (b) 不带 confcutdir + 不给 TEST_DATABASE_URL ⇒ **rc=4**(collection error)
#:      (a) 不带 confcutdir + 补 TEST_DATABASE_URL   ⇒ 4 passed
#:      (c) 带 confcutdir                            ⇒ 4 passed
#:    选 (c) 不选 (a):(a) 要在 ENV_PLAN 里声明一把**没人会创建**的库
#:    —— 那是往计划里写假话,而且让这个包同时挂 DSN 与铸库前缀、活性归属含糊。
#:    (a) 的绿还依赖 `_bind_app_db`(session autouse)恰好在任何 app 模块 import 之前
#:    硬赋值这一条顺序 —— 那是「碰巧绿」,不该当成设计。
#:
#:    🔴 这个机制**自己要有判据**:参数没传到 ⇒ 必红,不能只在字典里写个字符串。
#:    见 `test_mutation_gate9_extra_args_contract.py`(纯样本 + 数据流锁 + 真跑 collect-only)。
EXTRA_PYTEST_ARGS: dict[str, tuple[str, ...]] = {
    "tests/defgeo_gate8_2026_08_30": ("--confcutdir=tests/defgeo_gate8_2026_08_30",),
}


def pytest_argv(target: str, *, collect: bool, junit=None, extra=None) -> list[str]:
    """一个包这一轮的**完整 pytest argv** —— 两处调用点都必须走它。

    抽出来的理由与 `baseline_rc` / `gate_rc` 同:参数拼在两处,
    「额外参数没进真正跑的那条命令」这种毒不改任何字典、不动任何字符串,
    只有「两处都由同一个纯函数产出」+ 数据流锁才守得住。
    """
    extra = EXTRA_PYTEST_ARGS if extra is None else extra
    argv = [sys.executable, "-m", "pytest", target]
    argv += ["--collect-only", "-q"] if collect else ["-q"]
    argv += ["-p", "no:warnings", "-p", "no:cacheprovider", "--no-header"]
    if not collect:
        argv += ["-ra", f"--junitxml={junit}"]
    argv += list(extra.get(target, ()))
    return argv


#: 🔴 [V8-C · 2026-08-31] 跑包时每条连接的**锁等待上限**(毫秒)。单点,不逐包写。
#:
#:    出处是实测事故,不是防御性编程:2026-08-31 的 18 包基线卡在第 1 个包
#:    `tests/defensive_geo_2026_08_21`,**整轮挂 2h29m 零产出**。取证如下 ——
#:      pid 237  idle in transaction,攥着 diagnosis_runs / notification_outbox /
#:               point_freezes 共 17 个 AccessShareLock
#:      pid 277  在跑迁移 SQL,要 diagnosis_runs 的 AccessExclusiveLock,granted=f
#:      pg_blocking_pids(277) = {237}
#:    而包内显式 `SET statement_timeout = 0` ⇒ 这一等就是无限期。
#:
#:    🔴 为什么是 `lock_timeout` 而不是动那句 `statement_timeout=0`:
#:    两者是**不同的旋钮**,包内那句 SET 覆盖不到 lock_timeout;
#:    而包里为什么要关 statement_timeout 是那个包的事,不该由跑台替它决定。
#:    加了之后,同一个竞态从「挂死 2.5 小时零产出」变成「几十秒内一条点名的红」
#:    —— 红比挂死便宜,而且可归因到具体的表和 pid。
#:
#:    值可调:`GATE9_LOCK_TIMEOUT_MS`(默认 60000)。
DEFAULT_LOCK_TIMEOUT_MS = 60000


def lock_timeout_ms() -> int:
    raw = (os.environ.get("GATE9_LOCK_TIMEOUT_MS") or "").strip()
    if not raw:
        return DEFAULT_LOCK_TIMEOUT_MS
    try:
        v = int(raw)
    except ValueError:
        raise SystemExit(f"🔴 GATE9_LOCK_TIMEOUT_MS 不是整数:{raw!r}")
    if v <= 0:
        raise SystemExit(
            f"🔴 GATE9_LOCK_TIMEOUT_MS={v} —— 0 或负数等于**关掉**这道闸,"
            "而它存在的理由就是 2026-08-31 那次 2h29m 挂死。要关请显式说明并改这里。")
    return v


def env_for(target: str, base=None, plan=None, timeout_ms=None) -> dict:
    """跑一个包用的**完整 env** —— 单点。参数全可注入,便于判据机械验注入。"""
    env = dict(os.environ if base is None else base)
    plan = ENV_PLAN.get(target, {}) if plan is None else plan
    env.update(plan)
    # 纵深一层:``DATABASE_URL`` 各包自己在 import 期会写,但先钉住 ——
    # 万一哪天环境里飘着一个真库的值,读点又跑在写点前面,后果不是红是**连错库**。
    primary = (plan.get("TEST_DATABASE_URL") or plan.get("DEFGEO_PKGF_TEST_DB_URL")
               or plan.get("DEFGEO_W4_TEST_DB_URL") or plan.get("DEFGEO_P0FIX_TEST_DSN"))
    if primary:
        env["DATABASE_URL"] = primary
    env["PYTHONIOENCODING"] = "utf-8"
    ms = lock_timeout_ms() if timeout_ms is None else timeout_ms
    # libpq 读 PGOPTIONS ⇒ 每条连接一建立就带上,不依赖任何包记得自己 SET。
    # 已有值保留在前(别人的设置不被我吞掉),我的追加在后。
    env["PGOPTIONS"] = ((env.get("PGOPTIONS", "") + f" -c lock_timeout={ms}").strip())
    return env


def run_target(target: str, tag: str = "", collect: bool = True) -> dict:
    """跑一个包。``tag`` 给产物加前缀。

    🔴 tag 不是装饰:变异 runner 复用本函数时,如果产物名与基线撞车,
       基线的 junit 会被**逐发覆盖** —— 等到要回头核"基线当时到底几条绿"时,
       盘上只剩最后一发变异的那份。证据被自己的流程吃掉,和没取证一样。
    """
    env = env_for(target)
    slug = (tag + "_" if tag else "") + target.replace("/", "_")

    # collect-only 只有**基线**要(门9 要 collect 实收数)。
    # 变异每发再收一次等于把整轮时间翻倍,而变异关心的是红集不是分母。
    collected, c_rc = None, 0
    if collect:
        c = _sh(pytest_argv(target, collect=True), env=env)
        ctext = (c.stdout or "") + (c.stderr or "")
        m = _COLLECTED.search(ctext)
        collected = int(m.group(1)) if m else None
        c_rc = c.returncode

    xml = OUT / f"junit_{slug}.xml"
    # 🔴 [对抗审计 A] 跑之前**先删**。产物名只由 tag+target 拼、不含轮次,
    #    而下面那道闸只查 exists —— 不删的话,「这一轮 pytest 死在写 junit 之前」
    #    会读到**上一轮**的 XML,与「这一轮跑完全绿」完全同形。
    #    本轮没炸只因为我手动 rm 过一次:那是卫生习惯,不是判据。
    xml.unlink(missing_ok=True)
    r = _sh(pytest_argv(target, collect=False, junit=xml), env=env)
    rtext = (r.stdout or "") + (r.stderr or "")
    (OUT / f"log_{slug}.txt").write_text(rtext, encoding="utf-8", errors="replace")

    rec = {"target": target, "collected": collected, "rc": r.returncode,
           "collect_rc": c_rc}
    # 🔴 [对抗审计 A] rc 原来只写进 rec、**全脚本读 0 次**(grep 实证)。
    #    pytest rc:0=全绿 1=有失败 2=中断 3=内部错 4=用法错(含 conftest import 期崩)
    #    5=一条都没收到。2/3/4/5 都意味着"这一轮的数不可信",且**未必**不写 junit。
    if r.returncode not in (0, 1):
        rec["error"] = f"pytest rc={r.returncode}(只有 0|1 可信)—— 本包的数**不可信**"
        rec["tail"] = rtext.strip().splitlines()[-8:]
        return rec
    if not xml.exists():
        # 🔴 junit 不存在 ≠ 全绿。这一条是 WOA 合同判据钉过的同一个洞。
        rec["error"] = "junit 未生成 —— 本包的数**不可信**"
        rec["tail"] = rtext.strip().splitlines()[-8:]
        return rec
    ts = ET.parse(xml).getroot()
    if ts.tag == "testsuites":
        ts = ts.find("testsuite")
    n = lambda k: int(ts.get(k) or 0)          # noqa: E731
    rec.update(tests=n("tests"), failures=n("failures"), errors=n("errors"),
               skipped=n("skipped"))

    # 🔴 [V6-B · B-2] 只记一个 skip **数字**的话,白名单无从比对 ——
    #    「允许 1 条」拦不住「换了一条」。逐条记 nodeid。
    def _nodeid(tc) -> str:
        parts = [x for x in (tc.get("classname") or "").split(".") if x]
        klass = parts.pop() if parts and parts[-1][:1].isupper() else ""
        path = ("/".join(parts) + ".py") if parts else "?"
        return f"{path}::{(klass + '::') if klass else ''}{tc.get('name')}"

    rec["skipped_nodes"] = sorted(
        _nodeid(tc) for tc in ts.iter("testcase") if tc.find("skipped") is not None)
    # 🔴 红节点名从 junit 的结构里取,不从 stdout 摘要正则捞:
    #    `-rf` 只报 failed 不报 error,靠摘要取红集会把整包 error 读成"没红"。
    red_nodes = []
    for tc in ts.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            red_nodes.append(_RC.red_nodeid(tc.get("classname"), tc.get("name")))
    rec["red_nodes"] = sorted(red_nodes)
    rec["passed"] = rec["tests"] - rec["failures"] - rec["errors"] - rec["skipped"]
    rec["red"] = rec["failures"] + rec["errors"]
    return rec


# ══════════════════════════════════════════════════════════════════════════
# ④ 库活性反证
# ══════════════════════════════════════════════════════════════════════════
def _tables(db: str) -> int:
    import psycopg2
    conn = psycopg2.connect(f"{BASE}/{db}")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def db_activity(names) -> dict[str, int]:
    """每把库的 ``xact_commit`` 累计值 —— **零扰动**的本轮锚。

    🔴 为什么不再用「跑前冷重建整库」:那个锚是对的(证据只能来自本轮),
       但它**太贵** —— 每轮重灌 11 × 1.3MB 生产 dump,容器 I/O 压力足以让
       funding_p0 那条并发判据在 10 秒内起不来第二条连接,于是它**正确地
       fail-closed**、报「第二条连接没跑起来 —— 这条判据什么都没验到」。
       实测(只动这一个变量):带冷重建 1982/1(2/2 次),不带 1983/0(4/4 次)。
       **我的加固把基线弄脏了。修仪器不能以扰动被测环境为代价。**

       改读 ``pg_stat_database.xact_commit`` 前后差:一次 SELECT、零写入零 I/O,
       而「本轮这把库上有没有发生过事务」正是活性要问的那句话。
    """
    import psycopg2

    conn = psycopg2.connect(f"{BASE}/postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT datname, xact_commit FROM pg_stat_database "
                        "WHERE datname = ANY(%s)", (list(names),))
            return {r[0]: int(r[1] or 0) for r in cur.fetchall()}
    finally:
        conn.close()


def cold_reset_inplace_dbs(targets: list[str]) -> None:
    """[对抗审计 B] 跑之前把就地建库的那几把**整库重建**。

    没有这一步,``n > 0`` 数到的是**跨轮累积残留**:某个包这一轮即使静默回落到
    共享默认库(它根本不碰我的库),上一轮留下的表照样让活性臂 ✅。
    失效方向最毒 —— 第一轮对,之后**永久为真**,而且是安静地变绿。
    先 DROP 再 CREATE,「跑完还有表」就只能来自**这一轮**。
    """
    import psycopg2

    names = sorted({db for t in targets for db in INPLACE_DB.get(t, ())})
    conn = psycopg2.connect(f"{BASE}/postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for n in names:
                if "test" not in n:                      # 安全栓:不 DROP 非 test 库
                    raise SystemExit(f"🔴 拒绝 DROP 库名不含 'test' 的库:{n}")
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname=%s AND pid<>pg_backend_pid()", (n,))
                cur.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
                cur.execute(f'CREATE DATABASE "{n}"')
    finally:
        conn.close()
    print(f"   ✅ 已冷重建 {len(names)} 把就地库(活性反证从此只认**本轮**建的表)")


def liveness(targets: list[str], since: str | None = None,
             before: dict[str, int] | None = None) -> list[str]:
    print("\n── ④ 库活性反证(证明判据真的跑在**我的**库上)──")
    bad = []
    for t in [x for x in targets if x in NO_DB_PACKAGES]:
        print(f"   ⏭  {t:<44} 纯静态无库,活性反证不适用")
    for t in targets:
        for db in INPLACE_DB.get(t, ()):
            try:
                n = _tables(db)
            except Exception as exc:                    # noqa: BLE001
                print(f"   🔴 {t}:连不上 {db}{type(exc).__name__}: {exc}")
                bad.append(f"{t}::{db}")
                continue
            # 🔴 两条一起才算数:
            #    ① 有表(库真的建起来了);
            #    ② **本轮**在这把库上发生过事务(xact_commit 涨了)——
            #       没有②的话,表可能是**上一轮**留下的,而这个包这轮回落了共享库。
            delta = after = None
            recreated = False
            if before is not None:
                after = db_activity([db]).get(db, 0)
                delta = after - before.get(db, 0)
                # 🔴 域限:``pg_stat_database`` 的计数器**随建库归零**。
                #    e3 的夹具会 DROP+CREATE 冷库/模板,于是 after < before ——
                #    那不是"没活动",恰恰是"本轮把这把库重建过"。
                #    (今天第三次踩「规则适用域 < 使用域」:先是插入型变异的回读、
                #     再是拿多行当 fenced 的替身信号,现在是这一条。)
                recreated = after < before.get(db, 0)
            ok = n > 0 and (delta is None or delta > 0 or recreated)
            note = ""
            if delta is not None:
                note = (f" · 本轮**重建过**(计数器归零 {before.get(db, 0)}→{after})"
                        if recreated else f" · 本轮事务 +{delta}")
            print(f"   {'✅' if ok else '🔴'} {t:<44} {db} 有 {n} 张表" + note)
            if not ok:
                bad.append(f"{t}::{db}")
    if bad:
        print("   🔴 上面这些包的库是**空的** —— 它们没跑在我的库上(多半静默回落了共享默认库)")
    # 现建现删的两个:看服务端 DDL 日志
    # 🔴 **不加 --tail**:log_statement='ddl' 把整份迁移 SQL 都打进日志,
    #    这一轮 340 万行,CREATE 行早滚出任何小窗口(第一版 --tail 4000 就是这么
    #    报出两个假 🔴 的)。扫全量慢十几秒,但它是**对的**。
    # 🔴 第三处坑:PostgreSQL 默认把日志写到 **stderr**,``docker logs`` 因此
    #    也从 stderr 出 —— 只读 .stdout 会拿到空字符串。
    #    (我在 shell 里是 `docker logs ... 2>&1` 才看见的,搬进 Python 时丢了这一半。)
    # 🔴 [对抗审计 B] 扫全量修好了 --tail 假红,却把**唯一的时效边界**一起去掉了:
    #    上一轮的 CREATE DATABASE 行永远留在日志里 ⇒ 这条臂第二轮起永久为真。
    #    ``--since <本轮开跑时刻>`` 把证据钉回这一轮。
    if PG_LOG_FILE is not None:
        # 🔴 边界在**我们自己**手里:shim 会静默吞掉 --since,Python 侧不会。
        logs = filter_log_since(
            PG_LOG_FILE.read_text(encoding="utf-8", errors="replace"), since)
    else:
        _cmd = (["docker", "logs"] + (["--since", since] if since else [])
                + [PG_CONTAINER])
        _p = subprocess.run(_cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        logs = (_p.stdout or "") + (_p.stderr or "")
    # 🔴 [V6-B · B-3/B-5] 这一臂搬进纯函数 `liveness_from_logs`:
    #    ① 无 CREATE ⇒ bad 非空(原来 `return bad` 空集,「探针不可信」与「正常」同形);
    #    ② 只核**本轮选中**的 minting 包(原来遍历整张 MINTING_DB,--only 永远不干净)。
    ddl_bad = liveness_from_logs(logs, targets)
    for t, prefix in sorted((t, p) for t, p in MINTING_DB.items() if t in set(targets)):
        hit = re.findall(rf'CREATE DATABASE "{prefix}[0-9a-z_]+"', logs)
        print(f"   {'✅' if hit else '🔴'} {t:<44} 服务端 DDL 日志见 "
              f'CREATE DATABASE "{prefix}…" × {len(hit)}')
        if hit:
            print(f"        例:{sorted(set(hit))[:3]}")
    for b in ddl_bad:
        print(f"   🔴 {b}")
    bad += ddl_bad
    return bad
    for t, prefix in MINTING_DB.items():
        # 🔴 字符类是 [0-9a-z_]:真库名形如 defgeo_p0fix_67c86509_**test**,
        #    按 hex 写成 [0-9a-f_] 时 t/s 匹配不上,永远 0 命中。
        hit = re.findall(rf'CREATE DATABASE "{prefix}[0-9a-z_]+"', logs)
        ok = bool(hit)
        print(f"   {'✅' if ok else '🔴'} {t:<48} 服务端 DDL 日志见 "
              f"CREATE DATABASE \"{prefix}…\" × {len(hit)}")
        if hit:
            print(f"        例:{sorted(set(hit))[:3]}")
        if not ok:
            bad.append(t)
    return bad


def main() -> int:
    # [Review 机制令 2026-08-26 (1)] 树级排他锁 —— 本脚本**不改源**,但它**读整棵树**。
    # 2026-08-26 三伤里最贵的一次就是"读到别人写了一半的判据文件报 5 红,
    # 单跑一条都复现不了"。锁只护 runner 不护读者时,假红照样进结论。
    from mutation_tree_lock import tree_lock

    with tree_lock("gate9_full_denominator_baseline"):
        return _main_locked()


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某个包(调试用)")
    ap.add_argument("--host-segment", action="store_true",
                    help="段②:只跑标了 transport=host 的包,且必须真在宿主上跑。"
                         "不给这个开关时,那些包被显式跳过并打印理由。")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    targets, prov = derive_targets()
    seven = [t for t in targets if "七包(extsel FULL_DENOMINATOR)" in prov[t]]
    newly = [t for t in targets if t not in seven]

    print(f"── ① 分母(三来源机械求并)= {len(targets)} 个包 "
          f"[七包 {len(seven)} + 新增 {len(newly)}] ──")
    for t in targets:
        tag = "七包" if t in seven else "新增"
        print(f"   [{tag}] {t}")
        print(f"          来源:{' / '.join(sorted(set(prov[t])))}")
    if len(seven) != 7:
        raise SystemExit(f"🔴 七包实得 {len(seven)} 个 —— 来源变了,停机报 Review")

    _identity = repo_identity("host" if args.host_segment else "container")     # 🔴 全轮**只算这一次**
    _printed = print_transport_identity(_identity)
    selftest_denominator_lock()
    assert_denominator_frozen(targets)
    # 🔴 [V6-B · B-1] 夹具 schema 必须显式落值 —— 排在第一个包跑之前。
    # 🔴 [V9-B] 运输分裂:标了 host 的包不在这一段跑,但**必须被看见**。
    #    静默少跑一个包 = 「零红在悄悄变小的分母上永远成立」,这正是本轮
    #    P1-6 去抓的病;所以这里逐个打印它们和理由,而不是悄悄过滤掉。
    #    🔴 位置在**所有 per-target 闸之前**:那些闸的适用域必须等于
    #    「这一段真要跑的包」。适用域宽于使用域,段② 会被别的包的
    #    夹具 env 挡在门外(实测踩过:宿主段被四个与它无关的 schema 变量拦停)。
    _hostset = set(host_targets())
    if args.host_segment:
        # 段②:**只**跑宿主运输的包;跑集为空要停机(空分母不是通过)。
        _host = []
        run = [t for t in targets if t in _hostset
               and (not args.only or args.only in t)]
        if not run:
            raise SystemExit("🔴 --host-segment 选出 0 个包 —— 空分母不是通过")
    else:
        _host = [t for t in targets if t in _hostset]
        run = [t for t in targets if t not in _hostset
               and (not args.only or args.only in t)]
    if _host:
        print("\n── ③a 本段**不跑**(运输 = 宿主),由段② 覆盖 ──")
        for _t in _host:
            print(f"   ⇢ {_t}")
            print(f"     理由:{TRANSPORT[_t]}")

    schema_prov = assert_schema_fixtures(os.environ, ROOT, run)
    _assert_latch_armed()
    env_census(run)
    # 本轮锚:先记时刻,再冷重建 —— 两条活性臂都只认这一刻之后的证据。
    import datetime as _dt

    run_since = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 🔴 上面这段原写「默认仍然冷重建」,与现行默认**相反**,已删。
    #    当初那句「带冷重建 2/2 红、不带 4/4 绿」后来被推翻:不带冷重建也红过,
    #    红 3 / 绿 4 共 7 轮,**触发条件没隔离出来**。见交付文 §6.6。
    # 本轮锚 = 事务计数快照(零扰动)。冷重建**默认不做** —— 它是对的但太贵,
    # 实测会把 funding_p0 的并发判据挤成「没验到」(理由见 db_activity 的 docstring)。
    _inplace = sorted({db for t in run for db in INPLACE_DB.get(t, ())})
    activity_before = db_activity(_inplace)
    if os.environ.get("GATE9_COLD_RESET"):
        cold_reset_inplace_dbs(run)
        activity_before = db_activity(_inplace)

    _seg = "段②(宿主运输)" if args.host_segment else "段①(容器运输)"
    print(f"\n── ③ 逐包跑({len(run)} 个 · {_seg} · "
          f"冻结分母 {len(GATE9_DENOMINATOR)},需两段合起来覆盖满)──")
    recs = []
    for t in run:
        rec = run_target(t)
        recs.append(rec)
        if "error" in rec:
            print(f"   🔴 {t:<48} {rec['error']}")
            continue
        print(f"   {'✅' if rec['red'] == 0 else '🔴'} {t:<48} "
              f"collect {rec['collected']} · 跑 {rec['tests']} · "
              f"绿 {rec['passed']} · 红 {rec['red']} · skip {rec['skipped']}")
        if rec["collected"] is not None and rec["collected"] != rec["tests"]:
            print(f"      ⚠ collect({rec['collected']}) ≠ 跑({rec['tests']}) —— 有条目没进这一轮")

    live_bad = liveness(run, since=run_since, before=activity_before)

    tot = {k: sum(r.get(k) or 0 for r in recs)
           for k in ("collected", "tests", "passed", "red", "skipped", "failures", "errors")}
    broken = [r for r in recs if "error" in r]
    print("\n── 合计 ──")
    print(f"   collect 实收 {tot['collected']} · 跑 {tot['tests']} · 绿 {tot['passed']} "
          f"· 红 {tot['red']}(fail {tot['failures']} / error {tot['errors']}) "
          f"· skip {tot['skipped']}")
    _payload = {"identity": _identity,
                "targets": targets,
                # 🔴 [V9-B] 运输分裂之后,「分母」与「这一段覆盖了谁」是两件事。
                #    `targets` 仍是**冻结分母全集**(18),`segment_targets` 才是
                #    这一段真跑的那些。合成一个字段的话,段② 单独拿出来会长得像
                #    「18 包全绿」——而它只跑了 1 个。
                "segment_targets": list(run),
                "provenance": prov, "records": recs,
                    "totals": tot, "liveness_bad": live_bad,
                    "schema_fixtures": schema_prov,
                    "python": sys.version.split()[0],
                    "transport": {"base": BASE, "pg_container": PG_CONTAINER,
                                  "ddl_log_source": transport_log_source()},
                "unexpected_skips": unexpected_skips(recs)}
    (OUT / "baseline.json").write_text(
        json.dumps(_payload, ensure_ascii=False, indent=1), encoding="utf-8")
    # 🔴 [V6-B · B-2] skip 原来**不在退出条件里** —— 整包 skip 于是六数好看、rc 0。
    skip_bad = unexpected_skips(recs)
    if skip_bad:
        print(f"🔴 白名单之外的 skip {len(skip_bad)} 条(白名单按 nodeid 精确):")
        for x in skip_bad[:20]:
            print(f"     {x}")
        if len(skip_bad) > 20:
            print(f"     …… 另 {len(skip_bad) - 20} 条")
    assert_identity_recorded(_payload, expect_tip=_identity["tip"],
                             expect_identity=_printed)
    rc = baseline_rc(broken=broken, live_bad=live_bad, red=tot["red"],
                     skip_bad=skip_bad)
    if rc:
        print("🔴 基线不干净 —— 这一轮的分母**不能当证据用**")
    else:
        print("✅ 全分母基线干净(红 0),且每个包都实证跑在自有冷建库上")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
