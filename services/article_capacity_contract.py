"""篇数容量合同 SSOT(P1 · 2026-08-08)

【一句话】报价冻结的 `required_articles` 是**可交付容量上限**,不是"必须完成量"。
交付允许 `0..capacity`;用不满不算失败,**也不得为了把进度做成 100% 而制造文章**
(GEO 缺口作战计划设计合同 §6.1)。

【为什么要有这个模块】
  2026-08-08 生产实测,同一个"篇数"在四个地方各自有一套口径:
    · `quotes.total_articles`      —— 137 张有词的单里 **52 张与词表对不上,且全是 0**
    · `SUM(confirmed_keywords.required_articles)` —— 真正被交付侧消费的那个数
    · 交付实况 `topics`             —— 12/137 张单已经**超出**冻结容量(386 单 45→117)
    · `articles`                   —— 372 单 15 篇容量下有 136 行 draft(重写稿,不占容量)
  谁读哪个数,决定他看到的是"容量 0"还是"容量 15"。本模块把它收成一个口径。

【容量单位裁定(实证驱动,不是拍脑袋)】
  容量占用单位 = **topics(交付槽)**,不是 articles 行数。铁证:
    · 报价 372:authorized 15 / topics 15 / articles 136(draft)
      → P4 合同说"报价 372 容量 0",只有按 topics 口径才成立(15-15=0);
        按 articles 口径会是 15-136 = 负数,按 quotes.total_articles 口径恒 0(那是脏数据不是容量)。
    · 报价 373:authorized 29 / topics 29;报价 409:每词 1/1。
  articles 是同一个 topic 下的多次生成/重写,**不重复占容量**。

【历史超额怎么办】
  12 张单的 topics 已经超过冻结容量。本合同**不追溯**:`available_articles` 夹到 0,
  同时把 `over_delivered_articles` 如实吐出来给对账,不假装守恒、也不回头改历史快照
  (08_billing §7.5「不得读取当前价格/关系/倍率重算历史」的同一条纪律)。

红线:本模块只读不写。任何容量增加都必须走报价快照链
(services/quote_pricing_snapshot.py 的版本化不可变快照),不许在这里改余额/改冻结。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT,
    normalize_article_capacity,
)


CAPACITY_CONTRACT_VERSION = "article-capacity-v1"

# 对齐 P4 设计合同 04-P4-api-contract-draft §2 的 capacity.display_status 三态
# (02-全状态文案表.csv 第 26-28 行)。**只有这三个值**,前端按值取文案,不自己算。
CAPACITY_STATUS_AVAILABLE = "capacity_available"
CAPACITY_STATUS_ZERO = "capacity_zero"
CAPACITY_STATUS_RESERVED = "capacity_reserved"

CAPACITY_STATUSES = (
    CAPACITY_STATUS_AVAILABLE,
    CAPACITY_STATUS_ZERO,
    CAPACITY_STATUS_RESERVED,
)

# 不足额原因回流词表(SSOT)。P4 缺口计划把它填进 delivery_plan_snapshot;
# 没有原因的不足额 = `capacity_shortfall_unexplained`,**不许静默当成"已完成"**。
SHORTFALL_REASON_NO_GAP = "no_worthwhile_gap"            # 眼下没有值得写的缺口(额度保留)
SHORTFALL_REASON_DOMAIN_BLOCKED = "domain_not_accessible"  # 引用位被进不去的域占据
SHORTFALL_REASON_DUPLICATE_COVERAGE = "duplicate_coverage"  # 已被另一篇覆盖,省下一篇额度
SHORTFALL_REASON_SWITCH_QUERY = "switch_to_adjacent_query"  # 建议换邻近问题
SHORTFALL_REASON_UNEXPLAINED = "capacity_shortfall_unexplained"

SHORTFALL_REASONS = (
    SHORTFALL_REASON_NO_GAP,
    SHORTFALL_REASON_DOMAIN_BLOCKED,
    SHORTFALL_REASON_DUPLICATE_COVERAGE,
    SHORTFALL_REASON_SWITCH_QUERY,
    SHORTFALL_REASON_UNEXPLAINED,
)

# 「有容量但一篇没排」与「有容量且已排过」在文案上不同(额度保留 vs 有可用额度)。
# 这条判据只影响 display_status,不影响任何数字。


class CapacityContractError(RuntimeError):
    """容量合同自身被违反(调用方给了不可能的输入)。"""


def normalize_capacity(raw: Any) -> int:
    """报价侧冻结值 → 容量上限。缺失走具名兜底常量,显式 0 保留,负数夹 0。"""
    return normalize_article_capacity(raw, when_missing=LEGACY_MISSING_CAPACITY_DEFAULT)


def build_capacity_view(
    *,
    authorized_articles: int,
    consumed_articles: int,
    reserved_articles: int = 0,
    quote_is_payable: bool = True,
) -> dict[str, Any]:
    """纯函数:给定三个原始数 → P4 合同里的 capacity 块。

    参数:
      authorized_articles —— 报价冻结的容量上限(SUM(required_articles) · is_core IS NOT FALSE)
      consumed_articles   —— 已占用的交付槽数(topics 口径,见模块头「容量单位裁定」)
      reserved_articles   —— 已被计划占住但还没落成交付槽的量(P4 的计划项;当前恒 0)
      quote_is_payable    —— 报价是否已付款/已激活。**未付款一律按容量 0 对外**
                             (设计合同 §3:未付款或容量为 0 → 只显示研究建议,不创建执行任务)

    返回字段名与 04-P4-api-contract-draft §2 逐字对齐,P4 直接透传,不再自己算。
    """
    authorized = normalize_capacity(authorized_articles)
    consumed = max(0, int(consumed_articles or 0))
    reserved = max(0, int(reserved_articles or 0))

    # 历史超额如实吐出,不假装守恒(12/137 张单真的超了)
    over_delivered = max(0, consumed - authorized)
    remaining = max(0, authorized - consumed - reserved)

    if not quote_is_payable:
        # 未付款:对外恒 0 可用,但 authorized 照实显示(客户要看到自己买的是多少)
        available = 0
        status = CAPACITY_STATUS_ZERO
    elif available_is_zero(remaining):
        available = 0
        status = CAPACITY_STATUS_ZERO
    elif consumed == 0 and reserved == 0:
        # 有额度、一篇没排 → 「额度已保留」(不是「快去写满」)
        available = remaining
        status = CAPACITY_STATUS_RESERVED
    else:
        available = remaining
        status = CAPACITY_STATUS_AVAILABLE

    return {
        "contract_version": CAPACITY_CONTRACT_VERSION,
        "authorized_articles": authorized,
        "reserved_articles": reserved,
        "consumed_articles": consumed,
        "available_articles": available,
        "over_delivered_articles": over_delivered,
        "display_status": status,
        # 语义护栏:任何消费方读到这个字段都应知道「篇数是上限不是完成率」
        "semantics": "upper_bound_0_to_capacity",
        # ── [WO_225-c1 §8.5] 🔴 零的**两种来源**必须能被分开 ──────────────
        #   未付款 ⇒ available=0;已付款但槽用完 ⇒ 也是 available=0。
        #   两者 display_status 都是 capacity_zero,消费方**无从分辨** ——
        #   而 Owner 2026-09-15 ④ 只对后者放行写作(「我们不是可以一直写嘛,
        #   正常计费即可,不用阻断不用提示」),对前者仍必须挡
        #   (08_billing:未付款不执行)。所以把这个事实如实吐出来,
        #   而不是让 P4 自己再判一次付款(那就是第二套口径,迟早打架)。
        "quote_is_payable": bool(quote_is_payable),
    }


def available_is_zero(remaining: int) -> bool:
    """单独拆出来是为了让变异测试能打到这条判据(内联三元运算打不上锁)。"""
    return int(remaining) <= 0


def describe_shortfall(
    *,
    capacity_view: Mapping[str, Any],
    reasons: Optional[list[str]] = None,
) -> dict[str, Any]:
    """不足额 + 原因回流。**不足额本身不是失败**,但必须有原因。

    P4 的缺口计划负责给 reasons;没给 → `capacity_shortfall_unexplained`,
    让"用不满且说不出为什么"这件事在数据里留痕,而不是被当成正常完成。
    """
    authorized = int(capacity_view.get("authorized_articles") or 0)
    consumed = int(capacity_view.get("consumed_articles") or 0)
    shortfall = max(0, authorized - consumed)
    clean: list[str] = []
    for reason in reasons or []:
        code = str(reason or "").strip()
        if code and code in SHORTFALL_REASONS and code not in clean:
            clean.append(code)
    if shortfall > 0 and not clean:
        clean = [SHORTFALL_REASON_UNEXPLAINED]
    return {
        "contract_version": CAPACITY_CONTRACT_VERSION,
        "shortfall_articles": shortfall,
        "reasons": clean,
        # 明示:不足额不计作失败(设计合同 §6.1 / 状态表 retained_capacity_no_gap)
        "counts_as_failure": False,
    }


# ============================================================
# 落库读取(只读 · 不写任何表)
# ============================================================

_AUTHORIZED_SQL = """
    SELECT COALESCE(SUM(required_articles), 0) AS authorized
      FROM confirmed_keywords
     WHERE quote_id = %s AND (is_core IS NOT FALSE)
