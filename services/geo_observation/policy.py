"""geo_observation_policy —— 业务策略配置唯一 SSOT(AI-2 独占)。

- policy_version CAS(旧版本保存返回 409);
- 保存配置 + 版本推进 + before/after/request_id 审计 + epoch bump 在同一 PostgreSQL 事务;
- 环境变量覆盖可编辑 flag 时返回 423(不假成功写入);
- 跨 worker 用独立 epoch key(不与 pricing_config_epoch 串扰);Redis 不可用/无法证明新鲜 → fail-closed;
- runtime provider 健康不是可写配置(由 AI-1 只读 platform_health_fields 产出),本表不含健康。

env-first 覆盖 idiom 复用 config/v3_3_1_flags._read_from_env 的语义(env 非 None 即锁定)。
epoch 模式复用 services/config_epoch 的形状,但 key='geo_observation_policy_epoch'。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Optional

from psycopg2.extras import Json

from db.connection import get_db
from . import contracts
from .audit import write_audit
from .aggregate_basis import canonical_policy_basis, refresh_manifest_problems

logger = logging.getLogger("GEO-ObservationPolicy")

_EPOCH_KEY = "geo_observation_policy_epoch"
_EPOCH_PROBE_TTL = 2.0

# P1-1 金标准门验收阈值(契约 §601:≥100 双人复核样本 · macro-F1 ≥ 0.90 · 高风险相似品牌误推荐 = 0)。
# outcome_gold_gate_passed 只能由服务端按这些阈值从不可变评估记录派生,禁 API/前端直接提交可信布尔值。
GOLD_MIN_SAMPLES = 100
GOLD_MIN_MACRO_F1_BPS = 9000
GOLD_MAX_HIGH_RISK_FALSE_RECO = 0


def compute_gold_gate_passed(*, sample_count: Optional[int], macro_f1_bps: Optional[int],
                             high_risk_false_reco: Optional[int], dataset_version: Optional[str],
                             report_hash: Optional[str]) -> bool:
    """按契约 §601 阈值从评估证据派生金标准门通过状态(纯函数;任一不满足→False)。"""
    if not dataset_version or not report_hash:
        return False
    if sample_count is None or macro_f1_bps is None or high_risk_false_reco is None:
        return False
    return (int(sample_count) >= GOLD_MIN_SAMPLES
            and int(macro_f1_bps) >= GOLD_MIN_MACRO_F1_BPS
            and int(high_risk_false_reco) <= GOLD_MAX_HIGH_RISK_FALSE_RECO)

# feature flag → 覆盖它的环境变量名(04 §12.2 部署清单)
_FLAG_ENV = {
    "ingest_enabled": "GEO_OBSERVATION_INGEST_ENABLED",
    "promotion_enabled": "GEO_OBSERVATION_PROMOTION_ENABLED",
    "aggregation_enabled": "GEO_OBSERVATION_AGGREGATION_ENABLED",
    "product_enabled": "GEO_OBSERVATION_PRODUCT_ENABLED",
}

_epoch_cache: Optional[int] = None
_epoch_cache_ts: float = 0.0
_lock = threading.Lock()


class PolicyVersionConflict(RuntimeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"policy 版本冲突,当前={current_version}")


class PolicyEnvOverride(RuntimeError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__(f"以下字段被环境变量覆盖,DB 写入不会生效: {fields}")


class GoldEvaluationConflict(RuntimeError):
    """同一 (dataset_version, report_hash) 已存在但指标不同 → 拒绝(不可变评估身份不得改写结论)。"""
    def __init__(self, dataset_version: str, report_hash: str):
        self.dataset_version = dataset_version
        self.report_hash = report_hash
        super().__init__(f"金标准评估身份冲突(同 dataset_version+report_hash 指标不一致,拒绝改写): "
                         f"{dataset_version}/{report_hash}")


class ProductActivationBlocked(RuntimeError):
    """Product flag cannot be committed before every signed prerequisite is real."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("product_enabled 开闸条件未满足")


