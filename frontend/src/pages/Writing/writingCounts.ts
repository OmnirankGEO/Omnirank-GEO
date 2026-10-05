/**
 * 写作中心的两个数:**槽** 与 **篇**。
 *
 * 🔴 [#225 a1] 这是同一屏上最容易混的一对,Review 09-15 定的口径:
 *   · **槽** = 合同分配给这个词的授权数(`required_articles`)。
 *     它是**冻结在报价上**的,全仓 32 个生产文件在读,一个字都不许改。
 *   · **篇** = 按这单的交付口径,这个词实际要做几篇文章
 *     (`planned_posts_default`,服务端按 `槽数 × 每槽篇数` 算好)。
 *
 *   同一屏上「槽」只指合同分配,「篇」只指要做/做了的文章 —— 两个词不许混用。
 *
 * 🔴 **零 import**:判据要能真调它,而不是只扫源码里写没写这么一句。
 * 🔴 **前端不算**:`planned` 直接读服务端给的值,不做 `槽 × 倍数` 的换算。
 *    换算里带 `round_half_up`,前端自己算一遍就会和媒体组合那屏对不上
 *    (取整位置不同:那边逐桶取整,这边会变成逐词取整)。
 */

export interface KeywordCounts {
    /** 合同授权的**槽**数。缺省按老语义:NULL ⇒ 1(不是 0),显式 0 保留。 */
    required_articles?: number | null;
    /** 服务端按交付口径算好的**篇**数。 */
    planned_posts_default?: number | null;
}

/**
 * 取整数;取不到回 `null`。
 *
 * 🔴 `null` / `undefined` / `''` 必须**先挡掉**:`Number(null)` 与 `Number('')`
 *    都等于 **0**,而 0 是有限数 —— 不挡的话「服务端没给」会变成「服务端给了 0」,
 *    屏幕上就是「需 **0** 篇」。判据 W2c 当场抓到的就是这个。
 */
const int = (v: unknown): number | null => {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? Math.trunc(n) : null;
};

/**
 * 这个词授权了几**槽**。
 *
 * 老语义逐字复刻后端 `writing.keyword_topic_generator._required_article_count`:
 * NULL ⇒ 1,显式 0 保留。两边必须同一条规则,否则 NULL 的那一行会从 1 变 0。
 */
export function slots(kw: KeywordCounts | null | undefined): number {
    const raw = kw ? kw.required_articles : null;
    if (raw === null || raw === undefined) return 1;
    const n = int(raw);
    return n === null ? 1 : Math.max(0, n);
}

/**
 * 这个词要做几**篇**。
 *
 * 🔴 服务端没给 `planned_posts_default` 时回落到槽数 —— 那正是 225 之前的老行为
 *    (口径读不到 / `mixed` 时后端本来也回落 k=1,planned == required)。
 *    这不是"猜一个数":它和老版本显示的是同一个值,只是标签仍叫「篇」。
 */
export function plannedPosts(kw: KeywordCounts | null | undefined): number {
    const p = kw ? int(kw.planned_posts_default) : null;
    return p === null ? slots(kw) : Math.max(0, p);
}

/**
 * 「授权 N 槽」那句说明。
 *
 * 🔴 只在**篇数与槽数不一样**时才说 —— 一样的时候说一遍等于把同一个数
 *    换个量词又讲一次,屏幕上凭空多出一个要读者去比对的东西。
 */
export function slotsNote(kw: KeywordCounts | null | undefined): string {
    const s = slots(kw);
    return plannedPosts(kw) === s ? '' : `授权 ${s} 槽`;
}

/**
 * 整单要做几**篇**的合计。
 *
 * 🔴 优先用**服务端给的**合计(`total_planned_posts_default`)。
 *    C 侧注释写得很清楚:「只有一条取整路径 —— 逐词先取整,再相加,合计由服务端给出」。
 *    前端自己 `reduce` 一遍,取整位置一变就会和别处差 1,而**没有任何东西会报错**。
 *
 * 🔴 服务端没给时才回落到逐行相加 —— 加的是**已经取整过**的每行值,
 *    不引入新的取整,结果与老行为一致。这不是"前端自己算换算",
 *    那种事(槽 × 倍数)在本模块里是被判据 W2e 禁掉的。
 */
export function plannedTotal(
    serverTotal: unknown,
    rows: readonly KeywordCounts[] | null | undefined,
): number {
    const t = int(serverTotal);
    if (t !== null) return Math.max(0, t);
    return (rows || []).reduce((sum, kw) => sum + plannedPosts(kw), 0);
}
