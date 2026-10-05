"""客户受理证据(工单 E3-4 · P1-8 · Codex 二审「accepted 审计不接受原样延期」)。

要回答的那句话
--------------
「这一笔激活,是**谁**、在**什么授权下**、对**哪一份请求**点的确认?」

在本单之前,``keyword_selection_sessions`` 上落的只有三件:
``customer_confirmed_snapshot_id`` / ``_hash`` / ``_at``。它们回答的是
"她接受了哪份报价快照",不回答上面那句。而 043 建表时就留好的
``customer_confirmed_token_purpose`` / ``_actor`` / ``_request_hash``
**全仓零写入方、零读取方** —— 三列一直是死列。

不可重建性是这件事的全部要害
----------------------------
snapshot id / hash 事后能重查;canonical request 的**正文**、
token 的 purpose / subject、点确认的 actor —— 这几样一旦当时没落,
事后**没有任何地方**能把它们算出来。所以它们必须在确认那一刻同事务落库。

🔴 存量行怎么办:标 ``legacy_unproven``,**禁伪造回填**
------------------------------------------------------
存量已确认行有三件组、没有审计列。把当前值回填进去就是**伪造受理证据** ——
那比没有证据更坏:没有证据时我们知道自己不知道;伪造之后我们以为自己知道。
所以处置是**读时判定**:见 :func:`classify_acceptance`。

🔴 actor 为什么不是 user id
---------------------------
``confirm_quote`` 是 **token-only** 端点(C 端客户不登录,§用户定位铁律),
它的签名里根本没有 fastapi ``Request``,也就没有登录身份可取。
硬造一个 user id 会是编造。这里如实记录**令牌持有者**这个主体:
``customer_token:<token 指纹>`` —— 它是这次受理真实的、可核对的行为主体。
(要把它变成一个真实自然人身份,得改 token-only 的对外合同,那是 Owner 的决定,
 不是本单能顺手做的。)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

ACCEPTANCE_EVIDENCE_VERSION = "defgeo-acceptance-evidence-v1"

#: 这个 token 在这一刻被用来做什么。落库的是**行为**,不是端点名。
TOKEN_PURPOSE_CONFIRM = "keyword_selection_confirm"

#: actor 的主体类型前缀。token-only 场景下的真实行为主体是令牌持有者。
ACTOR_KIND_CUSTOMER_TOKEN = "customer_token"

#: :func:`classify_acceptance` 的三档。
EVIDENCE_ABSENT = "absent"                 #: 还没确认过 —— 三件组全空。
EVIDENCE_PROVEN = "proven"                 #: 受理证据齐全,可据以激活。
EVIDENCE_LEGACY_UNPROVEN = "legacy_unproven"  #: 有指针、缺证据(存量行)。

#: 一条"证据齐全"的受理必须同时具备的列。判据拿它当**分母**机械遍历,
#: 不手抄 —— 手抄的那份漏掉哪一列都不会让任何判据变红(本仓记过)。
REQUIRED_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "customer_confirmed_snapshot_id",
    "customer_confirmed_snapshot_hash",
    "customer_confirmed_at",
    "customer_confirmed_token_purpose",
    "customer_confirmed_token_subject",
    "customer_confirmed_actor",
    "customer_confirmed_request_hash",
    "customer_confirmed_request",
)

#: 三件组(042 的 group_complete CHECK 管的那三列)。
POINTER_COLUMNS: tuple[str, ...] = (
    "customer_confirmed_snapshot_id",
    "customer_confirmed_snapshot_hash",
    "customer_confirmed_at",
)


def token_fingerprint(token: str) -> str:
    """令牌指纹。**不落原文** —— 审计列不是放密钥的地方。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:32]


def actor_of(token: str) -> str:
    return f"{ACTOR_KIND_CUSTOMER_TOKEN}:{token_fingerprint(token)}"


def token_subject_of(*, quote_id: Any, brand_id: Any) -> str | None:
    """这个 token 授权的**对象**。

    purpose 回答"能做什么",subject 回答"对谁做" —— 两者分开才挡得住
    "拿 A 单的令牌去确认 B 单"这类问题在事后无法复核。

    🔴 任一 id 取不到时返回 ``None``,**不拼一个残缺的 subject**:
       ``quote:12|brand:None`` 看起来像证据,其实证明不了归属 ——
       落 NULL 会让 :func:`classify_acceptance` 判 ``legacy_unproven``,
       那才是诚实的说法。
    🔴 也**不抛** —— 这条路径上跑的是客户点"确认报价"那一刻,
       为了一列审计值把整笔确认打掉是更大的伤害。
    """
    try:
        return f"quote:{int(quote_id)}|brand:{int(brand_id)}"
    except (TypeError, ValueError):
        return None


