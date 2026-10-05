/**
 * #195 · 发布中心「图文」档的**状态投影**。**零 import**(判据要能 transpile 后真调)。
 *
 * 这一层只回答四件事,多一个都不加:
 *   ① 一条作品的发布状态该显示成哪句人话;② 它落进哪个筛选桶;
 *   ③ 那条真实链接什么时候能点;④ 失败行能说什么、**不能**说什么。
 *
 * 🔴 **状态只投影,不推断。** `bucketOf()` 的入参只有 `publish_status` 一个字符串 ——
 *    故意不接 `published_url`。前端一旦写成「有链接就算已发布」,
 *    那些 `published` 却没回链接的行会被说成"已发布"、而
 *    `self_reported_unverified`(客户端自报、我们没核实)会被说成"已发布" ——
 *    后者是在替服务商向客户做一个我们没证据的承诺。
 *
 * 🔴 **白名单是封闭的**,认不出的值一律 `unknown` ⇒ 显示「状态未知」。
 *    后端 `db/geo_douyin_db.py` 里写着:这一列**没有 CHECK 约束**,取值域靠代码维持,
 *    「前端对 publish_status 没有白名单,新加一个裸串会直接上屏」。
 *    所以这里既不上屏裸串,也不假装认识它。
 *    漂移由 `scripts/verify-image-note-publish-status-domain.mjs` 盯(它读后端源码比对),
 *    那把闸**不在 build 链**里(build 镜像只有 frontend/)。
 */

/* ── 取值域 ────────────────────────────────────────────────────────────
 * 🔴 这张表是**按后端写入点逐个枚举**出来的,不是照工单抄的。
 *    工单只写了四档(已发布/发布中/失败/未发布),实测后端能写出**六**种:
 *
 *   | 值                        | 谁写的                                        |
 *   |---------------------------|-----------------------------------------------|
 *   | ''(或 NULL)             | 建作品时的默认值 —— 从没提交过发布            |
 *   | 'publishing'              | `db.geo_douyin_db.bind_publish_submission`     |
 *   |                           | · `api/meijiehezi_api.py` 下单成功回写         |
 *   | 'failed'                  | `api/meijiehezi_api.py` 下单未 submitted        |
 *   |                           | · `services/geo_douyin/publish_convergence`    |
 *   | 'published'               | `services/geo_douyin/publish_convergence`      |
 *   | 'self_reported_unverified'| `api/geo_douyin_api.py` 客户端自报的**线索值** |
 *   | 'measured'                | **今天没有写入方**;后端把它列进                |
 *   |                           | `PUBLISH_TERMINAL_STATUSES`(已发出的终态之一)  |
 *
 *    漏掉 `self_reported_unverified` 的后果不是"少一种颜色":
 *    照 `published_url` 推的话它会被说成「已发布」,而它的字面意思是"还没核实"。
 *
 * 🔴 `measured` 我**照后端的终态语义**收进白名单(归已发布桶)——它没有写入方,
 *    但后端的守卫已经把它当"已经发出去了"。留着是为了哪天真有人写时不掉进「状态未知」。
 *
 * 🔴 **别从同名列抄取值域**:`writing_style_flywheel` 也有一列叫 `publish_status`
 *    (`db/writing_style_flywheel_db.py`),它的取值是 `measured` / `insufficient` 之类,
 *    由 `services/writing_strategy_assignment.py` 写 —— **另一张表、另一套语义**。
 *    我第一版差点把那边的值当成这边的取值域(验对了动作、验错了对象)。
 */
export type PublishBucket = 'published' | 'inflight' | 'failed' | 'unpublished' | 'unknown' | 'incomplete';

export interface StatusView {
    bucket: PublishBucket;
    /** 徽章上的人话。**不含任何裸状态串。** */
    label: string;
}

/**
 * 原值 → 桶 + 人话。**只看状态,不看链接、不看时间。**
 *
 * 🔴 `self_reported_unverified` 归 `inflight` 而不是 `published`:
 *    它的字面意思就是「客户端自己说发了,我们没核实」。
 *    归进已发布 = 我们替服务商向客户断言了一件没有证据的事;
 *    归进失败 = 把"不知道"说成"坏了"。所以它是**未定**,和还在发的归一档,
 *    但徽章文字不同 —— 桶决定筛选,文字决定诚实。
 */
const TABLE: Record<string, StatusView> = {
    '': { bucket: 'unpublished', label: '未发布' },
    publishing: { bucket: 'inflight', label: '发布中' },
    published: { bucket: 'published', label: '已发布' },
    measured: { bucket: 'published', label: '已发布 · 已测到' },
    failed: { bucket: 'failed', label: '发布失败' },
    self_reported_unverified: { bucket: 'inflight', label: '已自报 · 待核实' },
};