# ────────────────────────────── epoch(独立 key)──────────────────────────────
def read_policy_epoch_strict(cursor) -> int:
    cursor.execute("SELECT value FROM system_settings WHERE key=%s", (_EPOCH_KEY,))
    row = cursor.fetchone()
    raw = row.get("value") if isinstance(row, dict) else (row[0] if row else "0")
    text = str(raw if raw is not None else "0")
    if not text.lstrip("-").isdigit():
        raise RuntimeError(f"{_EPOCH_KEY} 非法值")
    return int(text)


def _read_policy_epoch_soft() -> int:
    global _epoch_cache, _epoch_cache_ts
    now = time.time()
    if _epoch_cache is not None and (now - _epoch_cache_ts) < _EPOCH_PROBE_TTL:
        return _epoch_cache
    try:
        with get_db() as conn:
            val = read_policy_epoch_strict(conn.cursor())
    except Exception as exc:  # noqa: BLE001
        logger.warning("read policy epoch 失败: %s", exc)
        return _epoch_cache if _epoch_cache is not None else 0
    with _lock:
        _epoch_cache = val
        _epoch_cache_ts = now
    return val


def bump_policy_epoch_cursor(cursor) -> int:
    cursor.execute(
        """
        INSERT INTO system_settings (key, value, value_type, description)
        VALUES (%s, '1', 'integer', 'geo 观测策略纪元 · 每次 policy CAS +1 · 跨 worker 缓存失效')
        ON CONFLICT (key) DO UPDATE
            SET value = (COALESCE(NULLIF(system_settings.value,'')::bigint,0)+1)::text
        RETURNING value
        """,
        (_EPOCH_KEY,),
    )
    row = cursor.fetchone()
    value = int(row.get("value") if isinstance(row, dict) else row[0])
    if value < 1:
        raise RuntimeError("policy epoch bump 失败")
    return value


def accept_committed_epoch(value: int) -> None:
    global _epoch_cache, _epoch_cache_ts
    with _lock:
        _epoch_cache = int(value)
        _epoch_cache_ts = time.time()


# ────────────────────────────── env 覆盖检测 ──────────────────────────────
def _env_flag_value(flag_name: str) -> Optional[bool]:
    env_key = _FLAG_ENV[flag_name]
    raw = os.environ.get(env_key)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_overridden_flags() -> dict[str, bool]:
    """当前被环境变量锁定的 feature flag(名→env 值)。"""
    out = {}
    for name in _FLAG_ENV:
        v = _env_flag_value(name)
        if v is not None:
            out[name] = v
    return out


