# -*- coding: utf-8 -*-
"""P0-9 / P0-10 存量脚本**实跑**(构造夹具 —— 真数据副本上分母为 0,零判别力)。

🔴 为什么必须造夹具:本地那份生产副本里 P0-9 孤儿=0、今日真跑过=0,
   直接跑脚本只能得到"跑前分母为 0"的拒跑提示 —— 那证明不了脚本会做对事。
   所以这里把四类样本各造几条,跑 --apply,逐类断言。
"""
from __future__ import annotations
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/backfill_fake_monitor_switches_2026_08_16.py"
B = 990400


def _seed(cur):
    cur.execute("INSERT INTO users (id, username, password_hash, display_name) "
                "VALUES (%s,'p09','x','P09') ON CONFLICT (id) DO NOTHING", (B,))
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,'P09品牌',%s) "
                "ON CONFLICT (id) DO NOTHING", (B, B))
    cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status) VALUES (%s,'P09品牌',%s,'paid')", (B, B))

    def ck(i, is_mon, mstatus):
        cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, monitoring_query, "
                    "is_monitored, monitoring_status) VALUES (%s,%s,%s,'问法',%s,%s)",
                    (i, B, f"kw{i}", is_mon, mstatus))

    def sub(i, status, charged_today=False):
        cur.execute("INSERT INTO keyword_monitor_subscriptions "
                    "(user_id, keyword_id, quote_id, brand_id, status, daily_points, feature_code, "
                    " keyword_source, last_charged_at) VALUES "
                    "(%s,%s,%s,%s,%s,130,'monitoring_keyword_daily','confirmed',%s)",
                    (B, i, B, B, status, "NOW()" if charged_today else None))

    # ① 孤儿(假开关):开着但没订阅 —— P0-9 该关掉
    ck(B + 1, True, "active")
    ck(B + 2, True, "active")
    # ② 反向对照:有活订阅且开着 —— 必须全程不被动
    ck(B + 3, True, "active"); sub(B + 3, "active")
    # ③ 僵尸订阅:订阅 active 但词已归档 —— P0-10 该 cancel
    ck(B + 4, False, "archived"); sub(B + 4, "active")
    # ④ 反向对照:今天真跑过 —— 必须保持 active
    ck(B + 5, True, "active"); sub(B + 5, "active", charged_today=True)
    # ⑤ paused_low_balance 那一族 —— 脚本刻意不动(充值后自愈)
    ck(B + 6, False, "active"); sub(B + 6, "paused_low_balance")


def _money(cur):
    cur.execute("SELECT count(*) n FROM point_freezes")
    fr = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(sum(total_charged),0) s FROM keyword_monitor_subscriptions")
    return fr, cur.fetchone()["s"]


def _run(*args):
    env = dict(os.environ)
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def test_P09_P10_apply_touches_only_the_broken_ones(committed):
    cur = committed.cursor()
    _seed(cur)
    money_before = _money(cur)

    r = _run("--apply")
    assert r.returncode == 0, f"脚本非零退出:\n{r.stdout}\n{r.stderr}"

    def ckrow(i):
        cur.execute("SELECT is_monitored FROM confirmed_keywords WHERE id=%s", (i,))
        return cur.fetchone()["is_monitored"]

    def substatus(i):
        cur.execute("SELECT status FROM keyword_monitor_subscriptions WHERE keyword_id=%s", (i,))
        return cur.fetchone()["status"]

    # P0-9:两条孤儿被关掉
    assert ckrow(B + 1) is False and ckrow(B + 2) is False, "P0-9 没关掉假开关"
    # 🔴 反向对照:有活订阅的词一根汗毛都不许动
    assert ckrow(B + 3) is True, "反向对照被误伤 = 脚本认的是『所有的』不是『跑不动的』"
    assert substatus(B + 3) == "active"
    # P0-10:僵尸订阅被 cancel
    assert substatus(B + 4) == "cancelled", "P0-10 没 cancel 僵尸订阅"
    # 🔴 反向对照:今天真跑过的必须全程 active
    assert substatus(B + 5) == "active", "把今天真在跑的订阅也 cancel 了"
    # paused_low_balance 那一族:只报告不动
    assert substatus(B + 6) == "paused_low_balance", \
        "动了 paused_low_balance —— 充值后自愈的路径被永久杀掉"

    # 💰 这个动作不许碰钱
    assert _money(cur) == money_before, f"资金基线变了:{money_before} -> {_money(cur)}"


def test_P09_P10_is_idempotent(committed):
    """复跑一次:不该再改任何东西(幂等),且分母为 0 时拒跑而不是静默全绿。"""
    cur = committed.cursor()
    _seed(cur)
    assert _run("--apply").returncode == 0
    money_after_first = _money(cur)

    r2 = _run("--apply")
    assert _money(cur) == money_after_first, "第二次跑动了钱"
    cur.execute("SELECT is_monitored FROM confirmed_keywords WHERE id=%s", (B + 3,))
    assert cur.fetchone()["is_monitored"] is True, "第二次跑误伤了反向对照"
    assert "分母为 0" in r2.stdout or r2.returncode != 0, \
        f"分母归零后既没拒跑也没提示 = 复跑会被读成『全绿』:\n{r2.stdout}"
