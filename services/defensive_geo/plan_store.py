"""问题计划与运行预览的持久化 + **应用层**不可变守卫。

两层守卫,分工写清楚
--------------------
**DB 层是承重的**:``trg_defgeo_qplan_immutable`` / ``trg_defgeo_preview_immutable``
挡住一切来源 —— 我们的代码、psql 手改、别的包顺手写、以后某个 ORM 的 upsert。

**应用层是这一层**:它只做一件 DB 做不到的事 —— **把拒绝翻译成人话**,
并在写之前就挡住,免得每次都靠 DB 抛异常来发现问题。
它 **不是** DB 那层的替代品;判据对两层各自注毒,证明**拆掉应用层这层 DB 仍然挡得住**。

🔴 归属**每请求现做**(Review-CTO 附带条件):
   本模块所有读写都要求调用方传 ``tenant_owner_user_id``,并把它写进 WHERE。
   两张表**刻意不加 FK**,归属不能靠 FK 假装做过。跨租户读一律返回 None
   (调用方转同形 404 —— 不泄露对象存在性,ACT-02)。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from services.defensive_geo.question_plan import PlannedQuestion


class ImmutableViolation(RuntimeError):
    """试图原地改已签发对象。应用层先挡;DB trigger 是最后一道且是承重那道。"""


class PlanNotFound(LookupError):
    """找不到,或**不属于该租户**。两者返回同一个异常 —— 调用方转同形 404。

    刻意不区分:区分了就等于告诉调用方「这个 id 存在,只是不归你」,
    那是 ACT-02 点名的对象存在性泄露。
    """


def uuid_or_absent(value, what: str) -> str:
    """把**形状不合法的 id** 当成「不存在」,而不是让它撞进 SQL(P1-1 返修③)。

    🔴 被测缺陷是一个可被任何登录用户打出的裸 500:
       ``plan_id`` / ``preview_id`` 在库里是 ``uuid`` 列。传 ``not-a-uuid``
       进去,psycopg2 抛 ``InvalidTextRepresentation`` —— 没人接,
       FastAPI 吐一段裸文本 ``Internal Server Error``(连 JSON 都不是)。
       她看到的是"系统坏了",于是重试;而真相只是她手里那个链接抄错了一位。

    🔴 为什么翻成 ``PlanNotFound`` 而不是「参数错误」:
       调用方对 ``PlanNotFound`` 的既定处置就是**同形 404**(跨租户与不存在
       共用的那一个)。形状不合法的 id 和不存在的 id 在客户那一面本来就是
       同一件事;分开反而多出一个可以用来判断"这个 id 存不存在"的差异面。

    🔴 为什么放在 store 层而不是每个端点:
       id 只从这里进 SQL。放这里是一处;放端点是四处,而漏掉的那一处
       不会有任何判据变红 —— 那正是这个缺陷第一次出现的方式。
    """
    text = str(value or "").strip()
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        raise PlanNotFound(f"{what} 形状不合法:{text[:40]!r}") from None


# ══════════════════════════════════════════════════════════════════════════
# question plans
# ══════════════════════════════════════════════════════════════════════════
def insert_plan_revision(
    cur,
    *,
    plan_id: str,
    plan_revision: int,
    tenant_owner_user_id: int,
    brand_id: int,
    profile_revision_id: str,
    mode: str,
    question_set_version: str,
    canonical_hash: str,
    questions: tuple[PlannedQuestion, ...],
    defensive_count: int,
    offensive_count: int,
    total_count: int,
    client_request_id: str,
    request_content_hash: str,
    expires_at,
    created_by_user_id: int,
) -> dict[str, Any]:
    """写入一个新 revision。同 (tenant, client_request_id, canonical_hash) 重放返回原行。

    幂等靠 DB 唯一约束 ``uq_defgeo_qplan_idempotency`` + ``ON CONFLICT DO NOTHING``,
    **不是**靠先 SELECT 再 INSERT —— 后者在并发下必然双写
    (两个请求同时 SELECT 到空)。
    """
    payload = {
        "questions": [
            {
                "question_identity_key": q.question_identity_key,
                "question_revision": q.question_revision,
                "global_ordinal": q.global_ordinal,
                "text": q.text,
                "mode_side": q.mode_side,
                "family_key": q.family_key,
                "brand_exposure": q.brand_exposure,
                "origin": q.origin,
                "classifier_version": q.classifier_version,
            }
            for q in sorted(questions, key=lambda x: x.global_ordinal)
        ]
    }
    cur.execute(
        """
        INSERT INTO defgeo_question_plans
            (plan_id, plan_revision, tenant_owner_user_id, brand_id, profile_revision_id,
             mode, question_set_version, canonical_hash, frozen_payload,
             defensive_count, offensive_count, total_count,
             client_request_id, request_content_hash, expires_at, created_by_user_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_owner_user_id, client_request_id, request_content_hash) DO NOTHING
        RETURNING *
        """,
        (plan_id, plan_revision, tenant_owner_user_id, brand_id, profile_revision_id,
         mode, question_set_version, canonical_hash,
         json.dumps(payload, ensure_ascii=False, sort_keys=True),
         defensive_count, offensive_count, total_count,
         client_request_id, request_content_hash, expires_at, created_by_user_id),
    )
    row = cur.fetchone()
    if row is not None:
        return dict(row)

    # 0 行 = 幂等命中。取回原行 —— 并且**只在同租户内取**。
    cur.execute(
        """
        SELECT * FROM defgeo_question_plans
         WHERE tenant_owner_user_id=%s AND client_request_id=%s AND request_content_hash=%s
        """,
        (tenant_owner_user_id, client_request_id, request_content_hash),
    )
    existing = cur.fetchone()
    if existing is None:
        # 唯一约束命中但同租户查不到 ⇒ 只可能是别人的行占了 key。
        # 绝不返回它,也绝不假装成功。
        raise PlanNotFound("幂等键冲突但归属不符")
    return dict(existing)


def get_plan_exact_revision(
    cur, *, plan_id: str, plan_revision: int, tenant_owner_user_id: int
) -> dict[str, Any]:
    """按 **exact revision** 读(REV-11)。

    🔴 没有「不传 revision 就给 latest」这条路 —— 函数签名里 revision 是必填。
       静默升 latest 会让旧报告 CTA 把旧报告绑到新题单上(§19 变异 84)。
    🔴 归属进 WHERE,不是查出来再比 —— 查出来再比的代码,漏一个 return 就泄露。
    """
    cur.execute(
        """
        SELECT * FROM defgeo_question_plans
         WHERE plan_id=%s AND plan_revision=%s AND tenant_owner_user_id=%s
        """,
        (uuid_or_absent(plan_id, "plan_id"), plan_revision, tenant_owner_user_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PlanNotFound(f"plan {plan_id} rev {plan_revision}")
    return dict(row)


def latest_revision_number(cur, *, plan_id: str, tenant_owner_user_id: int) -> int | None:
    """只用于告诉调用方「还有更新的版本」,**不**用于替换请求的那一版。"""
    cur.execute(
        "SELECT MAX(plan_revision) AS m FROM defgeo_question_plans "
        "WHERE plan_id=%s AND tenant_owner_user_id=%s",
        (uuid_or_absent(plan_id, "plan_id"), tenant_owner_user_id),
    )
    row = cur.fetchone()
    return row["m"] if row and row["m"] is not None else None


def guard_no_inplace_update(cur, *, plan_id: str, plan_revision: int) -> None:
    """应用层守卫:调用方若想「改一版」,这里当场拒并告诉它正确做法。

    ⚠️ 这层拆掉了,DB trigger 仍然会拒 —— 判据 test_db_trigger_is_load_bearing
    专门证明这一点。留这层只为把错误变成人话,不是为了承重。
    """
    raise ImmutableViolation(
        f"已签发 revision 不可原地修改(REV-01):plan_id={plan_id} revision={plan_revision}。"
        "改题请调用 apply_edit_revision 生成 superseding revision —— "
        "身份键不变、question_revision 递增、旧结果不回写。"
    )


# ══════════════════════════════════════════════════════════════════════════
# run previews
# ══════════════════════════════════════════════════════════════════════════
def insert_run_preview(
    cur,
    *,
    preview_id: str,
    tenant_owner_user_id: int,
    brand_id: int,
    created_by_user_id: int,
    question_plan_id: str,
    question_plan_revision: int,
    question_plan_hash: str,
    profile_revision_id: str,
    campaign_mode: str,
    frozen_payload: dict,
    canonical_hash: str,
    feature_code: str,
    pricing_catalog_version: str,
    base_points: int,
    extra_points: int,
    exact_total_points: int,
    funding_policy: str,
    principal_kind: str,
    sponsor_policy_ref: str | None,
    approval_requirement: str,
    planned_cells: int,
    idempotency_key: str,
    canonical_request_hash: str,
    expires_at,
) -> tuple[dict[str, Any], bool]:
    """写 preview。返回 (行, replayed)。

    ``replayed=True`` 表示这是幂等重放,**不是**新建。
    调用方必须用它决定「要不要再冻结一次钱」—— 本仓 2026-08-xx 记过
    「资金调用返 success ≠ 钱按你要的方向动了」,幂等返回等于「不用我动了」。
    """
    cur.execute(
        """
        INSERT INTO defgeo_diagnosis_run_previews
            (preview_id, tenant_owner_user_id, brand_id, created_by_user_id,
             question_plan_id, question_plan_revision, question_plan_hash, profile_revision_id,
             campaign_mode, frozen_payload, canonical_hash,
             feature_code, pricing_catalog_version, base_points, extra_points, exact_total_points,
             funding_policy, principal_kind, sponsor_policy_ref, approval_requirement,
             planned_cells, lifecycle, idempotency_key, canonical_request_hash, expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)
        ON CONFLICT (tenant_owner_user_id, idempotency_key, canonical_request_hash) DO NOTHING
        RETURNING *
        """,
        (preview_id, tenant_owner_user_id, brand_id, created_by_user_id,
         question_plan_id, question_plan_revision, question_plan_hash, profile_revision_id,
         campaign_mode, json.dumps(frozen_payload, ensure_ascii=False, sort_keys=True), canonical_hash,
         feature_code, pricing_catalog_version, base_points, extra_points, exact_total_points,
         funding_policy, principal_kind, sponsor_policy_ref, approval_requirement,
         planned_cells, idempotency_key, canonical_request_hash, expires_at),
    )
    row = cur.fetchone()
    if row is not None:
        return dict(row), False

    cur.execute(
        """
        SELECT * FROM defgeo_diagnosis_run_previews
         WHERE tenant_owner_user_id=%s AND idempotency_key=%s AND canonical_request_hash=%s
        """,
        (tenant_owner_user_id, idempotency_key, canonical_request_hash),
    )
    existing = cur.fetchone()
    if existing is None:
        raise PlanNotFound("preview 幂等键冲突但归属不符")
    return dict(existing), True


def get_run_preview(cur, *, preview_id: str, tenant_owner_user_id: int) -> dict[str, Any]:
    """exact GET。归属进 WHERE;跨租户与不存在同形 ``PlanNotFound``。"""
    cur.execute(
        "SELECT * FROM defgeo_diagnosis_run_previews WHERE preview_id=%s AND tenant_owner_user_id=%s",
        (uuid_or_absent(preview_id, "preview_id"), tenant_owner_user_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PlanNotFound(f"preview {preview_id}")
    return dict(row)


def mark_preview_consumed(
    cur, *, preview_id: str, tenant_owner_user_id: int, command_id: str
) -> bool:
    """open → consumed 的**唯一**写入口。CAS 语义:只有还 open 时才成功。

    返回 False = 这份 preview 已经不是 open 了(别人先 confirm 了,或已过期)。
    调用方必须据此重放原 command,**不得**新建第二个
    (§19 变异 96「consumed preview 同 root/hash 新建第二 command」)。

    🔴 用 ``WHERE lifecycle='open'`` 做 CAS,不是先读再写 ——
       先读再写在 20 并发下必然多个 winner(DIA-FIN-02 要求恰一)。
    """
    cur.execute(
        """
        UPDATE defgeo_diagnosis_run_previews
           SET lifecycle='consumed', consumed_command_id=%s, consumed_at=NOW()
         WHERE preview_id=%s AND tenant_owner_user_id=%s AND lifecycle='open'
        RETURNING preview_id
        """,
        (command_id, uuid_or_absent(preview_id, "preview_id"), tenant_owner_user_id),
    )
    return cur.fetchone() is not None


def new_preview_id() -> str:
    return str(uuid.uuid4())
