"""
自助发布成功的文章 → 进"已分发"列表 · 端到端验证

不依赖前端，直接验证：
1. publish_records 表有 article_id 字段（迁移幂等）
2. /api/meijiehezi/published-articles 的 UNION SQL 能查到自助发布的 article_id

[WO_273 · 2026-09-23 肯定式退役] 原有 6 条里退 4 条:pubreq 缓存透传 article_id、插件发布路由接收
article_id、PUBLISH_PROGRESS 入库写 article_id、前端插件发布请求带 article_id。这 4 个环节是链路的
**上游**,随插件后端(与 A 同单去掉的前端自助 tab)整体删除 —— 不会再有新的自助发布记录。
仍在役的是链路两端:表结构(存量行的 article_id 列)与已分发 UNION(存量自助记录照旧进「已分发」)。
接替:tests/extension_retirement_2026_09_23 的路由 / 模块缺席锁(上游不许悄悄回来)。

跑法：
  python scripts/test_self_publish_in_published_list.py
（只读源码字符串，不需要 Redis / PG）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"[PASS] {msg}")


def _fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def test_schema_migration_idempotent():
    """验证 publish_records 冷启动建表的 ALTER TABLE 是幂等的

    [WO_273 · 改指向] 原读插件后端的 `_init_extension_tables`;插件后端退役时,这段建表
    按字节原样搬到 `db/publish_records_schema.py`(逐字相同由 tests/extension_retirement_2026_09_23 锁)。
    """
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "db", "publish_records_schema.py"),
        encoding="utf-8",
    ).read()
    if "ADD COLUMN IF NOT EXISTS article_id INTEGER" not in src:
        _fail("publish_records_schema.py 缺少 ALTER TABLE ADD article_id 迁移")
    if "idx_pr_article_id" not in src:
        _fail("publish_records_schema.py 缺少 article_id 索引")
    _ok("schema 迁移幂等（IF NOT EXISTS）+ 加了索引")


def test_published_articles_sql_unions():
    """验证 /api/meijiehezi/published-articles 的 SQL UNION 了 publish_records"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "api", "meijiehezi_api.py"),
        encoding="utf-8",
    ).read()
    # 找 api_published_articles 函数体
    func_start = src.find("async def api_published_articles")
    if func_start == -1:
        _fail("找不到 api_published_articles 函数")
    func_body = src[func_start:func_start + 3000]

    if "UNION" not in func_body:
        _fail("published-articles 没 UNION（自助发布仍然查不到）")
    if "publish_records" not in func_body:
        _fail("published-articles SQL 没引用 publish_records 表")
    if "pr.status = 'success'" not in func_body:
        _fail("publish_records 查询没过滤 status='success'")
    if "pr.article_id IS NOT NULL" not in func_body:
        _fail("publish_records 查询没过滤 article_id NOT NULL（避免脏数据）")
    if "::TEXT" not in func_body:
        _fail("publish_records.user_id (TEXT) 比对没 cast，会因类型不匹配跑不通")
    _ok("/published-articles UNION publish_records 逻辑 OK")


def main():
    print("======== bug: 自助发布成功不进\"已分发\"列表 · 修复验证(WO_273 后只剩链路两端) ========\n")
    test_schema_migration_idempotent()
    test_published_articles_sql_unions()
    print("\n========== 2/2 链路两端全过 ==========")
    print("- 表结构: publish_records 加 article_id 字段 + 索引")
    print("- 入库 → 查询: published-articles UNION 进存量自助发布成功记录")


if __name__ == "__main__":
    main()