def cluster_fields(item: Any) -> dict[str, Any]:
    """把**一条** cluster 选择读成普通 mapping。

    🔴 为什么需要它:同一份数据在两种形态下到达这里 ——
       HTTP 路径上是 ``api.selection_api.ClusterSelectionItem``(Pydantic 模型),
       判据/回放脚本里常是 dict。**只能有一个读取器**:分两条路径写,
       就会出现"端点算出的 hash 与判据算出的不一样",而没有任何判据会红。

    🔴 这里**不用** ``getattr(c, k, None) or c.get(k)`` 那种写法 ——
       Pydantic 模型没有 ``.get``,一旦 ``getattr`` 落空就 ``AttributeError``,
       整笔确认在证据落库**之前**就 500(Codex 三审 P1-4 实况)。
    """
    if item is None:
        return {}
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)          # pydantic v2
    if callable(dump):
        return dict(dump())
    dump = getattr(item, "dict", None)                # pydantic v1
    if callable(dump):
        return dict(dump())
    return {k: v for k, v in vars(item).items() if not str(k).startswith("_")}


#: canonical form 里 cluster 选择保留哪几项。分母来自**真实请求体**
#: (``ClusterSelectionItem`` 的字段),不手抄一个想象中的 ``cluster_id``。
CLUSTER_CANONICAL_KEYS: tuple[str, ...] = (
    "cluster_name", "selected_core_ids", "covered_count",
)


def _canonical_clusters(clusters_selection: Any) -> list[dict[str, Any]]:
    """cluster 选择的规范形 —— 键固定、id 排序、包按名排序。

    🔴 排序是**必须**的:同一笔确认前端两次发来的包顺序可能不同,
       不排序的话同一笔会算出两个 hash,那 hash 就只能证明"我算过一次"。
    """
    out: list[dict[str, Any]] = []
    for c in clusters_selection or []:
        f = cluster_fields(c)
        out.append({
            "cluster_name": str(f.get("cluster_name") or ""),
            "selected_core_ids": _int_ids(f.get("selected_core_ids")),
            "covered_count": _as_int(f.get("covered_count")),
        })
    return sorted(out, key=lambda d: (d["cluster_name"], d["selected_core_ids"]))


def _int_ids(raw: Any) -> list[int]:
    return sorted(int(i) for i in (raw or [])
                  if str(i).strip().lstrip("-").isdigit())


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def canonical_request(
    *, token: str, tier: str, selected_keyword_ids: Any,
    clusters_selection: Any = None,
) -> dict[str, Any]:
    """确认请求的**规范形**。

    🔴 键序固定、id 排序 —— 同一笔确认必须每次算出同一个 hash,
       否则 hash 只能证明"我算过一次",证明不了"就是这一笔"。
    🔴 token 只进指纹,不进正文。
    """
    ids = _int_ids(selected_keyword_ids)
    payload: dict[str, Any] = {
        "schema_version": ACCEPTANCE_EVIDENCE_VERSION,
        "purpose": TOKEN_PURPOSE_CONFIRM,
        "token_fingerprint": token_fingerprint(token),
        "tier": str(tier or ""),
        "selected_keyword_ids": ids,
    }
    if clusters_selection:
        payload["clusters_selection"] = _canonical_clusters(clusters_selection)
    return payload


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def classify_acceptance(row: Mapping[str, Any] | None) -> str:
    """一行会话的受理证据处于哪一档。

    🔴 判"缺"用的是 :data:`REQUIRED_EVIDENCE_COLUMNS` 全集,不是挑几列看。
       挑几列 = 手抄清单,漏掉的那一列永远不会让任何判据变红。
    🔴 ``absent``(还没确认)与 ``legacy_unproven``(确认了但没留证据)
       必须分开:前者是正常状态,后者是**已知欠账**,处置方式不同。
    """
    if not row:
        return EVIDENCE_ABSENT
    pointer_present = [
        row.get(c) is not None for c in POINTER_COLUMNS]
    if not any(pointer_present):
        return EVIDENCE_ABSENT
    missing = [c for c in REQUIRED_EVIDENCE_COLUMNS if row.get(c) is None]
    return EVIDENCE_LEGACY_UNPROVEN if missing else EVIDENCE_PROVEN


def missing_evidence_columns(row: Mapping[str, Any] | None) -> tuple[str, ...]:
    """缺哪几列 —— 给运维看,也给判据当可读的失败信息。"""
    if not row:
        return REQUIRED_EVIDENCE_COLUMNS
    return tuple(c for c in REQUIRED_EVIDENCE_COLUMNS if row.get(c) is None)


def census() -> dict[str, Any]:
    return {
        "version": ACCEPTANCE_EVIDENCE_VERSION,
        "requiredColumns": list(REQUIRED_EVIDENCE_COLUMNS),
        "pointerColumns": list(POINTER_COLUMNS),
        "clusterCanonicalKeys": list(CLUSTER_CANONICAL_KEYS),
        "states": [EVIDENCE_ABSENT, EVIDENCE_PROVEN, EVIDENCE_LEGACY_UNPROVEN],
        # 可机读断言:本模块**不写库**,只判定。回填=伪造,结构上不给这个能力。
        "writesToDatabase": False,
    }