# ────────────────────────────── 读 ──────────────────────────────
def get_policy(cursor=None) -> dict:
    """读当前策略行,返回 {policy_version, policy(dict), env_overrides, effective_flags}。

    effective_flags = env 覆盖优先,否则 DB 存储值。
    """
    def _do(cur):
        cur.execute(
            """SELECT policy_version, policy_json, promotion_legal_basis, consent_policy_version,
                      outcome_gold_gate_passed, gold_dataset_version, gold_macro_f1_bps,
                      gold_sample_count, gold_high_risk_false_reco, gold_report_hash,
                      collection_mode, active_aggregate_policy_basis
                 FROM public.geo_observation_policy WHERE singleton_id=1"""
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("geo_observation_policy 默认行缺失(migration 未跑?)")
        version = int(row["policy_version"])
        policy = row["policy_json"]
        stored_flags = dict(policy.get("feature_flags", {}))
        overrides = env_overridden_flags()
        effective = dict(stored_flags)
        effective.update(overrides)
        return {
            "policy_version": version,
            "policy": policy,
            "collection_mode": row["collection_mode"],
            "aggregate_policy_basis": row.get("active_aggregate_policy_basis"),
            "candidate_aggregate_policy_basis": canonical_policy_basis(policy),
            "env_overrides": overrides,
            "effective_flags": effective,
            "promotion_legal_basis": row["promotion_legal_basis"],
            "consent_policy_version": row["consent_policy_version"],
            "outcome_gold_gate_passed": bool(row["outcome_gold_gate_passed"]),
            "gold_dataset_version": row["gold_dataset_version"],
            "gold_macro_f1_bps": row["gold_macro_f1_bps"],
            "gold_sample_count": row["gold_sample_count"],
            "gold_high_risk_false_reco": row["gold_high_risk_false_reco"],
            "gold_report_hash": row["gold_report_hash"],
        }

    if cursor is not None:
        return _do(cursor)
    with get_db() as conn:
        return _do(conn.cursor())


def is_flag_enabled(flag_name: str) -> bool:
    """gate 读:env → DB。任何异常 fail-closed(返回 False,不放行)。"""
    if flag_name not in _FLAG_ENV:
        raise ValueError(f"未知 flag {flag_name}")
    env_v = _env_flag_value(flag_name)
    if env_v is not None:
        return env_v
    try:
        # 探测 epoch(捕捉跨 worker 变更);读 DB 存储值
        _read_policy_epoch_soft()
        cur_policy = get_policy()
        return bool(cur_policy["effective_flags"].get(flag_name, False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_flag_enabled(%s) 无法证明新鲜 → fail-closed(False): %s", flag_name, exc)
        return False


# ────────────────────────────── 写(CAS)──────────────────────────────
def update_policy(
    cur,
    *,
    expected_version: int,
    new_policy: dict,
    reason: str,
    request_id: str,
    operator_id: str,
    collection_readiness: Optional[dict] = None,
) -> int:
    """在调用方事务(cur)内 CAS 更新策略。返回新版本号。

    409: expected_version 不符 → PolicyVersionConflict。
    423: 试图修改被 env 覆盖且值不一致的 feature flag → PolicyEnvOverride。
    数据 + 版本 + 审计 + epoch bump 全部在同一事务(由调用方 get_db 提交/回滚)。
    """
    # 1) 强类型校验(extra=forbid + policy_schema.checks:权重合计=10000 等)
    validated = contracts.ObservationPolicyV1.model_validate(new_policy)
    new_flags = validated.feature_flags.model_dump()

    # 2) env 覆盖检测 → 423:被 env 锁定且请求值与 env 不一致的 flag(写了也不生效=误导)
    overrides = env_overridden_flags()
    conflict = [name for name, env_val in overrides.items() if new_flags.get(name) != env_val]
    if conflict:
        raise PolicyEnvOverride(conflict)

    # 3) CAS 锁行 + 版本核对
    cur.execute(
        "SELECT policy_version,policy_json,promotion_legal_basis,consent_policy_version,"
        "outcome_gold_gate_passed FROM public.geo_observation_policy "
        "WHERE singleton_id=1 FOR UPDATE"
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("geo_observation_policy 默认行缺失")
    current_version = int(row["policy_version"])
    before_json = row["policy_json"]
    if current_version != int(expected_version):
        raise PolicyVersionConflict(current_version)

    # Product is the final gate. Requiring readiness for the previously
    # committed policy version prevents one request from jumping directly from
    # all-false flags to a user-visible product.
    active_aggregate_policy_basis: Optional[str] = None
    if bool(new_flags.get("product_enabled", False)):
        from . import hmac_buckets
        from services.geo_observation_analytics.contract import (
            AGGREGATION_VERSION,
            CONTRACT_VERSION,
            METRIC_VERSION,
        )

        activation_problems: list[str] = []
        current_policy = contracts.ObservationPolicyV1.model_validate(before_json)
        current_policy_json = current_policy.model_dump(mode="json")
        current_flags = current_policy.feature_flags.model_dump()
        for flag in ("ingest_enabled", "promotion_enabled", "aggregation_enabled"):
            if not bool(new_flags.get(flag, False)):
                activation_problems.append(f"{flag} 尚未开启")
            if not bool(current_flags.get(flag, False)):
                activation_problems.append(f"{flag} 必须在此前已提交版本中开启")
        # Product activation is a final one-bit gate, not a vehicle for changing
        # metrics, platforms, retention or any other policy input underneath an
        # aggregate/readiness proof computed for the previous version.
        current_basis = deepcopy(current_policy_json)
        requested_basis = validated.model_dump(mode="json")
        current_basis["feature_flags"]["product_enabled"] = False
        requested_basis["feature_flags"]["product_enabled"] = False
        if requested_basis != current_basis:
            activation_problems.append(
                "product_enabled 开启时不得同时修改其他 policy;请先单独提交并重新聚合"
            )
        if not row["promotion_legal_basis"]:
            activation_problems.append("promotion_legal_basis 尚未批准")
        if not row["consent_policy_version"]:
            activation_problems.append("consent_policy_version 尚未批准")
        if not bool(row["outcome_gold_gate_passed"]):
            activation_problems.append("金标准门尚未通过")
        if not hmac_buckets.is_configured():
            activation_problems.append("HMAC bucket key 未配置")
        readiness = collection_readiness if isinstance(collection_readiness, dict) else {}
        if readiness.get("status") != "ready":
            activation_problems.append("采集 readiness 尚未 ready")
        if readiness.get("policy_version") != current_version:
            activation_problems.append("采集 readiness policy_version 不是当前已提交版本")
        active_aggregate_policy_basis = canonical_policy_basis(current_policy_json)
        activation_problems.extend(
            refresh_manifest_problems(
                cur,
                policy_basis_hash=active_aggregate_policy_basis,
                contract_version=CONTRACT_VERSION,
                aggregation_version=AGGREGATION_VERSION,
                metric_version=METRIC_VERSION,
                # Aggregate bucket windows are UTC in aggregates._window_utc;
                # activation must address the same current bucket.
                today=datetime.now(timezone.utc).date(),
            )
        )
        if activation_problems:
            raise ProductActivationBlocked(activation_problems)

    new_version = current_version + 1
    policy_out = validated.model_dump(mode="json")

    # 4) 更新数据 + 版本
    cur.execute(
        """
        UPDATE public.geo_observation_policy
           SET policy_json=%s, policy_version=%s, updated_by=%s, updated_reason=%s,
               last_request_id=%s, active_aggregate_policy_basis=%s, updated_at=NOW()
         WHERE singleton_id=1 AND policy_version=%s
        RETURNING policy_version
        """,
        (
            Json(policy_out), new_version, operator_id, reason, request_id,
            active_aggregate_policy_basis, current_version,
        ),
    )
    if cur.fetchone() is None:
        # 并发下 FOR UPDATE 后仍失守(极端)→ 视为冲突
        raise PolicyVersionConflict(current_version)

    # 5) 同事务审计(before/after/request_id;audit_event_key 幂等)
    write_audit(
        cur,
        action="policy_update",
        operator_type="admin",
        event_id=None,
        operator_id=operator_id,
        before={"policy_version": current_version, "policy_json": before_json},
        after={"policy_version": new_version, "policy_json": policy_out},
        reason_codes=[reason],
        request_id=request_id,
        idempotency_token=f"policy_v{new_version}",
    )

    # 6) 同事务 epoch bump(跨 worker 失效)
    bump_policy_epoch_cursor(cur)
    return new_version


def update_promotion_governance(
    cur,
    *,
    expected_version: int,
    promotion_legal_basis: Optional[str],
    consent_policy_version: Optional[str],
    reason: str,
    request_id: str,
    operator_id: str,
) -> int:
    """CAS 更新法务晋升治理(法务依据 + 同意版本)。与 policy_version 同一 CAS 序列化;同事务审计 + epoch bump。

    ⚠️ 金标准门(outcome_gold_gate_passed)不在此设置 —— 它由服务端从不可变评估记录派生(见 record_gold_evaluation),
       API/管理员无法直接提交可信布尔值(P1-1)。本函数只批法务可自主断言的依据/同意版本。
    """
    cur.execute(
        """SELECT policy_version, promotion_legal_basis, consent_policy_version
             FROM public.geo_observation_policy WHERE singleton_id=1 FOR UPDATE"""
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("geo_observation_policy 默认行缺失")
    current_version = int(row["policy_version"])
    if current_version != int(expected_version):
        raise PolicyVersionConflict(current_version)
    new_version = current_version + 1
    before = {k: row[k] for k in ("promotion_legal_basis", "consent_policy_version")}
    cur.execute(
        """UPDATE public.geo_observation_policy
              SET promotion_legal_basis=%s, consent_policy_version=%s, policy_version=%s,
                  updated_by=%s, updated_reason=%s, last_request_id=%s, updated_at=NOW()
            WHERE singleton_id=1 AND policy_version=%s
           RETURNING policy_version""",
        (promotion_legal_basis, consent_policy_version, new_version,
         operator_id, reason, request_id, current_version),
    )
    if cur.fetchone() is None:
        raise PolicyVersionConflict(current_version)
    write_audit(
        cur, action="promotion_governance_update", operator_type="admin", event_id=None, operator_id=operator_id,
        before=before,
        after={"promotion_legal_basis": promotion_legal_basis, "consent_policy_version": consent_policy_version},
        reason_codes=[reason], request_id=request_id, idempotency_token=f"governance_v{new_version}",
    )
    bump_policy_epoch_cursor(cur)
    return new_version


def record_gold_evaluation(
    cur,
    *,
    expected_version: int,
    dataset_version: str,
    sample_count: int,
    macro_f1_bps: int,
    high_risk_false_reco: int,
    report_hash: str,
    reason: str,
    request_id: str,
    operator_id: str,
) -> dict:
    """记录一次不可变金标准评估,并**服务端派生** outcome_gold_gate_passed(P1-1)。

    - 证据写 append-only geo_observation_gold_eval(不可变;幂等 (dataset_version, report_hash))。
    - gate_passed 由 compute_gold_gate_passed 按契约 §601 阈值计算,**不接受调用方直接给布尔值**。
    - policy 派生列(gate + 证据快照)与 policy_version 同一 CAS 序列化;DB CHECK ck_geo_obs_policy_gold_gate 兜底
      (即便代码算错,无证据也无法把 gate 写成 true)。返回 {policy_version, gate_passed}。
    """
    if not dataset_version or not report_hash:
        raise ValueError("dataset_version 和 report_hash 必填(不可变评估记录标识)")
    if sample_count is None or sample_count < 0:
        raise ValueError("sample_count 必须 >=0")
    if macro_f1_bps is None or not (0 <= int(macro_f1_bps) <= 10000):
        raise ValueError("macro_f1_bps 必须 0..10000")
    if high_risk_false_reco is None or high_risk_false_reco < 0:
        raise ValueError("high_risk_false_reco 必须 >=0")

    cur.execute(
        """SELECT policy_version, gold_dataset_version, gold_report_hash, outcome_gold_gate_passed
             FROM public.geo_observation_policy WHERE singleton_id=1 FOR UPDATE"""
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("geo_observation_policy 默认行缺失")
    current_version = int(row["policy_version"])
    if current_version != int(expected_version):
        raise PolicyVersionConflict(current_version)

    req_gate = compute_gold_gate_passed(
        sample_count=sample_count, macro_f1_bps=macro_f1_bps, high_risk_false_reco=high_risk_false_reco,
        dataset_version=dataset_version, report_hash=report_hash)

    # 不可变评估记录:先试插;冲突则锁既有行,全字段一致=幂等,不一致=拒绝(同证据身份绝不改写结论)。
    #   report_hash 由调用方给,不能假设"同 hash=同证据":必须核对 → 防"先失败后通过复用同 hash 把 policy 刷成 true"。
    cur.execute(
        """INSERT INTO public.geo_observation_gold_eval
               (dataset_version, sample_count, macro_f1_bps, high_risk_false_reco, report_hash,
                gate_passed, created_by, created_reason, request_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (dataset_version, report_hash) DO NOTHING
           RETURNING sample_count, macro_f1_bps, high_risk_false_reco, gate_passed""",
        (dataset_version, int(sample_count), int(macro_f1_bps), int(high_risk_false_reco), report_hash,
         req_gate, operator_id, reason, request_id),
    )
    inserted = cur.fetchone()
    if inserted is None:
        # 冲突:锁既有不可变行核对;字段全等才幂等,任一不等 = 拒绝(不更新 policy)
        cur.execute(
            """SELECT sample_count, macro_f1_bps, high_risk_false_reco, gate_passed
                 FROM public.geo_observation_gold_eval WHERE dataset_version=%s AND report_hash=%s FOR SHARE""",
            (dataset_version, report_hash),
        )
        eval_row = cur.fetchone()
        if eval_row is None:
            raise RuntimeError("gold_eval 冲突后既有行读取失败")
        if (int(eval_row["sample_count"]), int(eval_row["macro_f1_bps"]), int(eval_row["high_risk_false_reco"])) \
                != (int(sample_count), int(macro_f1_bps), int(high_risk_false_reco)):
            raise GoldEvaluationConflict(dataset_version, report_hash)
    else:
        eval_row = inserted

    # policy 从已持久化的不可变评估行派生(非请求参数)——即便请求指标与既存不同也已在上面拒绝
    persisted_gate = bool(eval_row["gate_passed"])
    # 真幂等:policy 已精确反映本次评估(同 dataset/report_hash + 同派生 gate)→ 零额外写(不 bump version/不审计/不 bump epoch)。
    #   防"响应丢失后用最新版本重试"再次推进版本/写审计/翻 epoch(P2)。
    if (row["gold_dataset_version"] == dataset_version and row["gold_report_hash"] == report_hash
            and bool(row["outcome_gold_gate_passed"]) == persisted_gate):
        return {"policy_version": current_version, "gate_passed": persisted_gate}
    new_version = current_version + 1
    cur.execute(
        """UPDATE public.geo_observation_policy
              SET outcome_gold_gate_passed=%s, gold_dataset_version=%s, gold_macro_f1_bps=%s,
                  gold_sample_count=%s, gold_high_risk_false_reco=%s, gold_report_hash=%s,
                  policy_version=%s, updated_by=%s, updated_reason=%s, last_request_id=%s, updated_at=NOW()
            WHERE singleton_id=1 AND policy_version=%s
           RETURNING policy_version""",
        (persisted_gate, dataset_version, int(eval_row["macro_f1_bps"]), int(eval_row["sample_count"]),
         int(eval_row["high_risk_false_reco"]), report_hash,
         new_version, operator_id, reason, request_id, current_version),
    )
    if cur.fetchone() is None:
        raise PolicyVersionConflict(current_version)
    write_audit(
        cur, action="gold_evaluation_record", operator_type="admin", event_id=None, operator_id=operator_id,
        before={"policy_version": current_version},
        after={"policy_version": new_version, "gate_passed": persisted_gate, "dataset_version": dataset_version,
               "sample_count": int(eval_row["sample_count"]), "macro_f1_bps": int(eval_row["macro_f1_bps"]),
               "high_risk_false_reco": int(eval_row["high_risk_false_reco"]), "report_hash": report_hash},
        reason_codes=[reason], request_id=request_id, idempotency_token=f"gold_eval_v{new_version}",
    )
    bump_policy_epoch_cursor(cur)
    return {"policy_version": new_version, "gate_passed": persisted_gate}
