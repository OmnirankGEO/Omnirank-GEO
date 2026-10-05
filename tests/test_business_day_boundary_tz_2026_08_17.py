"""§2 判据 · 业务日边界不再跟着会话时区跑(WO-LATENT-TRAPS 2026-08-17)

判据成对 —— 每个"必须命中"配一个"必须不命中":

  A 旧写法(裸 CURRENT_DATE)在 UTC 库上**必须漂**        ← 证明这条轴是真的,不是纸上谈兵
  B 新写法(BIZ_TODAY_SQL)在三个时区上**必须不漂**       ← 反向对照,证明修法有效
  C 生产 TimeZone(Asia/Shanghai)下新旧写法**结果相同**  ← 证明这次改动对今天的行为零影响
  D 7 处内联文本与 db/business_day_sql.BIZ_TODAY_SQL **逐字相同** ← 内联不许跟 SSOT 漂开
  E 普查回归闸:必修域里"驱动行为"的 DB_TZ_LIVE 必须清零(禁区那处除外)

A/B/C 需要一个可写 PG(`TZ_PROBE_DATABASE_URL` 或 `TEST_DATABASE_URL`);
D/E 纯静态,任何环境都跑 —— 不会因为没库整组静默消失(门禁组缺分母的坑)。
"""

from __future__ import annotations

import io
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "research"))

from db.business_day_sql import BIZ_TODAY_SQL, BUSINESS_TIMEZONE  # noqa: E402

# 跨日界的判别力夹具:这一刻在上海是 08-18,在 UTC / 纽约是 08-17。
# (第一版夹具用 NOW(),三个时区碰巧同一天 → 零判别力。夹具选错比没夹具更坏。)
INSTANT_UTC = "2026-08-17 16:30:00+00"
NAIVE_SHANGHAI_WALLCLOCK = "2026-08-18 00:30:00"
ZONES = ("Asia/Shanghai", "UTC", "America/New_York")

# 本包动手改过的 7 处(file, 该行必须含的片段)
INLINED_SITES = [
    # [开源 E3 · B3c · 2026-09-28] api/m3_api.py 那处在 get_monitoring_anomaly 里,随 M3 端点(G2)删除
    # [开源 E3 · B2 · 2026-09-28] db/chat_attachment_billing_db.py 那处随文件删除(E3 孤儿)
    ("db/marketing_db.py", "applied_at::date ="),
    ("db/marketing_db.py", "created_at::date ="),
    ("db/meijiehezi_db.py", "created_at::date ="),
    ("db/monitoring_db.py", "tested_at >="),
    ("services/marketing/geo_factory.py", "created_at::date="),
]


def _pg_url():
    return os.environ.get("TZ_PROBE_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def run_in_zones():
    url = _pg_url()
    if not url:
        pytest.skip("未给 TZ_PROBE_DATABASE_URL / TEST_DATABASE_URL,跳过真 PG 判据")
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(url)
    conn.autocommit = True

    def _run(expr_sql):
        out = {}
        cur = conn.cursor()
        for tz in ZONES:
            cur.execute("SET TimeZone=%s", (tz,))
            cur.execute("SELECT (%s)::text" % expr_sql)
            out[tz] = cur.fetchone()[0]
        return out

    yield _run
    conn.close()


def _old_form():
    """旧写法:naive 墙钟的日期 == 会话派生的今天。"""
    return "('%s'::timestamp)::date = ('%s'::timestamptz)::date" % (
        NAIVE_SHANGHAI_WALLCLOCK, INSTANT_UTC)


def _new_form():
    """新写法:把"今天"显式钉到业务时区(与 BIZ_TODAY_SQL 同形,只是把 NOW() 换成固定夹具)。"""
    return "('%s'::timestamp)::date = (('%s'::timestamptz) AT TIME ZONE '%s')::date" % (
        NAIVE_SHANGHAI_WALLCLOCK, INSTANT_UTC, BUSINESS_TIMEZONE)


def test_A_old_form_drifts_across_timezones(run_in_zones):
    """必须命中:旧写法在三个时区上给出不同答案 —— 这条轴不是纸上谈兵。"""
    got = run_in_zones(_old_form())
    assert len(set(got.values())) > 1, (
        "旧写法在三个时区上竟然一致(%r)—— 说明夹具没跨日界,判据零判别力,"
        "后面 B 的'不漂'也就什么都没证明" % got
    )
    assert got["Asia/Shanghai"] != got["UTC"], got


def test_B_new_form_is_timezone_stable(run_in_zones):
    """必须不命中(反向对照):新写法三个时区恒等。"""
    got = run_in_zones(_new_form())
    assert len(set(got.values())) == 1, "新写法仍随会话时区漂:%r" % got


def test_C_no_behavior_change_under_production_timezone(run_in_zones):
    """必须不命中:生产 TimeZone 下新旧写法同值 —— 这次改动对今天的线上行为零影响。"""
    old = run_in_zones(_old_form())
    new = run_in_zones(_new_form())
    assert old[BUSINESS_TIMEZONE] == new[BUSINESS_TIMEZONE], (
        "生产时区下新旧写法不同值(%s vs %s)—— 那就不是无损修复,得先说清差在哪"
        % (old[BUSINESS_TIMEZONE], new[BUSINESS_TIMEZONE])
    )


def test_D_inlined_text_matches_ssot():
    """内联文本必须与 db/business_day_sql.BIZ_TODAY_SQL 逐字相同,不许各写各的。"""
    problems = []
    for rel, marker in INLINED_SITES:
        path = os.path.join(REPO_ROOT, rel)
        with io.open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        hits = [ln for ln in text.replace("\r\n", "\n").split("\n")
                if marker in ln and "AT TIME ZONE" in ln]
        if not hits:
            problems.append("%s 找不到含 %r 且已转换的行(修丢了?)" % (rel, marker))
            continue
        for ln in hits:
            if BIZ_TODAY_SQL not in ln:
                problems.append("%s 的内联文本与 SSOT 不逐字相同: %s" % (rel, ln.strip()[:110]))
    assert not problems, "\n  " + "\n  ".join(problems)


def test_E_no_behavior_driving_hits_remain():
    """普查回归闸:必修四域里"驱动行为"的 DB_TZ_LIVE 必须清零(禁区那处只标注,不计入)。"""
    from naive_time_business_day_census import MUST_FIX_DOMAINS, census

    rows, _n_files, _n_sql, _unparseable = census(REPO_ROOT)
    assert rows, "普查一处都没扫到 —— 探针没打进去,这条判据零判别力"
    left = sorted({
        "%s:%d %s/%s" % (r["file"], r["line"], r["domain"], r["col"])
        for r in rows
        if r["verdict"] == "DB_TZ_LIVE"
        and r["domain"] in MUST_FIX_DOMAINS
        and r["nature"] == "BEHAVIOR"
        and not r["frozen"]
    })
    assert not left, "以下驱动行为的日期边界仍挂在会话时区上:\n  " + "\n  ".join(left)
