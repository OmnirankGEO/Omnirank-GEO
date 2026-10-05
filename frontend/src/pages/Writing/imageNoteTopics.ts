/**
 * #204 · 图文选题的**领域层**(零 import,判据能真调)。
 *
 * Owner 09-13 夜口径:「不是全自动,是半自动,和写作一样:默认把这几个选题写出来,
 * 用户可以修改,修改后就按照标题来进行创作。**用习惯写文章的用户可以完全不用学习**。」
 *
 * 🔴 所以这一层的每条规则都不是新发明的,都能在写文章大厅里指出对应物:
 *    · 只有「待做」能改标题、能勾选   ← 写文章只有「待写」能改标题、能勾选
 *    · 按钮上带数量与价             ← 「开始写作 (N) · X 算力」
 *    · 价**只来自服务端**            ← 写文章 4061 行:价目没到就禁开始并说清
 *    · 进度用服务端的 stage/percent  ← 元指令 8
 *
 * 🔴 **本文件一个价格算术都没有。** 有的只是"把服务端给的数放进一句话里"。
 */

export type TopicStatus = 'pending' | 'making' | 'done' | 'failed' | 'archived' | 'unknown';

/** 后端 `geo_douyin_topics` 的一行(只取界面用得到的字段)。 */
export interface TopicRow {
    id: number;
    /** 选题标题。这就是「按标题制作」里那个标题。 */
    title: string;
    keyword: string;
    city: string;
    /** 卡面风格;空串 = 还没选,界面显示「系统推荐」。 */
    styleKey: string;
    status: TopicStatus;
    /** 三态在屏幕上的说法(人话,不是状态码)。 */
    statusLabel: string;
    /** 做出来的作品 id;只有 done 才有(c1 的 `ck_geo_douyin_topic_post_shape` 钉着)。 */
    postId: number | null;
    confirmedKeywordId?: number | null;
    fromKeyword?: boolean;
    topicId?: number | null;
    quoteId?: number | null;
    originLabel?: string;
    producedCount?: number;
    /** 这一行能不能改标题。 */
    editable: boolean;
    /** 这一行能不能勾选去制作。 */
    selectable: boolean;
    /** 改过标题的会变成 'user'(c1 的 PATCH 会置)。 */
    source: string;
    /** 失败时服务端给的原话;没有就空串(**不编**)。 */
    failure: string;
}

const str = (v: unknown): string => (typeof v === 'string' ? v.trim() : '');
const int = (v: unknown): number | null => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
};

/*
 * 🔴 [WO_283-F7] 状态的人话**只写在这一张表里**:行上的徽章、面板的筛选 chip、关键词行都从这里取
 *    (`statusText`)。原来 chip 另写了一遍,待做那一档 chip 叫「未制作」、徽章叫「待做」,已经对不上;
 *    统一叫「未制作」—— 默认的关键词视图与 chip 一直就是这么叫的。
 */
const STATUS_LABEL: Record<string, string> = {
    pending: '未制作',
    making: '制作中',
    done: '已完成',
    failed: '没做成',
    archived: '已归档',
    /* 认不出的状态。**不叫「待做」** —— 见 `topicRow` 里那段注释。 */
    unknown: '状态未知',
};
/** 某个状态在屏幕上的说法(筛选 chip 等处用它,不许另写一份)。 */
export function statusText(status: string): string {
    return STATUS_LABEL[status] || STATUS_LABEL.unknown;
}
const price = (value: unknown): number | null => typeof value === 'number'
    && Number.isInteger(value) && value >= 0 ? value : null;

/**
 * 一行选题的投影。
 *
 * 🔴 `editable` / `selectable` **只认 pending**。
 *    不是"界面上不让点"而已 —— c1 的 `PATCH /topics/{id}` 对 making/done 直接 409,
 *    `POST /posts` 对已 making/done 的 topic 也 409。
 *    界面给一颗按下去必然 409 的按钮,比不给更糟。
 */
export function topicRow(raw: unknown): TopicRow {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    const s = str(o.status) as TopicStatus;
    /*
     * 🔴 认不出的状态兜底成 **`unknown`**,不是 `pending`。
     *
     *    第一版写的是 `: 'pending'` —— 而「待做」是**要扣钱的那一档**:
     *    它能被勾选、会被算进「开始制作 (N) · X 算力」的那个 N 里。
     *    后端哪天加一个新状态,前端就把它悄悄放进待做,用户一勾一点,
     *    就为一个我们根本不认识的东西付了钱。
     *    ⇒ 认不出就**明说认不出**:不进四档、不进计数、不能勾、不能改。
     *
     *    (这一行是注毒 S6 顺出来的:那发毒本来打的是下面 `archived` 那道
     *     过滤,毒下成了却全绿 —— 因为 `pick()` 是精确匹配,那道过滤本来
     *     就冗余。顺着"为什么冗余"查下去,真正的口子在这里。)
     */
    const status = (STATUS_LABEL[s] ? s : 'unknown') as TopicStatus;
    return {
        id: int(o.id) || 0,
        title: str(o.title),
        keyword: str(o.keyword),
        city: str(o.city),
        styleKey: str(o.style_key),
        status,
        statusLabel: STATUS_LABEL[status],
        postId: int(o.post_id),
        confirmedKeywordId: int(o.confirmed_keyword_id),
        editable: status === 'pending',
        selectable: status === 'pending',
        source: str(o.source) || 'distilled',
        failure: str(o.failure_reason) || str(o.error_msg),
    };
}

