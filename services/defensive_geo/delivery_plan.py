"""``geo-delivery-plan-v1`` —— 不可变交付计划的 schema 与 activation 前置校验。

规格 = `docs/AI-CONTEXT/DEFENSIVE_GEO_SYSTEM_INTEGRATION_PLAN_2026-08-20.md`
@ `e710be6c2` §4.1(JSON 形态)/§4.2(身份与版本)/§4.4(最低交付与容量分开)。
判据 = DEL-01 / DEL-02 / DEL-11 / DEL-12,反向变异 §19 第 11 发
(「用 required_articles 同时表示最低和上限」必须打红 DEL-01/02)。

三条本模块**不做**的事(免得后来人以为这里是唯一权威)
------------------------------------------------------
1. **不落库、不建表**。权威计划冻结在现役
   ``quote_pricing_snapshots.pricing_snapshot.delivery_plan``(§4.1 L380
   「不要先新建第二套 defensive_plan_revisions」),本模块只做纯函数校验。
2. **不碰 `confirmed_keywords.required_articles` 的语义**。它继续、且只继续
   表示「可交付容量上限,允许 0..capacity」(§4.3)。最低交付是**另一组字段**,
   这正是 DEL-01「capacity 与 contract minimum 分开落库、分开展示」的全部内容。
3. **不判资金**。四格 funding SSOT 在 ``services.defensive_geo.funding_projection``,
   本模块一个字都不复述 —— 复述就是第二套真值。

为什么校验器返回「差额 + 修复出口」而不是抛异常
------------------------------------------------
§0.5.6 铁律 =「任何阻塞与错误必须自带解决方案」,DEL-02 逐字要求
「不足显示差额和修复」。只抛一个 ValueError 满足不了 DEL-02:调用方拿不到
差多少、该点哪。所以 :func:`validate_plan` 恒返回 :class:`PlanVerdict`,
里面每条违规都自带 ``shortfall``(差多少)与 ``repair``(下一步干什么)。

🔴 `question_identity_key` 不叫 `question_key`
----------------------------------------------
§0.5.1-7:本文的「稳定逻辑身份键」与现役
``geo_article_target_question_snapshots.question_key``(CHARACTER(64) 内容哈希、
全局 UNIQUE)**同名异义**,裁定落地时改名。两者语义相反 —— 现役那个是
「改一个字就换一个 key」,本文这个是「改一个字 key 不变、只升 revision」。
所以本模块只认 ``question_identity_key``;见到裸 ``question_key`` 会**显式报错**
(而不是静默忽略),让写错的人当场知道,不要等到监测漂移了才发现。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, NamedTuple, Sequence

SCHEMA_VERSION = "geo-delivery-plan-v1"

#: §4.2 L454:报价头才有 hybrid;**每个 item 只能是 defensive 或 offensive**。
ObjectiveMode = Literal["defensive", "offensive"]
OBJECTIVE_MODES: frozenset[str] = frozenset({"defensive", "offensive"})

#: §4.1 header 的 campaign_mode。hybrid 只描述报价头。
CAMPAIGN_MODES: frozenset[str] = frozenset({"defensive", "offensive", "hybrid"})

#: §4.2 L457:冻结题面是否已点名品牌。**所有提及/推荐分母必须使用它**。
BRAND_EXPOSURES: frozenset[str] = frozenset({"named", "unnamed", "comparison"})

#: §4.4 L497:bonus item 不得计入 header minimum。
COMMITMENT_KINDS: frozenset[str] = frozenset({"contract", "bonus"})

#: §4.4 L493:v1 每个 plan item 的 authorized_article_capacity **固定为 1**。
#: 「需要加量就创建新的 child item/新计划 revision,不把一个 item 扩成隐形多槽」。
V1_ITEM_CAPACITY = 1

#: §4.4 L488-491 的五个 header 最低量键。分母写在这里,判据从这里取,不手抄。
CONTRACT_MINIMUM_KEYS: tuple[str, ...] = (
    "questions",
    "articles",
    "publications",
    "distinct_public_media",
    "distinct_root_domains",
)

#: 见模块 docstring:现役同名列语义相反,见到它要响亮地炸。
_FORBIDDEN_ITEM_KEYS: dict[str, str] = {
    "question_key": (
        "item 用了 `question_key`。§0.5.1-7:该名与现役 "
        "geo_article_target_question_snapshots.question_key(内容哈希、全局 UNIQUE)"
        "同名异义且语义相反,本计划的稳定逻辑身份键必须写作 `question_identity_key`。"
    ),
    "required_articles": (
        "item 用了 `required_articles`。§4.3:该列只表示**容量上限**且只属于 "
        "confirmed_keywords;交付计划里的容量键是 `authorized_article_capacity`,"
        "最低量键是 `minimum_articles`。§19 第 11 发变异正是「拿 required_articles "
        "同时表示最低和上限」,DEL-01/02 必须为此转红。"
    ),
}


class PlanViolation(NamedTuple):
    """一条违规。**自带差额与修复出口** —— DEL-02 要求的就是这两样。"""

    code: str
    #: 违规落在哪一项;header 级违规为 ``None``。
    plan_item_key: str | None
    #: 人读句(服务商面)。不含内部枚举裸串 —— §0.5.5 U-1「内部枚举裸串上屏 = 红」。
    message: str
    #: 差多少。非数量类违规为 ``None``。
    shortfall: int | None
    #: 下一步干什么。恒非空 —— 没有出口的违规不许存在。
    repair: str


class PlanVerdict(NamedTuple):
    """校验结论。``ok`` 为真时 violations 必为空,反之亦然。"""

    ok: bool
    violations: tuple[PlanViolation, ...]

    def blocking_codes(self) -> frozenset[str]:
        return frozenset(v.code for v in self.violations)


def _fail(
    out: list[PlanViolation],
    code: str,
    message: str,
    repair: str,
    *,
    item: str | None = None,
    shortfall: int | None = None,
) -> None:
    out.append(
        PlanViolation(
            code=code, plan_item_key=item, message=message,
            shortfall=shortfall, repair=repair,
        )
    )


def _as_int(value: Any) -> int | None:
    """严格取整。``True`` 是 int 的子类,必须挡掉,否则 bool 会冒充数量。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _contract_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [i for i in items if i.get("commitment_kind") == "contract"]