/** 白名单里的原值(判据与漂移闸共用一份,免得两处各写一遍)。 */
export const KNOWN_PUBLISH_STATUSES: readonly string[] = Object.keys(TABLE)
    .filter((k) => k !== '');

export function bucketOf(publishStatus: unknown): PublishBucket {
    return statusView(publishStatus).bucket;
}

export function statusView(publishStatus: unknown): StatusView {
    const raw = typeof publishStatus === 'string' ? publishStatus.trim() : '';
    const hit = TABLE[raw];
    if (hit) return hit;
    /* 🔴 认不出 ⇒ 不上屏裸串、也不猜。它在「全部」里仍然看得见,不会凭空消失。 */
    return { bucket: 'unknown', label: '状态未知' };
}

/* ── 筛选 chip ───────────────────────────────────────────────────────── */

export interface ChipDef {
    key: 'all' | PublishBucket;
    label: string;
}

/**
 * chip 清单。
 * 🔴 只有工单点名的四个桶做成 chip;`unknown` **不单独做 chip**
 *    (给一个平时恒 0 的筛选按钮是噪音),但它**必须**落在「全部」里 ——
 *    否则一条状态没人认识的作品会从所有视图里消失,而那正是最该被看见的一条。
 */
export const PUBLISH_CHIPS: readonly ChipDef[] = [
    { key: 'unpublished', label: '待发布' },
    { key: 'inflight', label: '提交中' },
    { key: 'published', label: '已发布' },
    { key: 'failed', label: '失败' },
    { key: 'incomplete', label: '尚未做完' },
    { key: 'unknown', label: '待核对' },
    { key: 'all', label: '全部' },
];

/* ── 行视图 ──────────────────────────────────────────────────────────── */

export interface PublishRow {
    id: number;
    keyword: string;
    title: string;
    body: string;
    /** 服务端签好的封面 URL(私有 bucket);空串表示没有,界面给占位。 */
    cover: string;
    /** 张数,服务端 `card_count`;拿不到给 null,**不自己数** */
    cardCount: number | null;
    bucket: PublishBucket;
    statusLabel: string;
    /** 发布时间原值(服务端 `published_at`),空串表示没有。 */
    publishedAt: string;
    /**
     * 站外真实链接。**只有桶是 published 且服务端给了非空 url 时才有。**
     * 🔴 不做任何补全/拼接:拼一个站内详情页塞进这里,用户点了看不到"发出去的那篇",
     *    而界面看起来一切正常。
     */
    url: string;
    /** 校对页(站内,已有路由 `/writing/image-note/:postId`)。 */
    proofHref: string;
    /**
     * 锁定版本 id(服务端 `active_revision_id`)。**没有版本就发不出去。**
     * 🔴 [#203 §1.3] 发布动作从制作台搬到本档之后,这一行必须自己带着它 ——
     *    不带就得为了一个 id 再拉一次接口,那是同一份数据的第二个源。
     */
    revisionId: number | null;
    /**
     * 发布失败的**原话**(服务端 `publish_failure_reason` = 供应商 `reject_reason`)。
     * 空串 = 服务端没给(非失败行恒空串;失败但原话为空时也是空串)。
     * 🔴 **不许**用同一行上的 `failure_reason` 顶替 —— 那是**制作**任务的 error_msg。
     *    混用的后果很具体:用户看到"生成失败"会去重做内容,
     *    而发布被拒真正要做的是换账号或改文案。
     */
    failureReason: string;
    /**
     * 服务端派生的「发布结果待确认」(`publish_pending_confirm`)。
     * 🔴 这是**后端的**展示态(publishing 且超过 24h),前端**不自己算时间差** ——
     *    两处各算一套阈值,必有一天两边说法不一样。
     */
    pendingConfirm: boolean;
    productionStatus?: string;
    assetsComplete?: boolean;
}

const int = (v: unknown): number | null => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
};
const str = (v: unknown): string => (typeof v === 'string' ? v.trim() : '');