export interface TopicBuckets {
    pending: TopicRow[];
    making: TopicRow[];
    done: TopicRow[];
    failed: TopicRow[];
    /** 每一档的计数(含 0)——写文章大厅的三 tab 也是计数常驻。 */
    counts: Record<string, number>;
}

/** Card and checkbox share one bounded selection transition. */
export function toggleTopicSelection(current: ReadonlySet<number>, id: number): Set<number> {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else if (next.size < 20) next.add(id);
    return next;
}

export function currentTopicRead(scope: string, activeScope: string, sequence: number,
    activeSequence: number, aborted = false): boolean {
    return !aborted && scope === activeScope && sequence === activeSequence;
}

/**
 * 一次列表读取该怎么碰屏幕。
 *
 * `read`    = 首读 / 换客户换页换来源 / 用户点「重新读取」:清屏、出「正在读取…」、出错就报错。
 * `refresh` = 制作中的后台心跳(每 4 秒一次):**不碰 loading、不清行、不报错**,
 *             读到了就原地换行,读不到就记一次 miss 留给 `refreshNotice` 出声。
 *
 * 🔴 Owner 2026-09-22:「心跳一直在前端刷新…前端一直看着它闪」。
 *    根因是心跳走的是和首读同一条路:每 4 秒 `setLoading(true)` 一次,
 *    列表整块被换成「正在读取内容和制作进度…」再换回来 —— 用户看到的就是闪。
 */
export type TopicReadMode = 'read' | 'refresh';
export interface TopicReadPlan {
    /** 要不要把列表换成「正在读取…」占位 */
    showLoading: boolean;
    /** 读失败时:true = 行照旧、不报错、只记 miss;false = 报错并清行 */
    silentErrors: boolean;
}
export function topicReadPlan(mode: TopicReadMode): TopicReadPlan {
    if (mode === 'refresh') return { showLoading: false, silentErrors: true };
    return { showLoading: true, silentErrors: false };
}

/**
 * 后台心跳连着几次没读到才出声。
 * 沉默是最坏的那一种:行停在「制作中」不动,和"卡死了"长得一模一样。
 * 出声不换掉列表 —— 下面还是上次读到的内容。
 */
export const REFRESH_STALL_AFTER = 3;
export function refreshNotice(misses: number): string {
    const n = Number.isFinite(misses) ? Math.max(0, Math.floor(misses)) : 0;
    if (n < REFRESH_STALL_AFTER) return '';
    return `制作进度连着 ${n} 次没刷新到，下面还是上次读到的内容；任务多半还在跑，页面留着别关。`;
}

/** Negative UI key identifies a keyword, never a persisted topic. No GET writes. */
export function keywordRow(raw: unknown): TopicRow {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    const id = int(o.confirmed_keyword_id);
    const row = topicRow({ id: 1, status: o.production_status, keyword: o.keyword,
        title: o.post_title || o.topic_title || o.keyword, city: o.post_city || o.quote_city,
        post_id: o.post_id, style_key: o.style_key, confirmed_keyword_id: id });
    return { ...row, id: id ? -id : 0, fromKeyword: true, topicId: int(o.topic_id),
        quoteId: int(o.quote_id), originLabel: o.quote_status === 'paid' ? '已购关键词' : '已确认关键词',
        producedCount: Math.max(0, Number(o.produced_count) || 0), source: 'keyword', editable: false,
        statusLabel: row.status === 'pending' ? (Number(o.required_articles) > 0 ? STATUS_LABEL.pending : '未安排内容')
            : o.production_status === 'incomplete' ? '尚未完成' : row.statusLabel,
        // Zero content allocation is not a purchased production obligation. The
        // separate manual topic route remains available for additional content.
        selectable: row.selectable && Number(o.required_articles) > 0 };
}

/**
 * 三态分档。
 *
 * 🔴 `archived` **不进任何一档,也不进计数** —— 它是被删掉的那些。
 *    但它照样从接口回来,所以这里要**显式**把它挡掉:
 *    不挡的话它会悄悄算进"待做",让「开始制作 (N)」的 N 比真的多。
 */
