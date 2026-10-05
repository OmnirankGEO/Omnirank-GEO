"""判别性回归 · GEO-R1-CAN-030 品牌合并部分迁移/破坏性假成功

旧逻辑:某表迁移失败 → conn.rollback() + 重连 + 继续跑剩余表 + 末尾 DELETE 源品牌 + commit
→ 部分迁移 + 孤儿引用 + migrated_counts 报被回滚的计数(破坏性假成功)。
修复:任一表迁移失败 → raise(整体中止)→ 外层不 commit → finally 关闭未提交连接回滚 → 不删源品牌。

源码判别锁:回退成 rollback+重连+继续 则断言失败。
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "server.py").read_text(encoding="utf-8")


def _merge_body() -> str:
    i = SRC.find("async def api_merge_brands(")
    assert i != -1
    j = SRC.find("\n@app.", i + 10)
    return SRC[i: j if j != -1 else i + 6000]


class TestBrandMergeAtomic:
    def test_table_migration_failure_aborts(self):
        body = _merge_body()
        # 迁移循环的 except 必须 raise 整体中止,不得 rollback+重连+继续
        assert "GEO-R1-CAN-030" in body, "brand merge 缺 R1-CAN-030 原子化标记"
        assert body.count("raise RuntimeError(f\"品牌合并迁移") >= 2, \
            "R1-CAN-030: simple_tables 与 user_clients 两处迁移失败都须 raise 中止"

    def test_no_reconnect_continue_pattern(self):
        body = _merge_body()
        # 旧的 rollback→重连→继续 反模式必须消失(在迁移段落内)
        assert "conn = get_connection()\n                    cursor = conn.cursor()" not in body, \
            "R1-CAN-030: 迁移失败不得再 rollback+重连+继续(破坏原子性)"

    def test_delete_brands_after_migrations(self):
        body = _merge_body()
        # DELETE FROM brands 仍在迁移之后、commit 之前(结构未破坏),原子事务内
        assert "DELETE FROM brands WHERE id = ANY(%s)" in body
        assert "conn.commit()" in body
        assert body.index("DELETE FROM brands") < body.index("conn.commit()")
