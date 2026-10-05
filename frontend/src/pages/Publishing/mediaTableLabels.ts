/**
 * #199 · 发布中心媒体表上那几处「要人去猜」的文案。**零 import**(判据要能真调)。
 *
 * 质量标准第 11 条:不直观、需要人去猜、本可模板化的展示,不允许出现。
 * 这一层只产出**文字**,不碰数据、不碰请求。
 */

/* ── 1.1 权重列的量纲 ────────────────────────────────────────────────
 *
 * 现场:表头只有「电脑权重 / 移动权重」四个字;唯一的说明藏在**收录率**那一列的
 * HelpHint 里(「PC / 手机端的搜索权重, 越高曝光越强」)——既不在权重列上,
 * 也没说区间。服务商看到「7」和「8」,不知道满分是多少、0 是"最差"还是"没数据"。
 *
 * 🔴 **区间数字不许猜**。`mhz_media.pc_weight / m_weight` 是渠道方同步字段
 *    (`db/meijiehezi_db.py` INTEGER DEFAULT 0),`db/publish_db.py` 用 `pc_weight >= 4`
 *    当门槛,只能说明它不是 0/1,**说明不了上限**。
 *    上限要等 Deploy 的 199-d1 只读取证(`max(pc_weight)`),取证前这里**保持 null**,
 *    界面上只写方向与 0 的含义,不写区间。
 *
 * 🔴 判据两边都钉:有取证文件时常量必须等于文件里的 max;没有取证文件时常量必须是
 *    `null` —— 也就是**不许在没有证据的时候填一个数**。
 */
export const WEIGHT_RANGE_MAX: number | null = null;

/**
 * 199-d1 取证文件 —— **仓内相对 `frontend/` 的路径**(判据据此核对上面那个常量)。
 *
 * 🔴 a2 订正:第一版把它写成了仓**外**的一个 .md 文件名,判据去 `../..` 读。
 *    那样一填上数字,任何**没有那份文件的机器**(CI / 别人的 worktree / 干净 clone)
 *    构建就会红 —— 判据的参照物必须跟代码一起进仓,否则它证的是"我这台机器上有什么"。
 *    形状:`{ max_pc, max_m, source, sha256, queried_at }`。
 */
export const WEIGHT_RANGE_EVIDENCE = 'src/pages/Publishing/weightRange.evidence.json';

/** 权重列头的文字:有区间就写区间,没有就只写名字(不编)。 */
export function weightHeader(which: 'pc' | 'mobile'): string {
    const name = which === 'pc' ? '电脑权重' : '移动权重';
    return WEIGHT_RANGE_MAX === null ? name : `${name} · 0–${WEIGHT_RANGE_MAX}`;
}

/** 权重列头 HelpHint 的正文。**挂在权重列自己身上**,不借收录率那一格。 */
export function weightHint(): string {
    const range = WEIGHT_RANGE_MAX === null
        ? '数值由渠道方同步,越高越容易被收录和曝光'
        : `区间 0–${WEIGHT_RANGE_MAX},越高越容易被收录和曝光`;
    return `渠道方给的搜索权重:${range};0 = 渠道方没标,不代表最差。`;
}

/* ── 1.3 「高级搜索」折叠按钮的文案 ──────────────────────────────────
 *
 * 现场:折叠态只露"频道 / 综合门户"两行,而**地区 / 新闻源 / 特别行业 / 价格 / 排序**
 * 都在折线下面。其中**排序**(缺省 `geo_authority_score_desc`)决定你先看到谁,
 * 价格档位决定你看到的是哪一批 —— 两个都是"会改变结果的默认值",
 * 而按钮上只写「全量 N 家」。
 *
 * 🔴 **排序永远列出来**:它不是"筛选",不会因为"没改过"就不影响结果 ——
 *    不列的话,一个从没点开过高级搜索的人永远不知道这张表是按什么排的。
 */
export interface MediaFilterState {
    /** 排序值,例如 `geo_authority_score_desc` */
    sort: string;
    /** 非「不限」的筛选项:`[['价格', '0–6500'], ['地区', '广东']]` */
    activeFilters: readonly (readonly [string, string])[];
    /** 服务端返回的条数 */
    total: number;
    /**
     * 🔴 `total` 是不是**筛选后**的数。
     *    自核(#199 §1.3 要求):`loadMhzMedia` 把 search / area / resource_type /
     *    news_resource / portal_media / resource_type_name / points_min / points_max /
     *    geo_platform / special_industry **全部**放进了请求,`total` 取自同一个响应,
     *    所以它是**筛选后**的总数 ⇒ 文案用「符合 N 家」,不是「全量 N 家」。
     *    这个开关留着是为了让判据能把两种说法都测到,而不是给调用方选。
     */
    totalIsFiltered: boolean;
}

