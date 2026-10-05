"""复审三问 · 真出口实跑(WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15)

Review-CTO 2026-08-15 指定三项,一律**打真函数**,不看列默认值、不看测试汇总:

  Q1 默认关是不是真的关 —— 真调 add_keyword() 加一个词,
     ① 落库 is_monitored 必须 false;② 取词函数**真的取不到它**;
     ③ 开关打开后**必须取得到**(正反两态给不同值,否则证明不了闸在起作用)。
  Q2 闸盖了几个出口 —— 机械枚举 get_keywords_for_monitoring 里所有 extra 取词分支,
     逐条打印"加没加闸 / 为什么",并与 test_W1 锁死的分支数互校。
  Q3 289/662 那 6 个真客户词回填后仍在跑 —— 用**取词函数**验(不是查列),
     并给反向对照:把其中一个临时关掉,它必须从结果里消失(改完立刻回滚)。

用法(指向**生产数据副本**,不是生产):
  DATABASE_URL=postgresql://.../prodcopy_..._test python -X utf8 scripts/verify_monitoring_optin_2026_08_15.py

🔴 本脚本只允许连库名含 test 的库;库名不含 test 直接拒跑(防手滑连生产)。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("DATABASE_URL", "")
_dbname = DSN.rsplit("/", 1)[-1].lower()
if "test" not in _dbname or "prod" in _dbname.replace("prodcopy", ""):
    print(f"🔴 拒跑:库名必须含 test 且不是生产 —— 当前 '{_dbname}'")
    sys.exit(2)

from db.monitoring_db import (  # noqa: E402
    add_keyword,
    get_connection,
    get_keywords_for_monitoring,
    get_client_keywords,
    set_extra_keyword_monitored,
)

BRAND_289, QUOTE_289 = 289, 354
BRAND_662, QUOTE_662 = 662, 386


def _sql(q, args=None, one=False):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(q, args)
        if cur.description is None:      # DELETE/UPDATE 没有结果集,fetchall 会抛
            conn.commit()
            return None if one else []
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def q1_default_off_is_real():
    print("=" * 78)
    print("Q1 默认关是不是真的关(真调 add_keyword,不是看列默认值)")
    print("=" * 78)
    kw = "复审探针词_默认关验证"
    _sql("DELETE FROM extra_keywords WHERE keyword = %s", (kw,))

    kid = add_keyword(brand_id=BRAND_662, client_id=str(QUOTE_662), keyword=kw,
                      target_brand="QZQZ木作美学定制", difficulty="中等", target_rate=60)
    row = _sql("SELECT id, status, is_monitored FROM extra_keywords WHERE id=%s", (kid,), one=True)
    print(f"  ① 落库          : id={kid} status={row['status']} is_monitored={row['is_monitored']}")
    assert row["status"] == "active", "词本身要正常入库(不是不让加词)"
    assert row["is_monitored"] is False, "🔴 加词就自动开启 = 本单要消灭的形态"

    got_off = [k["id"] for k in get_keywords_for_monitoring(quote_ids=[QUOTE_662]) if k["source"] == "extra"]
    print(f"  ② 取词(默认关) : extra 命中 {got_off}  → 新词 {kid} 在里面吗: {kid in got_off}")
    assert kid not in got_off, "🔴 默认关却被取去跑 = 闸没起作用"

    disp = {r["id"]: r["is_monitored"] for r in get_client_keywords(QUOTE_662) if r["source"] == "extra"}
    print(f"  ②b 界面显示     : id={kid} 显示 is_monitored={disp.get(kid)}(应为 False · 且不再是硬编码)")

    set_extra_keyword_monitored(keyword_id=kid, is_monitored=True, operator_user_id=None)
    got_on = [k["id"] for k in get_keywords_for_monitoring(quote_ids=[QUOTE_662]) if k["source"] == "extra"]
    print(f"  ③ 开关打开后   : extra 命中 {got_on}  → 新词 {kid} 在里面吗: {kid in got_on}")
    assert kid in got_on, "🔴 开了却取不到 = 开关是摆设"
    disp2 = {r["id"]: r["is_monitored"] for r in get_client_keywords(QUOTE_662) if r["source"] == "extra"}
    print(f"  ③b 界面显示     : id={kid} 显示 is_monitored={disp2.get(kid)}(应为 True)")

    _sql("DELETE FROM extra_keywords WHERE id=%s", (kid,))
    print(f"  ✅ 正反两态给出不同结果(不在→在),探针词已清理")


def q2_how_many_outlets():
    """Q2 · 结构锚:枚举全仓每一处 FROM extra_keywords,逐个映射到所属函数再定性。

    🔴 换尺子的原因(2026-08-15 复审抓到):第一版用语义签名
       (窗口内同时有 entitlement_platforms + monitoring_query = 拿去跑)。
       复审用结构锚抓到第 5 个口 get_active_client_keywords —— 它两个签名字段都不 SELECT,
       (本 docstring 刻意不写那个被扫描的表名字面量:扫描器只剥散文,
        而 print 文案与 SQL 同为普通字符串、结构上分不开 —— 第六个马甲就栽在这。)
       签名**看不见它**。签名扫描只能证明"我找的那种形状不存在",证明不了"这类东西不存在"。
    """
    print()
    print("=" * 78)
    print("Q2 闸盖了几个出口(结构锚 · 全分母逐个归类 · 不是找可疑形状)")
    print("=" * 78)
    sys.path.insert(0, str(ROOT / "tests" / "monitoring_optin_2026_08_15"))
    import test_optin_wiring as W

    sites = W._scan_extra_sites(W._py_sources())
    total = sum(len(v) for v in sites.values())
    print(f"  全分母:extra 取词点共 {total} 处 / {len(set(k[0] for k in sites))} 文件 / {len(sites)} 个函数")
    by_cat = {}
    for key, branches in sorted(sites.items()):
        cat = W.EXTRA_KEYWORDS_SITES.get(key, "🔴未归类")
        by_cat.setdefault(cat, []).append((key, len(branches)))
    for cat in ("dispatch", "read_only", "maintenance", "🔴未归类"):
        rows = by_cat.get(cat) or []
        if not rows:
            continue
        print(f"  [{cat}] {sum(n for _, n in rows)} 处 / {len(rows)} 函数")
        for (rel, fn), n in rows:
            print(f"      {rel}::{fn}  ×{n}")
    assert "🔴未归类" not in by_cat, "有未归类的取词点"

    branches = sites[("db/monitoring_db.py", "get_keywords_for_monitoring")]
    bulk = [b for b in branches if not b[1]]
    named = [b for b in branches if b[1]]
    print(f"  唯一漏斗内:跑全部 {len(bulk)} 条(加闸 {sum(1 for g,_ in bulk if g)} 条)"
          f" · 显式点名 {len(named)} 条(豁免 {sum(1 for g,_ in named if not g)} 条)")
    assert len(bulk) == 2 and all(g for g, _ in bulk), "有『跑全部』分支没加闸"
    assert len(named) == 2 and all(not g for g, _ in named), "显式点名分支不该加闸"

    src = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    assert "def get_active_client_keywords" not in src, "无闸的第 5 个出口被复活了"
    print("  第 5 个出口 get_active_client_keywords:已删除 ✅(死函数 · 且缺的不止本包这个闸)")


def q3_real_customers_still_running():
    print()
    print("=" * 78)
    print("Q3 289/662 那 6 个真客户词 · 回填后仍被取去跑(打取词函数,不是查列)")
    print("=" * 78)
    total = 0
    for brand, quote in ((BRAND_289, QUOTE_289), (BRAND_662, QUOTE_662)):
        rows = [k for k in get_keywords_for_monitoring(quote_ids=[quote]) if k["source"] == "extra"]
        total += len(rows)
        print(f"  brand {brand}(quote {quote}) → extra 取到 {len(rows)} 条:")
        for r in rows:
            print(f"      id={r['id']:<5} {r['keyword']}")
    print(f"  ⇒ 合计 {total} 条(期望 6)")
    assert total == 6, f"🔴 真客户词没被取到(实测 {total})"

    # 反向对照:临时关掉一条,它必须从结果里消失 —— 证明"取到"是开关决定的,不是恒真
    probe = [k for k in get_keywords_for_monitoring(quote_ids=[QUOTE_662]) if k["source"] == "extra"][0]
    set_extra_keyword_monitored(keyword_id=probe["id"], is_monitored=False)
    after = [k["id"] for k in get_keywords_for_monitoring(quote_ids=[QUOTE_662]) if k["source"] == "extra"]
    print(f"  反向对照:临时关掉 id={probe['id']} → 再取 {after}(应不含它): {probe['id'] not in after}")
    assert probe["id"] not in after, "🔴 关掉了还取得到 = 闸恒真,前面那个 6 什么都证明不了"
    set_extra_keyword_monitored(keyword_id=probe["id"], is_monitored=True, operator_user_id=None)
    restored = [k["id"] for k in get_keywords_for_monitoring(quote_ids=[QUOTE_662]) if k["source"] == "extra"]
    print(f"  复位:{restored}(应恢复含 {probe['id']}): {probe['id'] in restored}")
    assert probe["id"] in restored, "🔴 复位失败,别把真客户留在关闭态"


if __name__ == "__main__":
    print(f"库 = {_dbname}\n")
    q1_default_off_is_real()
    q2_how_many_outlets()
    q3_real_customers_still_running()
    print("\n✅ 三项全部通过(每项都带反向对照)")
