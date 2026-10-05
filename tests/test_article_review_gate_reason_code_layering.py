"""WP4 · V9 · SSOT business-governance-master §11.4 / manual §1.6 事故#4.

article_review_gate merged legal (``blocked``) and platform-TOS (``rewrite_required``)
into one un-differentiated reason. This locks the reason-code sub-layering:

  * legal_hard (blocked) — permanently unoverridable;
  * platform_profile_hard (rewrite_required) — unoverridable for THIS channel,
    re-evaluated on channel switch.

Critically the raw ``reason`` string is preserved (== machine status) so
set_human_review and list_review_queue keep blocking human sign-off / queueing
exactly as before — the change is additive (adds reason_class + guidance), it
does NOT weaken the publication hard gate.
"""
import hashlib

from services import article_review_gate as gate

# set_human_review blocks human approval when eligibility.reason is in this set.
# 🔴 [2026-08-01] 这里原本是一份**硬编码副本**。§3A 改了 set_human_review 的真集合
# 之后,依赖它的那条测试照样绿 —— 断言的是一个已不成立的事实(假绿)。
# 现在直接 import 真常量,漂移源消失:改代码不改测试,下面的反向断言会立刻转红。
from services.article_review_gate import HUMAN_APPROVAL_BLOCKING_REASONS as _HUMAN_APPROVAL_BLOCK_SET


class _FakeCursor:
    def __init__(self, article_row):
        self._row = article_row

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return self._row


def _row(machine):
    content = "正文内容"
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": 1, "content": content, "current_content_hash": h,
        "style_code": "", "style": "", "style_family": "",
        "article_review_status": machine, "article_human_review_status": "",
        "article_review": {"reviewed_content_hash": h, "reviewed_evidence_manifest_hash": "ev1"},
        "evidence_manifest_hash": "ev1",
        "publication_profile": None, "platform_review": None,
    }


def _evaluate(machine):
    return gate.evaluate_publication_eligibility(
        1, cursor=_FakeCursor(_row(machine)), _evaluate_when_rollout_disabled=True
    )


def test_blocked_is_classified_legal_hard():
    """[§3A 降级 2026-08-01] 只翻 eligible/overridable 两项;分层字段一律保留。
    保留 reason/reason_class/message/repair_hint 是刻意的 —— 降级包不该顺手改 API。"""
    res = _evaluate("blocked")
    assert res["eligible"] is True               # [§3A] 降为提示级,不再阻塞发布
    assert res["reason"] == "blocked"            # preserved -> gate/queue unchanged
    assert res["reason_class"] == "legal_hard"   # preserved -> 分层不丢
    assert res["overridable"] is True            # [§3A] 提示级一律可继续发布
    assert "法律" in res["message"]
    assert res["repair_hint"]
    assert res["content_notice_classes"] == ["legal_hard"]


def test_rewrite_required_is_classified_platform_profile_hard():
    res = _evaluate("rewrite_required")
    assert res["eligible"] is True               # [§3A] 降为提示级
    assert res["reason"] == "rewrite_required"   # preserved -> gate/queue unchanged
    assert res["reason_class"] == "platform_profile_hard"
    assert res["overridable"] is True            # [§3A]
    assert ("渠道" in res["message"]) or ("平台" in res["message"])
    assert res["repair_hint"]
    assert res["content_notice_classes"] == ["platform_profile_hard"]


def test_legal_and_platform_differ_in_class_and_message():
    legal = _evaluate("blocked")
    platform = _evaluate("rewrite_required")
    assert legal["reason_class"] != platform["reason_class"]
    assert legal["message"] != platform["message"]


def test_downgraded_content_reasons_no_longer_block_human_signoff():
    """🔴 [§3A 同步改 2026-08-01] 原断言"blocked / rewrite_required 仍挡人工签发"。

    降级后门不再拦这两类 → 若签发还报错,就成了"能发布却不能签发"的自相矛盾。
    所以断言**反向**:这两个 reason 必须已经不在真集合里。
    因为 `_HUMAN_APPROVAL_BLOCK_SET` 现在是 import 来的真常量,谁把它们加回去
    (等于悄悄恢复硬拦)本锁立刻转红。"""
    for machine in ("blocked", "rewrite_required"):
        assert _evaluate(machine)["reason"] not in _HUMAN_APPROVAL_BLOCK_SET


def test_lineage_reasons_still_block_human_signoff():
    """🔴 反向:仍是 H0 的 lineage 两类必须仍在集合里。
    与上一条成对 —— 单向断言证明不了判别力(把集合清空,上一条照样绿)。"""
    assert _HUMAN_APPROVAL_BLOCK_SET == {
        "content_changed_after_review", "evidence_changed_after_review",
    }
