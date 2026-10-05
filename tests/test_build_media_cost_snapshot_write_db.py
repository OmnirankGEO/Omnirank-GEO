"""
tests/test_build_media_cost_snapshot_write_db.py — P0-A 前置 hardening 单测(2026-06-15)

锁住 scripts/build_media_cost_snapshot._write_db 的【单 active 不变量】:
  - 新版本写入:事务内先降所有旧 active,再 INSERT;
    * INSERT 成功(rowcount==1)→ commit,留下恰一条 active。
    * INSERT 未命中(rowcount==0,并发写同版本触发 ON CONFLICT DO NOTHING)→
      rollback 撤销"降旧 active",raise,绝不留下 0 active(否则系统静默回落 bootstrap)。
  - 同版本同内容已存在(no-op 分支):把已存在行归一为唯一 active 后 commit。

全程用 fake 连接/游标,不碰任何真实 DB(连 import gate 都不需要真库)。
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# 无 scripts/__init__.py → 按文件路径加载(脚本自身会 sys.path.insert 项目根)
_SPEC = importlib.util.spec_from_file_location(
    "build_media_cost_snapshot_under_test",
    os.path.join(_ROOT, "scripts", "build_media_cost_snapshot.py"),
)
_BMCS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BMCS)


# ----------------------------------------------------------------------------
# Fake 连接 / 游标:按 SQL 前缀模拟 rowcount,记录 commit/rollback。
# ----------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, existing_row, insert_rowcount):
        self._existing_row = existing_row      # SELECT 命中行(dict)或 None
        self._insert_rowcount = insert_rowcount
        self.rowcount = -1
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT"):
            self.rowcount = 1 if self._existing_row else 0
        elif s.startswith("INSERT"):
            self.rowcount = self._insert_rowcount
        else:  # UPDATE 降旧 / 置 active
            self.rowcount = 1

    def fetchone(self):
        return self._existing_row


class _FakeConn:
    def __init__(self, existing_row=None, insert_rowcount=1):
        self.cur = _FakeCursor(existing_row, insert_rowcount)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


_PAYLOAD = {
    "snapshot_version": "mcs_test_v1",
    "payload_sha256": "deadbeef",
    "schema_version": "mcs_v1",
    "source": "db",
    "geo_count": 478,
    "platform_media_markup_ratio": 2.0,
}


def _patch_conn(monkeypatch, fake):
    import db.connection as C
    monkeypatch.setattr(C, "get_connection", lambda: fake)


def test_write_db_new_version_insert_ok_commits(monkeypatch):
    """新版本 + INSERT 成功 → commit,不 rollback,返回 inserted。"""
    fake = _FakeConn(existing_row=None, insert_rowcount=1)
    _patch_conn(monkeypatch, fake)
    msg = _BMCS._write_db(_PAYLOAD)
    assert "inserted" in msg
    assert fake.committed is True
    assert fake.rolled_back is False
    assert fake.closed is True
    # 必须先降旧 active(UPDATE ... is_active=FALSE)再 INSERT
    joined = " || ".join(fake.cur.executed).upper()
    assert "UPDATE MEDIA_COST_SNAPSHOT SET IS_ACTIVE=FALSE WHERE IS_ACTIVE" in joined
    assert "INSERT INTO MEDIA_COST_SNAPSHOT" in joined


def test_write_db_insert_conflict_rolls_back_and_raises(monkeypatch):
    """🔴 核心:INSERT rowcount=0(并发同版本)→ rollback 撤销降旧 + raise · 绝不留 0 active。"""
    fake = _FakeConn(existing_row=None, insert_rowcount=0)
    _patch_conn(monkeypatch, fake)
    with pytest.raises(SystemExit):
        _BMCS._write_db(_PAYLOAD)
    assert fake.rolled_back is True       # 降旧 active 被撤销
    assert fake.committed is False        # 绝不 commit 半截
    assert fake.closed is True            # finally 仍归还连接


def test_write_db_existing_same_content_normalizes_single_active(monkeypatch):
    """同版本同内容已存在 → 不新增行,归一为唯一 active 后 commit。"""
    existing = {"id": 7, "payload_sha256": "deadbeef"}
    fake = _FakeConn(existing_row=existing, insert_rowcount=99)  # insert_rowcount 不该被用到
    _patch_conn(monkeypatch, fake)
    msg = _BMCS._write_db(_PAYLOAD)
    assert "no-op" in msg
    assert fake.committed is True
    assert fake.rolled_back is False
    joined = " || ".join(fake.cur.executed).upper()
    # 降其它 active + 把本行置 active,且没有 INSERT
    assert "IS_ACTIVE=FALSE WHERE IS_ACTIVE AND ID" in joined
    assert "SET IS_ACTIVE=TRUE WHERE ID" in joined
    assert "INSERT INTO" not in joined


def test_write_db_existing_diff_content_refuses(monkeypatch):
    """同版本但内容 hash 不同 → 拒绝静默覆盖(SystemExit),不 commit。"""
    existing = {"id": 7, "payload_sha256": "OTHER_SHA"}
    fake = _FakeConn(existing_row=existing, insert_rowcount=1)
    _patch_conn(monkeypatch, fake)
    with pytest.raises(SystemExit):
        _BMCS._write_db(_PAYLOAD)
    assert fake.committed is False
