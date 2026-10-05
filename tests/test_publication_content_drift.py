"""[P0 代发内容漂移]根因阻断 + 软失败 + 出口 的判别。

死锁链:下单冻结快照 → 发布窗口内系统自动改稿 → 提交对不上 = 漂移 →
重试沿用旧快照必再失败 → 重试耗尽 → 退款 → 用户重新下单撞同一堵墙。

Deploy 提的四条 + Owner 加的两条,逐条锁死:
① 漂移不进"重试耗尽 → 退款"通道(软失败挂起);
② 失败返回体带 actions,至少一个「重新准备并审核」;
③ 渠道超时/限流仍走原硬失败路径,不受影响;
④ 拒绝原因分层成 code + user_message + actions;
⑤ 系统在订单活跃期改稿 → 不得进重试耗尽,且文案不得指称用户修改;
⑥ 漂移后重试必须重新捕获快照(或压根不重试)—— 锁死"旧快照空转"不复发。
"""
import inspect
import json
from types import SimpleNamespace

import pytest

from services import publication_content_drift as drift


class _Cursor:
    """最小假游标:按 SQL 关键词分派返回值。"""

    def __init__(self, *, active_publication=False, article_row=None, raise_on=None):
        self._active = active_publication
        self._article = article_row
        self._raise_on = raise_on
        self._last = None

    def execute(self, sql, params=None):
        if self._raise_on and self._raise_on in sql:
            raise RuntimeError("boom")
        self._last = sql

    def fetchone(self):
        sql = self._last or ""
        if "mhz_publish_order_items" in sql:
            return {"1": 1} if self._active else None
        if "FROM articles" in sql:
            return self._article
        return None


# ---------------------------------------------------------------- 根因阻断

def test_active_publication_blocks_content_rewrite():
    assert drift.article_has_active_publication(_Cursor(active_publication=True), 1) is True
    assert drift.article_has_active_publication(_Cursor(active_publication=False), 1) is False


def test_active_publication_check_fails_closed():
    """查不出来就按"有在途"处理 —— 宁可少改一次稿,不能把在途订单改崩。"""
    cursor = _Cursor(raise_on="mhz_publish_order_items")
    assert drift.article_has_active_publication(cursor, 1) is True


def test_auto_fix_defers_instead_of_rewriting_during_publication():
    """根因:placement_service 的 LLM 自动改稿必须先问在途发布。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "services" / "placement_service.py").read_text(encoding="utf-8")
    body = src[src.index("UPDATE articles SET content=%s") - 3000: src.index("UPDATE articles SET content=%s")]
    assert "article_has_active_publication" in body, "自动改稿没让路 = 死锁根因还在"
    assert "deferred" in body, "应记为暂缓而不是静默丢弃(发布完要能重新捞起)"


def test_image_insertion_also_defers():
    """插图是另一条改内容的路径,同样必须让路。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "image_asset_api.py").read_text(encoding="utf-8")
    idx = src.index("UPDATE articles SET content = %s")
    assert "article_has_active_publication" in src[idx - 2000: idx]
    assert "ARTICLE_LOCKED_BY_PUBLICATION" in src


# ---------------------------------------------------------------- 漂移判定与归因

def test_no_drift_when_hash_matches():
    text = "稿件正文"
    order = {"id": 9, "article_id": 1, "article_content_snapshot_hash": drift.content_hash(text)}
    verdict = drift.detect_order_content_drift(_Cursor(article_row={"content": text}), order)
    assert verdict.drifted is False


def test_drift_when_content_changed():
    order = {"id": 9, "article_id": 1,
             "article_content_snapshot_hash": drift.content_hash("原文")}
    verdict = drift.detect_order_content_drift(_Cursor(article_row={"content": "改过的正文"}), order)
    assert verdict.drifted is True


def test_missing_snapshot_does_not_claim_drift():
    """没有快照 = 无从判断,不能凭空说漂移把用户拦住。"""
    order = {"id": 9, "article_id": 1, "article_content_snapshot_hash": None}
    assert drift.detect_order_content_drift(_Cursor(), order).drifted is False


# ---------------------------------------------------------------- ⑤ 文案不甩锅

def test_system_rewrite_copy_does_not_blame_the_user():
    contract = drift.build_drift_contract(
        drift.DriftVerdict(drifted=True, origin=drift.ORIGIN_SYSTEM, order_id=9, article_id=1)
    )
    assert "系统在准备期间优化了这篇稿件" in contract["message"]
    for blame in ("你修改", "您修改", "被修改过"):
        assert blame not in contract["message"], "系统自己改的却说用户改的 = 甩锅"


def test_unknown_origin_copy_states_facts_only():
    """判不出谁改的时候,只陈述事实,不指向任何一方。"""
    contract = drift.build_drift_contract(
        drift.DriftVerdict(drifted=True, origin=drift.ORIGIN_UNKNOWN, order_id=9, article_id=1)
    )
    for blame in ("你修改", "您修改", "被修改过"):
        assert blame not in contract["message"]
    assert "不一致" in contract["message"]


def test_confirmed_user_edit_may_say_so():
    contract = drift.build_drift_contract(
        drift.DriftVerdict(drifted=True, origin=drift.ORIGIN_USER, order_id=9, article_id=1)
    )
    assert "被修改过" in contract["message"]


def test_origin_defaults_to_unknown_not_user():
    """归因判不出来时的默认值必须是 unknown —— 绝不能默认成用户。"""
    assert drift.DriftVerdict(drifted=True).origin == drift.ORIGIN_UNKNOWN


# ---------------------------------------------------------------- ② 出口

