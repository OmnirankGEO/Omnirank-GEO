/**
 * #180 · 发布中心的「当前客户」只有一个入口:左上角。**零 import**。
 *
 * Owner 09-12 图 3 原话:「这两个选择是完全割裂的,应该统一客户选择入口;
 * 这里直接参考左上角就行,还能省空间」。
 *
 * 🔴 割裂的代价不是"多一个下拉框":发布中心自己维护一份 `projects/selectedProject`,
 *    默认落到 **`list[0]`**(与左上角毫无关系的第一个项目)。于是页面上方显示的是
 *    A 客户,左上角显示的是 B 客户,而提交体里的 `brand_id` 取的是页面这一份 ——
 *    「显示对、记错人」。同一条教训在 `DouyinPostDetail` 的注释里有生产实证:
 *    svideo 3 条里 2 条把内容记到了别的服务商的客户名下。
 *
 * 所以本模块的三个函数合起来只说一件事:**客户是谁,由 ClientContext 一处决定**;
 * 找不到对应项目时**宁可不选**,也不替用户猜一个。
 */

/** `/api/writing/projects` 的一行(只取本模块用得到的字段)。 */
export interface ProjectRef {
    id: number;
    brand_id?: number | null;
    brand_name?: string;
    industry?: string;
    quote_ids?: number[] | null;
}

const num = (v: unknown): number | null => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
};

/**
 * 当前客户对应哪个写作项目。
 *
 * 🔴 **没有 `list[0]` 兜底**。老代码在三个分支都落空时 `setSelectedProject(list[0].id)`,
 *    那一行就是"显示对、记错人"的来源:左上角选了 B,页面却默默在 A 上工作。
 *    找不到就返回 null,由界面**明说**「该客户还没有可发布的文章」。
 */
export function pickProjectForBrand(
    projects: readonly ProjectRef[] | null | undefined,
    brandId: number | null | undefined,
): number | null {
    const bid = num(brandId);
    if (!bid) return null;
    const hit = (projects || []).find((p) => num(p?.brand_id) === bid);
    return hit ? hit.id : null;
}

/**
 * 这个客户的**全部**有效报价 id(按列表顺序)。
 *
 * 🔴 [#210 · Owner 09-14 点名 P1] 这是「发布中心缺文」的病根。
 *    `pickProjectForBrand` 用的是 `.find()` —— 它只回**第一份**报价;
 *    发布中心拿它去 `GET /api/placement/articles/{quote_id}`,
 *    而那条 SQL 是 `WHERE t.quote_id = %s`,**一次只看一份**。
 *    真客户取证(210-d1):品牌 17 有两份有效报价,94 下 18 篇、378 下 1 篇;
 *    用户打开的是 378 ⇒ 另外 18 篇(含他补的和重写的那几篇)**一篇都看不见**。
 *    文章一篇没丢,丢的是取文的作用域。
 *
 * 🔴 不在这里过滤 draft:`/api/writing/projects` 的 SQL 本身就是
 *    `WHERE q.status IN ('confirmed','paid')` —— 列表里的每一行都已经是有效报价。
 *    在这儿再滤一遍就是同一个谓词两处实现,而且这一处看不到 status 字段
 *    (回包里根本没有),只能靠猜。
 */
export function quoteIdsForBrand(
    projects: readonly ProjectRef[] | null | undefined,
    brandId: number | null | undefined,
): number[] {
    const bid = num(brandId);
    if (!bid) return [];
    return (projects || [])
        .filter((p) => num(p?.brand_id) === bid)
        .map((p) => p?.id)
        .filter((id): id is number => num(id) !== null);
}

/**
 * 深链想说的是哪个客户。
 *
 * 支持两种写法:直接给 `brand_id`,或给 `quote_id`(再由它反查项目的 brand)。
 * 🔴 只**解析**,不决定要不要切 —— 那一步交给 `shouldSwitchClient`,
 *    两件事分开才各自可测。
 */
export function resolveDeepLinkBrand(input: {
    preBrandId?: number | null;
    preQuoteId?: number | null;
    projects?: readonly ProjectRef[] | null;
}): number | null {
    const direct = num(input?.preBrandId);
    if (direct) return direct;
    const q = num(input?.preQuoteId);
    if (!q) return null;
    const hit = (input.projects || []).find(
        (p) => p?.id === q || (Array.isArray(p?.quote_ids) && p.quote_ids.includes(q)));
    return hit ? num(hit.brand_id) : null;
}

/**
 * 深链带来的客户与左上角不一致时,**反向把左上角切过去**。
 *
 * 🔴 方向很重要:不是"页面自己记一个别的客户",而是**让左上角跟着深链走** ——
 *    两处永远一致,用户看到的和提交的才是同一个人。
 *    深链没带客户、或本来就一致 ⇒ 不动(不要在每次渲染里反复 switch)。
 */
export function shouldSwitchClient(input: {
    deepLinkBrandId: number | null;
    currentBrandId: number | null;
}): boolean {
    const target = num(input?.deepLinkBrandId);
    if (!target) return false;
    return num(input?.currentBrandId) !== target;
}

export type ScopeKind = 'all_clients' | 'no_client' | 'no_project' | 'ready';

export interface ScopeState {
    kind: ScopeKind;
    /** 只有 ready 时非 null。 */
    projectId: number | null;
    /**
     * 这个客户的**全部**有效报价 id。取文要按它**逐份拉再合并**(#210)。
     * 🔴 与 `projectId` 的关系:`projectId` 是"默认选中哪一份",
     *    `quoteIds` 是"这个客户一共有哪几份"。老代码只有前者,
     *    于是"选中哪一份"被当成了"这个客户只有这一份"。
     */
    quoteIds: number[];
    /** 非 ready 时给用户看的话(每一格都必须有下一步,不能只说"不行")。 */
    message: string;
}

/**
 * 页面此刻处于哪一格。四格各有各的出路,**没有一格是"默默换个客户继续"**。
 */
export function scopeState(input: {
    isAllClientsMode: boolean;
    currentBrandId: number | null;
    projects: readonly ProjectRef[] | null | undefined;
}): ScopeState {
    if (input.isAllClientsMode) {
        return { kind: 'all_clients', projectId: null, quoteIds: [], message: '先在左上角选择一个客户,再来发布' };
    }
    const bid = num(input.currentBrandId);
    if (!bid) {
        return { kind: 'no_client', projectId: null, quoteIds: [], message: '先在左上角选择一个客户,再来发布' };
    }
    const pid = pickProjectForBrand(input.projects, bid);
    if (pid === null) {
        return {
            kind: 'no_project',
            projectId: null,
            quoteIds: [],
            message: '这个客户还没有可发布的文章 —— 先去「AI 写文章」给他写一篇',
        };
    }
    return { kind: 'ready', projectId: pid, quoteIds: quoteIdsForBrand(input.projects, bid), message: '' };
}