export function publishRow(post: unknown): PublishRow {
    const o = (post && typeof post === 'object' ? post : {}) as Record<string, unknown>;
    let view = statusView(o.publish_status);
    const productionStatus = str(o.status);
    const assetsComplete = Array.isArray(o.oss_keys) && o.oss_keys.length > 0 && o.oss_keys.every(k => typeof k === 'string' && k.trim());
    if (view.bucket === 'unpublished' && productionStatus && (productionStatus !== 'ready' || !int(o.active_revision_id) || !assetsComplete)) {
        view = { bucket: 'incomplete', label: ['generating', 'completing'].includes(productionStatus) ? '制作中' : '尚未做完' };
    } else if (view.bucket === 'unpublished' && productionStatus === 'ready') {
        view = { bucket: 'unpublished', label: '待发布' };
    }
    const rawUrl = str(o.published_url);
    return {
        id: int(o.id) || 0,
        keyword: str(o.keyword),
        title: str(o.title),
        body: str(o.body_text),
        cover: str(o.cover_url),
        cardCount: int(o.card_count),
        bucket: view.bucket,
        statusLabel: view.label,
        publishedAt: str(o.published_at),
        /* 🔴 两个条件都要:桶是 published **且** 服务端真的给了 url。
           `published` 但 url 为空是真实存在的行(收敛时 item 没回链接),
           那种行给一个空 href 的链接 = 又一颗点了没反应的东西。 */
        url: view.bucket === 'published' && rawUrl ? rawUrl : '',
        proofHref: int(o.id) ? `/writing/image-note/${int(o.id)}` : '',
        revisionId: int(o.active_revision_id) || null,
        /* 只有失败桶才有意义;其余桶服务端恒给空串,这里不再二次判断
           (两道守卫管同一件事时,后一道永远不承重 —— C 在 195-c1 里刚删掉一个)。 */
        failureReason: str(o.publish_failure_reason),
        pendingConfirm: o.publish_pending_confirm === true,
        productionStatus,
        assetsComplete,
    };
}

/**
 * 这一行能不能**在这里**发出去(#203 §1.3)。两个条件都要:
 *
 * · 有锁定版本(`active_revision_id`)—— 没版本发出去的会是旧图,后端也不收;
 * · 还没发出去,或者上次发失败了 —— 已发布 / 发布中 / 已自报待核实都不给按钮。
 *
 * 🔴 一处谓词一处实现:界面按它决定给不给按钮,判据也调它。
 *    在 JSX 里再写一遍条件,就会有一天按钮的条件和判据的条件不是同一句话。
 */
export function canPublishRow(row: PublishRow | null | undefined): boolean {
    if (!row || row.revisionId === null) return false;
    if (row.productionStatus && (row.productionStatus !== 'ready' || !row.assetsComplete)) return false;
    return row.bucket === 'unpublished' || row.bucket === 'failed';
}

export interface ListState {
    kind: 'empty' | 'list';
    rows: PublishRow[];
    /** 每个 chip 的计数(含 all)。与 `rows` **同一份数据**算出来。 */
    counts: Record<string, number>;
}

export function listState(posts: unknown): ListState {
    const rows = (Array.isArray(posts) ? posts : []).map(publishRow).filter((r) => r.id > 0);
    /* 🔴 计数与筛选必须来自**同一个** bucketOf —— 各写一遍,
       就会出现"chip 说 3 条、点进去 2 条"。 */
    const counts: Record<string, number> = { all: rows.length };
    for (const c of PUBLISH_CHIPS) {
        if (c.key === 'all') continue;
        counts[c.key] = rows.filter((r) => r.bucket === c.key).length;
    }
    return { kind: rows.length === 0 ? 'empty' : 'list', rows, counts };
}

/** 按 chip 过滤。`all` 返回全部(含 unknown)。 */
export function filterRows(rows: readonly PublishRow[], chip: string): PublishRow[] {
    if (chip === 'all') return [...(rows || [])];
    return (rows || []).filter((r) => r.bucket === chip);
}

/**
 * 失败行没拿到原话时说什么。
 *
 * 🔴 **这段抬头 2026-09-13 已翻面。** 上一版写的是「今天缺字段,所以只说实话」——
 *    那是对的,但只对了几个小时:C 的 195-c1(`452b8e812`)已经把
 *    `publish_failure_reason`(供应商 `reject_reason` 原话)带进 `GET /posts` 的每一行,
 *    `publish_status='failed'` 时非空、其余状态恒空串(稳定形状,不是 None)。
 *    ⇒ 失败行现在**必须显示原话**;这句兜底只在原话为空时用。
 *
 * 🔴 仍然**不许**碰同一行上的 `failure_reason`:那是**制作**任务的 error_msg。
 *    C 的提交说得最清楚:混用会让"发布被拒"显示成"生成失败",
 *    用户按前者去重做内容,而实际要做的是换账号或改文案。
 *    —— 一句**具体但错**的解释比含糊更糟,判据两边都钉。
 *
 * 🔴 记一笔方法:**错判据会红,错注释不会**。上一版那段"缺字段"的抬头
 *    如果没人回来翻面,它会一直看起来像事实(#188 的 `exportHref` 刚栽过同一跤)。
 */
