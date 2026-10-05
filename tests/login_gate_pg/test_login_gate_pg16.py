"""工单 2026-07-29 判别锁 · 真 PG16 行为级(锁 2 / 锁 4 / 锁 5 + §4 可观测)

一条原则贯穿全文件:**断言状态,不断言源码**。
每条锁都真的调被测函数、真的落真库、再回读真库判定。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _schema_of(cur) -> str:
    """运行时问库拿隔离 schema 名。

    🔴 不能 `from ...conftest import SCHEMA` —— conftest 会被 pytest 当插件加载一次、
    被这条 import 当普通模块再加载一次,两次各生成一个随机 schema 名,
    于是测试拿到的名字和 fixture 连的那个库**不是同一个**(实测踩到)。
    """
    cur.execute("SELECT current_schema() AS s")
    return cur.fetchone()["s"]


def _q(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchall()


# ============================================================================
# 锁 2 · 补签落库 → 门禁真的放行
# ============================================================================

def test_lock2_supplement_writes_two_rows_and_reopens_the_gate(pg):
    """🔴 锁 2:补签勾选后 `agreement_signatures` 落两行 → 门禁判定转为放行。

    这是"再登录成功"的**判定核心** —— 登录接口在
    `has_current_registration_agreements` 返回 True 之后才会签发 JWT。
    变异(记录函数少写一份 / 版本写错)→ 本锁转红。
    """
    from services.legal_agreements import (
        PRIVACY_VERSION,
        USER_TERMS_VERSION,
        has_current_registration_agreements,
        record_registration_agreement_acceptance,
    )

    with pg.cursor() as cur:
        cur.execute("INSERT INTO users(id,username) VALUES (901,'blocked-user')")
        assert has_current_registration_agreements(cur, user_id=901) is False

        record_registration_agreement_acceptance(
            cur, user_id=901, ip_address="127.0.0.1", user_agent="pytest",
            auth_method="password", agreement_session_jti="jti-lock2",
        )

        rows = _q(cur, "SELECT agreement_type, agreement_version FROM agreement_signatures "
                       "WHERE user_id=901 ORDER BY agreement_type")
        assert len(rows) == 2, rows
        assert {(r["agreement_type"], r["agreement_version"]) for r in rows} == {
            ("privacy", PRIVACY_VERSION), ("user_terms", USER_TERMS_VERSION),
        }
        # 门禁真的开了
        assert has_current_registration_agreements(cur, user_id=901) is True


def test_lock2b_supplement_is_idempotent_on_replay(pg):
    """重复补签不得堆行(UNIQUE + ON CONFLICT 真的生效)。"""
    from services.legal_agreements import record_registration_agreement_acceptance

    with pg.cursor() as cur:
        cur.execute("INSERT INTO users(id,username) VALUES (902,'replay-user')")
        for jti in ("a", "b", "c"):
            record_registration_agreement_acceptance(
                cur, user_id=902, ip_address="127.0.0.1", user_agent="pytest",
                auth_method="password", agreement_session_jti=jti,
            )
        rows = _q(cur, "SELECT id FROM agreement_signatures WHERE user_id=902")
        assert len(rows) == 2, f"重放后应仍是两行,实得 {len(rows)}"


def test_lock2c_partial_evidence_does_not_open_the_gate(pg):
    """只签了一份 → 门禁**仍必须拦**(反向锁:别把校验放宽成"签过任意一份就算")。"""
    from services.legal_agreements import (
        USER_TERMS_VERSION,
        has_current_registration_agreements,
    )

    with pg.cursor() as cur:
        cur.execute("INSERT INTO users(id,username) VALUES (903,'half-signed')")
        cur.execute(
            "INSERT INTO agreement_signatures(user_id,agreement_type,agreement_version,content_hash) "
            "VALUES (903,'user_terms',%s,'x')", (USER_TERMS_VERSION,),
        )
        assert has_current_registration_agreements(cur, user_id=903) is False


def test_lock2d_stale_version_signature_does_not_open_the_gate(pg):
    """签的是**旧版本**(如已撤回的 user-v2.1)→ 仍然拦。

    生产里真有 4 个账号(18/24/29/46)签过 v2.1/v1.1 这对后来被撤回的版本。
    """
    from services.legal_agreements import has_current_registration_agreements

    with pg.cursor() as cur:
        cur.execute("INSERT INTO users(id,username) VALUES (904,'stale-version')")
        for kind, version in (("user_terms", "user-v2.1"), ("privacy", "privacy-v1.1")):
            cur.execute(
                "INSERT INTO agreement_signatures(user_id,agreement_type,agreement_version,content_hash) "
                "VALUES (904,%s,%s,'x')", (kind, version),
            )
        assert has_current_registration_agreements(cur, user_id=904) is False


# ============================================================================
# 锁 4 · 零条目零售目录禁止发布
# ============================================================================

def _retail_plan(entries, scope_key="SV-JV6TCE9C", version_code="v-test-1"):
    return {
        "catalog_type": "retail",
        "scope_key": scope_key,
        "version_code": version_code,
        "source_fingerprint": "f" * 64,
        "calc_meta": {"source_fingerprint": "f" * 64},
        "entries": entries,
    }


def _entry(product_code="starter_pack"):
    return {
        "product_code": product_code, "base_price_cents": 19900, "multiplier_bps": 10000,
        "final_price_cents": 19900, "paid_points": 15000, "bonus_points": 0,
        "cost_floor_cents": 1000, "usage_example_version": "v1", "source_ref_jsonb": {},
    }


def test_lock4_empty_retail_catalog_publish_is_rejected_and_writes_nothing(pg):
    """🔴 锁 4:零条目零售目录 → publish 被拒,且**一行都没写进去**。

    07-27 事故正是"校验全过 + 写进去 + 零告警"。所以只断言抛异常不够,
    必须回读真库确认没有半成品版本行残留。
    变异(去掉守卫)→ 版本行会被写进去 → 本锁转红。
    """
    from services.pricing_publication import EmptyRetailCatalogRejected, _publish_plan

    with pg.cursor() as cur:
        with pytest.raises(EmptyRetailCatalogRejected) as exc:
            _publish_plan(cur, _retail_plan([]), actor_id=1, reason="测试零条目")
        assert exc.value.scope_key == "SV-JV6TCE9C"

        rows = _q(cur, "SELECT id FROM pricing_catalog_versions")
        assert rows == [], f"被拒的发布不得留下版本行,实得 {rows}"


def test_lock4b_non_empty_retail_catalog_still_publishes(pg):
    """反向锁:守卫不能把正常发布也一起挡了。"""
    from services.pricing_publication import _publish_plan

    with pg.cursor() as cur:
        result = _publish_plan(cur, _retail_plan([_entry()]), actor_id=7, reason="正常发布")
        assert result["changed"] is True
        rows = _q(cur, "SELECT status, scope_key FROM pricing_catalog_versions")
        assert len(rows) == 1 and rows[0]["status"] == "published"
        entries = _q(cur, "SELECT product_code FROM pricing_catalog_entries")
        assert [r["product_code"] for r in entries] == ["starter_pack"]


def test_lock4c_explicit_per_scope_confirmation_allows_empty_and_leaves_evidence(pg):
    """确需清空时必须**逐 scope 点名**确认,并留下确认人与 reason。"""
    from services.pricing_publication import _publish_plan

    with pg.cursor() as cur:
        result = _publish_plan(
            cur, _retail_plan([]), actor_id=42, reason="该服务商已停止合作",
            confirmed_empty_retail_scopes=["SV-JV6TCE9C"],
        )
        assert result["changed"] is True
        row = _q(cur, "SELECT calc_meta_jsonb, reason FROM pricing_catalog_versions")[0]
        assert row["calc_meta_jsonb"]["empty_retail_confirmed_by"] == 42
        assert row["calc_meta_jsonb"]["empty_retail_confirmed_reason"] == "该服务商已停止合作"


def test_lock4d_confirming_a_different_scope_does_not_unlock_this_one(pg):
    """确认清单是**逐 scope 精确匹配**,不是"填了就放行"。"""
    from services.pricing_publication import EmptyRetailCatalogRejected, _publish_plan

    with pg.cursor() as cur:
        with pytest.raises(EmptyRetailCatalogRejected):
            _publish_plan(
                cur, _retail_plan([], scope_key="SV-AAAAAAAA"), actor_id=42,
                reason="确认的是别人", confirmed_empty_retail_scopes=["SV-JV6TCE9C"],
            )


def test_lock4e_procurement_empty_is_not_touched_by_this_guard(pg):
    """守卫只管零售 —— 进货侧本来就有自己的空目录规则,不要重复拦。"""
    from services.pricing_publication import _publish_plan

    plan = _retail_plan([_entry("agent_pack")])
    plan["catalog_type"] = "procurement"
    plan["scope_key"] = "PLATFORM_BASE"
    with pg.cursor() as cur:
        result = _publish_plan(cur, plan, actor_id=1, reason="进货发布")
        assert result["changed"] is True


# ============================================================================
# 锁 5 · 已发布零条目零售目录必须被体检出来(管理端可见的数据源)
# ============================================================================

def _publish_version(cur, *, scope_key, status="published", effective_to=None,
                     catalog_type="retail", version_code=None, with_entry=False):
    cur.execute(
        "INSERT INTO pricing_catalog_versions"
        "(catalog_type,scope_key,version_code,status,effective_from,effective_to,reason)"
        " VALUES (%s,%s,%s,%s,NOW(),%s,'seed') RETURNING id",
        (catalog_type, scope_key, version_code or f"vc-{scope_key}-{status}",
         status, effective_to),
    )
    version_id = cur.fetchone()["id"]
    if with_entry:
        cur.execute(
            "INSERT INTO pricing_catalog_entries"
            "(version_id,product_code,base_price_cents,multiplier_bps,final_price_cents,paid_points)"
            " VALUES (%s,'starter_pack',19900,10000,19900,15000)", (version_id,),
        )
    return version_id


def test_lock5_empty_published_retail_catalog_is_reported(pg):
    """🔴 锁 5:published + 开放 + 零条目 → 必须出现在体检结果里。

    变异(把体检查询的 NOT EXISTS 去掉 / 改成只看平台直营一个 scope)→ 本锁转红。
    """
    from services.retail_catalog_health import empty_published_retail_scopes

    with pg.cursor() as cur:
        empty_id = _publish_version(cur, scope_key="SV-JV6TCE9C")
        _publish_version(cur, scope_key="SV-HEALTHY1", with_entry=True)

        scopes = empty_published_retail_scopes(cur)
        assert [s["scope_key"] for s in scopes] == ["SV-JV6TCE9C"]
        assert scopes[0]["version_id"] == empty_id


def test_lock5b_archived_or_closed_versions_are_not_reported(pg):
    """只看**当前开放**的那一版 —— 历史空版本不是断供,别制造噪音告警。"""
    from services.retail_catalog_health import empty_published_retail_scopes

    with pg.cursor() as cur:
        _publish_version(cur, scope_key="SV-ARCHIVED", status="archived",
                         version_code="vc-archived")
        _publish_version(cur, scope_key="SV-CLOSED", effective_to="2026-07-01",
                         version_code="vc-closed")
        assert empty_published_retail_scopes(cur) == []


def test_lock5c_alert_contract_has_impact_and_a_next_step(pg):
    """告警必须带影响面和下一步 —— 红码无出口是本单要治的病本身。"""
    from services.retail_catalog_health import (
        empty_published_retail_scopes,
        empty_retail_catalog_alert,
    )

    with pg.cursor() as cur:
        assert empty_retail_catalog_alert([]) is None
        _publish_version(cur, scope_key="SV-JV6TCE9C")
        alert = empty_retail_catalog_alert(empty_published_retail_scopes(cur))

    assert alert is not None
    assert alert["code"] == "EMPTY_PUBLISHED_RETAIL_CATALOG"
    assert "SV-JV6TCE9C" in alert["reason"]
    assert alert.get("impact")
    assert alert.get("actions"), "告警必须给下一步动作"


# ============================================================================
# §4 可观测 · 428 门禁触发计数
# ============================================================================

def test_gate_metrics_record_and_summarise(pg):
    """记录 → 汇总 → 连续天数,全链在真库上跑通。"""
    from services.registration_agreement_gate_metrics import (
        gate_trigger_summary,
        record_gate_trigger,
    )

    with pg.cursor() as cur:
        cur.execute("INSERT INTO users(id,username) VALUES (905,'gate-user')")

    assert record_gate_trigger(user_id=905, auth_method="password") is True
    assert record_gate_trigger(user_id=905, auth_method="sms") is True
    assert record_gate_trigger(user_id=906, auth_method="password") is True

    with pg.cursor() as cur:
        summary = gate_trigger_summary(cur, days=14)

    assert summary["available"] is True
    assert summary["total"] == 3
    assert summary["distinct_users"] == 2, "按天去重之和会把跨天同一账号重复计数"
    assert summary["consecutive_days_with_triggers"] == 1


def test_gate_metrics_degrade_silently_when_table_absent(pg):
    """迁移没跑 → 计数不可用,但**绝不抛** —— 观测不得成为登录的新故障源。"""
    from services.registration_agreement_gate_metrics import gate_trigger_summary

    with pg.cursor() as cur:
        schema = _schema_of(cur)
        cur.execute(f'ALTER TABLE "{schema}".registration_agreement_gate_events '
                    f'RENAME TO gate_events_hidden')
        try:
            summary = gate_trigger_summary(cur, days=14)
            assert summary["available"] is False
            assert summary["total"] == 0
        finally:
            cur.execute(f'ALTER TABLE "{schema}".gate_events_hidden '
                        f'RENAME TO registration_agreement_gate_events')
