/**
 * #236-c1a · 「口径已排除『直接问本品牌名』」这句声明要不要说、怎么说。
 *
 * 🔴 抽成**纯函数**是为了让判据能**真调**它。原来这句是 JSX 里无条件写死的一段文字,
 *    判据只能做文本匹配,而文本匹配对「守卫写了但那句仍渲染」这种改动天生没分辨力
 *    —— 本轮已经被同一件事咬过三次(T2 数 testid / A 段 price_preview_id / 教程死消费点)。
 *    这里只有 `number | null | undefined` 进、字符串出,**零运行时依赖**。
 *
 * 🔴 语义(与后端 `public_report_presentation.py` 对表,F1 2026-09-17 写死):
 *    `excludedBrandDirectedCount` = 本次**被排除出竞争分母的「有效回答」条数**
 *    —— 是**回答条数**不是题数(一道题 × 4 平台 = 4 条)。
 *
 * 🔴 缺省(老报告没有这个字段)按 **0** 处理 ⇒ **不说**。
 *    不许回落成「按老样子说」:那等于继续对客户宣称排过,而这次真客户 #700/#726 一条都没排。
 */

/** 这次到底排没排掉东西 —— 没排(含缺省/异常值)就不许说那句。 */
export function shouldClaimExclusion(n: number | null | undefined): boolean {
    return typeof n === 'number' && Number.isFinite(n) && n > 0;
}

/**
 * 那句声明的正文。不该说时返回空串 —— 调用方据此不渲染。
 * 🔴 句中的数字**只能**来自入参,不许调用方另算一个:两处各自算就会出现
 *    「脚注说排了 5 条、明细里是 8 条」而两边各自看起来都对。
 */
export function exclusionClaimText(n: number | null | undefined): string {
    if (!shouldClaimExclusion(n)) return '';
    return `口径已排除「直接问本品牌名」这类必然命中的问题（本次排除 ${n} 条回答），只看真实竞争问题。`;
}

/**
 * 平台表抬头里的那半句(与上面同一个判定、同一个数,只是句式不同)。
 *
 * 🔴 [#238 甲] 它原来是 `PlatformPerformance.tsx` 里的**无条件字面量**,
 *    于是同一句谎话长在两个屏上:#236 修了竞争格局那屏,平台表还在说。
 *    搬进来是为了让这句话**只有一个家** —— 判据 E10 钉的就是这件事。
 *    按实例点名的判据只能钉住已经知道的那一处;钉「只有一个家」才钉得住这一类。
 *
 * 不该说时返回**不含该声明**的那半句,而不是空串:这半句在句子中间,
 * 返回空串会让整段读成「识别率看全部问题;「被推荐」是…」,缺一个连接。
 */
export function platformExclusionClause(n: number | null | undefined): string {
    return shouldClaimExclusion(n)
        ? `提及率与提及率（正面措辞）均已排除品牌定向问答（本次排除 ${n} 条回答），且`
        : '提及率与提及率（正面措辞）中，';
}
