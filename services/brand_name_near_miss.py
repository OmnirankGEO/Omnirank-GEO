"""近失(near-miss)品牌名检出 —— 「严进匹配」的**假阴性出口**。

[WO_DIAGNOSIS_MENTION_VARIANT_AND_QUESTION_FITNESS 2026-08-06 §1]

## 存在的理由(生产实证,不是设想)

诊断 561(brand 799 = ``阿强小龙虾``,``brand_aliases`` 0 行、``company_name``
空、``brand_display_names`` 空 —— 三源里只有一个形态)。8 题 × 4 引擎 = 32 格,
**23 格的 AI 原文里写的是「阿强龙虾」**(少一个「小」字,就是这家店的口语叫法):

===========================  ====  ============================================
落库 verdict                 格数  发生了什么
===========================  ====  ============================================
YES                            4   原文逐字含「阿强小龙虾」→ 确定性层命中
UNKNOWN/invalid_matched_text  16   LLM 复核层判 YES 且 matched_text="阿强龙虾",
                                   被 ``_is_plausible_verified_match`` 打回
NO(原文含「阿强龙虾」)        7   LLM 复核层自己判 NO,理由逐条写着
                                   「证据中为'阿强龙虾',与'阿强小龙虾'专有前缀不同」
NO(原文真的没有)              5   真未提到
===========================  ====  ============================================

**近失并没有漏检 —— 它被检出了三次,然后被丢了三次。**

  1. ``resolve_local`` 的 ``_candidate_tokens`` 拿「阿强」建了证据窗口(所以才会
     走到 LLM 复核);
  2. LLM 复核层把「阿强龙虾」当 ``matched_text`` 交了回来;
  3. ``_is_plausible_verified_match`` 正确地拒绝了它(长度不等 → 连它自带的
     "等长 1 字差" 分支都进不去)—— 但拒绝之后**整条候选就地蒸发**:
     ``BrandDecision`` 不带 ``matched_alias``,``identity_candidates`` 落库为空。

后果是双重的,且都对客户不利:
  · 16 格 UNKNOWN 被排除出分母(``ai_total_tests`` 16 而不是 32)—— 花钱买的
    一半样本静默消失,报告上一个字都不说;
  · 待确认卡片虽然生成了(``invalid_matched_text`` 本来就在
    ``PENDING_IDENTITY_REASONS`` 里),但 ``cell_candidates`` 拿不到任何候选名,
    代理看到的是一坨原文和零个可点的按钮 —— **闭环的最后一米断在这里**。

## 本模块只做一件事

把「疑似同一品牌的另一种写法」从答案里挑出来,**作为待人工确认的候选**返回。

🔴 **近失命中 ≠ 提到**(测量诚实,WO §1.3 铁律)。本模块不产生 verdict、不改
分母、不进 ``all_trusted_names``。它的产物只有一个去处:待确认卡片。确认之后
走的是既有闭环(``diagnosis_identity_decision.decide_brand_cell``:写
``brand_display_names`` + ``monitoring_identity_name_decisions`` → 本地重判 →
重算漏斗/总分,零 provider 调用、零扣费)。

## 判据边界(每一条都对着一次真实误伤)

``NEAR_MISS_MIN_LENGTH = 4`` + 编辑距离 ≤ 1,两条一起用:

  · **「大型」→「大鹏」**(飞轮包 2026-08-06 实证的编辑距离误伤):两串归一后
    都是 2 字 < 4 → 长度闸直接拦掉,轮不到距离判。这就是长度下限存在的原因 ——
    2-3 字的中文词之间差一个字**太常见**,距离本身没有判别力。
  · **「深圳」**(08-05 城市别名假阳性 P0):2 字 → 长度闸拦掉;就算不拦,
    ``_is_geographic_only`` 也会拦。那单管**假阳性**(城市名不许进可信集),
    本单管**假阴性**(变体要进待确认)—— 同一个匹配层的两面,方向相反,
    所以本模块**只往待确认里加,绝不往可信集里加**,两单不打架。
  · **「深圳市富士恒电梯有限公司」vs「深圳市晨光富士电梯有限公司」**(551 那格):
    编辑距离 = 3(删「恒」插「观光」),> 1 → 不命中。
  · **「知乎(知+)」**:那是 ``split_brand_aliases`` 的**可信**别名路径,不经本
    模块,行为不变(反向对照防修过头)。

⚠️ 本模块**会**把「李仔小龙虾」这种真·别家(距离 1、长度 5)判成近失候选。
这是**有意接受**的代价:近失产物是一张待确认卡片,代价 = 代理点一次「不是」;
而收紧到能分辨它,就等于要求机器判断"哪个字是专有的",那正是 08-05 到 08-06
连着两个 P0 证明机器做不了、必须交给人的那件事。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# 归一后两串都必须达到的最短长度。见模块 docstring「大型→大鹏」那条。
NEAR_MISS_MIN_LENGTH = 4
# 允许的最大编辑距离(增/删/改各算 1)。1 = 「阿强龙虾」少的那个「小」字。
NEAR_MISS_MAX_DISTANCE = 1
#: 规则 B(受限扩展)允许候选比可信形态多出的最大字数。
#:
#: 🔴 这条是给「≤5 字品牌 + 门店/分店后缀」用的(工单 §1.3 点名的
#: 「阿强小龙虾万象店」)。为什么规则 A 接不住它:多出「万象店」= 编辑距离 3。
#: 为什么确定性层也接不住:``_trusted_exact_hit`` 找到「阿强小龙虾」的位置后,
#: 右边界是「万」—— 既不是非中文边界、也不在 ``_SHORT_SUFFIX_CONTEXT``
#: (那张表只有 酒店/品牌/门店/公司/集团/科技/生物,没有「万」),判不安全 → 放弃;
#: ``_derived_storefront_exact_hit`` 只认 ``_STOREFRONT_SUFFIXES``(酒店/门店),
#: 「…万象店」是「店」不是「门店」→ 也不认。**两条路都是对的**(它们防的是
#' 把连锁不同分店合并成一家),所以出口只能开在待确认这一侧。
NEAR_MISS_MAX_EXTENSION = 4
# 一次最多返回几个候选(待确认卡片放得下的量,防超长 payload)。
NEAR_MISS_MAX_CANDIDATES = 5


@dataclass(frozen=True)
class NearMissCandidate:
    """一条疑似同品牌变体。

    ``display`` 是**答案原文里的写法**(给人看的,要能在原文里搜到);
    ``trusted_form`` 是它像的那个可信形态(告诉人"像哪一个")。
    """

    display: str
    trusted_form: str
    distance: int

    def as_dict(self) -> dict:
        return {
            "display": self.display,
            "trusted_form": self.trusted_form,
            "distance": self.distance,
        }


def _normalize(value: Any) -> str:
    from services.brand_identity_resolver import normalize_brand_name

    return normalize_brand_name(str(value or "").strip())


def _bounded_edit_distance(left: str, right: str, *, limit: int) -> int:
    """两串编辑距离;超过 ``limit`` 时提前返回 ``limit + 1``(不算满)。

    只在 limit 很小(1)时用,所以不做 DP 表 —— 长度差 > limit 直接出局,
    否则线性扫一遍找第一个差异点再对齐比较。
    """
    if left == right:
        return 0
    len_left, len_right = len(left), len(right)
    if abs(len_left - len_right) > limit:
        return limit + 1
    if limit != 1:  # pragma: no cover - 本模块只用 limit=1;留个显式护栏
        raise ValueError("_bounded_edit_distance 目前只支持 limit=1")

    if len_left == len_right:
        diffs = sum(1 for a, b in zip(left, right) if a != b)
        return diffs if diffs <= limit else limit + 1

    # 长度差 1:长串删掉一个字符后应与短串相等。
    shorter, longer = (left, right) if len_left < len_right else (right, left)
    index = 0
    while index < len(shorter) and shorter[index] == longer[index]:
        index += 1
    # 跳过 longer 的第 index 个字符后必须完全相同。
    return 1 if shorter[index:] == longer[index + 1:] else limit + 1


def _is_geographic_only(normalized: str) -> bool:
    """整串就是个地名(「深圳」「广东省」)—— 绝不能当品牌变体候选。

    🔴 判据必须是**白名单**,不能用 ``geo_tokens`` 反推。第一版我写成
    「``geo_tokens(s)`` 吐回的 token 等于 s 就算地名」,当场被自己的锁抓出来:
    ``geo_tokens`` 对**任何**无行政后缀的 2+ 字串都会原样吐回一个 token
    (那是它的设计 —— tail 残串也算一块),于是「阿强龙虾」被判成地名,
    整个近失层恒空。**"某函数认得它"不等于"它是那个东西"。**

    白名单复用 ``brand_identity_resolver._administrative_prefix_variants()``
    —— 省/直辖市/主要地级市的带后缀与不带后缀两种写法,是 08-05 城市别名
    P0 之后已经在用的那份表,不另立第二份。
    """
    try:
        from services.brand_identity_resolver import _administrative_prefix_variants
    except Exception:  # pragma: no cover - 依赖缺失时交给下面的通用词闸
        return False
    return any(
        _normalize(name) == normalized for name in _administrative_prefix_variants()
    )


def _is_generic_only(display: str) -> bool:
    """整串由通用行业词拼成(「科技有限公司」「电梯」)—— 不是身份。"""
    try:
        from services.brand_identity_resolver import _is_generic_only_derived_form
    except Exception:  # pragma: no cover
        return False
    try:
        return bool(_is_generic_only_derived_form(display))
    except Exception:  # pragma: no cover
        return False


def find_near_miss_candidates(
    trusted_names: Sequence[str] | Iterable[str],
    candidates: Sequence[str] | Iterable[str],
    *,
    rejected_names: Sequence[str] | Iterable[str] = (),
    limit: int = NEAR_MISS_MAX_CANDIDATES,
) -> tuple[NearMissCandidate, ...]:
    """在 ``candidates`` 里找出疑似 ``trusted_names`` 变体的写法。

    🔴 返回值**不是提及判定**。见模块 docstring。

    ``candidates`` 的两个真实来源:
      · 该格的 ``mentioned_brands``(LLM 抽的同答案品牌名单;NO 格有,
        UNKNOWN 格因为跳过抽取而没有);
      · LLM 复核层被 ``_is_plausible_verified_match`` 打回的那个 ``matched_text``
        (UNKNOWN/invalid_matched_text 格唯一的候选来源,证据最硬 —— 它是
        源文里逐字存在、且被复核层认过的一段)。
    """
    trusted_norm: list[tuple[str, str]] = []
    seen_trusted: set[str] = set()
    for name in trusted_names or ():
        normalized = _normalize(name)
        if not normalized or normalized in seen_trusted:
            continue
        seen_trusted.add(normalized)
        trusted_norm.append((normalized, str(name or "").strip()))
    if not trusted_norm:
        return ()

    rejected_norm = {
        _normalize(name) for name in (rejected_names or ()) if _normalize(name)
    }

    out: list[NearMissCandidate] = []
    seen_candidates: set[str] = set()
    for raw in candidates or ():
        display = str(raw or "").strip()
        if not display:
            continue
        normalized = _normalize(display)
        if not normalized or normalized in seen_candidates:
            continue
        # 已经全等于某个可信形态 = 真命中,不是近失(判定 SSOT 的事,不归本模块)。
        if normalized in seen_trusted:
            continue
        # 人工判过「不是」的名字不再回到待确认队列(否则确认动作没有终点)。
        if normalized in rejected_norm:
            continue
        if len(normalized) < NEAR_MISS_MIN_LENGTH:
            continue
        if _is_geographic_only(normalized) or _is_generic_only(display):
            continue

        best: tuple[int, str] | None = None
        for trusted_normalized, trusted_display in trusted_norm:
            if len(trusted_normalized) < NEAR_MISS_MIN_LENGTH:
                continue
            # 规则 A · 一字之差(增/删/改)——「阿强龙虾」少的那个「小」。
            distance = _bounded_edit_distance(
                normalized, trusted_normalized, limit=NEAR_MISS_MAX_DISTANCE
            )
            if distance > NEAR_MISS_MAX_DISTANCE:
                # 规则 B · 受限扩展 ——「阿强小龙虾万象店」多出来的那三个字。
                # 🔴 只认**候选包含可信形态**这一个方向:反方向(可信形态包含候选)
                # 是"截短",那条路属于确定性层的 _derived_legal_exact_hit /
                # storefront 变体,有它们自己经过锤炼的边界闸,不许在这里开第二条。
                extension = len(normalized) - len(trusted_normalized)
                if (
                    0 < extension <= NEAR_MISS_MAX_EXTENSION
                    and trusted_normalized in normalized
                ):
                    distance = NEAR_MISS_MAX_DISTANCE + extension
                else:
                    continue
            if best is None or distance < best[0]:
                best = (distance, trusted_display)
        if best is None:
            continue
        seen_candidates.add(normalized)
        out.append(
            NearMissCandidate(display=display, trusted_form=best[1], distance=best[0])
        )
        if len(out) >= max(1, int(limit)):
            break
    return tuple(out)


def near_miss_candidates_for_identity(
    identity: Any,
    candidates: Sequence[str] | Iterable[str],
    *,
    limit: int = NEAR_MISS_MAX_CANDIDATES,
) -> tuple[NearMissCandidate, ...]:
    """``BrandIdentity`` 版入口。

    🔴 可信形态只吃 ``canonical_names`` / ``trusted_aliases`` **原串**,
    **不走** ``identity.all_trusted_names`` —— 后者过 ``split_brand_aliases``,
    会把「XX(深圳)科技有限公司」的括号城市拆成独立别名(08-05 城市别名 P0),
    拿它当基准就等于允许「深圳」当近失基准。同一条纪律 brandq 包已立过一次。
    """
    if identity is None:
        return ()
    trusted = [
        *(getattr(identity, "canonical_names", ()) or ()),
        *(getattr(identity, "trusted_aliases", ()) or ()),
    ]
    return find_near_miss_candidates(
        trusted,
        candidates,
        rejected_names=(getattr(identity, "rejected_aliases", ()) or ()),
        limit=limit,
    )