export function topicBuckets(rows: readonly TopicRow[] | null | undefined): TopicBuckets {
    /*
     * `archived` 与 `unknown` 都不进四档、不进计数。
     * 🔴 注毒 S6 证明过:光靠下面 `pick()` 的精确匹配,这道过滤其实是**冗余**的。
     *    留着只是为了把"哪些状态不该进计数"写进代码,而不是让它隐含在
     *    "我恰好只 pick 了四档"这个事实里。有牙的那一格在 `topicRow`。
     */
    const all = (rows || []).filter((r) => r && (r.id > 0 || (r.fromKeyword && r.id < 0 && !!r.confirmedKeywordId))
        && r.status !== 'archived' && r.status !== 'unknown');
    const pick = (s: TopicStatus) => all.filter((r) => r.status === s);
    const pending = pick('pending');
    const making = pick('making');
    const done = pick('done');
    const failed = pick('failed');
    return {
        pending,
        making,
        done,
        failed,
        counts: {
            pending: pending.length,
            making: making.length,
            done: done.length,
            failed: failed.length,
        },
    };
}

/**
 * 「生成选题」按钮上的字。
 *
 * 🔴 价**只从服务端来**(元指令 2:按钮级确认扣费,且价目 SSOT 唯一)。
 *    取不到就**明说取不到**,不要显示一个看起来合理的数 —— 也不要不写价就让人点。
 */
export function distillLabel(points: number | null | undefined): string {
    const p = price(points);
    return p === null ? '生成选题 · 价目读不到' : `生成选题 · ${p} 算力`;
}

/**
 * 「开始制作」按钮上的字。
 *
 * 🔴 同上:`points` 是 `production-quote` 回来的 `total_points`,**前端不做乘加**。
 *    (制作台那一页的抬头写着"本页一个价格算术都没有",这里沿用同一条。)
 */
export function startLabel(n: number, points: number | null | undefined): string {
    const count = Number.isFinite(Number(n)) && Number(n) > 0 ? Math.floor(Number(n)) : 0;
    const p = price(points);
    if (count === 0) return '开始制作';
    return p === null ? `开始制作 (${count}) · 价目读不到` : `开始制作 (${count}) · ${p} 算力`;
}

/** 这一刻能不能点「开始制作」:勾了东西 + 价拿到了 + 没有正在制作的。 */
export function canStart(input: {
    selectedCount: number;
    points: number | null | undefined;
    busy?: boolean;
}): boolean {
    return (input?.selectedCount || 0) > 0 && price(input?.points) !== null && !input?.busy;
}

export interface DistillProgress {
    /** 屏幕上那一行字。 */
    line: string;
    /** 进度条百分比。**服务端给多少就是多少。** */
    percent: number;
    /** 还要不要接着问。 */
    keepPolling: boolean;
    /** 终态且失败时的原话;没有就空串。 */
    failure: string;
    /** [WO_283-F6] 服务端估的剩余时间(「约 N 秒」);估不出 / 已到终态就空串,不编。 */
    eta: string;
    /** [WO_283-F2] 这批选题是降级做出来的 ⇒ 一句人话;没降级就空串。 */
    degraded: string;
}

/**
 * [WO_283-F2] 少样本降级原因 → 人话(旧入口页那张表原样搬回;工程术语不给用户看)。
 * 后端 `db/douyin_corpus_db.load_fewshot` 写得出的四种原因逐个有人话;认不出的也要说,不沉默。
 */
const DEGRADE_LABEL: Record<string, string> = {
    corpus_unavailable: '参考语料暂不可用',
    corpus_empty: '还没有参考语料',
    no_same_industry_image_post: '同行业图文样本不足',
    partial_same_industry_image_post: '同行业图文样本偏少',
};

export function degradeNotice(code: unknown): string {
    const c = str(code);
    if (!c) return '';
    return `这批选题参考的样本不够（${DEGRADE_LABEL[c] || '参考样本不足'}），贴合度可能差一些，可以改标题后再制作。`;
}

/**
 * 蒸馏进度。
 *
 * 🔴 **一个数都不许前端编。** `percent` 直接取服务端的 `percent`,
 *    `line` 用服务端的 `stage_label`。假进度条是元指令 8 的反面:
 *    它让"卡住了"和"正在跑"长得一模一样,而用户正是靠这条线判断要不要等下去。
 *    (判据 A2 的毒就是"前端自己按时间涨百分比" ⇒ 必须红。)
 *
 * 🔴 任务行查不到时服务端给的是**终态 failed**(不是 queued)——
 *    给 queued 前端会永远转圈。这里照它给的来,不另做判断。
 */
