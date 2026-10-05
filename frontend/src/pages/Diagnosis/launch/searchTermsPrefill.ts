/**
 * 搜索词「预填 + dirty + 提交决策」的纯逻辑(订正三十① 及其补)。
 *
 * 本模块**不含任何 HTTP**:C 的只读端点契约还没定形,但语义已经定死了,
 * 先把语义做成可注毒的纯函数,契约到了只接一根线。
 *
 * 🔴 Owner 口径:**「看到的必须就是跑的」**。
 *    所以未改动时提交体带的是**屏幕上显示的那一份**(端点返回的逐值),
 *    而不是留空让服务端在**提交时刻**再派生一次 —— 那一刻可能已经有新的已发布诊断,
 *    于是她看到 A、系统跑 B(与 #62 同形)。
 */

/** C 的阶梯四级 + 空态。名字以 C 的契约为准,这里只规定**必须总覆盖**。 */
export type PrefillSource = string;

export type PrefillState =
    /** 还在取。 */
    | { kind: 'loading' }
    /** 取到了。**含 `terms: []`(确实没有词)** —— 与「没取到」是两件事,别并档。 */
    | { kind: 'ready'; terms: string[]; source: PrefillSource; aiAugmented?: boolean }
    /** 没取到(503 / 网络)。她屏幕上什么都没看到。 */
    | { kind: 'unavailable' };

/**
 * 🔴 回填只在**未改动**时发生。
 *    否则:她已经开始打字,端点响应晚到 200ms 把整框冲掉 ——
 *    屏幕上的表现只是「字自己变了」,不报错、不闪烁、没有任何东西可以点回去。
 *    这类缺陷没人会报,因为她只会以为自己手滑了。
 */
export function shouldAcceptRefill(dirty: boolean): boolean {
    return !dirty;
}

/**
 * 换品牌:替换但出声(订正三十补 ①)。
 * 词是**关于某个品牌**的 —— 把 A 客户改过的词带进 B 客户明确是错的;
 * 但静默替换掉她打的字又正是我们一直在修的那类毛病。所以替换 + 一行说明,
 * 不设撤销、不拦操作(:76 不中断对话)。
 */
export function brandSwitchNotice(
    prevBrandName: string | null | undefined,
    nextBrandName: string | null | undefined,
    wasDirty: boolean,
): string | null {
    if (!wasDirty) return null;                       // 她没改过 ⇒ 没什么可交代的
    const prev = (prevBrandName || '').trim() || '上一个客户';
    const next = (nextBrandName || '').trim() || '新客户';
    return `已换成${next}的词;你为${prev}改的那份没有被提交过。`;
}

/**
 * 提交体带什么。**三态分开**,别让「取到空数组」和「没取到」落进同一档 ——
 * 它们在"框里是空的"这一点上完全同形,而处置相反。
 *
 * 🔴 `send: false` 只有一种情形:**没取到**。此时她确实什么都没看到,
 *    交服务端派生不违反「看到的就是跑的」。其余一律 `send: true` 带显示值。
 *
 * 🔴 她把预填的词**全删光**也算 `send: true` 且 `terms: []` ——
 *    而服务端见空会派生。所以界面上必须同时说清「留空 = 我们按品牌信息自动规划」,
 *    否则"看到空 / 跑了词"又是一次看到的≠跑的。这一格规格没覆盖,是我按口径补的,
 *    已回报 Review;若判为不可接受,替代方案要后端加一个"显式无词"的表达,
 *    前端单独做不到(空列表在 DTO 里恒等于"请你派生")。
 */
export function keywordsForSubmit(
    state: PrefillState,
    dirty: boolean,
    userTerms: string[],
): { send: boolean; terms: string[]; why: string } {
    if (state.kind === 'unavailable') {
        return { send: false, terms: [], why: '没取到建议词:她屏幕上是空的,交服务端派生' };
    }
    if (state.kind === 'loading') {
        return { send: false, terms: [], why: '还在取:此刻不该提交(门会拦)' };
    }
    if (dirty) {
        return { send: true, terms: userTerms, why: '她改过:以她改后的为准(Owner 口径)' };
    }
    return { send: true, terms: state.terms, why: '她没改:带屏幕上显示的那一份,不留空' };
}

/**
 * 来源标签(Review 裁定的两句映射)。
 *
 * `last_diagnosis` ⇒「上次体检的词」;`brand_name`/`industry` **或 `aiAugmented=true`**
 * ⇒「AI 按品牌信息规划」;`none` ⇒ 无标签(空态另有文案)。
 *
 * 🔴 我原提了第三句(「上次体检的词 + AI 补充」),Review 裁两句。它的裁定是**安全的那一侧**:
 *    混合态说「AI 按品牌信息规划」只是**少说了**复用的那部分,不会误导;
 *    而若说成「上次体检的词」,框里有她上次根本没见过的词 ⇒ 那是一句
 *    **具体但错误**的话,她会照着去回忆上次填了什么然后对不上。两害相权,少说胜过说错。
 *
 * 🔴 映射必须**总覆盖**:未知 source 也要给一句话。渲染空白 = 标签整块消失,
 *    而"标签没出现"与"这一档不该有标签"在屏幕上同形。未知档落到笼统那句,不编具体来源。
 */
export function sourceLabel(
    source: PrefillSource | null | undefined,
    aiAugmented?: boolean,
): string {
    const s = (source || '').trim();
    if (s === 'none' || s === '') return '';
    if (aiAugmented) return 'AI 按品牌信息规划';
    return (s === 'last_diagnosis' || s === 'latest_published_diagnosis')
        ? '上次体检的词'
        : 'AI 按品牌信息规划';
}

/**
 * 空态文案。**三种"框里是空的"必须说不同的话** —— 它们在屏幕上同形,而处置完全不同。
 *
 * 🔴 `none` 那句不能用「不填也行」。C 的契约写明:三源皆空时**提交路径 raise → 422**
 *    (不许零搜索词的诊断跑起来)。所以在 `source:"none"` 这一态下,
 *    「不填也行」是一句**必然为假**的承诺 —— 她照做就撞一条像系统故障的 422。
 *    这一格已回报 Review(它把 none 并进了"取不到"那句文案)。
 */
export function emptyStateNotice(state: PrefillState): { text: string; mustFill: boolean } {
    if (state.kind === 'unavailable') {
        // 她什么都没看到 ⇒ 交服务端派生,而服务端多半派生得出(品牌名/行业还在)。
        return { text: '这次没能取到建议词,不填也行。', mustFill: false };
    }
    if (state.kind === 'ready' && state.source === 'none' && state.terms.length === 0) {
        return {
            text: '这个客户还没有可用的搜索词,先填 1 个再开始(一个词就够)',
            mustFill: true,
        };
    }
    return { text: '', mustFill: false };
}
