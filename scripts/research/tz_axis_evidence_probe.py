import os, sys, psycopg2
url = os.environ.get("TZ_PROBE_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
if not url:
    sys.exit("需要 TZ_PROBE_DATABASE_URL / TEST_DATABASE_URL")
c = psycopg2.connect(url); c.autocommit = True; cur = c.cursor()
INSTANT = "'2026-08-17 16:30:00+00'::timestamptz"
NAIVE   = "'2026-08-18 00:30:00'::timestamp"
cases = [
 ("A naive::date            (naive 墙钟截断)", "(%s)::date" % NAIVE),
 ("B tstz::date             (会话时区渲染)  ", "(%s)::date" % INSTANT),
 ("C (tstz AT TIME ZONE SH)::date  显式转换 ", "((%s AT TIME ZONE 'Asia/Shanghai'))::date" % INSTANT),
 ("D naive::date = tstz::date  ← 真正的轴   ", "(%s)::date = (%s)::date" % (NAIVE, INSTANT)),
 ("E naive::date = (tstz AT TZ SH)::date 修后", "(%s)::date = ((%s AT TIME ZONE 'Asia/Shanghai'))::date" % (NAIVE, INSTANT)),
 ("F date_trunc('day', naive)               ", "date_trunc('day',%s)" % NAIVE),
]
res = {}
print("判别力夹具:2026-08-17 16:30 UTC == 2026-08-18 00:30 Asia/Shanghai(跨日界,不是碰巧同一天)")
for tz in ("Asia/Shanghai", "UTC", "America/New_York"):
    cur.execute("SET TimeZone=%s", (tz,))
    print("--- session TimeZone = %s" % tz)
    for label, expr in cases:
        cur.execute("SELECT (%s)::text" % expr); v = cur.fetchone()[0]
        res.setdefault(label, []).append(v); print("   %s = %s" % (label, v))
print()
print("=== 判别力汇总(跨三个时区取值是否变化)===")
for label, vals in res.items():
    uniq = len(set(vals))
    print("   %-42s %s  取值 %s" % (label, "会话时区敏感[HIT]" if uniq > 1 else "时区无关[OK]  ", vals))
print()
print("结论:naive::date 本身时区无关(A/F);会话时区敏感的是 tstz::date(B)。")
print("      所以这条轴的真形态 = naive 的日期边界 **对上** CURRENT_DATE/NOW() 这类会话派生的今天(D),")
print("      修法 = 把那个『今天』显式钉到业务时区(E,三时区恒等)。")
c.close()
