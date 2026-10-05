"""防御型 GEO · 新增 runtime H0 硬门的**机械登记表**(规格 §1.1 / ACT-12 / §0.5.3 G-1)。

为什么必须有这个文件
--------------------
§1.1 写死:任何新增 H0 运行时门都要由 Owner 在版本化规则目录中签发
``rule_id + rule_version + H0 类别 + 拦截对象 + 可执行修复/重试/人工出口 + 判别测试 ID``;
未签发前,除**现役**资金守恒、权限/隐私/租户隔离与对象完整性边界外,
质量与业务资格问题**只能**落 A1/H1/O1。

G-1 又补了一刀:「手写清单不算分母」。所以这里不是一份文档,是一个
**运行时会自己报错的东西**:

  · ``enforce_h0(rule_id)`` 是所有新增硬门的唯一入口。
    规则不在表里 / 缺任一必填项 / Owner 未签 → 抛 ``H0RuleNotSigned``,
    调用方只能降级成 A1/H1/O1,**不能**当硬门用。
  · ``census()`` 把表导成机读分母,判据拿它对账 —— 不拿人手抄的清单。

一句话:**光写进文档的规则,读过也会踩**;能自己炸的规则才算做完。

现在表里有几条
--------------
**零条已签**。本窗(WP0+WP1+WP2)没有引入任何新的 runtime H0 硬门 ——
WP1 三处修的是 producer 接线(数据诚实性,不是闸),
WP2 的资金/权限约束全部复用现役已有边界(现役诊断 admission、
``auth/brand_access``、``services/organization_route_contract`` 等),
按 §1.1 那些属于「现役边界」,不需要新签发。

候选项(下面 ``_PENDING``)是规格里点名、但**尚未获 Owner 签发**的门。
它们躺在这里是为了让「待签」这件事本身可机读、可对账,
而不是散在某份交接文档的一段话里。签发前它们**不得**进入 runtime 硬门 ——
``enforce_h0`` 会当场拒绝。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: 目录版本。加/删/改任何一条都必须升版 —— 否则旧 sidecar 会按新版执行
#: (§19 变异 181「改 mapping 不升 version/hash」)。
CATALOG_VERSION = "defensive-geo-h0-catalog-v1"

H0Class = Literal["H0-AUTH", "H0-MONEY", "H0-DATA", "H0-LEGAL"]


class H0Rule(NamedTuple):
    """一条已签发的 runtime H0 硬门。**六项全部必填**,缺一不可启用(ACT-12)。"""

    rule_id: str
    rule_version: str
    h0_class: H0Class
    #: 拦截对象:这道门到底挡的是哪个具体动作。禁止写成「保证数据正确」这种无法验证的话。
    intercepts: str
    #: 可执行出口:修复 / 重试 / 转人工,至少一条,且必须是用户真点得到的动作。
    #: 「只有 label 没有 target」的死动作等于没有出口(§19 变异 143)。
    exits: tuple[str, ...]
    #: 对应判别测试 ID。没有判据的硬门 = 没人能证明它真的会拦。
    test_ids: tuple[str, ...]
    #: Owner 签发标记。**本规格不能自我签名**(§1.1 原文)。
    owner_signed: bool


class H0RuleNotSigned(RuntimeError):
    """请求把一条未签发/不完整的规则当硬门使用。

    调用方接到它**只有一条合法出路**:降级成 A1/H1/O1(可解释、可继续),
    绝不能 except 掉然后照样拦 —— 那就是「未签就上硬门」。
    """


#: 🔴 已签发的新增 runtime H0 门。**当前为空**,理由见模块 docstring。
#:
#: 空不是占位:``enforce_h0`` 对空表的行为与对未签条目完全一致(拒绝),
#: 而判据 ``test_h0_catalog.py`` 会证明「表空」和「门被启用」不可能同时成立。
_SIGNED: dict[str, H0Rule] = {}


#: 🟡 规格点名、但**尚未获 Owner 签发**的候选门。
#:
#: 放这里的意义是让「待签」可机读:§21.1 的 Owner 待签清单不该只活在文档段落里,
#: 否则「签了没有」永远靠人记。这些条目**不参与** ``enforce_h0`` ——
#: 它们连 ``H0Rule`` 都不是,只是 (rule_id, 说明) 二元组。
_PENDING: tuple[tuple[str, str], ...] = (
    (
        "defgeo.diagnosis.mode_recommendation",
        "§15.1 start-context 的 signed recommendation:没有 Owner 签发的推荐规则时,"
        "服务端必须返回 status=not_signed 且四个字段全 null,前端不得冒充个性化推荐。"
        "这是「不许硬来」的约束,不是硬门本身;真要按推荐**阻断**用户选别的模式,才需要签发。",
    ),
    (
        "defgeo.report.level_policy",
        "§18.6 / MET-36 的对客等级阈值与评分口径。属 Owner 商业口径"
        "(§0.5.2 R-2 明确:等级阈值/对客评分归 Owner,工程 registry 归 Review-CTO)。"
        "未签前不得对客 enrollment。",
    ),
)


# ══════════════════════════════════════════════════════════════════════════
# 🟢 派生签发(窗C / WP5 新增)—— 签名来自**别的文件里 Owner 真签过的那一行**
# ══════════════════════════════════════════════════════════════════════════
# 为什么需要第二种签发形态,而不是往 ``_SIGNED`` 里手写一条:
#
#   §1.1「本规格不能自我签名」。往 ``_SIGNED`` 写 ``owner_signed=True`` 就是
#   **实现方给自己盖章** —— 它读起来像签发,实际只是一个布尔字面量。
#
# ``defgeo.publish.legal_catalog`` 有一个真实的、Owner 签过的载体:
# ``config/legal_prohibited_pack.json``,其 ``owner_signed`` 段写着
# ``{"signed_by": "Owner", "signed_at": "2026-07-23", "source": "…SSOT…"}``,
# 且加载器 ``services/marketing/guards.py::legal_pack_info()`` 会告诉你这份包
# 到底来自 ``config``(已签)还是 ``embedded_fallback``(未签的内嵌兜底)。
#
# 所以这条规则的 ``owner_signed`` **不是常量,是每次读包算出来的**:
#   · 包在、来自 config、owner_signed.signed_by 非空 → 硬门可用;
#   · 包缺失/损坏/回落 embedded → ``enforce_h0`` 照样拒绝,发布链降级成 A1 提示。
#
# 这与该包自己的既定口径一致(包 ``_doc`` 原文:「包缺失/损坏时 fail-open for
# content:内嵌集合**未经签发无权硬拦**」)—— 我们没有扩大禁区,只是把
# 「谁签的」这件事接到了运行时。
#
# 撕锁方式(判据必须能拆红):把 pack 的 ``owner_signed`` 清空或让 source 变成
# ``embedded_fallback``,``enforce_h0('defgeo.publish.legal_catalog')`` 必须抛。
from typing import Callable

_DERIVED: dict[str, Callable[[], H0Rule | None]] = {}


def _resolve_publish_legal_catalog() -> H0Rule | None:
    """从 Owner 签发的法律包派生这条 H0-LEGAL 规则。未签 → ``None``。"""
    try:                                    # 惰性 import:避免 marketing 层反向依赖
        from services.marketing.guards import legal_pack_info
    except Exception:                       # pragma: no cover - 环境缺件
        return None
    try:
        info = legal_pack_info()
    except Exception:                       # pragma: no cover - 包损坏
        return None
    signed = dict(info.get("owner_signed") or {})
    version = str(info.get("version") or "").strip()
    source = str(info.get("source") or "").strip()
    # 三项缺一即视为未签:版本、签名人、来自 config(而不是内嵌兜底)。
    if not version or not str(signed.get("signed_by") or "").strip() or source != "config":
        return None
    return H0Rule(
        rule_id="defgeo.publish.legal_catalog",
        rule_version=version,
        h0_class="H0-LEGAL",
        intercepts=(
            "对外发布 worker 在写 external-start marker、调用任何 provider 之前,"
            "对即将发布的**冻结** article revision 按当时最新 Owner-signed 广告法 catalog 再验一次"
            "(§11.1 / §11.3 第五步)"
        ),
        exits=("repair_legal_passage",),     # 唯一出口:按 rule/passage 局部修复
        test_ids=("MED-11", "MED-18"),
        owner_signed=True,                   # ← 由上面三项**算**出来的,不是写死的
    )


_DERIVED["defgeo.publish.legal_catalog"] = _resolve_publish_legal_catalog


def catalog_version() -> str:
    return CATALOG_VERSION


def _resolve(rule_id: str) -> H0Rule | None:
    """静态签发优先,其次派生签发。两处都没有 → ``None``。"""
    rule = _SIGNED.get(rule_id)
    if rule is not None:
        return rule
    resolver = _DERIVED.get(rule_id)
    return resolver() if resolver is not None else None


def derived_rule_ids() -> frozenset[str]:
    """**当前**派生签发成立的规则 ID。签名来自别处的真签发,所以这个集合会随
    那份包的状态变化 —— 判据必须两向都撕(签在 → 有;签没了 → 空)。"""
    return frozenset(r for r, fn in _DERIVED.items() if fn() is not None)


def derived_candidate_ids() -> frozenset[str]:
    """登记了派生解析器的规则 ID(不管当前签没签)。census 的分母。"""
    return frozenset(_DERIVED)


def signed_rule_ids() -> frozenset[str]:
    """已签发规则 ID 集合(静态 ∪ 当前派生成立)。判据拿它当分母,不手抄。"""
    return frozenset(_SIGNED) | derived_rule_ids()


def statically_signed_rule_ids() -> frozenset[str]:
    """🔴 **实现方自己写死**的签发。这个集合应当恒为空 ——
    非空就意味着有人在代码里给自己盖了章(§1.1「本规格不能自我签名」)。"""
    return frozenset(_SIGNED)


def pending_rule_ids() -> frozenset[str]:
    return frozenset(rule_id for rule_id, _ in _PENDING)


def _completeness_gaps(rule: H0Rule) -> list[str]:
    """ACT-12 六项逐项体检。返回缺失项名;空列表 = 完整。"""
    gaps: list[str] = []
    if not rule.rule_id.strip():
        gaps.append("rule_id")
    if not rule.rule_version.strip():
        gaps.append("rule_version")
    if rule.h0_class not in ("H0-AUTH", "H0-MONEY", "H0-DATA", "H0-LEGAL"):
        gaps.append("h0_class")
    if not rule.intercepts.strip():
        gaps.append("intercepts")
    if not rule.exits or not all(e.strip() for e in rule.exits):
        gaps.append("exits")
    if not rule.test_ids or not all(t.strip() for t in rule.test_ids):
        gaps.append("test_ids")
    if not rule.owner_signed:
        gaps.append("owner_signed")
    return gaps


def enforce_h0(rule_id: str) -> H0Rule:
    """新增 runtime H0 硬门的**唯一**入口。

    返回 = 允许把这条规则当硬门拦截。
    抛 ``H0RuleNotSigned`` = 不允许;调用方必须降级成 A1/H1/O1。

    🔴 这个函数存在的全部意义,是让「未签就上硬门」变成一件**做不到**的事,
       而不是一条「请记得先签」的规矩。
    """
    rule = _resolve(rule_id)
    if rule is None:
        if rule_id in pending_rule_ids():
            hint = " (在待签候选里,尚未签发)"
        elif rule_id in derived_candidate_ids():
            hint = " (有派生解析器,但其签发载体当前未签/已回落内嵌兜底)"
        else:
            hint = ""
        raise H0RuleNotSigned(
            f"H0 规则 {rule_id!r} 不在已签目录 {CATALOG_VERSION}{hint}。"
            "按 §1.1,未签发的质量/业务资格问题只能落 A1/H1/O1,不得作为运行时硬门。"
        )
    gaps = _completeness_gaps(rule)
    if gaps:
        raise H0RuleNotSigned(
            f"H0 规则 {rule_id!r} 缺少必填项 {gaps}(ACT-12 六项)。缺任一项即不得启用该硬门。"
        )
    return rule


def census() -> dict:
    """机读导出。判据拿这个当分母对账,不拿人手抄的清单(G-1)。"""
    resolved = {rid: _resolve(rid) for rid in sorted(signed_rule_ids())}
    return {
        "catalog_version": CATALOG_VERSION,
        "signed": {
            rule_id: {
                "rule_id": rule.rule_id,
                "rule_version": rule.rule_version,
                "h0_class": rule.h0_class,
                "intercepts": rule.intercepts,
                "exits": list(rule.exits),
                "test_ids": list(rule.test_ids),
                "owner_signed": rule.owner_signed,
                "signature_origin": (
                    "static" if rule_id in _SIGNED else "derived"
                ),
                "completeness_gaps": _completeness_gaps(rule),
            }
            for rule_id, rule in resolved.items() if rule is not None
        },
        "statically_signed": sorted(statically_signed_rule_ids()),
        "derived_candidates": sorted(derived_candidate_ids()),
        "derived_signed": sorted(derived_rule_ids()),
        "signed_count": len(signed_rule_ids()),
        "pending": [{"rule_id": r, "note": n} for r, n in _PENDING],
        "pending_count": len(_PENDING),
    }