/* ── 排序:芯片与折叠行的**同一个源** ─────────────────────────────────
 *
 * 🔴 a2 订正(Review 复跑抓到的接缝)。第一版这里是一张**手写的** `SORT_LABEL` 表,
 *    而排序芯片的真列表写死在 `PublishCenter.tsx` 里 —— 两个源,于是:
 *    · 芯片有 9 个值,表里只有 6 个键;
 *    · 其中一个键还**拼错了**(表里 `included_rate_desc`,芯片真值 `inclusion_rate_desc`)。
 *    结果选「收录率↓ / 出稿时间↑ / GEO 引擎数↓ / 默认 ID」这四档,折叠行都写
 *    「排序 默认顺序」—— **具体但错**,而且兜底句把错配整个盖住了:
 *    没有一格会红,因为"认不出就说默认顺序"永远返回一个看着合理的答案。
 *
 * ⇒ 现在**列表在这里**,`PublishCenter` 从它渲染芯片,`sortLabel` 从同一条记录取说法。
 *    加一档排序只能加在这一个地方,加完两处同时跟上。
 *
 * 🔴 兜底句改成**不声称任何顺序**的 `未识别的排序`:原来的「默认顺序」是一句
 *    关于结果排列方式的**断言**,用它兜一个认不出的值就是拿断言替不知道。
 */
export interface MediaSortOption {
    /** 请求参数里的值,也是 state 的值 */
    readonly value: string;
    /** 芯片上的短标签(带箭头/图标,给横排芯片用) */
    readonly chip: string;
    /** 折叠行里的说法(整句,不用箭头 —— 那一行是给人读的句子) */
    readonly summary: string;
}

export const MEDIA_SORT_OPTIONS: readonly MediaSortOption[] = [
    // [CTO-15.23 2026-05-18 v2-F Phase 8] GEO 真权威综合分(authority + 一线门户 + 6/6 引擎 + 价格甜点)= 默认
    { value: 'geo_authority_score_desc', chip: '🔥 GEO 真权威↓', summary: 'GEO 真权威从高到低' },
    { value: 'geo_engine_coverage_desc', chip: 'GEO 引擎数↓', summary: 'GEO 引擎数从多到少' },
    { value: 'price_asc', chip: '价格↑', summary: '价格从低到高' },
    { value: 'price_desc', chip: '价格↓', summary: '价格从高到低' },
    { value: 'inclusion_rate_desc', chip: '收录率↓', summary: '收录率从高到低' },
    { value: 'pc_weight_desc', chip: '电脑权重↓', summary: '电脑权重从高到低' },
    { value: 'm_weight_desc', chip: '移动权重↓', summary: '移动权重从高到低' },
    { value: 'avg_time_asc', chip: '出稿时间↑', summary: '出稿时间从短到长' },
    { value: 'id_asc', chip: '默认 ID', summary: '按入库顺序' },
];

/** 认不出的值走这句;它**不声称**任何顺序。判据只许列表**外**的值落到这里。 */
export const SORT_FALLBACK_LABEL = '未识别的排序';

export function sortLabel(sort: unknown): string {
    const raw = typeof sort === 'string' ? sort.trim() : '';
    const hit = MEDIA_SORT_OPTIONS.find((o) => o.value === raw);
    return hit ? hit.summary : SORT_FALLBACK_LABEL;
}

/**
 * 折叠按钮上的那一行。
 *
 * 例:`当前生效:排序 GEO 真权威 · 筛选 2 项(价格 0–6500 · 地区 广东)· 展开调整 · 符合 12 家`
 *     `当前生效:排序 GEO 真权威 · 筛选 不限 · 展开调整 · 符合 128 家`
 */
export function collapsedFilterSummary(state: MediaFilterState): string {
    const s = state || ({} as MediaFilterState);
    const list = Array.isArray(s.activeFilters) ? s.activeFilters : [];
    const filterPart = list.length === 0
        ? '筛选 不限'
        : `筛选 ${list.length} 项(${list.map(([k, v]) => `${k} ${v}`).join(' · ')})`;
    const n = Number.isFinite(Number(s.total)) ? Number(s.total) : 0;
    const countPart = s.totalIsFiltered ? `符合 ${n} 家` : `全量 ${n} 家`;
    return `当前生效:排序 ${sortLabel(s.sort)} · ${filterPart} · 展开调整 · ${countPart}`;
}
