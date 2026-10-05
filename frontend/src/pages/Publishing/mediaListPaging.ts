/**
 * #181 · 媒体列表「点了下一页还是当前页」—— 分页与陈旧回包的判定。**零 import**。
 *
 * 客户多次反馈、Owner 自测复现不了 —— 因为它要**三件事同时发生**:
 *   ① 选了一个行业大类;② 换了一个索引(省份/平台/价格/粉丝/权威),
 *   使该行业在新条件下计数为 0;③ 在 facets 回包**之前**点了「下一页」。
 * 这时慢回来的 facets 包发现所选行业不在新 facet 里,就把页码打回 1
 * (`PublishCenter.tsx:1907-1909`)—— 那是全文件**唯一**一处在 click handler 之外改页码,
 * 而且不受列表请求那个 AbortController 约束。
 *
 * 🔴 这里的三条规则各自对应一个"看起来没反应":
 *   · 异步回包**永远不许**改页码 —— 页码只由同步的点击改;
 *   · 陈旧回包(不是这一次筛选变更的)一律丢弃 —— 否则上上次的答案会覆盖这次的;
 *   · 钳位只能用**这一次响应回显的** pages,不能用旧 state
 *     (切筛选后到回包前,旧 pages 会放行一个越界页,后端返空列表 ⇒ 屏幕"没翻")。
 *
 * 判据要能**真调**这些函数,所以它们不碰 React、不碰 fetch。
 */

/** 一次列表响应里与分页有关的部分。服务端**回显** page,前端不猜。 */
export interface PageEcho {
    page?: unknown;
    pages?: unknown;
    total?: unknown;
}

export interface PageState {
    /** 要显示的页码(已按本次响应钳过)。 */
    page: number;
    /** 总页数(本次响应回显的)。 */
    pages: number;
    /** 是否发生了钳位 —— 发生了就说明刚才那一下越界了,界面可据此提示。 */
    clamped: boolean;
}

const intOf = (v: unknown, fallback: number): number => {
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : fallback;
};

/**
 * 用**这一次响应**校准页码。
 *
 * 🔴 `requested` 是发出去时用的页码,`echo.page` 是服务端回显的。两者不一致时
 *    以服务端为准 —— 前端此前**从不回写** `page`,于是"我以为在第 2 页"与
 *    "服务端给的是第 1 页的数据"可以长期并存,而屏幕上只看得到后者(所以像"没翻")。
 * 🔴 钳位只看 `echo.pages`。用旧 state 里的 pages 钳,等于用上一次筛选的总页数
 *    判这一次的越界。
 */
export function reconcilePage(requested: number, echo: PageEcho | null | undefined): PageState {
    const e = (echo && typeof echo === 'object' ? echo : {}) as PageEcho;
    const pages = intOf(e.pages, 0);
    const req = Math.max(1, intOf(requested, 1));
    // 服务端回显了 page 就以它为准;没回显就用我们请求的那个
    const echoed = e.page === undefined || e.page === null ? req : Math.max(1, intOf(e.page, req));
    if (pages > 0 && echoed > pages) {
        return { page: pages, pages, clamped: true };
    }
    return { page: echoed, pages, clamped: false };
}

/**
 * 「下一页 / 上一页」能不能点。
 *
 * 🔴 `loading` 必须进来:切筛选之后到回包之前,`pages` 还是**上一次**的值,
 *    照它放行就会请求一个越界页,后端返空 `media` ⇒ 屏幕看起来"没翻"。
 *    请求在途时两个键都不给点,这一格就没了。
 */
export function canGoNext(input: { page: number; pages: number; loading: boolean }): boolean {
    if (input.loading) return false;
    return input.page < input.pages;
}

export function canGoPrev(input: { page: number; loading: boolean }): boolean {
    if (input.loading) return false;
    return input.page > 1;
}

/**
 * facets 回包要不要清掉所选行业。
 *
 * 🔴 **只回答"清不清行业",不碰页码**。老代码在这里顺手 `setWmPage(1)` ——
 *    那是全文件唯一一处在 click handler 之外改页码,也是本单的根因:
 *    用户抢在 facets 回包前点了「下一页」,慢包回来把他打回第 1 页。
 *    行业被清掉之后,列表 effect 自己会因为依赖变化重取;当前页若越界,
 *    由 `reconcilePage` 按**回显的** pages 钳 —— 两件事分开,各有各的判据。
 */
export function shouldClearIndustry(input: {
    selected: string;
    facetKeys: readonly string[];
}): boolean {
    if (!input.selected) return false;
    // facet 一条都没有时不清:那多半是这次没算出来,不是"这个行业没了"
    if (!input.facetKeys || input.facetKeys.length === 0) return false;
    return !input.facetKeys.includes(input.selected);
}

/**
 * 陈旧回包判定。每次筛选变更递增一个序号,回包带着它发出时的序号回来;
 * 序号对不上就整包丢弃。
 *
 * 🔴 为什么 AbortController 不够:facets 与列表是**两个**请求,
 *    列表那个有自己的 controller,facets 那个此前谁也没管。
 *    两次筛选连点时,第一次的 facets 包可能在第二次之后才回来,
 *    把**上一次**的判断(比如"这个行业没了")按在这一次的状态上。
 */
export function isStalePacket(packetEpoch: unknown, currentEpoch: unknown): boolean {
    const p = Number(packetEpoch);
    const c = Number(currentEpoch);
    if (!Number.isFinite(p) || !Number.isFinite(c)) return true;
    return p !== c;
}