def test_contract_has_seven_fields_and_a_real_exit():
    contract = drift.build_drift_contract(
        drift.DriftVerdict(drifted=True, origin=drift.ORIGIN_SYSTEM, order_id=9, article_id=1)
    )
    for field in ("code", "message", "reason", "impact", "repair_hint", "actions", "rule_version"):
        assert contract.get(field), field
    ids = [a["id"] for a in contract["actions"]]
    assert "reprepare_and_review" in ids, "红码没有下一步 = 事故"
    reprepare = next(a for a in contract["actions"] if a["id"] == "reprepare_and_review")
    assert reprepare["target"].endswith("/orders/9/re-prepare")


def test_reprepare_endpoint_exists_and_recaptures_snapshot():
    """⑥ 出口必须**重新捕获快照**,否则还是旧快照空转。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
    assert '@router.post("/orders/{order_id}/re-prepare")' in src
    body = src[src.index('@router.post("/orders/{order_id}/re-prepare")'):]
    body = body[: body.index("@router.post", 40)]
    assert "article_content_snapshot_hash = %s" in body, "没重捕获快照 = 空转不会停"
    assert "refresh_article_review" in body, "换了内容必须重走审核"
    assert "submit_attempts = 0" in body, "不清零重试计数就放不回去"


# ---------------------------------------------------------------- ①⑥ 不空转、不退款

def test_retry_stops_on_drift_instead_of_reusing_old_snapshot():
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "scheduler.py").read_text(encoding="utf-8")
    idx = src.index('content_md = order.get("article_content_snapshot")')
    after = src[idx: idx + 3000]
    assert "detect_order_content_drift" in after, "重试前没判漂移 = 拿旧快照空转"
    assert "suspend_item_for_user_action" in after, "漂移后没挂起 = 会一路转到退款"
    # 挂起后必须 continue,不能继续往下提交
    assert "continue" in after[after.index("suspend_item_for_user_action"):]


def test_suspension_does_not_use_failed_status_so_no_refund():
    """① 漂移是软失败:不走 failed,因此不触发退款往返。"""
    import db.meijiehezi_db as mdb

    src = inspect.getsource(mdb.suspend_item_for_user_action)
    assert "ITEM_STATUS_AWAITING_ACTION" in src
    assert "'failed'" not in src and '"failed"' not in src, "走了 failed 就会触发退款"
    # 已完成的不许被挂起回去
    assert "NOT IN ('published', 'success', 'cancelled', 'withdrawn')" in src


def test_suspension_stops_auto_retry():
    """⑥ 挂起同时必须停掉自动重试,否则后台还在拿旧快照转。"""
    import db.meijiehezi_db as mdb

    src = inspect.getsource(mdb.suspend_item_for_user_action)
    assert "submit_attempts = GREATEST" in src
    assert "DEFAULT_MAX_SUBMIT_ATTEMPTS" in src


def test_retry_limit_is_single_sourced():
    """挂起逻辑与锁逻辑必须同源,否则一边停一边还在转。"""
    import db.meijiehezi_db as mdb

    assert mdb.DEFAULT_MAX_SUBMIT_ATTEMPTS == 3
    src = inspect.getsource(mdb.try_lock_for_submit)
    assert "DEFAULT_MAX_SUBMIT_ATTEMPTS" in src


# ---------------------------------------------------------------- ③ 硬失败不受影响

def test_channel_failures_still_take_the_original_hard_path():
    """渠道超时/限流仍走原 failed → 退款路径,本次改动不得波及。"""
    import db.meijiehezi_db as mdb

    src = inspect.getsource(mdb.try_lock_for_submit)
    assert "status = 'failed'" in src
    assert "needs_refund = True" in src
    # 硬失败路径不得引入漂移概念
    assert "drift" not in src.lower()


# ---------------------------------------------------------------- ④ 拒绝原因分层

def test_reject_reason_is_layered_into_three_columns():
    from pathlib import Path

    src = (Path(__file__).parent.parent / "db" / "meijiehezi_db.py").read_text(encoding="utf-8")
    for column in ("reject_code", "reject_user_message", "reject_contract"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in src, column
    # 旧列保留 —— 历史数据与日志依赖它
    assert "reject_reason TEXT," in src


def test_contract_to_columns_round_trips():
    contract = drift.build_drift_contract(
        drift.DriftVerdict(drifted=True, origin=drift.ORIGIN_SYSTEM, order_id=9, article_id=1)
    )
    code, message, payload = drift.contract_to_columns(contract)
    assert code == drift.DRIFT_CODE
    assert message == contract["message"]
    assert json.loads(payload)["actions"], "合同落库后 actions 不能丢"


# ---------------------------------------------------------------- P2 死字段

@pytest.mark.parametrize("counts,expected", [
    ({}, "pending"),
    ({"awaiting_action": 1, "published": 2}, "awaiting_action"),
    ({"pending": 1, "published": 1}, "processing"),
    ({"published": 3}, "completed"),
    ({"success": 1, "published": 1}, "completed"),
    ({"cancelled": 2}, "cancelled"),
    ({"published": 1, "failed": 1}, "partial"),
    ({"failed": 2}, "failed"),
])
def test_order_status_rolls_up_from_item_terminal_states(counts, expected, monkeypatch):
    """P2:订单 status 此前 346/346 恒 pending —— 谁读谁被误导。现按 item 终态归并。"""
    import db.meijiehezi_db as mdb

    class _C:
        def __init__(self): self.updated = None
        def execute(self, sql, params=None):
            self._sql = sql
            if "UPDATE mhz_publish_orders" in sql:
                self.updated = params[0]
        def fetchall(self):
            return [{"status": k, "n": v} for k, v in counts.items()]

    cursor = _C()
    assert mdb.recompute_order_status(1, cursor=cursor) == expected
    assert cursor.updated == expected
