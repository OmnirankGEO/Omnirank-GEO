"""
tests/system_kb/test_reindex_system.py

Task 2：验证 parse_final_page + reindex_system 从 final 目录确定性拆 chunk。
Task 8 复审补强：sys_button chunk + `_*.md` 样例排除。
"""
import os
import tempfile

from tools.xiaobang_system_kb import reindex_system, parse_final_page
from db.kb_db import load_all_chunks, clear_system_chunks


_SAMPLE_PAGE = """---
route: /pricing
page_name: 报价方案
is_admin_only: false
visible_to: both
---
## 用途
给客户生成三档报价方案。

## 字段
- 客户名称（必填）：报价单上展示的客户名。
- 报价系数 [仅代理]（可选）：代理私有加价倍数，普通用户不可知。

## 按钮
- 生成报价：校验客户信息和关键词后生成三档方案。
- 调加价系数 [仅代理]：调整对客户的加价倍数（代理专属）。

## 常见异常
- 余额不足 → 去钱包充值。
"""


def _write_tmp_page(tmpdir: str) -> str:
    fp = os.path.join(tmpdir, "pricing.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(_SAMPLE_PAGE)
    return fp


def test_parse_final_page_example():
    card = parse_final_page("knowledge/system_kb/pages/_example-pricing.md")
    assert card["route"] == "/pricing"
    assert card["is_admin_only"] is False
    assert any(f["name"].startswith("客户名称") for f in card["fields"])


def test_reindex_system_produces_sys_chunks():
    with tempfile.TemporaryDirectory() as td:
        _write_tmp_page(td)
        clear_system_chunks()
        n = reindex_system(pages_glob=os.path.join(td, "*.md"), with_embeddings=False)
        assert n >= 2
        chunks = [c for c in load_all_chunks(include_admin=True) if c["source_type"].startswith("sys_")]
        routes = {c["route"] for c in chunks}
        assert "/pricing" in routes
        assert all(c["origin"] == "manual" for c in chunks)
        assert all(c["is_admin_only"] is False for c in chunks)
        # sys_button chunk 已产出
        assert any(c["source_type"] == "sys_button" for c in chunks)
        clear_system_chunks()


def test_reindex_button_level_visible_to_gate():
    """共享页里 [仅代理] 按钮 → sys_button chunk 收紧到 agent，不进普通用户 KB。"""
    with tempfile.TemporaryDirectory() as td:
        _write_tmp_page(td)
        clear_system_chunks()
        reindex_system(pages_glob=os.path.join(td, "*.md"), with_embeddings=False)
        chunks = [c for c in load_all_chunks(include_admin=True) if c["source_type"] == "sys_button"]
        by_name = {c["section_title"]: c for c in chunks}
        assert by_name["生成报价"]["visible_to"] == "both"
        assert by_name["调加价系数"]["visible_to"] == "agent"  # [仅代理] 按钮收紧
        # 字段级闸仍生效
        fields = [c for c in load_all_chunks(include_admin=True) if c["source_type"] == "sys_field"]
        fmap = {c["section_title"]: c for c in fields}
        assert fmap["客户名称"]["visible_to"] == "both"
        assert fmap["报价系数"]["visible_to"] == "agent"
        clear_system_chunks()


def test_reindex_excludes_underscore_pages():
    """`_*.md` 样例/草稿页不入库。

    [R3-P9 ④ 改断言,不退役] 不变式没变(下划线页仍然不入库),变的是
    「排完之后一页不剩」时的**出口**:原来是 ``return 0``(fail-open),
    现在抛 ``SystemKbReleaseAborted`` 保留上一 release。
    "返回 0" 是旧出口的形状,不是这条判据要守的性质 —— 所以改断言。

    真正要守的两条各占一档:
      ① 只有下划线页 ⇒ 零 final 页 ⇒ abort(且**没写进任何东西**);
      ② 下划线页与正常页混在一起 ⇒ 正常页入库、下划线页不入库。
    """
    import pytest

    from tools.xiaobang_system_kb import SystemKbReleaseAborted

    clear_system_chunks()
    with pytest.raises(SystemKbReleaseAborted):
        reindex_system(pages_glob="knowledge/system_kb/pages/_example-pricing.md")
    # abort 之后库里不该多出任何 sys_* 行
    assert _count_sys_chunks() == 0
    clear_system_chunks()


def _count_sys_chunks() -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM kb_chunks WHERE source_type LIKE %s",
                    (r"sys\_%",))
        return int(cur.fetchone()["c"])
    finally:
        conn.close()


def test_reindex_keeps_normal_pages_and_skips_underscore_ones(tmp_path):
    """② 档:混合目录 —— 正常页入库,下划线页不入库。

    与上一条拆开:上一条守的是「零 final 页时的出口」,这一条守的是「排除规则本身」。
    合成一条的话,拆掉排除规则会被 abort 兜住。
    """
    def page(slug):
        return (
            "---" + chr(10)
            + "route: /" + slug + chr(10)
            + "page_name: " + slug + " 页" + chr(10)
            + "visible_to: both" + chr(10)
            + "---" + chr(10) + chr(10)
            + "## 用途" + chr(10) + chr(10) + "这一页讲算力怎么用。" + chr(10)
        )

    (tmp_path / "real.md").write_text(page("real"), encoding="utf-8")
    (tmp_path / "_draft.md").write_text(page("draft"), encoding="utf-8")
    clear_system_chunks()
    n = reindex_system(pages_glob=str(tmp_path / "*.md"), with_embeddings=False)
    assert n > 0

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT source_slug FROM kb_chunks "
                    "WHERE source_type LIKE %s", (r"sys\_%",))
        slugs = {r["source_slug"] for r in cur.fetchall()}
    finally:
        conn.close()
    assert any("real" in s for s in slugs), slugs
    assert not any("draft" in s for s in slugs), slugs
    clear_system_chunks()
