"""[#184 J4] 给存量 ready 作品补上 active revision —— **默认 dry-run**。

## 为什么需要它

d1 让**新生成**的作品在落 ready 时冻出 active revision。存量那批(生产上
36 条,其中 ready 22 条)是 d1 之前做出来的,`active_revision_id` 全空 ⇒
v2 素材准备对它们一律 409 `SOURCE_NOT_READY` ⇒ 发不出去。

## 🔴 走同一道门,不绕 CAS

回填**不是**「给那一列填个值」。`active_revision_id` 是代际机制的产物:

    supersede_active_tasks → INSERT 合成的 backfill 任务 → begin_generation
      → stage_revision(从库里现有的列与卡表取)→ activate_revision(CAS)
      → 任务置 done

绕过去直接 `UPDATE geo_douyin_posts SET active_revision_id = ...` 会造出一条
**没有对应 revision 行**、或 revision 与 `generation_epoch` 对不上的作品:
它看起来可发布,一发就撞 artifact 身份校验,而那时钱已经冻上了。
判据里有一发毒专门钉这件事。

## 用法

    python scripts/backfill_active_revisions_2026_09_13.py            # dry-run,只打印
    python scripts/backfill_active_revisions_2026_09_13.py --execute  # 真写(要 Owner 批)

🔴 `--execute` 是**写库**动作,按红线要 Owner 在 Deploy 窗口亲口批;
   执行前先 `pg_dump` 备份。本脚本自己不备份 —— 备份是 Deploy 的动作,
   放进脚本里会变成"脚本说它备份过了"这种无人复核的声明。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FIND_CANDIDATES = """
SELECT id, status, jsonb_array_length(COALESCE(oss_keys, '[]'::jsonb)) AS n_assets
  FROM geo_douyin_posts
 WHERE deleted_at IS NULL
   AND active_revision_id IS NULL
   AND status = 'ready'
   AND COALESCE(jsonb_array_length(COALESCE(oss_keys, '[]'::jsonb)), 0) > 0
 ORDER BY id
"""


def find_candidates(cur) -> list[dict]:
    cur.execute(_FIND_CANDIDATES)
    return [dict(r) for r in (cur.fetchall() or [])]


def backfill_one(post_id: int, actor_user_id: int) -> int | None:
    """给一条作品补版本。返回 revision_id;CAS 输了返回 None。

    全程复用生产同一批函数,一行自写的 SQL 都没有 —— 回填与真生成走同一条路,
    才谈得上"回填出来的东西和新做的一样"。
    """
    from db import geo_douyin_db as ddb
    from services.geo_douyin.production_task import _freeze_ordinary_revision

    task_id, epoch = ddb.create_task_with_generation(
        post_id=int(post_id), user_id=int(actor_user_id),
        task_ref="backfill-rev:%d" % int(post_id),
        freeze_id=None, progress_total=0)
    revision_id = _freeze_ordinary_revision(
        int(post_id), int(actor_user_id), int(task_id), int(epoch))
    ddb.update_task(task_id, status="succeeded", stage="done", mark_finished=True)
    return revision_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="真写库(默认只 dry-run 打印)。要 Owner 在 Deploy 窗口批。")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条(0=全部)")
    args = parser.parse_args()

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        rows = find_candidates(cur)
    finally:
        conn.close()
    if args.limit:
        rows = rows[: args.limit]

    print("候选(ready 且无 active revision 且有资产):%d 条" % len(rows))
    for r in rows:
        print("  post=%-8s 资产 %s 张" % (r["id"], r["n_assets"]))
    if not args.execute:
        print()
        print("dry-run:**一个字节都没写**。要真写请加 --execute(写库动作,需 Owner 批)。")
        return 0

    done = failed = superseded = 0
    for r in rows:
        post_id = int(r["id"])
        try:
            revision_id = backfill_one(post_id, actor_user_id=0)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ✗ post=%s 回填失败(不影响其余):%s" % (post_id, exc))
            continue
        if revision_id is None:
            superseded += 1
            print("  · post=%s CAS 输了(期间有新一代生成),跳过" % post_id)
            continue
        done += 1
        print("  ✓ post=%s → revision=%s" % (post_id, revision_id))
    print()
    print("完成:成功 %d · 让位 %d · 失败 %d" % (done, superseded, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