export function distillProgress(task: unknown): DistillProgress {
    const o = (task && typeof task === 'object' ? task : {}) as Record<string, unknown>;
    const state = str(o.state);
    const label = str(o.stage_label);
    const pctRaw = Number(o.percent);
    const percent = Number.isFinite(pctRaw) ? Math.max(0, Math.min(100, Math.floor(pctRaw))) : 0;
    const active = o.active === true;
    const failure = str(o.message);
    const line = state === 'succeeded' ? '选题已经生成好了'
        : state === 'failed' ? (failure || '这次没生成出来，可以再试一次')
            : (label || '正在想选题');
    /* ETA 只在还在跑时说;percent 按阶段取档,最长的那一段里只有它在动(WO_283-F6) */
    const etaRaw = Number(o.eta_seconds);
    const eta = active && o.eta_seconds !== null && Number.isFinite(etaRaw) && etaRaw > 0
        ? `约 ${Math.max(1, Math.round(etaRaw))} 秒` : '';   // 不用 Math.ceil:图文流里它是「前端自算配额」的锁定痕迹
    const degraded = state === 'succeeded' ? degradeNotice(o.fewshot_degraded) : '';
    return { line, percent, keepPolling: active, failure: state === 'failed' ? failure : '', eta, degraded };
}

/**
 * 选中某一行时,中栏/右栏该显示什么。
 *
 * 🔴 [A6/A7] 「待做」的选题**还没有图也没有文案** —— 中栏要显示那一档风格的**样图**
 *    并明说「还没做」,右栏的编辑区**禁用并写清原因**。
 *    不写原因的禁用就是第 11 条要清的那种猜(#199 刚清过一批)。
 */
export interface SelectionView {
    /** 中栏显示什么:真图 / 风格样图 / 空 */
    preview: 'artifact' | 'style_sample' | 'empty';
    /** 右栏编辑区能不能用 */
    canEdit: boolean;
    /** 不能用时那一句话(能用时空串) */
    reason: string;
    /** 中栏那一行说明 */
    note: string;
}

export function selectionView(row: TopicRow | null | undefined): SelectionView {
    if (!row || !row.id) {
        return { preview: 'empty', canEdit: false, reason: '', note: '选择一个已有关键词，就能开始制作图文' };
    }
    if (row.status === 'done' && row.postId) {
        return { preview: 'artifact', canEdit: true, reason: '', note: '' };
    }
    if (row.status === 'making') {
        return {
            preview: 'style_sample',
            canEdit: false,
            reason: '这一条正在制作，做完才能改文案',
            note: '正在制作，做好了这里就会出图',
        };
    }
    if (row.status === 'failed') {
        return {
            preview: 'style_sample',
            canEdit: false,
            reason: row.failure || '这一条没做成，可以重新制作',
            note: row.failure || '这一条没做成',
        };
    }
    return {
        preview: 'style_sample',
        canEdit: false,
        reason: '这一条还没做，先勾上它点「开始制作」',
        note: '还没做 —— 下面这张是这个风格做出来大概的样子',
    };
}

/* ── 出错分支给人看的那句话(WO_282-F4)──────────────────────────────── */

/**
 * 我们自己写给人看、可以原样上屏的错误,按**名字**认,不按类认:
 * 本模块零 import,`imageNoteProduction.ts` 也不能值导入它(判据按 data: URL 单独加载那几个模块,
 * 相对 import 在那里解析不了),所以那边抛错时直接带上同一个名字。
 */
export const USER_ERROR_NAME = 'ImageNoteUserError';

export function userError(message: string): Error {
    return Object.assign(new Error(message), { name: USER_ERROR_NAME });
}

/** 断网时 fetch 的原话:Chrome「Failed to fetch」/ Firefox「NetworkError …」/ Safari「Load failed」。 */
const NETWORK_ERROR = /Failed to fetch|NetworkError|Load failed|Network request failed/i;

export const NETWORK_ERROR_TEXT = '网络不通，请检查后重试。';

/**
 * 出错分支上屏的那一句。
 *
 * 🔴 原话(浏览器、解析器,任何不是我们写的)**只进 console,不上屏**:
 *    我们自己抛的 → 原样;断网 → 统一人话;其余 → 调用方给的兜底(现有文案口径)。
 *    改写前「开始制作」断网时屏幕上是英文 `Failed to fetch`;工作台路由先裸 `.json()` 再判 ok,
 *    nginx 的 HTML 错误页会原样变成 `Unexpected token '<'` 上屏 —— 原锁要拦的正是这一种。
 */
export function userFacingError(e: unknown, fallback: string): string {
    const err = (e && typeof e === 'object' ? e : {}) as { name?: unknown; message?: unknown };
    const message = typeof err.message === 'string' ? err.message : '';
    if (err.name === USER_ERROR_NAME && message) return message;
    try { console.warn('[image-note]', e); } catch { /* 记不下也不影响上屏那一句 */ }
    return NETWORK_ERROR.test(message) ? NETWORK_ERROR_TEXT : fallback;
}
