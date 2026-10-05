"""行业归并 · **付费读路径唯一入口**(零 LLM · 绝不写共享表)。

问题(2026-08-08 工单 `WO_INDUSTRY_KEY_RESOLVER_WIRING`):
    `normalize_industry_key(自由文本)` 对别名表外的行业**退化成 slugify**。
    品牌档案里的行业是自由文本(`brands.industry` / `geo_douyin_posts.industry_key`),
    候选池 `geo_research_answer_entities.industry_key` 是受控枚举 —— 两侧文字不同就
    `WHERE ... = '<自由文本>'` 命中 0 行,**不报错**,只表现为"这个行业没有数据"。

🔴 为什么是**共享服务**而不是"在榜单链上再接一次":
    同一个失败模式在本仓已经发生过三次,三条链各自长出了三种行为 ——
      ① 自助调研写侧    `industry_resolver.resolve_or_create_industry`(全量 · 会写库)
      ② 自助调研轻量读  `resolve_or_create_industry(allow_llm=False, persist=False)`
                        (`api/research_selfserve_api.py` FIX1-01 出口审核裁定)
      ③ 发布中心读榜    `media_effectiveness_board._resolve_to_canonical`
                        (缺陷 #3 · 2026-07-06 · 刻意不 import resolver 以避 LLM 重依赖)
      ④ 品牌/榜单/图文  **什么都没有**
    再照抄一遍就是第三份本地实现。本模块是③④共用的那一份;②是 resolver 自己的只读模式,
    不是副本,不动它。

🔴 **签名上就不接受写模式** —— 本模块没有 `persist` 参数,也不 import
   `industry_resolver`(那条路径带 httpx / llm_track / intent 分类器,且默认
   `persist=True` 会 `ensure_research_industry` 建行业 + `write_alias` 覆盖全局别名)。
   付费读路径靠"调用方记得传 persist=False"是守不住的,所以这里连传都传不了。

四态合同(REVIEW 裁定 2026-08-08 · finding 4):
    `resolved_ready`        归并成功且候选池有货 → 正常生成
    `resolved_no_inventory` 归并成功但池子真没数据 → 如实说覆盖不足 + 给下一步
    `alias_missing`         平台可能有数据,只是这个写法还没归并 → 一键确认
    `ambiguous`             无法可靠判断 → 保留原文让人选
🔴 今天那句「这个行业还没攒够可以点名的同行数据」**只对 `resolved_no_inventory` 是真话**;
   对 `alias_missing` / `ambiguous` 是**假话** —— 那正是 Owner 在生产看到的那条降级提示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Final, Optional

from services.media_entity_flywheel import is_all_industry_scope, normalize_industry_key

logger = logging.getLogger("GEO-IndustryCanonical")

#: 冻结件的口径版本。包 B 把它连同 `industry_key` 一起冻进 `generation_meta`,
#: 执行期只消费冻结值 —— 期间 admin 改行业名不影响这一单(RFC C 落地前的最小护栏)。
#: 🔴 v1.1(WO_267 · 2026-09-23):带品牌上下文的调用方显式开启「行业路由前段」
#:    (admin 人工别名 > 行业大类字典 > LLM 别名,与写路径 industry_resolver 共用
#:    `services.industry_routing.route_front`)。v1.0 冻结件按设计返 None ⇒ 调用方现场重归并。
MAPPING_CONTRACT_VERSION: Final = "industry-canonical-v1.1"

STATUS_READY: Final = "resolved_ready"
STATUS_NO_INVENTORY: Final = "resolved_no_inventory"
STATUS_ALIAS_MISSING: Final = "alias_missing"
STATUS_AMBIGUOUS: Final = "ambiguous"

#: 四态全集。**只有四个** —— `general` / 全行业 scope 不另立第五态,
#: 它照样走 ready / no_inventory 两支(只是没做归并),见 `resolve_readonly`。
ALL_STATUSES: Final = (STATUS_READY, STATUS_NO_INVENTORY,
                       STATUS_ALIAS_MISSING, STATUS_AMBIGUOUS)

#: 归并来源。`frozen` = 包 B 在下单前冻好的,执行期原样消费,不再查库。
BY_ALIAS_CACHE: Final = "alias_cache"
BY_PASSTHROUGH: Final = "passthrough"
BY_ALL_SCOPE: Final = "all_scope"
BY_FROZEN: Final = "frozen"
#: 行业大类字典判出的调研行(WO_267)。字典判出了大类就算「归并到了」——
#: 该行还没建 / 停用 / 没数据 ⇒ `resolved_no_inventory`(如实说覆盖不足),不是 alias_missing。
BY_TAXONOMY: Final = "taxonomy"

#: 子串碰撞判歧义时的最短长度。1 个字的行业名(如"业")跟谁都像,
#: 放进来只会把所有行业判成 ambiguous。
_MIN_COLLIDE_LEN: Final = 2

#: LLM 归并的最低采信置信度。低于它**不归并、也不沉淀别名**,落 `ambiguous` 让人确认。
#:
#: 🔴 为什么必须有这道闸(2026-08-08 Owner 实测点破):
#:    上一版拿到 `confidence` 只是原样传给 `write_alias`,**从不据此判断** ——
#:    0.6 的归并与 0.95 的归并待遇完全相同,而且**会被沉淀成别名**。
#:    一条错别名沉淀之后此后永远零 LLM 缓存命中,**再没有人会发现它是错的**。
#:    实测样本:`包车 → 旅游酒店`(0.6) —— 清单里明明有「汽车」;
#:            `黄金回收 → 金融理财`(0.75) —— Owner 判牵强。
#:    本模块注释里一直写着「归错行业比归不到严重得多」(家装客户拿到金融行业的同行名单,
#:    而且他不会察觉),但那句话此前没有任何一行代码在执行它。
#:
#: 取 0.8 的依据是实测分布本身有个空档:0.6 / 0.7×5 / 0.75 | 0.8×6 / 0.85×3 / 0.9×2 / 0.95×12。
#: 代价:43 种里 7 种(15 品牌)从「静默归并」变成「问一句」,其中 2 种本来就会归错。
#: 🔴 这是**系统系数**,后台可调 —— 文档与锁都不许把 0.8 这个数字当业务承诺。
MERGE_CONFIDENCE_MIN: Final = 0.8


@dataclass(frozen=True)
class CanonicalIndustry:
    """一次行业归并的完整结果。

    🔴 `status` 在 `resolve_readonly()` 出来时**还没有最终值** —— 判「有没有货」
       需要候选池的行数,那是调用方查的。必须再过一次 `with_inventory()`。
       故意分两步:让"归并"和"有无库存"在类型上分得开,免得又出现
       `resolved_no_inventory` 被当成"归并失败"的混淆(那正是本工单的根因形态)。
    """

    raw: str
    canonical_name: str
    industry_key: str
    industry_id: Optional[int] = None
    status: str = STATUS_ALIAS_MISSING
    resolved_by: str = BY_PASSTHROUGH
    #: 歧义时命中的多个平台行业名(给"让人选"用),非歧义为空。
    candidates: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        """是否真的归并到了平台标准行业(而不是原样透传)。"""
        if self.resolved_by == BY_TAXONOMY:
            return True
        return self.resolved_by in (BY_ALIAS_CACHE, BY_FROZEN) and self.industry_id is not None

    def to_meta(self) -> dict[str, Any]:
        """落 `generation_meta` 的那一份(冻结件 + 留痕)。"""
        meta: dict[str, Any] = {
            "raw": self.raw,
            "canonical_name": self.canonical_name,
            "industry_key": self.industry_key,
            "industry_id": self.industry_id,
            "status": self.status,
            "resolved_by": self.resolved_by,
            "mapping_version": MAPPING_CONTRACT_VERSION,
        }
        if self.candidates:
            meta["candidates"] = list(self.candidates)
        return meta


def _clean(text: Any) -> str:
    return str(text or "").strip()


def _fetch_one(sql: str, params: tuple) -> Optional[dict]:
    """一次只读 SELECT。**惰性 import**,不在模块 import 期强连库。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _industry_name_by_id(industry_id: int) -> str:
    """按 id 回取规范行业名(仅 active)。软删行业返空 → 上层退回原文。"""
    row = _fetch_one(
        "SELECT name FROM geo_research_industries WHERE id = %s AND active = TRUE",
        (int(industry_id),))
    return _clean(row["name"]) if row else ""