"""

# 容量占用 = 交付槽(topics),不是 articles 行数(见模块头「容量单位裁定」)
# ══════════════════════════════════════════════════════════════════════
# 状态谓词(WO_225-c1 §8.4b · Review 2026-09-15 裁定)
# ══════════════════════════════════════════════════════════════════════
# 🔴 改之前是 `COUNT(*)` 不看 status:**出标题那一刻槽就被占满**。
#    生产实证(Deploy 225-d0):topics = draft 3350 / completed 653 / pending 405 /
#    failed 19 / write_timeout 1;12 张「超容量」单里 8 张是草稿与待办顶爆的,
#    真交付超额只有 4 单 37 篇。客户一篇没拿到就被告知「额度用完」。
#
# 🔴 `pending` **不占槽**,尽管它听起来像「已受理」:
#    `services/topic_generation_reservation.py` 抬头明写 ——
#    进 LLM **之前**先落 `status='pending'` 占位行、`optimized_title` 留空,
#    且「凭据行不是"成品",**不参与任何交付计数**」。
#    `db/diagnosis_db.py:5766` 的 flat 模式还会 DELETE 掉
#    `status IN ('draft','pending','regenerating')` —— 三者都是会被下一批顶掉的过渡态。
#
# 🔴 三套状态词表互相对不上,所以这里**显式列全**:
#      DDL 注释(db/diagnosis_db.py:454)写 draft/confirmed/writing/published
#        —— 其中 `confirmed` 全仓零写入点,且漏了 completed/pending/failed/
#           regenerating/write_timeout 五个;
#      生产实测只有 5 个有行(另外 3 个那一刻恰好为零);
#      **代码实际会写进 public.topics 的是下面这 8 个**。
#    「生产现在没有」不等于「不会出现」—— 谓词必须覆盖代码会写的全部状态,
#    否则新状态出现时被静默归错档。判据 `test_status_vocabulary_is_complete`
#    扫写入点得出的集合与这里声明的集合逐项比对,多一个少一个都红。
CONSUMED_STATUSES = ("completed", "published")      # 已交付:正文已生成 / 已发布
RESERVED_STATUSES = ("writing",)                    # 进行中:真的在写
NON_OCCUPYING_STATUSES = ("draft", "pending", "regenerating",
                          "failed", "write_timeout")
#: 本模块声明的全部状态 —— 三组必须**恰好**覆盖它,不重不漏。
ALL_TOPIC_STATUSES = tuple(sorted(
    CONSUMED_STATUSES + RESERVED_STATUSES + NON_OCCUPYING_STATUSES))

#: 🔴 声明了、但**全仓没有任何写入点**的状态。每一条都要写明为什么还留着。
#:
#:   `published` —— 机械扫描(tests/media_slot_conversion_2026_09_15/status_scan.py,
#:     AST 扫 39 个对 public.topics 的 execute 写入点)的结论是:**没有写入点**。
#:     工单 §8.4b 与我自己的设计注记 §5② 都写过「代码有写入点」,
#:     指的是 `services/optimize_title_jobs.py:80` —— 那一行是
#:     `SELECT 1 FROM topics t WHERE ... t.status IN (...)`,**是读不是写**。
#:     留着它的理由:两处**读**谓词把它当成 topics 的合法态
#:     (optimize_title_jobs.py:80 与 writing/article_generator_service.py:2631),
#:     哪天有人补上写入点,已发布的稿子必须立刻算进 consumed,而不是
#:     因为谓词漏了它被静默归成「不占槽」。
#:     🔴 结构锁会检查这份名单:一旦 `published` 真的有了写入点,扫描集合就多一个,
#:        而它还挂在这里 —— 判据红,逼人把它挪出去。名单不会烂在这里。
DECLARED_WITHOUT_WRITE_SITE = ("published",)

#: 🔴 **出现在 topics.status 的读谓词里、却既没有写入点也不在声明集合里**的值。
#:   `titles_ready` 是 `quotes.writing_status` 的值(optimize_title_jobs.py 同一条 SQL
#:   的 UPDATE 侧就在写它),被顺手写进了子查询的 `t.status IN (...)` —— 那个位置上
#:   它**永远不成立**。不加进声明集合(声明一个不存在的状态等于编),
#:   在这里留痕:它是一处读谓词里的死值,不是本单引入的,也不由本单删。
READ_ONLY_PHANTOM_STATUSES = ("titles_ready",)

#: 折算用的定点标度(微槽)。**全程整数**,禁 float —— 与 08_billing §3.3 同纪律。
_SLOT_SCALE = 1_000_000

_TOPIC_GROUPS_SQL = """
    SELECT status, media_bucket, COUNT(*) AS cnt
      FROM topics
     WHERE quote_id = %s
     GROUP BY status, media_bucket