def _validate_item(
    item: Mapping[str, Any], index: int, out: list[PlanViolation]
) -> None:
    key = item.get("plan_item_key")
    label = key if isinstance(key, str) and key else f"#{index}"

    for forbidden, why in _FORBIDDEN_ITEM_KEYS.items():
        if forbidden in item:
            _fail(out, "forbidden_legacy_key", why,
                  "改用本 schema 的键名后重新签发计划。", item=label)

    if not isinstance(key, str) or not key:
        _fail(out, "item_missing_identity",
              f"第 {index} 项缺少交付格身份 plan_item_key。",
              "由服务端为该项签发交付格身份后重新出计划。", item=label)

    if item.get("objective_mode") not in OBJECTIVE_MODES:
        _fail(out, "item_bad_objective_mode",
              f"交付项 {label} 的目标类型不是「品牌防守」或「抢推荐」之一。",
              "把该项归到其中一侧后重新出计划。", item=label)

    if item.get("brand_exposure") not in BRAND_EXPOSURES:
        _fail(out, "item_bad_brand_exposure",
              f"交付项 {label} 没有冻结题面是否点名品牌。",
              "补齐该项的题面点名口径后重新出计划;它是提及/推荐分母的依据。",
              item=label)

    if item.get("commitment_kind") not in COMMITMENT_KINDS:
        _fail(out, "item_bad_commitment_kind",
              f"交付项 {label} 没有标明是合同交付还是赠送项。",
              "标明后重新出计划;赠送项不计入合同最低量。", item=label)

    if not isinstance(item.get("question_identity_key"), str) or not item.get(
        "question_identity_key"
    ):
        _fail(out, "item_missing_question_identity",
              f"交付项 {label} 缺少稳定的问题身份。",
              "为该项绑定一道冻结问题后重新出计划。", item=label)

    revision = _as_int(item.get("question_revision"))
    if revision is None or revision < 1:
        _fail(out, "item_bad_question_revision",
              f"交付项 {label} 的问题版本号不合法。",
              "问题版本号从 1 起;改题只升版本,不换身份。", item=label)

    ordinal = _as_int(item.get("global_ordinal"))
    if ordinal is None or ordinal < 1:
        _fail(out, "item_bad_ordinal",
              f"交付项 {label} 缺少稳定的排序号。",
              "补齐排序号;同一份已确认计划内它唯一且不重排。", item=label)

    capacity = _as_int(item.get("authorized_article_capacity"))
    minimum_articles = _as_int(item.get("minimum_articles"))
    minimum_publications = _as_int(item.get("minimum_publications"))

    # §4.4 L493:v1 固定 capacity=1。写 2 不是「加量」,是把一个格扩成隐形多槽。
    if capacity != V1_ITEM_CAPACITY:
        _fail(out, "item_capacity_not_v1",
              f"交付项 {label} 的可写容量不是 1 篇。",
              "本版每格固定 1 篇;要加量请新增交付格,不要把一格撑大。",
              item=label)

    if minimum_articles is None or minimum_articles < 0:
        _fail(out, "item_bad_minimum_articles",
              f"交付项 {label} 的最低稿件数不合法。",
              "最低稿件数填 0 或 1。", item=label)
    if minimum_publications is None or minimum_publications < 0:
        _fail(out, "item_bad_minimum_publications",
              f"交付项 {label} 的最低发布数不合法。",
              "最低发布数填 0 或 1。", item=label)

    # §4.4 L494:0 <= minimum_publications <= minimum_articles <= capacity。
    # 「禁止签出 0 稿 1 发布」就是左半边这条不等式。
    if (
        minimum_publications is not None
        and minimum_articles is not None
        and minimum_publications > minimum_articles
    ):
        _fail(out, "item_publication_exceeds_article",
              f"交付项 {label} 承诺的发布数多于稿件数(没有稿子发不出去)。",
              "把该项的最低发布数降到不超过最低稿件数,或补一篇稿子。",
              item=label, shortfall=minimum_publications - minimum_articles)

    if (
        minimum_articles is not None
        and capacity is not None
        and minimum_articles > capacity
    ):
        _fail(out, "item_article_exceeds_capacity",
              f"交付项 {label} 承诺的稿件数超过该格可写容量。",
              "新增交付格来承载多出来的稿件。",
              item=label, shortfall=minimum_articles - capacity)