def _industry_by_name(name: str) -> Optional[int]:
    """按规范名精确取 id(仅 active)。用户原文**恰好**就是标准行业名时走这里。"""
    row = _fetch_one(
        "SELECT id FROM geo_research_industries WHERE name = %s AND active = TRUE LIMIT 1",
        (name,))
    return int(row["id"]) if row else None


def _active_industry_names() -> list[str]:
    """平台标准行业清单(仅 active)。只在**别名未命中**那一支才查,成本有界。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM geo_research_industries WHERE active = TRUE")
        return [_clean(r["name"]) for r in (cur.fetchall() or []) if _clean(r["name"])]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _colliding_industries(raw: str) -> list[str]:
    """原文与哪些平台标准行业**子串互含** —— 与 `normalize_industry_key` 同口径。

    🔴 这是零 LLM 判歧义的唯一依据,**数据驱动**(读 `geo_research_industries`),
       不新写第二份行业词表(工单 §6 边界)。命中 ≥2 家 = 这段原文同时像好几个行业,
       此时**宁可让人选,也不硬塞** —— 归错行业比归不到严重得多
       (家装客户拿到金融行业的同行名单)。
    """
    if len(raw) < _MIN_COLLIDE_LEN:
        return []
    hits: list[str] = []
    for name in _active_industry_names():
        if len(name) < _MIN_COLLIDE_LEN:
            continue
        if name in raw or raw in name:
            hits.append(name)
    return hits


def resolve_readonly(raw_industry: str, *, taxonomy: bool = False,
                     brand: Optional[dict] = None) -> CanonicalIndustry:
    """把行业原文归并到平台标准行业。**零 LLM · 只读 · 绝不写任何共享表**。

    三级(与 FIX1-01 裁定的 `allow_llm=False, persist=False` 同口径,但不拉 LLM 依赖):
      级 0  全行业 scope(`general` / 空)→ 不做归并,原样交给下游 all-scope 判定。
      级 1  别名缓存精确命中 → 取该行业规范名(仅 active)。**这是零成本的那一级**。
      级 2  未命中 → 原文恰好是标准行业名?是则也算命中(头部行业从没自助过也能对上)。
      级 3  仍未命中 → 原样透传(**行为与今天逐字相同**)。

    🔴 出来时 `status` 还没有最终值,必须再过 `with_inventory()` —— 歧义也在那里判。

    `taxonomy=True`(WO_267 · 只有带品牌上下文的调用方开):级 1 换成**行业路由前段**
    `route_front`(admin 人工别名 > 行业大类字典 > LLM 别名)—— 与付费写路径同一份顺序,
    同一原文 + 同一品牌判出同一调研行。不开 ⇒ 与 v1.0 逐字相同。

    fail-soft:别名表 / DB 任何异常 → 退回原样透传,**绝不因归并失败让榜崩**。
    """
    raw = _clean(raw_industry)

    if not raw or is_all_industry_scope(raw):
        return CanonicalIndustry(
            raw=raw, canonical_name=raw, industry_key=normalize_industry_key(raw),
            resolved_by=BY_ALL_SCOPE)

    try:
        from db.research_selfserve_db import resolve_alias

        if taxonomy:
            from services.industry_routing import BY_TAXONOMY as _FRONT_TAXONOMY, route_front

            front = route_front(raw, brand=brand)
            if front is not None:
                return CanonicalIndustry(
                    raw=raw, canonical_name=front.industry_name,
                    industry_key=normalize_industry_key(front.industry_name),
                    industry_id=front.industry_id,
                    resolved_by=BY_TAXONOMY if front.by == _FRONT_TAXONOMY else BY_ALIAS_CACHE)
            industry_id = None        # 前段已查过别名(admin 与 LLM 两种),不再查第二遍
        else:
            industry_id = resolve_alias(raw)          # 内部已 _normalize_alias_text
        if industry_id is not None:
            name = _industry_name_by_id(industry_id)
            if name:
                return CanonicalIndustry(
                    raw=raw, canonical_name=name,
                    industry_key=normalize_industry_key(name),
                    industry_id=industry_id, resolved_by=BY_ALIAS_CACHE)
            # 别名指向的行业已软删 → 不路由死行业,落穿到下面。
            logger.info("[industry-canonical] 别名命中但行业已软删 raw=%r id=%s",
                        raw, industry_id)

        exact_id = _industry_by_name(raw)
        if exact_id is not None:
            return CanonicalIndustry(
                raw=raw, canonical_name=raw,
                industry_key=normalize_industry_key(raw),
                industry_id=exact_id, resolved_by=BY_ALIAS_CACHE)

    except Exception as exc:  # noqa: BLE001 — fail-soft:归并失败绝不断链
        logger.warning("[industry-canonical] 归并失败(退回原样透传): %s", str(exc)[:200])

    return CanonicalIndustry(
        raw=raw, canonical_name=raw, industry_key=normalize_industry_key(raw),
        resolved_by=BY_PASSTHROUGH)


def with_inventory(ci: CanonicalIndustry, pool_size: int) -> CanonicalIndustry:
    """用候选池行数把 `status` 定下来 —— 这是四态真正成形的地方。

    | 归并到了标准行业 | 池子有货 | status                  |
    |------------------|----------|-------------------------|
    | 是               | 是       | `resolved_ready`        |
    | 是               | 否       | `resolved_no_inventory` |
    | 否(原样透传)   | 是       | `resolved_ready`        ← 头部行业,normalize 就够了
    | 否(原样透传)   | 否       | `alias_missing`         ← **不是**"这个行业没数据"
    | 歧义             | 是       | `resolved_ready`        ← 碰巧有货就别再烦用户
    | 歧义             | 否       | `ambiguous`(保持)     |

    🔴 第 4 行是本工单的全部要害:原样透传 + 空池,今天一律说成
       "这个行业还没攒够可以点名的同行数据",而真相是**我们没把这个写法认出来**。

    🔴 歧义判定**只在这里做**(而不是在 `resolve_readonly` 里):它要多查一次
       行业清单,而只有"池子空了、又没归并上"的时候这个信息才有用。
       放在归并处 = 给发布中心每次渲染、给榜单每条成功路径都白加一次查询。
    """
    if int(pool_size or 0) > 0:
        return replace(ci, status=STATUS_READY)
    # 🔴 已经判过歧义的**不再改判**(freeze 的低置信裁决 / 冻结件带回来的)。
    #    否则下面那条子串碰撞只命中 1 家时会把它降成 alias_missing,
    #    "我们猜是 X 但没把握" 就退化成 "我们没认出来" —— 丢掉了那个建议。
    if ci.status == STATUS_AMBIGUOUS and ci.candidates:
        return ci
    if ci.merged or ci.resolved_by == BY_ALL_SCOPE:
        # 归并成功(或全行业口径)却没货 = 覆盖缺口,如实说,这句是真话。
        return replace(ci, status=STATUS_NO_INVENTORY)
    try:
        collisions = _colliding_industries(ci.raw)
        if len(collisions) >= 2:
            return replace(ci, status=STATUS_AMBIGUOUS,
                           candidates=tuple(sorted(collisions)))
    except Exception as exc:  # noqa: BLE001 — 判歧义失败退回 alias_missing,不断链
        logger.warning("[industry-canonical] 歧义判定失败: %s", str(exc)[:200])
    return replace(ci, status=STATUS_ALIAS_MISSING)


async def freeze_industry(raw_industry: str, *, taxonomy: bool = False,
                          brand: Optional[dict] = None) -> CanonicalIndustry:
    """**下单前**归并并冻结(包 B)。付费提交那一刻调,执行期不再归并。

    与 `resolve_readonly` 的区别只有两点,别的完全一样:
      ① 允许 LLM —— 这里是**付费提交**动作(不是浏览),不存在 FIX1-01 说的
         "浏览客户就烧一发 deepseek-v4-flash / 可被打成 DoS"。
      ② 归并成功时**沉淀别名** —— 只读路径没有这一步,而没有沉淀就没有复利:
         每一单都从头烧一遍,第二次不会更便宜。这才是"执行期不调 LLM"的真实理由
         (不是延迟 —— 这条链本来就在密集调 LLM,拿延迟当理由早晚被推翻)。

    🔴 **绝不新建行业**。调 resolver 时传 `persist=False`,所以它内部那条
       `elif persist: ensure_research_industry(fallback_name)`(LLM 挂了就把用户原文
       建成 active 行业)**永远走不到**。别名由本函数自己写,且只在 LLM 判定
       **merge 进一个已存在的行业**时写 —— `persist=False` 下 `industry_id` 非空
       当且仅当 decision 是 merge 且该行业真实存在。
       (Review 裁定 2026-08-08 finding 1:付费链禁止创建行业 / 覆盖别名。
        创建:本函数结构上做不到。
        覆盖:**由 `write_alias` 自己的原子「仅首次写入」保证** ——
        2026-08-09 之前这里写的是"下面 re-check 只在别名仍缺失时写",
        那句话在 re-check 删掉之后就成了文档残留,而且它描述的守卫本来就是
        check-then-act、不原子。现在守卫在数据库一次操作里,不在本函数里。)

    fail-soft:LLM / 写别名任何异常 → 退回 `resolve_readonly` 的结果,绝不断链。
    """
    ci = resolve_readonly(raw_industry, taxonomy=taxonomy, brand=brand)
    if ci.merged or ci.resolved_by == BY_ALL_SCOPE:
        return ci   # 别名已命中(或全行业口径)→ 零 LLM,直接冻。

    try:
        # 🔴 `resolve_alias` 这里**不再需要** —— 原来那句 re-check 已随 check-then-act
        #    一起删掉,守卫收进 `write_alias` 内部的原子操作里了。
        from db.research_selfserve_db import write_alias
        from services.research_monitor.industry_resolver import resolve_or_create_industry

        decision = await resolve_or_create_industry(
            ci.raw, allow_llm=True, persist=False)
        industry_id = decision.get("industry_id")
        name = _clean(decision.get("industry_name"))
        conf = decision.get("confidence")

        # 🔴 低置信**不归并、也不沉淀别名** —— 落 ambiguous 让人确认。
        #    把猜测写进共享别名表是不可逆的:此后永远缓存命中,错了也没人看得见。
        #    `candidates` 带上 LLM 的那个建议,好让用户"确认/否掉"而不是从零选。
        #
        # 🔴🔴 **本闸只对「归并到已存在行业」的决策生效**(2026-08-08 Review 增量裁定 ①)。
        #    resolver 对 **merge 与 new 两种决策都返回 `resolved_by="llm"`** ——
        #    区分信号是 `is_new`,上一版没读它。后果:LLM 判「这是个新行业」且低置信时,
        #    闸照样点火,把**平台根本不存在的行业名**塞进 candidates 推给用户去确认
        #    (实测 `test` → 新行业「测试」conf=0.1 就是这么漏的)。
        #    再叠一道 `industry_id is not None`:merge 到一个取不到 id 的名字(如已软删)
        #    同样不该拿去让用户确认。两条都不满足时落穿到下面,最终回 `alias_missing`。
        if (decision.get("resolved_by") == "llm" and name
                and not decision.get("is_new") and industry_id is not None
                and conf is not None and float(conf) < MERGE_CONFIDENCE_MIN):
            logger.info("[industry-canonical] 归并置信不足(%.2f < %.2f),不采信也不沉淀 raw=%r → %r",
                        float(conf), MERGE_CONFIDENCE_MIN, ci.raw, name)
            return replace(ci, status=STATUS_AMBIGUOUS, candidates=(name,))

        if decision.get("resolved_by") == "llm" and industry_id is not None and name:
            # 🔴🔴 2026-08-09 Review 裁定返工:这里原来是
            #        `if resolve_alias(...) is None: write_alias(...)`
            #    —— **check-then-act,不原子**。两句之间落进来的写会被无声覆盖,
            #    而 `write_alias` 当时是无条件 UPSERT,连 admin 人工改判
            #    (`resolved_by='admin'` + `reviewed_by`)一起盖掉。
            #    我在注释里宣称过"不覆盖 admin",**旧实现保证不了那句话**。
            #
            #    现在:`write_alias` 自己是「仅首次写入 + 写后重读」的原子操作,
            #    守卫收进数据库一次操作里,时间差没有了 → 这里的 re-check 删掉。
            #
            # 🔴 **以库里生效的那条为准,不是以我刚才想写的那个为准。**
            #    冲突时我们写不进去,而 `industry_id`/`name` 还是 LLM 那一版 ——
            #    照着它往下走,等于用一个**从未生效**的映射冻结这一单。
            effective = write_alias(ci.raw, int(industry_id), conf, resolved_by="llm")

            # 🔴🔴 **`active` 必须判在 ID 比较之前**(2026-08-09 Review 第四轮 P1)。
            #    我上一版把它塞进"ID 不同"那个分支里 —— 于是**停用的别名恰好指向
            #    LLM 选中的同一个行业**时,整段 active 判断被跳过,最后照样返回
            #    `resolved_by=alias_cache`。Review 在 PG16 上构造出来了:
            #        返回 industry_id=6 / resolved_by=alias_cache
            #        库里 active=false / industry_id=6
            #    「停用的别名被当成有效缓存」——**是不是同一个行业与它停没停用无关**,
            #    把两件事写在一个 if 里就是把它们绑成了一件。
            #
            #    ⚠️ 这条正是我上一轮"收窄影响面"收过头的产物:为了不改无冲突路径,
            #    我把 active 一起收进了冲突分支。收窄影响面是对的,收掉一条**独立的**
            #    前置条件不是。
            if effective and not effective.get("active"):
                # 停用的映射:既不采信它,也不拿我的去覆盖它 → 落穿回 alias_missing。
                logger.info("[industry-canonical] 生效别名已停用(industry_id=%s),不采信 raw=%r",
                            effective.get("industry_id"), ci.raw)
                return ci

            # 🔴 只在"生效值 ≠ 我写的值"时才改道 —— 没冲突就走原来那条路,逐字不变。
            #    (影响面刻意收到最小:本次要修的是"别人的映射被覆盖",
            #     不是顺手把行业名的取法也改了。)
            if effective and int(effective.get("industry_id") or 0) != int(industry_id):
                eff_id = int(effective["industry_id"])
                logger.info(
                    "[industry-canonical] 别名已被 %s 定为 industry_id=%s,"
                    "本次 LLM 的 %s 不采用 raw=%r",
                    effective.get("resolved_by"), eff_id, industry_id, ci.raw)
                eff_name = _industry_name_by_id(eff_id)
                if not eff_name:
                    # 指向已软删行业 → 不路由死行业(与 resolve_readonly 同口径)。
                    logger.info("[industry-canonical] 生效别名指向已软删行业 raw=%r id=%s",
                                ci.raw, eff_id)
                    return ci
                return CanonicalIndustry(
                    raw=ci.raw, canonical_name=eff_name,
                    industry_key=normalize_industry_key(eff_name),
                    industry_id=eff_id, resolved_by=BY_ALIAS_CACHE)

            return CanonicalIndustry(
                raw=ci.raw, canonical_name=name,
                industry_key=normalize_industry_key(name),
                industry_id=int(industry_id), resolved_by=BY_ALIAS_CACHE)
        logger.info("[industry-canonical] LLM 未归到已有行业(decision=%s),不建行业 raw=%r",
                    decision.get("resolved_by"), ci.raw)
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.warning("[industry-canonical] 冻结前归并失败(退回只读结果): %s", str(exc)[:200])
    return ci


def from_frozen(frozen: Any) -> Optional[CanonicalIndustry]:
    """把包 B 冻在 `generation_meta` 里的那一份读回来(执行期不再查库)。

    读不出 / 形状不对 → 返 `None`,调用方退回现场归并(fail-soft,不因冻结件缺失断链)。
    🔴 冻结件的 `mapping_version` 与本模块不一致时也返 `None`:
       口径变了就重新归并,拿旧口径的键去查新池子是静默错配的老路。
    """
    if not isinstance(frozen, dict):
        return None
    if _clean(frozen.get("mapping_version")) != MAPPING_CONTRACT_VERSION:
        return None
    key = _clean(frozen.get("industry_key"))
    if not key:
        return None
    status = _clean(frozen.get("status")) or STATUS_READY
    if status not in ALL_STATUSES:
        return None
    raw_id = frozen.get("industry_id")
    return CanonicalIndustry(
        raw=_clean(frozen.get("raw")),
        canonical_name=_clean(frozen.get("canonical_name")) or key,
        industry_key=key,
        industry_id=int(raw_id) if isinstance(raw_id, int) else None,
        status=status,
        resolved_by=BY_FROZEN,
        candidates=tuple(str(c) for c in (frozen.get("candidates") or []) if str(c)),
    )