"""

#: [WO_225-c1] 原口径保留在这里,只给判据用:老单(桶全 NULL)在**同一状态谓词**下,
#: 新读数必须逐字等于它。名字不叫 _CONSUMED_SQL 了 —— 它已经不是唯一读数。
_LEGACY_CONSUMED_SQL = """
    SELECT COUNT(*) AS consumed FROM topics
     WHERE quote_id = %s AND status = ANY(%s)
"""


def _fold_slots(rows, bps_table) -> int:
    """把 (bucket, cnt) 折算成**槽数**。全整数。

    一条 bucket 媒体的帖子占 `10000/bps` 个槽(coverage 50000 bps ⇒ 0.2 槽,
    即 5 条填满一个槽)。桶为 NULL ⇒ 10000 ⇒ 一条一槽(老行为)。
    """
    from services.media_slot_conversion import ONE_SLOT_BPS, bps_for
    total = 0
    for bucket, cnt in rows:
        bps = bps_for(bucket, bps_table)
        total += int(cnt) * _SLOT_SCALE * ONE_SLOT_BPS // int(bps)
    return (total + _SLOT_SCALE // 2) // _SLOT_SCALE

_QUOTE_SQL = """
    SELECT q.id, q.brand_id, q.owner_user_id, q.status, q.service_status,
           q.paid_amount, q.source_type, b.owner_user_id AS brand_owner_user_id
      FROM quotes q
      LEFT JOIN brands b ON b.id = q.brand_id
     WHERE q.id = %s AND q.deleted_at IS NULL
