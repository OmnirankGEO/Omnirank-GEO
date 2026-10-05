"""存量 5 条 dry-run 工具的判别锁(工单 §4.4)。

本轮只交 dry-run(拆包表第 3 包),`--apply` 是第 4 包且等 Owner 授权。
因此这里锁两件事:
  1. dry-run **零写入、零删除**(§4.4 末条:dry-run 与 apply 都不得删除任何关系行);
  2. `--apply` 必须**拒绝执行**,不得留一条半通的写路径。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.invrel_legacy_bindings_dryrun import TARGET_BINDING_IDS, collect

from .conftest import TEST_ID_BASE, make_binding, make_channel, make_user

ROOT = Path(__file__).resolve().parents[2]

# 复制工单 §1.4 的五条形态:客户侧当前是服务商 + 同对渠道关系
C1 = TEST_ID_BASE + 21
A1 = TEST_ID_BASE + 22


@pytest.fixture
def legacy_shape(db):
    cur = db.cursor()
    make_user(cur, A1, phone="13911100001", name="上游", agent_level=2)
    make_user(cur, C1, phone="13911100002", name="客户侧已升级为服务商", agent_level=1)
    make_binding(cur, customer_user_id=C1, agent_user_id=A1)
    make_channel(cur, buyer_dealer_id=C1, upstream_user_id=A1, bps=12000)
    db.commit()
    return cur


def _snapshot(cur):
    cur.execute("SELECT count(*) AS n FROM customer_agent_bindings")
    bindings = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM channel_pricing_relationships")
    channels = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM customer_agent_binding_history")
    history = cur.fetchone()["n"]
    return bindings, channels, history


def test_dryrun_writes_nothing_and_deletes_no_relationship_rows(legacy_shape, db):
    before = _snapshot(legacy_shape)
    collect(legacy_shape)
    db.commit()
    assert _snapshot(legacy_shape) == before


def test_dryrun_target_list_is_hardcoded_to_the_five_rows():
    """窄范围是刻意的:能对任意 binding 跑的工具迟早会被对着别的行跑。"""
    assert TARGET_BINDING_IDS == (1, 10, 11, 38, 67)


def test_apply_is_refused_with_nonzero_exit():
    """🔴 `--apply` 必须 fail-closed。

    反向对照:同一个脚本不带 `--apply` 时必须**成功**(exit 0)——
    否则"拒绝"可能只是脚本本身就跑不起来(恒红等于没判据)。
    """
    proc = subprocess.run(
        [sys.executable, "scripts/invrel_legacy_bindings_dryrun.py", "--apply"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 2
    assert "本轮不可用" in (proc.stderr or "")
    # 反向对照:不带 --apply 能正常跑通(证明上面的非零退出是"拒绝",不是"崩了")
    ok = subprocess.run(
        [sys.executable, "scripts/invrel_legacy_bindings_dryrun.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert ok.returncode == 0, ok.stderr


def test_no_delete_statement_exists_anywhere_in_the_tool():
    """连删除关系行的**代码路径**都不许存在(工单 §P0-4「工具禁止」第 1 条)。"""
    src = (ROOT / "scripts" / "invrel_legacy_bindings_dryrun.py").read_text(encoding="utf-8")
    lowered = src.lower()
    for forbidden in ("delete from customer_agent_bindings",
                      "delete from channel_pricing_relationships"):
        assert forbidden not in lowered
