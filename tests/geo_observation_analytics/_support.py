"""Portable test helpers (test doubles + FastAPI app builder).

Kept in a plain module (NOT conftest) so test modules can `from _support import
...` regardless of whether `tests` is an importable package — conftest.py inserts
this directory onto sys.path before collection, so the import is portable across
environments (fixes the earlier collection failure).
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Optional

import psycopg2

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
os.environ.setdefault(
    "GEO_OBSERVATION_HMAC_KEY_V1",
    "dGVzdC1nZW8tb2JzZXJ2YXRpb24taG1hYy1rZXktMzJieXRlIQ==",
)

# Realistic AI-2 get_policy() shape: {policy_version, policy:{...}, effective_flags}
DEFAULT_TEST_POLICY = {
    "policy_version": 18,
    "policy": {
        "public_min_independent_brands": 3,
        "public_min_source_types": 2,
        "max_single_brand_share_bps": 1000,
        # each enabled platform selects ONE surface (mirrors AI-2's real policy
        # seed); readiness matches health by the exact (platform, surface) pair.
        "platforms": [
            {"platform_key": "doubao", "enabled": True, "surface_key": "doubao_ark_api_search"},
            {"platform_key": "qwen", "enabled": True, "surface_key": "qwen_dashscope_search"},
            {"platform_key": "deepseek", "enabled": True, "surface_key": "deepseek_dashscope_search_legacy"},
            {"platform_key": "yuanbao", "enabled": True, "surface_key": "yuanbao_hy3_tokenhub"},
            {"platform_key": "kimi", "enabled": False, "surface_key": None},
        ],
        "feature_flags": {"ingest_enabled": True, "promotion_enabled": True,
                          "aggregation_enabled": True, "product_enabled": True},
    },
    "effective_flags": {"ingest_enabled": True, "promotion_enabled": True,
                        "aggregation_enabled": True, "product_enabled": True},
    "env_overrides": {},
    "promotion_legal_basis": "legal_v1_approved",
    "consent_policy_version": "consent_v1",
    "outcome_gold_gate_passed": True,
}
from services.geo_observation_analytics.aggregates import DEFAULT_POLICY_BASIS_HASH

DEFAULT_TEST_POLICY["aggregate_policy_basis"] = DEFAULT_POLICY_BASIS_HASH
DEFAULT_TEST_POLICY["candidate_aggregate_policy_basis"] = DEFAULT_POLICY_BASIS_HASH


class FakeEvidenceSource:
    """Deterministic private-evidence double (no source-table coupling)."""

    def __init__(self, texts: Optional[dict] = None):
        self.texts = texts or {}

    def _default(self, obs_id: str):
        from services.geo_observation_analytics.evidence import ResolvedEvidence
        return self.texts.get(obs_id) or ResolvedEvidence(
            question=f"示例问题-{obs_id}", answer_excerpt=f"示例回答-{obs_id}",
            matched_text="示例品牌",
        )

    def resolve_one(self, conn, owner_user_id, brand_id, ref):
        return self._default(ref.observation_id)

    def resolve_batch(self, conn, owner_user_id, brand_id, refs):
        return {r.observation_id: self._default(r.observation_id) for r in refs}


class FakeTransport:
    """Records every request body; returns a canned valid insight response."""

    def __init__(self, response_content: Optional[str] = None, fail: bool = False):
        self.calls: list[dict] = []
        self.response_content = response_content
        self.fail = fail

    def chat(self, request_body: dict) -> dict:
        self.calls.append(request_body)
        if self.fail:
            raise RuntimeError("simulated official DeepSeek failure")
        content = self.response_content or (
            '{"summary": "示例洞察：证据不足时先补充项目验收证据并继续观察。", '
            '"evidence_refs": [], "allowed_actions": ["add_evidence"], "state": "ok"}'
        )
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }


# default injected AI-1 collection readiness = fully runnable (so baseline tests
# read 'ready'); pass collection_readiness=... to build_test_deps to override.
_READY_COLLECTION = {
    "mode": "existing_collectors_reconciled",
    "ready": True,
    "status": "ready",
    "problems": [],
    "reconciler_last_success_at": "2026-07-20T00:00:00+00:00",
    "reconciler_backlog": 0,
    "source_watermarks": {
        "paid_diagnosis": "2026-07-20T00:00:00+00:00",
        "recurring_monitoring": "2026-07-20T00:00:00+00:00",
        "research_round": "2026-07-20T00:00:00+00:00",
    },
    "duplicate_collection_jobs": [],
    "policy_version": 18,
}


def build_test_deps(*, evidence_source=None, transport=None, platform_health=None,
                    policy=None, kanon=None, collection_readiness="__ready__",
                    aggregate_readiness=None):
    from api.geo_observation_product_api import ProductApiDeps
    from services.geo_observation_analytics import explain, privacy

    def _get_conn():
        return psycopg2.connect(TEST_DATABASE_URL)

    if callable(policy):
        policy_provider = policy
    else:
        policy_payload = deepcopy(DEFAULT_TEST_POLICY if policy is None else policy)
        flags = policy_payload.setdefault("effective_flags", {})
        if flags.get("product_enabled"):
            flags.setdefault("ingest_enabled", True)
            flags.setdefault("promotion_enabled", True)
            policy_payload.setdefault("promotion_legal_basis", "legal_v1_approved")
            policy_payload.setdefault("consent_policy_version", "consent_v1")
            policy_payload.setdefault("outcome_gold_gate_passed", True)
            policy_payload.setdefault("aggregate_policy_basis", DEFAULT_POLICY_BASIS_HASH)
            policy_payload.setdefault(
                "candidate_aggregate_policy_basis", DEFAULT_POLICY_BASIS_HASH
            )
        policy_provider = lambda: policy_payload

    # None means "explicitly not wired" (fail-closed); the sentinel means "use
    # the ready default"; a dict/callable overrides. The default carries the
    # exact policy version returned by the policy provider, as production does.
    if collection_readiness == "__ready__":
        def coll_provider():
            payload = deepcopy(_READY_COLLECTION)
            raw_policy = policy_provider() or {}
            payload["policy_version"] = raw_policy.get("policy_version")
            return payload
    elif collection_readiness is None:
        coll_provider = None
    elif callable(collection_readiness):
        coll_provider = collection_readiness
    else:
        coll_provider = lambda: collection_readiness

    return ProductApiDeps(
        get_conn=_get_conn,
        evidence_source=evidence_source or FakeEvidenceSource(),
        insight_engine=explain.InsightEngine(transport=transport or FakeTransport()),
        platform_health_provider=(
            platform_health if callable(platform_health) else (lambda: platform_health or [])
        ),
        policy_provider=policy_provider,
        collection_readiness_provider=coll_provider,
        aggregate_readiness_provider=(
            aggregate_readiness
            or (lambda _basis: {"status": "ready", "problems": []})
        ),
        kanon=kanon or privacy.KAnonThresholds(),
    )


def build_test_app(deps):
    from fastapi import FastAPI, Request
    from api.geo_observation_product_api import build_routers

    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):
        uid = request.headers.get("X-Test-User-Id")
        if uid is not None:
            brands = request.headers.get("X-Test-Brands", "")
            request.state.user = {
                "user_id": int(uid), "username": f"user-{uid}",
                "is_admin": request.headers.get("X-Test-Admin") == "1",
                "client_brand_ids": [int(x) for x in brands.split(",") if x],
                "permissions": [], "roles": [], "perm_version": 1,
            }
        return await call_next(request)

    product, admin = build_routers(deps)
    app.include_router(product)
    app.include_router(admin)
    return app


def auth_headers(user_id: int, brand_ids=(), admin: bool = False) -> dict:
    return {
        "X-Test-User-Id": str(user_id),
        "X-Test-Brands": ",".join(str(b) for b in brand_ids),
        "X-Test-Admin": "1" if admin else "0",
    }


def seed_monitoring(conn, *, result_id: int, task_id: int, brand_id: int,
                    keyword: str, full_response: str, mention_type: str = "recommended"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitoring_tasks(id,brand_id) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
            (task_id, brand_id),
        )
        cur.execute(
            "INSERT INTO monitoring_results(id,task_id,keyword,platform,is_detected,mention_type,full_response) "
            "VALUES (%s,%s,%s,'doubao',1,%s,%s)",
            (result_id, task_id, keyword, mention_type, full_response),
        )
    conn.commit()
