"""三种 branded metric commitment —— MET-38 / MET-40 / POR-19。

规格 §15.5 L2062 逐字给定算法:

    「三者统一以 issuer+tenant 的 server-only commitment key 做 HMAC-SHA256,
      并用 NFC + RFC 8785 JCS + big-endian uint64 length-prefix framing;
      domain、issuer、tenant、brand、audience、projectionPolicyVersion
      与 canonical payload 都参与。」

三种 commitment(**互不可替换**,冒充即拒):

======================  ==========================================================
kind                    payload 精确内容(§15.5 L2062 逐字)
======================  ==========================================================
``scope_definition``    排序去重后的 surfaceKeys / searchModeKeys / runIndexes /
                        publicScopeLabels
``slice_definition``    metricDefinitionKey + metric_definition_registry_v1 的
                        **exact row** + denominatorRegistryVersion +
                        definitionVersion + calculationVersion
``matched_cohort``      **逐条存在于每个 metric comparison**:该项 baseline/current
                        各自的 scope/slice commitment、modeSide、brandExposure、
                        familyKey、platformKey,以及该项匹配 cell identity/count
======================  ==========================================================

🔴 为什么自己实现 JCS 而不是 ``json.dumps(sort_keys=True)``
-----------------------------------------------------------
两处不等价,且都会在**跨语言/跨版本**时悄悄改 hash:

1. ``sort_keys=True`` 按 **Unicode code point** 排序;RFC 8785 要求按
   **UTF-16 code unit** 排序。BMP 内两者一致,补充平面(emoji、部分 CJK 扩展)
   会给出**不同顺序** —— 于是同一份 payload 在含 emoji 的 label 上产生两个 hash。
   本模块显式按 UTF-16 排序。
2. JCS 的数字编码是 ECMAScript ``Number::toString``。Python 的 ``repr(float)``
   与它在若干值上不同。本模块**直接拒绝浮点**(见 :func:`_canonical`):
   commitment payload 里的量全是计数与版本号,出现小数本身就说明上游算错了
   —— MET-38 逐字「负/小数 count……拒绝」。

🔴 为什么 commitment 里**没有** reportSnapshotId / token / principal
--------------------------------------------------------------------
§15.5 L2062 明说这是**有意的**:要允许"已授权的同品牌历史同比"。
代价是 commitment 本身不携带授权语义,所以 resolver **必须另行重验**
baseline/current/auth 的同 tenant/brand/audience/policy(见 :func:`assert_same_authority`)。
把授权塞进 hash 会让历史对比永远算不出相等;把重验省掉会让跨 brand 混用 ——
两个错误方向,所以两件事都要做,且分开做。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import unicodedata
from typing import Any, Literal, Mapping, NamedTuple, Sequence

CommitmentKind = Literal["scope_definition", "slice_definition", "matched_cohort"]

#: 每种 kind 的 policy version(§15.5 L2062 三个专名)。**互不可替换**。
POLICY_VERSION: dict[CommitmentKind, str] = {
    "scope_definition": "metric_scope_commitment_v1",
    "slice_definition": "metric_slice_definition_commitment_v1",
    "matched_cohort": "public_comparison_commitment_v1",
}

#: domain separation 串。换一个 kind 就换一条 domain ——
#: 这是"用 scope commitment 冒充 slice/cohort"必然算不出相等的根本原因。
_DOMAIN: dict[CommitmentKind, bytes] = {
    "scope_definition": b"defgeo/metric-scope-commitment/v1",
    "slice_definition": b"defgeo/metric-slice-definition-commitment/v1",
    "matched_cohort": b"defgeo/public-comparison-commitment/v1",
}

COMMITMENT_KINDS: tuple[CommitmentKind, ...] = tuple(POLICY_VERSION)


class CommitmentError(ValueError):
    """payload 形态不合法。**投影失败**,不产出一个"看起来像"的 hash。"""


# ════════════════════════════════════════════════════════════════════
# RFC 8785 JCS
# ════════════════════════════════════════════════════════════════════

def _utf16_sort_key(key: str) -> tuple[int, ...]:
    """RFC 8785 §3.2.3:对象键按 **UTF-16 code unit** 升序。

    Python 字符串按 code point 迭代,补充平面字符是**一个** code point 但在
    UTF-16 里是**两个** code unit(代理对)。直接比较 code point 会与 JCS 不同序。
    这里显式编码成 UTF-16-BE 再按 16-bit 单元比较。
    """
    raw = key.encode("utf-16-be")
    return tuple(struct.unpack(f">{len(raw) // 2}H", raw))


def _canonical(value: Any) -> Any:
    """递归规范化:NFC 所有字符串;拒绝浮点与非法类型。"""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool):
        # bool 必须在 int 之前判 —— 它是 int 的子类,漏判会让 True 变成 1。
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CommitmentError(
            f"commitment payload 不接受浮点数({value!r})。"
            "此处的量全是计数与版本号;出现小数说明上游算错了"
            "(MET-38「负/小数 count 拒绝」)。"
        )
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(k)): _canonical(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        # 🔴 数组**保持签发顺序**(JCS 不排序数组)。排序会把
        #    "顺序变了"这件事悄悄抹平,而 MET-38 要求"输入换序漂移拒绝"。
        return [_canonical(v) for v in value]
    raise CommitmentError(f"commitment payload 不接受类型 {type(value).__name__}")


def _jcs_dumps(value: Any) -> bytes:
    """RFC 8785 序列化。键按 UTF-16 排序、无空白、UTF-8 输出。"""

    def enc(node: Any) -> str:
        if isinstance(node, Mapping):
            items = sorted(node.items(), key=lambda kv: _utf16_sort_key(kv[0]))
            return "{" + ",".join(
                f"{json.dumps(k, ensure_ascii=False)}:{enc(v)}" for k, v in items
            ) + "}"
        if isinstance(node, list):
            return "[" + ",".join(enc(v) for v in node) + "]"
        if isinstance(node, bool):
            return "true" if node else "false"
        if node is None:
            return "null"
        if isinstance(node, int):
            return str(node)
        if isinstance(node, str):
            return json.dumps(node, ensure_ascii=False)
        raise CommitmentError(f"不可序列化:{type(node).__name__}")

    return enc(_canonical(value)).encode("utf-8")


def _frame(*parts: bytes) -> bytes:
    """big-endian uint64 长度前缀 framing 后串联。

    为什么必须 framing:裸拼接下 ``("ab","c")`` 与 ``("a","bc")`` 得到同一串
    —— 那就等于给了攻击者一个"不同 payload 同 commitment"的构造法,
    而 MET-38 逐字要求拒绝这种情况。长度前缀让每段边界不可移动。
    """
    return b"".join(struct.pack(">Q", len(p)) + p for p in parts)


# ════════════════════════════════════════════════════════════════════
# commitment 计算
# ════════════════════════════════════════════════════════════════════

class CommitmentContext(NamedTuple):
    """参与 hash 的非 payload 字段(§15.5 L2062 逐字六项)。"""

    issuer: str
    tenant: str
    brand: str
    audience: str
    projection_policy_version: str


def _compute(
    kind: CommitmentKind,
    ctx: CommitmentContext,
    payload: Mapping[str, Any],
    *,
    key: bytes,
) -> str:
    if kind not in _DOMAIN:
        raise CommitmentError(f"未知 commitment kind {kind!r}")
    for name, value in (
        ("issuer", ctx.issuer), ("tenant", ctx.tenant), ("brand", ctx.brand),
        ("audience", ctx.audience),
        ("projectionPolicyVersion", ctx.projection_policy_version),
    ):
        if not isinstance(value, str) or not value:
            raise CommitmentError(f"commitment context 的 {name} 不得为空")
    framed = _frame(
        _DOMAIN[kind],
        unicodedata.normalize("NFC", ctx.issuer).encode("utf-8"),
        unicodedata.normalize("NFC", ctx.tenant).encode("utf-8"),
        unicodedata.normalize("NFC", ctx.brand).encode("utf-8"),
        unicodedata.normalize("NFC", ctx.audience).encode("utf-8"),
        unicodedata.normalize("NFC", ctx.projection_policy_version).encode("utf-8"),
        _jcs_dumps(payload),
    )
    return hmac.new(key, framed, hashlib.sha256).hexdigest()


def _sorted_unique(values: Sequence[Any], field: str) -> list[Any]:
    """§15.5 L2062:scope payload 是「**排序去重后**的」四个 key 列表。"""
    if not isinstance(values, (list, tuple)):
        raise CommitmentError(f"scope.{field} 必须是列表")
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise CommitmentError(f"scope.{field} 只接受字符串或整数,得到 {v!r}")
    return sorted({unicodedata.normalize("NFC", v) if isinstance(v, str) else v
                   for v in values}, key=lambda x: (isinstance(x, str), str(x)))


def scope_commitment(
    *, ctx: CommitmentContext, key: bytes,
    surface_keys: Sequence[str], search_mode_keys: Sequence[str],
    run_indexes: Sequence[int], public_scope_labels: Sequence[str],
) -> str:
    """``metric_scope_commitment_v1``。payload = 排序去重后的四个列表。"""
    payload = {
        "surfaceKeys": _sorted_unique(surface_keys, "surfaceKeys"),
        "searchModeKeys": _sorted_unique(search_mode_keys, "searchModeKeys"),
        "runIndexes": _sorted_unique(run_indexes, "runIndexes"),
        "publicScopeLabels": _sorted_unique(public_scope_labels, "publicScopeLabels"),
    }
    return _compute("scope_definition", ctx, payload, key=key)


def slice_commitment(
    *, ctx: CommitmentContext, key: bytes,
    metric_definition_key: str, registry_row: Mapping[str, Any],
    denominator_registry_version: str, definition_version: str,
    calculation_version: str,
) -> str:
    """``metric_slice_definition_commitment_v1``。

    ``registry_row`` 必须是 ``metric_definition_registry_v1`` 的 **exact row** ——
    传一个"差不多"的字典会得到一个"差不多"的 hash,而那正是 MET-38
    要拒绝的「改 registry row 而 hash 不变」的反面(这里是 hash 变了但没人发现)。
    所以调用方应当直接把 registry 里那一行原样传进来,不要就地拼。
    """
    if not isinstance(registry_row, Mapping) or not registry_row:
        raise CommitmentError("slice commitment 需要 metric registry 的 exact row")
    payload = {
        "metricDefinitionKey": metric_definition_key,
        "registryRow": dict(registry_row),
        "denominatorRegistryVersion": denominator_registry_version,
        "definitionVersion": definition_version,
        "calculationVersion": calculation_version,
    }
    return _compute("slice_definition", ctx, payload, key=key)


def cohort_commitment(
    *, ctx: CommitmentContext, key: bytes,
    baseline_scope: str, current_scope: str,
    baseline_slice: str, current_slice: str,
    mode_side: str, brand_exposure: str, family_key: str, platform_key: str | None,
    matched_cell_ids: Sequence[str], matched_cell_count: int,
) -> str:
    """``public_comparison_commitment_v1`` —— **逐条**存在于每个 metric comparison。

    🔴 两个不同 ``metricDefinitionKey`` 因 slice commitment 不同,
       **不能**复用同一 cohort hash(§15.5 L2062)。这条不是靠约定,
       是靠 baseline/current slice commitment 进 payload 自然保证的。
    """
    if isinstance(matched_cell_count, bool) or not isinstance(matched_cell_count, int):
        raise CommitmentError("matchedCellCount 必须是整数")
    if matched_cell_count < 0:
        raise CommitmentError("matchedCellCount 不得为负(MET-38)")
    if len(set(matched_cell_ids)) != len(matched_cell_ids):
        raise CommitmentError("matched cell identity 有重复 —— 会让 count 双算")
    if len(matched_cell_ids) != matched_cell_count:
        raise CommitmentError(
            f"matchedCellCount({matched_cell_count}) 与 identity 数"
            f"({len(matched_cell_ids)})不等 —— 分子分母必须可复算"
        )
    payload = {
        "baselineScopeDefinitionHash": baseline_scope,
        "currentScopeDefinitionHash": current_scope,
        "baselineSliceDefinitionHash": baseline_slice,
        "currentSliceDefinitionHash": current_slice,
        "modeSide": mode_side,
        "brandExposure": brand_exposure,
        "familyKey": family_key,
        "platformKey": platform_key,
        # 数组保持签发顺序(JCS 不排序数组);identity 已在上面查过重。
        "matchedCellIds": list(matched_cell_ids),
        "matchedCellCount": matched_cell_count,
    }
    return _compute("matched_cohort", ctx, payload, key=key)


def assert_same_authority(
    baseline: CommitmentContext, current: CommitmentContext
) -> None:
    """resolver 侧的另一半(§15.5 L2062「必须另行重验」)。

    commitment 有意不含 report/token/principal,所以"两侧 hash 相等"
    **不足以**说明可以对比 —— 还必须是同 tenant/brand/audience/policy。
    少了这一步,跨 brand 的两份报告只要 scope 恰好相同就会被判为可比。
    """
    for field in ("issuer", "tenant", "brand", "audience",
                  "projection_policy_version"):
        b, c = getattr(baseline, field), getattr(current, field)
        if b != c:
            raise CommitmentError(
                f"baseline 与 current 的 {field} 不同({b!r} vs {c!r}),"
                "不得跨 tenant/brand/audience/policy 对比"
            )
