# -*- coding: utf-8 -*-
"""WO_218-c1 · 「生成标题」这次会扣多少 —— 单点计算,后端算好交给前端**显示**。

现状(工单 §事实):`POST /api/writing/generate-titles` 按 `topic_gen` 真扣真客户算力,
而写作大厅那个按钮**一个数都不显示** —— 违反元指令 2「按钮级确认扣费」与
Owner 09-13「不直观、要人猜的展示不允许」。

🔴 为什么算在后端、交给前端显示,而不是让前端自己乘:
   08_billing / 元指令 1 —— **前端不算钱**。前端自己按关键词数乘一遍,
   迟早会和后端的倍率口径分家,而**分家时两边各自都对**,只有客户看到的数是错的。

🔴 为什么单独一个模块而不是写在 `server.py` 里那个函数中间:
   同一个金额在一个函数里被算三次(组织路径的 reserve 上限、个人路径的 extra、
   交给前端的预估),三处只要有一处跟不上,**客户看到的数就和实扣对不上**,
   而那正是最不容易被发现的一种错(本仓
   feedback_one_predicate_one_place_or_half_goes_unverified)。
   放成纯函数之后,它可以被判据直接验,不需要把半个 server.py 搭起来。

═══════════════════════════════════════════════════════════════════════
🔴 前端契约(218-a1 / WO_243 乙 按本文件实现)· **全文只有这一处权威声明:
   以本文件为准,不以消息为准。本文件内若两处说法冲突,以日期更近的那节为准,
   旧节必须当场作废成一行指路,不留可被照抄的表述。**

§1 字段契约
═══════════════════════════════════════════════════════════════════════

**载点(两处,同一个 `preview()`,不许各算一份)**

| 端点 | 什么时候拿得到 | 给谁看 |
|---|---|---|
| 🔴 `GET /api/writing/projects/{quote_id}` | **点按钮之前**,且 `keyword_count` 一变就会被重新拉 | 按钮上的「本次将扣 N」 |
| `POST /api/writing/generate-titles` | 已经扣完之后 | 回执 / 对账 |

🔴 **载点的选法是个原则,不是一个端点名**:必须挂在
**「`keyword_count` 一变就会被重新拉」**的端点上。
详情端点满足它,是因为 `add-keyword` 成功后前端已经在调它(`loadProjectDetail()`);
**列表端点不满足** —— 加完关键词不会重拉,屏幕会停在旧价上,
而「显示一个过期的价」比「不显示」更糟:客户是按那个数同意扣费的。

🔴 两处的数**必须逐字相等**。它们共用的不只是公式,是同一个函数
(`charge_card()`)和同一个份数定义(`keyword_count()`,**定义见该函数本身** ——
这里不复述它的公式:2026-09-19 丙 把它从「整表长度」改成「真正要出题的词数」,
而本节原来抄着旧公式,**抄一遍就是将来的一处分歧**),
而 `keywords` 两边都来自同一次 `get_writing_project_detail(quote_id)`。
「两边各写一遍、碰巧现在一样」是本单要消灭的东西,不是本单的实现方式。

═══════════════════════════════════════════════════════════════════════
§2 那颗按钮的两个面孔 —— WO_241 丙 定稿(2026-09-19,Review 裁定)
═══════════════════════════════════════════════════════════════════════
「生成标题」按钮(`WritingHall.tsx:6435` 的三元)拆成两条,
**拆的依据是「这个词的钱有没有付过」**,不是端点名字、也不是按钮文案:

| 面孔 | 钱 | 走哪条 | 显示价? |
|---|---|---|---|
| 新加的词出标题(`isNewKwOnly`) | **从没付过费** | `POST /api/writing/generate-titles` + `per_keyword_plan` 只把新词置 >0、其余置 0 | **显示 `charge_new_keywords_only`** |
| 某批次里缺题的词补救 | **已经付过费** | `POST .../keywords/{id}/generate-topic` + `generation_request_id` | **不显示价**(免费) |

**为什么补救免费**:退费只在**一条题都没出**时触发
(`api_generate_titles` 的 `if not topics:` / `if not topic_ids:`);
**部分成功一分不退** —— 出了一部分、另一部分缺题时,用户**已经按 N 个词付过钱**。
组织路径同口径(`settle_charge(actual_points=预留上限)`,不按实际出题数)。
再收一次就是**对同一个词收两次**。

**为什么新词收费**:那些词从没进过任何批次,也就从没付过 —— 它是全新生产。

🔴 **详情端点给两个报价,别用错**:
   · `charge` = **整表**的价(所有要出题的词);
   · `charge_new_keywords_only` = **还没有任何选题行的那些词**的价 ——
     「新词面」点击前显示的就是它。**空集给 `None` 不给 `0`**
     (`None` = 没有新词、不显示价;`0` = 有新词但免费)。
   两个都由**同一个 `charge_card()`** 产出,只差一道过滤;前端不许自己筛、自己乘。
   ⚠️ 「有没有选题」按 topics 行的 `keyword_id` 判,**不按标题是否为空** ——
   受理凭据行(`optimized_title IS NULL`)也算这个词已经进过批次,
   它是「缺题的词」走免费补救,不是「新词」。

🔴 **计费份数 = 本次真正要出题的词数**(`keyword_count()` ⇒ `plannable_keywords()`),
   不是整张关键词表的长度。带子集时只为子集收费;
   服务端另有运行期铁律锁:份数 != 真正出题词数 ⇒ **503 拒绝扣费,一分不扣**。

🔴 **`generation_request_id` 从哪里拿**(这一条写错会让前端去一个不存在的地方取):
   来自**项目详情**里 topics 行的 `generation_request_id` ——
   `GET /api/writing/projects/{id}` 的 topics 查询是 `SELECT t.*`
   (`db/diagnosis_db.py:5663`),所以这一列原样出现在每个 topic 行上。
   ⚠️ **不是**从 `POST /api/writing/generate-titles` 的回包拿 —— 那个回包里**没有**这个字段。
   ⚠️ 本节原引 `WritingHall.tsx:4475` 作为"前端已在读它"的旁证 —— **引错了**
   (A 2026-09-19 更正:那一行读的是 `failedList[0]` 的同名字段,不是 topics 行)。
   字段确实可用,但**证据是上面那条 `SELECT t.*`**,不是 4475。

🔴 **钱的标识不下发前端、也不由前端回传。** 前端只带 `generation_request_id`;
   服务端自己把它解到那一笔 charge 并校验**四件**
   (这一批真的付过 ∧ **这一笔没被退款** ∧ 这个词属于这一批 ∧ 这个词还没出题)。
   实现在 `services/title_batch_charge_registry.authorize_recovery`,以它为准。
   不符合 ⇒ **403 `TITLE_RECOVERY_NOT_AUTHORIZED`**,一律同一句文案 ——
   前端按 code 分支,别按文案。
   ⚠️ 第二件是 2026-09-19 追加的:整批失败会**全额退**,退完之后那批的词
   **不再是「已付」**,重试应走批量重新收费。

🔴🔴 **给 A 的待办(不是描述现状)· WO_243 乙后半必须与本单同车**:
   前端现有两处调用补救端点 **不带 `generation_request_id`**
   (`WritingHall.tsx:4180` / `:4207`)⇒ 本单上线后它们**全部 403**;
   且 `:4180` 那处**在循环里吞掉错误并报成功**,用户会看到"成功"而什么都没发生。
   ⇒ **这两处必须在同一班车里改**:要么带上 id 走补救,
   要么改走批量端点(新词场景本就该走批量)。
   ⚠️ 本节原来写的是「新词走补救端点会被 403」—— 那是**陈述现状**,
   读的人不会知道自己要做什么。跨窗契约里这类句子一律写成**点名的待办**。

═══════════════════════════════════════════════════════════════════════

🔴 **[作废 2026-09-19 · WO_241 丙] 本节原写「`isNewKwOnly` 走逐词端点、不显示价」——
   已改走批量端点并显示价,免费只剩「缺题补救」那一面。一切以上节为准。**

回包字段:

    charge: {
      "feature_code":     "topic_gen",   # 计费目录里的 code
      "base_points":      int,           # 目录基价(现值 80,以目录为准,不写死)
      "keyword_count":    int,           # 本次关键词数(>=1)
      "estimated_points": int,           # 本次将扣 = base × keyword_count
      "ceiling_points":   int,           # 上限,恒 >= estimated_points
    } | None

🔴 **`None` 不是 `0`**:
   · `None` = 本次**不走计费**(当前:admin)⇒ 前端**不显示价**;
   · `0`    = 本次**免费**。
   压成同一种表示的话,前端少写一个判断就会对客户说错话
   (2026-09-18 `excludedBrandDirectedCount` 栽的就是这个:缺省与 0 同义 ⇒
    前端回落到"按老样子显示")。

🔴 **上限不许显示得比实扣少**(工单口径逐字)。显示得比实扣少比不显示更糟 ——
   客户是**按那个数同意扣费的**。当前 `ceiling_points == estimated_points`
   (没有"实扣少于预估"的路径);哪天出现折扣,改的是本模块一处。

🔴 前端**不要自己乘** `base_points × keyword_count`:
   那样倍率口径一变就两边分家,而**分家时两边各自都对**,只有客户看到的数是错的。
   要显示什么就读什么字段。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

FEATURE_CODE = "topic_gen"


def plannable_keywords(keywords: Optional[Sequence[Any]]) -> list:
    """本次**真正要出题**的那些词。

    口径与 `writing/keyword_topic_generator._required_article_count` 一致:
    `planned_count` / `planned_posts_default` / `required_articles` 按优先级取第一个
    **非 None** 的,`> 0` 才算数。显式 0 = 「这个词这次不出题」,必须保留
    (三个键都用 `is None` 判缺失,不用真值判断)。
    """
    out = []
    for kw in keywords or []:
        if not isinstance(kw, dict):
            out.append(kw)
            continue
        count = None
        for key in ("planned_count", "planned_posts_default"):
            raw = kw.get(key)
            if raw is not None:
                count = raw
                break
        if count is None:
            raw = kw.get("required_articles")
            count = 1 if raw is None else raw
        try:
            if int(count) > 0:
                out.append(kw)
        except (TypeError, ValueError):
            out.append(kw)          # 读不出条数就当它要出题(宁可收,不可漏交付)
    return out


def keyword_count(keywords: Optional[Sequence[Any]]) -> int:
    """本次按几份计费 = **本次真正要出题的词数**。

    🔴 [WO_241 丙 · Review 裁定 2026-09-19] 改前是 `max(1, len(keywords))` ——
       **整张关键词表的长度**。而出题侧
       (`writing/keyword_topic_generator.py:322` `plannable_keywords`)
       只为 `planned_count > 0` 的词出题。
       ⇒ 带 `per_keyword_plan` 把旧词置 0、只留新词时,
       **出题只出新词、收费收整张表**(10 个词的项目出 2 个新词 ⇒ 收 10 份)。

    🔴 这一改**不动任何现有扣费**:前端今天**根本不发** `per_keyword_plan`
       (`grep per_keyword_plan frontend/src` = 0 命中)⇒ 整表出题 = 整表收费,
       两种算法逐值相等。它定义的是**新流程**的价 ——
       而新流程(按钮走批量端点带子集)正是因为这条才能成立。

    🔴 这个定义只许有一处。它同时决定「客户看到的数」和「实扣的数」——
       两处各写一遍的话,哪天有一处改了另一处不会跟着改,
       而**两边各自看都对**,只有客户看到的数是错的。
    """
    # 🔴 [Review 09-28 · WO_317 第三笔] 不再设下限 1:0 槽的词是有意不出题,没有要出题的词时
    #    份数就是 0 —— 详情端点据此给 None(不显示价、前端不给「为新词生成标题」),
    #    generate-titles 在冻结之前 400「没有需要出题的关键词」。原来按 1 份显示「将扣 80」,
    #    点了冻 80、出 0 条、退 80。
    return len(plannable_keywords(keywords))


def keywords_without_topics(keywords: Optional[Sequence[Any]],
                            topics: Optional[Sequence[Any]]) -> list:
    """**还没有任何选题行**的那些词 —— 「新词面」点击前要报的价就是它们的价。

    🔴 [WO_241 丙补 · Review 裁定 2026-09-19] 为什么后端给而不是前端算:
       前端不算钱(08_billing / 元指令 1)。前端自己按"哪些词没选题"筛一遍再乘,
       迟早和后端的口径分家,而**分家时两边各自都对**,只有客户看到的数是错的。

    🔴 判「有没有选题」按 `topics` 行里的 `keyword_id`,**不按标题是否为空**:
       受理凭据行(`optimized_title IS NULL`)也算这个词已经进过批次
       —— 它不是"新词",它是"缺题的词",走的是**免费补救**那一面。
       两者混一起会让已付费的词又被报一次价。
    """
    seen = set()
    for t in topics or []:
        if isinstance(t, dict) and t.get("keyword_id") is not None:
            seen.add(int(t["keyword_id"]))
    out = []
    for kw in keywords or []:
        if not isinstance(kw, dict):
            out.append(kw)
            continue
        kid = kw.get("id")
        if kid is None or int(kid) not in seen:
            out.append(kw)
    return out


def is_billable(*, is_organization_member: bool, bill_user_id: Any) -> bool:
    """这一次走不走计费。

    🔴 和 `keyword_count()` 同理,这个判断也只许有一处:
       它决定的是**前端显不显示价**(`None` ⇒ 不显示)。
       两个端点各写一遍的话,会出现「详情说要花钱、生成时不收」
       或者反过来 —— 而两边各自看都对。
    """
    return bool(is_organization_member or bill_user_id)


def charge_card(
    keywords: Optional[Sequence[Any]],
    *,
    billable: bool,
    lookup: Callable[[str], Any],
) -> Optional[Dict[str, Any]]:
    """两个载点都调这一个 —— 「点击前显示」与「扣完回执」是同一次计算。

    `lookup` 由调用方注入(通常是 `db.wallet_db.get_feature_pricing`),
    好让本模块保持无 DB 依赖、可被判据直接验。

    返回 `None` 表示**本次不走计费** —— 前端据此**不显示价**。
    🔴 `None` 不是 `0`:`0` 是「本次免费」。压成一种,前端少写一个判断
       就会对客户说错话。
    """
    if not billable:
        return None
    count = keyword_count(keywords)
    if count <= 0:
        return None      # 没有要出题的词:不走计费、不显示价(生成端点会在冻结前拒)
    row = lookup(FEATURE_CODE)
    if not row:
        return None
    return preview(int(row["cost_points"]), count)


def extra_points(base_points: int, keyword_count: int) -> int:
    """基价之外还要加收的部分。

    两条计费路径都用它:
      · 组织路径:`reserve_charge(..., dynamic_ceiling_extra_points=extra_points(...))`
      · 个人路径:`deduct_points(..., extra_cost=extra_points(...))`
    两者都是在**基价之上**追加,所以实扣 = base + extra。
    """
    base = max(0, int(base_points))
    count = max(1, int(keyword_count))
    return (count - 1) * base


def total_points(base_points: int, keyword_count: int) -> int:
    """这次实际会扣的算力 = 基价 + 追加 = 基价 × 关键词数。"""
    return max(0, int(base_points)) + extra_points(base_points, keyword_count)


def preview(base_points: int, keyword_count: int) -> Dict[str, Any]:
    """交给前端显示的那一份。

    `ceiling_points` 与 `estimated_points` 当前恒相等 —— 现在没有「实扣少于预估」
    的路径。保留这个字段是因为工单口径写着「有上限时显示上限,**不许显示比实扣少的数**」:
    哪天真出现折扣,要改的是这里一处,而不是去前端找那个乘法。
    """
    base = max(0, int(base_points))
    count = max(1, int(keyword_count))
    total = total_points(base, count)
    return {
        "feature_code": FEATURE_CODE,
        "base_points": base,
        "keyword_count": count,
        "estimated_points": total,
        "ceiling_points": total,
    }