"""

# 未付款判定:与 services/article_delivery_plan.load_quote_authority_event 同口径
#   (standard 走 confirmed+active;线下/代理激活走 paid|active + service_status=active;
#    零价写作项目 paid_amount=0 但 source_type 在白名单里也算已授权)
_PAYABLE_STATUSES = {"confirmed", "paid", "active"}
_ZERO_PRICE_SOURCE_TYPES = {"c_end_geo_plan", "quick_writing"}


def quote_is_payable(quote_row: Mapping[str, Any]) -> bool:
    """报价是否已进入"可执行"状态(未付款 → 对外容量 0)。"""
    status = str(quote_row.get("status") or "").lower()
    service_status = str(quote_row.get("service_status") or "").lower()
    if status not in _PAYABLE_STATUSES:
        return False
    if str(quote_row.get("source_type") or "") in _ZERO_PRICE_SOURCE_TYPES:
        return True
    if service_status in {"active", "expiring", "expired"}:
        return True
    return float(quote_row.get("paid_amount") or 0) > 0


def resolve_quote_capacity(cur, quote_id: int) -> dict[str, Any]:
    """读一张报价单的容量态(只读)。找不到报价 → 抛 CapacityContractError。"""
    cur.execute(_QUOTE_SQL, (int(quote_id),))
    row = cur.fetchone()
    if not row:
        raise CapacityContractError(f"quote_not_found:{quote_id}")
    quote = dict(row)

    cur.execute(_AUTHORIZED_SQL, (int(quote_id),))
    authorized = int((dict(cur.fetchone()) or {}).get("authorized") or 0)

    # [WO_225-c1] 按 (status, media_bucket) 分组,再按桶折算成槽。
    #   桶全 NULL 的老单 ⇒ 每条一槽 ⇒ 与原 COUNT(topics) 在同一状态谓词下逐字相等。
    from services.media_slot_conversion import resolve_for_quote
    #   🔴 把**调用方的游标**传下去:它决定读的是哪个 schema 的快照表。
    #      另开连接会读默认 schema,而这里的 cur 可能指着别处(判据的一次性 schema),
    #      且「读不到 ⇒ 回落 10000 ⇒ 老行为」会让读错对象也不红。
    _bps = dict(resolve_for_quote(int(quote_id), cur)["bps"])  # type: ignore[arg-type]
    cur.execute(_TOPIC_GROUPS_SQL, (int(quote_id),))
    _rows = [dict(r) for r in (cur.fetchall() or [])]
    _consumed_rows = [(r.get("media_bucket"), r.get("cnt") or 0)
                      for r in _rows if r.get("status") in CONSUMED_STATUSES]
    _reserved_rows = [(r.get("media_bucket"), r.get("cnt") or 0)
                      for r in _rows if r.get("status") in RESERVED_STATUSES]
    consumed = _fold_slots(_consumed_rows, _bps)
    reserved = _fold_slots(_reserved_rows, _bps)

    view = build_capacity_view(
        authorized_articles=authorized,
        consumed_articles=consumed,
        reserved_articles=reserved,
        quote_is_payable=quote_is_payable(quote),
    )
    view["quote_id"] = int(quote_id)
    return view


def resolve_owner_user_id(quote_row: Mapping[str, Any]) -> Optional[int]:
    """报价归属 = quotes.owner_user_id,为空回落品牌 owner(与 article_delivery_plan 同口径)。"""
    owner = quote_row.get("owner_user_id")
    if owner is None:
        owner = quote_row.get("brand_owner_user_id")
    return int(owner) if owner is not None else None
