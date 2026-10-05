"""本地测试数据:一个能跑通 happy path 的服务商 + 客户 + 已确认报价

Owner 2026-08-03:「你要不要先预设一个用户进行测试完成以后再提交？」——
对。之前两轮我都是拿生产 QA 账号看的,而那个账号的客户**没有已确认的词**,
所以「选词 → 规划 → 缺口」这条 happy path 我一次都没在浏览器里见过,
只在脚本里验过。脚本验不出前端把它渲染成什么样。

🔴 只往**本地 scratch 库**写,DATABASE_URL 必须指向 127.0.0.1 的临时库。
   脚本开头有硬校验:指向别处直接退出,不给"手滑连上生产"留口子。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("DATABASE_URL", "")
# 🔴 安全闸:只允许本地 scratch 库。写生产是不可逆的。
if "127.0.0.1" not in DSN or "geo_local" not in DSN:
    print(f"拒绝执行:DATABASE_URL 不是本地 scratch 库 → {DSN[:60]}", file=sys.stderr)
    raise SystemExit(2)

USERNAME = "local_qa"
# 🔴 [Deploy 2026-08-05] 原本这里是硬编码口令常量,被镜像密钥扫描门
#    (scripts/scan_repository_secrets.py · Dockerfile stage-1 13/20)fail-closed 拦下,
#    整个镜像构建不出来。它只是本地 scratch 库的测试口令、不是生产凭据,
#    但硬编码凭据进镜像本身就不该发生 —— 改从环境变量取。
#    🔴 不给默认值:默认值等于把口令换个地方硬编码,门迟早再响一次。
PASSWORD = os.environ.get("SEED_PASSWORD", "")
if not PASSWORD:
    print("拒绝执行:请先设置 SEED_PASSWORD(本地测试口令,不要写进代码)", file=sys.stderr)
    raise SystemExit(2)
BRAND = "贵阳老酸汤餐饮管理有限公司"

# 🔴 列名是从**本地已建好的真表**读出来的,不是照记忆写的。
#    第一版我写了 users(role, is_admin, agent_level, status) —— 四个列全不存在:
#    users 表没有 role/is_admin,角色在 user_roles,agent_level 在 user_wallets。
#    这正是本批反复栽的那类错(contact_display / is_core),所以先 \d 再写。
SQL_USER = """
INSERT INTO users (username, password_hash, display_name, is_active)
VALUES (%s, %s, %s, 1)      -- is_active 是 integer 不是 boolean(SQLite 遗留)
ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
RETURNING id
"""


def main() -> int:
    import bcrypt

    from db.connection import get_connection

    pw = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(SQL_USER, (USERNAME, pw, '本地测试-服务商'))
        uid = dict(cur.fetchone())["id"]

        # 角色:代理能力看 agent_level(在钱包表上),角色表只给一个存量角色
        cur.execute(
            """INSERT INTO user_roles (user_id, role_id)
               SELECT %s, id FROM roles WHERE name = 'geo_agent_full'
               ON CONFLICT DO NOTHING""", (uid,))

        # 钱包:蒸馏 130 / 制作 390 都要余额,不给钱测不了付费路径。
        # agent_level=1 = 代理(它在钱包表上,不在 users 上)。
        cur.execute(
            """INSERT INTO user_wallets (user_id, paid_points, bonus_points,
                                         commission_points, frozen_points, agent_level)
               VALUES (%s, 100000, 0, 0, 0, 1)
               ON CONFLICT (user_id) DO UPDATE
               SET paid_points = 100000, agent_level = 1""",
            (uid,))

        # brands 的列是 `name` 不是 `brand_name`;`cities` 是客户档案里的城市 ——
        # 故意写成生产那种脏格式("贵州省贵阳市"),洗干净就测不出归一化。
        cur.execute(
            """INSERT INTO brands (name, owner_user_id, industry, is_test, cities)
               VALUES (%s, %s, 'food', TRUE, %s) RETURNING id""",
            (BRAND, uid, "贵州省贵阳市"))
        bid = dict(cur.fetchone())["id"]

        # 客户资料:让知识库那张卡有真东西,不是空的
        cur.execute(
            """INSERT INTO client_materials
                   (brand_id, company_intro, core_selling_points,
                    unique_value, service_area)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (bid,
             "贵阳本地酸汤火锅连锁，12 年老店，主打苗家红酸汤与牛肉汤底。",
             "汤底当天现熬，不用浓缩包",
             "苗寨直供酸汤发酵基地，自建中央厨房",
             "贵州省贵阳市"))

        # 🔴 报价单的 city 用**生产真值那种脏格式**("贵州省贵阳市"),
        #    不是已经洗干净的"贵阳" —— 洗干净了就测不出归一化到底行不行。
        cur.execute(
            """INSERT INTO quotes (brand_id, status, city)
               VALUES (%s, 'confirmed', %s) RETURNING id""",
            (bid, "贵州省贵阳市"))
        qid = dict(cur.fetchone())["id"]

        rows = [
            ("贵阳酸汤火锅哪家味道最地道", 8, True),
            ("贵阳观山湖区酸汤火锅推荐", 5, True),
            ("贵阳酸汤火锅人均消费性价比高的店", 3, None),   # NULL 也算核心词
            ("贵阳火锅覆盖词", 6, False),                    # 覆盖词:不该出现
        ]
        for kw, need, core in rows:
            cur.execute(
                # 本地 init_db 建的表没有 ck.brand_id(生产是迁移补的),
                # 而我们的查询本来就走 quotes.brand_id,不受影响。
                """INSERT INTO confirmed_keywords
                       (quote_id, keyword, required_articles, is_core)
                   VALUES (%s, %s, %s, %s)""",
                (qid, kw, need, core))

        # 已经做过 2 条,用来验"缺口 = 配额 − 已产出"在页面上对不对
        for st in ("ready", "published"):
            cur.execute(
                """INSERT INTO geo_douyin_posts
                       (brand_id, created_by, keyword, status, city, industry_key)
                   VALUES (%s, %s, %s, %s, %s, 'food')""",
                (bid, uid, "贵阳酸汤火锅哪家味道最地道", st, "贵阳"))

        conn.commit()
    finally:
        conn.close()

    print(f"用户  {USERNAME} / {PASSWORD}  (user_id={uid})")
    print(f"客户  {BRAND}  (brand_id={bid})")
    print(f"报价  quote_id={qid} · city='贵州省贵阳市' · 3 核心词 + 1 覆盖词")
    print("预期:词表只出 3 个;城市自动填「贵阳」(brands.cities='贵州省贵阳市' 归一化后);")
    print("     「贵阳酸汤火锅哪家味道最地道」配额 8 已做 2 还差 6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