def validate_plan(plan: Mapping[str, Any]) -> PlanVerdict:
    """校验一份 ``geo-delivery-plan-v1``。**恒返回**,不抛业务异常。

    调用时机 = activation 之前(DEL-02)。返回 ``ok=False`` 时,
    ``violations`` 里每条都带差额与修复出口,可直接投影给销售看。
    """
    out: list[PlanViolation] = []

    if not isinstance(plan, Mapping):
        return PlanVerdict(False, (PlanViolation(
            "plan_not_object", None, "交付计划不是一个对象。", None,
            "重新签发这份报价的交付计划。"),))

    if plan.get("schema_version") != SCHEMA_VERSION:
        _fail(out, "plan_bad_schema_version",
              "这份交付计划不是本版格式。",
              "用本版重新签发计划;旧版计划继续走原有流程,不要就地改写。")
        # schema 不对就不必再逐项挑刺 —— 后面的键名都可能是另一套。
        return PlanVerdict(False, tuple(out))

    if plan.get("campaign_mode") not in CAMPAIGN_MODES:
        _fail(out, "plan_bad_campaign_mode",
              "这份报价没有标明是防守、抢推荐还是两条线一起看。",
              "在报价头标明目标类型后重新签发。")

    raw_items = plan.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail(out, "plan_items_not_list", "交付计划里没有交付项清单。",
              "至少签发一个合同交付格后再确认。")
        return PlanVerdict(False, tuple(out))
    items = [i for i in raw_items if isinstance(i, Mapping)]
    if len(items) != len(raw_items):
        _fail(out, "plan_item_not_object", "交付项清单里有不是对象的元素。",
              "重新签发这份计划。")

    for index, item in enumerate(items):
        _validate_item(item, index, out)

    minimums = plan.get("contract_minimums")
    if not isinstance(minimums, Mapping):
        _fail(out, "plan_missing_contract_minimums",
              "这份计划没有写明合同最低交付量。",
              "补齐最低问题数、稿件数、发布数、公开媒体数与站点数后重新签发。")
        return PlanVerdict(False, tuple(out))

    declared: dict[str, int | None] = {
        k: _as_int(minimums.get(k)) for k in CONTRACT_MINIMUM_KEYS
    }
    for name, value in declared.items():
        if value is None or value < 0:
            _fail(out, "plan_bad_contract_minimum",
                  f"合同最低交付量「{name}」不是合法的非负整数。",
                  "把该项最低量改成 0 或正整数后重新签发。")
    if any(v is None or v < 0 for v in declared.values()):
        return PlanVerdict(False, tuple(out))

    contract = _contract_items(items)

    # ---- §4.4 L495-496:header 等式只对 contract item 求和,bonus 不计 ----
    sum_min_articles = sum(_as_int(i.get("minimum_articles")) or 0 for i in contract)
    sum_min_publications = sum(
        _as_int(i.get("minimum_publications")) or 0 for i in contract
    )
    sum_capacity = sum(
        _as_int(i.get("authorized_article_capacity")) or 0 for i in contract
    )

    if declared["articles"] != sum_min_articles:
        _fail(out, "header_articles_mismatch",
              "报价头写的最低稿件数,和各交付格加起来的数量对不上。",
              "让两边一致:要么改报价头,要么调整交付格。",
              shortfall=abs((declared["articles"] or 0) - sum_min_articles))

    if declared["publications"] != sum_min_publications:
        _fail(out, "header_publications_mismatch",
              "报价头写的最低发布数,和各交付格加起来的数量对不上。",
              "让两边一致:要么改报价头,要么调整交付格。",
              shortfall=abs((declared["publications"] or 0) - sum_min_publications))

    # §4.4 L492:publications <= articles <= sum(capacity)。
    # 注意这条**独立于**上面两条等式:等式管「报价头有没有说谎」,
    # 这条管「这份承诺本身可不可能兑现」。两条都要,少一条就有一类假计划能过。
    if (declared["publications"] or 0) > (declared["articles"] or 0):
        _fail(out, "header_publication_exceeds_article",
              "整单承诺的发布数多于稿件数(没有稿子发不出去)。",
              "降低最低发布数,或增加最低稿件数。",
              shortfall=(declared["publications"] or 0) - (declared["articles"] or 0))

    if (declared["articles"] or 0) > sum_capacity:
        _fail(out, "header_article_exceeds_capacity",
              "整单承诺的稿件数超过所有交付格加起来的可写容量。",
              "新增交付格来承载多出来的稿件。",
              shortfall=(declared["articles"] or 0) - sum_capacity)

    # ---- §4.4 L494(DEL-12):每个合同格必须绑不同的冻结问题身份 ----
    identities = [
        (i.get("question_identity_key"), _as_int(i.get("question_revision")))
        for i in contract
    ]
    distinct = {p for p in identities if p[0] is not None and p[1] is not None}
    if len(distinct) != len(contract):
        _fail(out, "duplicate_contract_question_identity",
              "同一道问题被重复算进了多个合同交付格。",
              "把重复的交付格换成不同的问题,或把它标成赠送项。",
              shortfall=len(contract) - len(distinct))

    if declared["questions"] != len(distinct):
        _fail(out, "header_questions_mismatch",
              "报价头写的最低问题数,和实际签发的不同问题数量对不上。",
              "让两边一致:补足不同的问题,或改报价头的最低问题数。",
              shortfall=abs((declared["questions"] or 0) - len(distinct)))

    # ---- §4.4 L497:两个媒体指标不得互换 ----
    if (declared["distinct_public_media"] or 0) > (declared["publications"] or 0):
        _fail(out, "media_exceeds_publications",
              "承诺的不同公开媒体家数,多于总发布次数。",
              "降低不同媒体家数,或增加最低发布数。",
              shortfall=(declared["distinct_public_media"] or 0)
              - (declared["publications"] or 0))

    if (declared["distinct_root_domains"] or 0) > (
        declared["distinct_public_media"] or 0
    ):
        _fail(out, "root_domain_exceeds_media",
              "承诺的不同站点数,多于不同公开媒体家数。",
              "降低不同站点数,或增加不同公开媒体家数。",
              shortfall=(declared["distinct_root_domains"] or 0)
              - (declared["distinct_public_media"] or 0))

    # ---- §4.2 L458:global_ordinal 在同一份已确认计划内唯一 ----
    ordinals = [_as_int(i.get("global_ordinal")) for i in items]
    present = [o for o in ordinals if o is not None]
    if len(set(present)) != len(present):
        _fail(out, "duplicate_global_ordinal",
              "交付项的排序号有重复。",
              "重新编排序号;同一份已确认计划内它唯一且不重排。")

    keys = [i.get("plan_item_key") for i in items]
    present_keys = [k for k in keys if isinstance(k, str) and k]
    if len(set(present_keys)) != len(present_keys):
        _fail(out, "duplicate_plan_item_key",
              "交付格身份有重复。",
              "为每个交付格签发独立身份后重新出计划。")

    return PlanVerdict(not out, tuple(out))


def capacity_of(plan: Mapping[str, Any]) -> int:
    """整单可写容量上限 —— 与最低交付量**分开**取数(DEL-01)。

    这个函数存在的唯一理由:让「容量」和「最低量」在代码里也没有共用出口。
    共用一个 getter 迟早会有人把两者当同一个数用,那正是 §19 第 11 发变异。
    """
    items = plan.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return 0
    return sum(
        _as_int(i.get("authorized_article_capacity")) or 0
        for i in items
        if isinstance(i, Mapping)
    )


def contract_minimums_of(plan: Mapping[str, Any]) -> dict[str, int]:
    """整单合同最低交付量 —— 与容量**分开**取数(DEL-01)。"""
    minimums = plan.get("contract_minimums")
    if not isinstance(minimums, Mapping):
        return {k: 0 for k in CONTRACT_MINIMUM_KEYS}
    return {k: (_as_int(minimums.get(k)) or 0) for k in CONTRACT_MINIMUM_KEYS}
