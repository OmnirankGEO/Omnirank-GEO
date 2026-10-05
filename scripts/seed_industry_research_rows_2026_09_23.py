#!/usr/bin/env python3
"""WO_267 数据脚本:为行业大类字典的**新大类**补调研行业行(一律 `active=false`)。

用法(Deploy 上线后跑一次,记进班次记录):
    python scripts/seed_industry_research_rows_2026_09_23.py            # dry-run:只打印名单与库里现状
    python scripts/seed_industry_research_rows_2026_09_23.py --apply    # 真写

- 名单只从 `config/industry_taxonomy.json` 取(`pending_research_rows` + 对应大类的
  `research_industry_name` / `research_industry_slug`),**不在这里另抄一份**。
- 只 INSERT:`ON CONFLICT DO NOTHING`;绝不 UPDATE / DELETE,绝不翻 active。
- 为什么 `active=false`:每个活跃行业 = 定期跑批成本;开不开由管理员在现有 CRUD 页操作
  (Review 提 Owner 定)。有人付费点亮调研时,付费路径会把那一行翻成 active(WO_267 Review Q2)。
- 连接键是 slug(管理员编辑接口能改 name、不能改 slug):按 slug 已存在 ⇒ 视为同一行,跳过
  (哪怕名字被改过);按 name 已存在但 slug 不同 ⇒ **冲突**,不写,要人看。

退出码(三态):0 完成(dry-run 正常结束也是 0)· 1 有冲突 · 3 没跑成(字典坏 / 连不上库)。
"""
from __future__ import annotations

import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def plan_rows(tax) -> List[dict]:
    """字典里「待建调研行」对应的 (大类 key, 行名, slug)。"""
    pending = set(tax.pending_research_rows)
    return [{"category_key": c.key, "name": c.research_industry_name, "slug": c.research_industry_slug}
            for c in tax.categories if c.research_industry_name in pending and c.research_industry_slug]


INSERT_SQL = (
    "INSERT INTO geo_research_industries (name, slug, sort_order, active) "
    "VALUES (%s, %s, 999, FALSE) ON CONFLICT DO NOTHING RETURNING id"
)


def main(argv: List[str]) -> int:
    apply = "--apply" in argv
    try:
        from services.industry_taxonomy import load_taxonomy
        rows = plan_rows(load_taxonomy())
    except Exception as exc:                                  # noqa: BLE001
        print("[seed] 没跑成(rc=3):行业大类字典读不出:%s" % exc)
        return 3
    if not rows:
        print("[seed] 字典里没有待建调研行(pending_research_rows 为空)—— 无事可做")
        return 0
    try:
        from db.connection import get_connection
        conn = get_connection()
    except Exception as exc:                                  # noqa: BLE001
        print("[seed] 没跑成(rc=3):连不上库:%s" % exc)
        return 3

    conflicts, to_insert = [], []
    try:
        cur = conn.cursor()
        print("%s 名单(%d 行,全部 active=false):" % ("APPLY" if apply else "DRY-RUN", len(rows)))
        for r in rows:
            cur.execute("SELECT id, name, slug, active FROM geo_research_industries WHERE slug = %s",
                        (r["slug"],))
            by_slug = cur.fetchone()
            cur.execute("SELECT id, name, slug, active FROM geo_research_industries WHERE name = %s",
                        (r["name"],))
            by_name = cur.fetchone()
            if by_slug:
                state = "已存在(按 slug,id=%s name=%s active=%s)⇒ 跳过" % (
                    by_slug["id"], by_slug["name"], by_slug["active"])
            elif by_name:
                state = "🔴 冲突:同名行已存在但 slug=%s ≠ 字典 %s ⇒ 不写,要人看" % (by_name["slug"], r["slug"])
                conflicts.append(r)
            else:
                state = "将新建"
                to_insert.append(r)
            print("  %-12s %-6s slug=%s  %s" % (r["category_key"], r["name"], r["slug"], state))
        if apply and to_insert:
            inserted = []
            for r in to_insert:
                cur.execute(INSERT_SQL, (r["name"], r["slug"]))
                got = cur.fetchone()
                if got:
                    inserted.append((r["name"], got["id"]))
            conn.commit()
            print("已新建 %d 行(active=false):%s" % (len(inserted), inserted))
        elif not apply:
            print("(dry-run:未写库;加 --apply 真写)")
    except Exception as exc:                                  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        print("[seed] 没跑成(rc=3):%s" % exc)
        return 3
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