export const FAILED_REASON_FALLBACK =
    '发布失败 · 选择这条图文，查看发布回执和下一步处理。';

/** 已发布但服务端没回链接 —— 说清楚,不给空链接。 */
export const PUBLISHED_WITHOUT_URL_NOTE = '已发布 · 链接还没回来';

/** 空态(该客户名下一条图文都没有)。 */
/*
 * 🔴 [#204 a2] 区域级说明:**有内容、却一条都发不出去**。
 *
 *    这句话原来住在制作台区 E(`imageNoteStudioApi.zoneEState.noticeText`),
 *    原文还写着「系统补齐后就能在**发布中心**发出去」—— 一条指向别处的指路。
 *    发布 #203 收口到发布中心之后,用户是在**这里**看那排内容的;
 *    制作台本单退役,那句话要是随文件一起死掉,用户在这里看到的就是
 *    一排没有「发布」的行 + 零解释,只会以为坏了。
 *
 *    ⇒ 说明跟着**用户**走,不跟着文件走。而且在这里它才是真话:
 *      版本补齐后,「发布」确实会出现在这一行上(`canPublishRow`)。
 */
/*
 * 🔴 文案按 **C 的定义**(`services/.../contract_worker.py:243-253`,Review 09-15 转述):
 *    `ready ⟹ active_revision_id 非空` 是**设计**;生产上那批 null 是 09-13 之前的
 *    **存量**,要跑回填,不是前端放宽判定。
 *    所以这句话要说清"这是旧作品在等回填",而不是含糊的"还没有版本" ——
 *    后者会被读成"你还没做完",让用户去重做一遍(那要花钱)。
 */
export const BLOCKED_ALL_NOTE = '旧作品待系统回填,暂不能发';

/**
 * 这一行为什么发不了;发得了就返回空串。
 *
 * 🔴 区域级那句只在**全都发不了**时说。只有一部分发不了时,那几行同样要给原因 ——
 *    否则屏幕上就是"有的行有发布键、有的没有",用户会以为是自己哪里点错了。
 * 🔴 判的是 `revisionId` 非空,**不是** `status === 'ready'`:
 *    C 的定义是 `ready ⟹ active_revision_id 非空`(contract_worker),
 *    生产上那批 null 是 09-13 前的存量、要跑回填 —— 前端放宽判定
 *    只会把后端必然的 409 挪到用户点下去之后。
 */
export function rowBlockedReason(row: PublishRow | null | undefined): string {
    if (!row) return '';
    if (canPublishRow(row)) return '';
    if (['published', 'inflight'].includes(row.bucket)) return '';
    if (row.bucket === 'unknown') return '发布状态暂未识别，请刷新或打开记录核对；不要重复提交。';
    if (row.productionStatus && row.productionStatus !== 'ready') return '这条图文尚未制作完成，返回检查页查看进度或重试。';
    if (row.revisionId === null) return BLOCKED_ALL_NOTE;
    if (row.assetsComplete === false) return '图片尚未齐全，请返回检查页继续处理。';
    /* 有版本却仍不能发 = 桶不对(已发布 / 发布中)。那是状态,不是障碍,不用解释。 */
    return '';
}

/**
 * 有行、但**没有任何一行**能发 ⇒ 给出区域级说明;否则空串。
 *
 * 🔴 只在"有行"时说话:一条都没有是另一回事(`EMPTY_NOTE`),
 *    两件事混成一句,用户分不清是"没做"还是"做了发不了"。
 */
export function blockedAllNotice(rows: readonly PublishRow[] | null | undefined): string {
    const list = Array.isArray(rows) ? rows : [];
    if (list.length === 0) return '';
    return list.every((r) => rowBlockedReason(r) === BLOCKED_ALL_NOTE) ? BLOCKED_ALL_NOTE : '';
}

export const EMPTY_NOTE = '这个客户还没有图文作品';
export const EMPTY_CTA = '去创作中心做图文 →';
/*
 * 🔴 [#204 a2] 原来指向 `/writing` —— 那是创作中心的**壳**,图文靠它下面
 *    「制作 GEO 图文」那个 tab 进。本单把那块屏退役之后,`/writing` 上
 *    **没有图文入口**了:这条 CTA 会把人送到一个找不到图文的页面。
 *    改指 `/writing/image-note`(无 id 那条路由:有作品跳最近一条,
 *    一条都没有就落在选题左栏上)。
 */
export const EMPTY_CTA_HREF = '/writing/image-note';
