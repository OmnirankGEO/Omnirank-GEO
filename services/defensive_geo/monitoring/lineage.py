"""§6.1 冻结测量单元 —— 六个可核验边界的身份。

规格 §6.1 逐字给了两条公式:

    plan_cell_id = SHA256(
      run_authority_id / tenant_owner_id
    x service_projection_id-or-null / service_projection_version-or-null
    x plan_item_key-or-null
    x question_set_revision / question_key / question_revision / family
    x public_platform / planned_surface / search_mode
    x scheduled_window-or-null / run_index / route_plan_revision )

    attempt_id = SHA256(
      plan_cell_id / attempt_ordinal
    x actual_provider / actual_model / actual_model_revision
    x actual_surface / actual_search_mode / request_hash )

🔴 公式里的 ``question_key`` 是**本规格的稳定逻辑身份键**,不是现役
``geo_article_target_question_snapshots.question_key``(那个是内容哈希 +
全局 UNIQUE,改题文就变)。§0.5.1 第 7 条明令去混淆,窗A 已落地为
``services.defensive_geo.question_plan.question_identity_key``。本模块的入参名
逐字叫 ``question_identity_key``,**永不**叫 ``question_key`` —— 名字撞了,
迟早有人把两个 namespace 的值互相传。:func:`assert_not_legacy_question_key`
把这条写成运行时门。

🔴 三条会让身份静默塌掉的坑,逐条写成拒绝
----------------------------------------
1. **nullable 字段必须显式编码 null**(§6.1 逐字「其它 nullable 字段也显式
   编码为 null」)。若用 ``str(None)`` 或空串代替,``service_projection_id=None``
   与 ``service_projection_id=""`` 会撞成同一个 cell —— 报价前诊断与已激活服务
   的格就混了。本模块用 JCS ``null`` 编码,并且**拒绝空串**冒充 null。
2. **分隔符注入**。``a|b`` 与 ``a`` + ``|b`` 拼出同一条串。本模块不用分隔符
   拼接,走窗B 已实现的 ``_frame``(big-endian uint64 长度前缀),
   长度前缀让任何内容都无法伪造边界。
3. **字段漏一个 hash 照样算得出来**。所以两条公式的字段名各自冻结成
   :data:`PLAN_CELL_FIELDS` / :data:`ATTEMPT_FIELDS`,构造时逐字段核对
   **多一个少一个都抛**;判据拿这两个元组当分母机械遍历,不手抄。

🔴 ``observation_cell_id == attempt_id``(§6.1 逐字,MON-09 逐值验)
------------------------------------------------------------------
「每个 attempt 最多接纳一份 canonical raw response,因此
``observation_cell_id == attempt_id``」。所以这里**不另造**第三个 id 函数,
:func:`observation_cell_id` 是 :func:`attempt_id` 的显式别名 —— 写成别名而不是
"约定它们相等",是因为约定不会在有人改了其中一个时报错。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, NamedTuple

from services.defensive_geo.presentation.commitments import _canonical, _frame, _jcs_dumps

#: 身份算法版本。改任何一位入 hash 的字段/编码都必须升版。
LINEAGE_IDENTITY_VERSION = "defgeo-lineage-identity-v1"

#: domain separation —— plan cell 与 attempt 用不同 domain,
#: 于是"把 attempt_id 当 plan_cell_id 用"永远算不出相等。
_DOMAIN_PLAN_CELL = b"defgeo/plan-cell-id/v1"
_DOMAIN_ATTEMPT = b"defgeo/attempt-id/v1"
_DOMAIN_PROJECTION = b"defgeo/projection-id/v1"

#: §6.1 公式逐字的 plan cell 字段全集。**顺序即公式顺序**。
PLAN_CELL_FIELDS: tuple[str, ...] = (
    "run_authority_id",
    "tenant_owner_id",
    "service_projection_id",
    "service_projection_version",
    "plan_item_key",
    "question_set_revision",
    "question_identity_key",
    "question_revision",
    "family",
    "public_platform",
    "planned_surface",
    "search_mode",
    "scheduled_window",
    "run_index",
    "route_plan_revision",
)

#: 允许为 null 的 plan cell 字段(§6.1 逐字点名的四个)。其余**不许** null。
PLAN_CELL_NULLABLE: frozenset[str] = frozenset({
    "service_projection_id",
    "service_projection_version",
    "plan_item_key",
    "scheduled_window",
})

#: §6.1 公式逐字的 attempt 字段全集。
ATTEMPT_FIELDS: tuple[str, ...] = (
    "plan_cell_id",
    "attempt_ordinal",
    "actual_provider",
    "actual_model",
    "actual_model_revision",
    "actual_surface",
    "actual_search_mode",
    "request_hash",
)

#: attempt 里唯一允许 null 的:模型版本平台确实不给时(现役
#: ``monitoring_results.model_revision_unknown_reason`` 就是为这个存在的)。
ATTEMPT_NULLABLE: frozenset[str] = frozenset({"actual_model_revision"})

#: §6.1「分类升级创建 projection_id」的三个 version 字段。
PROJECTION_FIELDS: tuple[str, ...] = (
    "observation_cell_id",
    "resolver_version",
    "classifier_version",
    "evidence_extractor_version",
)

#: §6.2 六个可核验边界。**每一边界都必须有 stable ID、版本和 lineage,
#: 且第六步可从前五步重建**。判据拿它当分母。
BOUNDARY_KEYS: tuple[str, ...] = (
    "frozen_question_and_platform_matrix",
    "schedule_and_request",
    "raw_answer_and_error",
    "entity_mention_recommendation_classification",
    "source_consistency_attribution",
    "aggregate_snapshot",
)

#: 现役 ``question_identity_key`` 的字面形态(窗A:``q_`` + 32 位 hex)。
#: 现役 ``geo_article_target_question_snapshots.question_key`` 是**裸 64 位 hex**。
#: 两者形态不同 —— 于是"传错了 namespace"可以在构造期就被抓住,
#: 而不是等到两个报告的格对不上才发现。
_IDENTITY_KEY_SHAPE = re.compile(r"^q_[0-9a-f]{32}$")
_LEGACY_QUESTION_KEY_SHAPE = re.compile(r"^[0-9a-f]{64}$")


class LineageIdentityError(ValueError):
    """身份输入不合法。**不签发** id,而不是算一个出来先用着。"""


def assert_not_legacy_question_key(value: str) -> str:
    """§0.5.1 第 7 条的运行时门:拒绝把现役 ``question_key`` 当身份键传进来。

    两个 namespace 同名异义。名字撞了就一定会有人传错,靠"文档写了别传"
    拦不住 —— 只有构造期抛异常拦得住。
    """
    if not isinstance(value, str) or not value:
        raise LineageIdentityError("question_identity_key 必须是非空字符串")
    if _LEGACY_QUESTION_KEY_SHAPE.match(value):
        raise LineageIdentityError(
            f"{value!r} 是现役 geo_article_target_question_snapshots.question_key "
            "的形态(裸 64 位 hex 内容哈希),不是本规格的稳定逻辑身份键。"
            "请传 services.defensive_geo.question_plan.question_identity_key() 的产出"
            "(q_ + 32 位 hex)。§0.5.1 第 7 条。"
        )
    if not _IDENTITY_KEY_SHAPE.match(value):
        raise LineageIdentityError(
            f"{value!r} 不是合法 question_identity_key(应为 q_ + 32 位小写 hex)"
        )
    return value


def _require_fields(
    payload: Mapping[str, Any],
    fields: tuple[str, ...],
    nullable: frozenset[str],
    *,
    what: str,
) -> dict[str, Any]:
    """逐字段核对:多一个少一个都抛;不可空字段拒绝 None **和空串**。

    🔴 为什么连空串一起拒:``""`` 与 ``None`` 在 JCS 里是两个不同的值,
       但在"人以为自己没传"这件事上是同一个错误。允许空串等于允许
       ``service_projection_id=""`` 与 ``null`` 各占一个 cell 身份。
    """
    missing = [f for f in fields if f not in payload]
    if missing:
        raise LineageIdentityError(f"{what} 缺字段 {missing}(§6.1 公式逐字,不可省)")
    extra = [k for k in payload if k not in fields]
    if extra:
        raise LineageIdentityError(
            f"{what} 出现公式外字段 {extra} —— 多喂一个字段就换一个身份,"
            "而判据的分母只认公式里那些"
        )
    out: dict[str, Any] = {}
    for f in fields:
        v = payload[f]
        if v is None:
            if f not in nullable:
                raise LineageIdentityError(f"{what}.{f} 不允许为 null")
            out[f] = None
            continue
        if isinstance(v, str):
            if not v:
                raise LineageIdentityError(
                    f"{what}.{f} 是空串。空串不是 null —— 要表达"
                    "「本次没有这一格」请显式传 None(§6.1「显式编码为 null」)"
                )
            out[f] = v
            continue
        if isinstance(v, bool):
            raise LineageIdentityError(f"{what}.{f} 不接受布尔")
        if isinstance(v, int):
            out[f] = v
            continue
        raise LineageIdentityError(
            f"{what}.{f} 类型 {type(v).__name__} 不接受(只收 str / int / None)"
        )
    return out


def _digest(domain: bytes, payload: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    """长度前缀 framing + JCS,再 SHA256。

    framing 覆盖 **domain / 版本 / 每一个字段值**,所以任何内容都伪造不出
    别人的边界(``a|b`` vs ``a`` + ``|b`` 那类拼接歧义在这里不存在)。
    """
    parts: list[bytes] = [domain, LINEAGE_IDENTITY_VERSION.encode("utf-8")]
    for f in fields:
        parts.append(f.encode("utf-8"))
        parts.append(_jcs_dumps(_canonical(payload[f])))
    return hashlib.sha256(_frame(*parts)).hexdigest()


class PlanCell(NamedTuple):
    """一个**计划**格。planned denominator 的计量单位就是它。"""

    plan_cell_id: str
    fields: dict[str, Any]


class Attempt(NamedTuple):
    """一次**真实**调用。fallback / retry 各自是独立 attempt,不覆盖前一次。"""

    attempt_id: str
    plan_cell_id: str
    attempt_ordinal: int
    fields: dict[str, Any]

    @property
    def observation_cell_id(self) -> str:
        """§6.1 逐字:``observation_cell_id == attempt_id``。"""
        return self.attempt_id


def plan_cell_id(**fields: Any) -> str:
    """§6.1 第一条公式。返回 64 位 hex。"""
    payload = _require_fields(fields, PLAN_CELL_FIELDS, PLAN_CELL_NULLABLE,
                              what="plan_cell")
    assert_not_legacy_question_key(str(payload["question_identity_key"]))
    return _digest(_DOMAIN_PLAN_CELL, payload, PLAN_CELL_FIELDS)


def build_plan_cell(**fields: Any) -> PlanCell:
    payload = _require_fields(fields, PLAN_CELL_FIELDS, PLAN_CELL_NULLABLE,
                              what="plan_cell")
    assert_not_legacy_question_key(str(payload["question_identity_key"]))
    return PlanCell(
        plan_cell_id=_digest(_DOMAIN_PLAN_CELL, payload, PLAN_CELL_FIELDS),
        fields=payload,
    )


def attempt_id(**fields: Any) -> str:
    """§6.1 第二条公式。返回 64 位 hex。

    🔴 ``attempt_ordinal`` 从 **1** 起且在同一 plan cell 内严格递增 ——
       它是"第几次尝试",不是"重试了几次"。第一次调用就是 1,
       所以"从没被尝试过"与"尝试过一次"永远不会撞成同一个身份。
    """
    payload = _require_fields(fields, ATTEMPT_FIELDS, ATTEMPT_NULLABLE,
                              what="attempt")
    ordinal = payload["attempt_ordinal"]
    if not isinstance(ordinal, int) or ordinal < 1:
        raise LineageIdentityError(f"attempt_ordinal 必须是 >=1 的整数,收到 {ordinal!r}")
    return _digest(_DOMAIN_ATTEMPT, payload, ATTEMPT_FIELDS)


def build_attempt(**fields: Any) -> Attempt:
    payload = _require_fields(fields, ATTEMPT_FIELDS, ATTEMPT_NULLABLE,
                              what="attempt")
    ordinal = payload["attempt_ordinal"]
    if not isinstance(ordinal, int) or ordinal < 1:
        raise LineageIdentityError(f"attempt_ordinal 必须是 >=1 的整数,收到 {ordinal!r}")
    return Attempt(
        attempt_id=_digest(_DOMAIN_ATTEMPT, payload, ATTEMPT_FIELDS),
        plan_cell_id=str(payload["plan_cell_id"]),
        attempt_ordinal=ordinal,
        fields=payload,
    )


def observation_cell_id(**fields: Any) -> str:
    """§6.1:``observation_cell_id == attempt_id``。

    写成**别名**而不是写成注释里的约定 —— 约定不会在有人改了其中一边时报错。
    """
    return attempt_id(**fields)


def projection_id(**fields: Any) -> str:
    """§6.1:「分类升级创建 projection_id……**不改 raw observation**」。

    MON-04 承重点:classifier 升级 ⇒ 新 projection_id ⇒ 历史 outcome 不动。
    """
    payload = _require_fields(fields, PROJECTION_FIELDS, frozenset(),
                              what="projection")
    return _digest(_DOMAIN_PROJECTION, payload, PROJECTION_FIELDS)


def equivalent_fallback(
    planned_surface: str,
    planned_platform: str,
    actual_surface: str,
    actual_platform: str,
) -> bool:
    """§6.1:「如果 fallback 改变了对用户有意义的 public platform/surface,
    则**不能**视为等价 fallback,必须签新 plan revision」。

    返回 ``True`` = 可以挂在原 plan cell 下当 child attempt;
    ``False`` = 调用方必须签新 plan revision,**不许**偷偷挂进来。
    """
    return planned_surface == actual_surface and planned_platform == actual_platform


def assert_equivalent_fallback(
    *,
    planned_surface: str,
    planned_platform: str,
    actual_surface: str,
    actual_platform: str,
) -> None:
    if not equivalent_fallback(planned_surface, planned_platform,
                               actual_surface, actual_platform):
        raise LineageIdentityError(
            f"fallback 把公开平台/表面从 ({planned_platform},{planned_surface}) "
            f"改成了 ({actual_platform},{actual_surface}) —— 对用户有意义的维度变了,"
            "不是等价 fallback。必须签新 plan revision,不能挂在原 plan cell 下"
            "(§6.1)。"
        )


def census() -> dict[str, Any]:
    """机械导出本模块冻结的分母(G-2:手写枚举不算分母)。"""
    return {
        "identityVersion": LINEAGE_IDENTITY_VERSION,
        "planCellFields": list(PLAN_CELL_FIELDS),
        "planCellNullable": sorted(PLAN_CELL_NULLABLE),
        "attemptFields": list(ATTEMPT_FIELDS),
        "attemptNullable": sorted(ATTEMPT_NULLABLE),
        "projectionFields": list(PROJECTION_FIELDS),
        "boundaries": list(BOUNDARY_KEYS),
    }
