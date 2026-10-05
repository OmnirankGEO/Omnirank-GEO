/**
 * #178 · 「选中方向下的词全被排除」这一态的判定与出口 —— 纯函数,**零 import**。
 *
 * 为什么单独一个模块:本单的缺陷不是「少画了一张卡片」,而是**一个状态没有出口**。
 * 出口该不该给、给哪几条、提交键该不该点得动,这些判断必须能被判据**真的调用**
 * (而不是 grep 「卡片在不在」)。零 import 是为了让判据能 transpile 后直接 import 进来
 * 真跑 —— 本仓没有 RTL/vitest,这是唯一能在 build 链里跑真行为的形态。
 *
 * 🔴 与 `components/quote/ExcludedKeywordsPanel.tsx` **不是一份东西,别合**:
 *    那个是报价阶段的只读展示区,字段是 `reason_text / intent_type / needs_clarification`;
 *    这里是客户页死胡同的出口卡,字段是后端 #178 契约的 `reason / kind / policy_version`。
 *    两边形状不同、职责不同(一个只解释,一个必须给出路)。要合先问意图。
 *
 * 🔴 边界只断言一次「这是一行响应」,之后逐字段构造 —— 不写 `as T`。
 *    (09-10 的 ¥NaN 就是网络边界上一个 `as T`:泛型承诺了一个从没验过的形状。)
 */

/** 后端 #178 契约里的一条被排除词。`id` 只有目录词有,自定义词没有。 */
export interface ExcludedRow {
    id?: number;
    keyword: string;
    reason: string;
    /**
     * 后端只给两个值,**文案必须分开**(C 2026-09-12 契约):
     * - `knowledge_term_not_deliverable`:知识/百科类,不可改选,只解释;
     * - `needs_clarification`:歧义词,客户澄清成明确商业问法后**可重新提交**。
     */
    kind: string;
    policy_version?: string;
}

export const KIND_NOT_DELIVERABLE = 'knowledge_term_not_deliverable';
export const KIND_NEEDS_CLARIFICATION = 'needs_clarification';

export interface AllExcludedState {
    /** 后端明说的 `all_excluded`。**不由前端从计数推**,也不与任何固定 status 绑死。 */
    allExcluded: boolean;
    /** 后端落库后的真实状态,原样透传(C 明确:submit-keywords 零可交付时不落库)。 */
    status: string;
    notDeliverable: ExcludedRow[];
    needsClarification: ExcludedRow[];
    /** 两类之和 —— 给标题用。不等于 notDeliverable.length。 */
    total: number;
    /** 报价方是否已被通知(后端已推过就别让客户再点一次)。 */
    notifySent: boolean;
    nextActionKind: string;
    /**
     * 为什么没有可交付的问法。Review 09-12 预告 C 会把同函数里另一条无出口 400
     * (「选中业务线下没有可用关键词」)收进同类,届时契约加这个字段:
     * - `all_excluded`(默认):词有,但全被政策排除了 —— 客户改问法能救;
     * - `no_keywords_for_lines`:这个方向下**压根还没配词** —— 客户改问法也救不了,
     *   得报价方去配;两条出口一样,但标题不能说成"你的词被排除了"(那是冤枉客户)。
     *
     * 🔴 现在**没有任何后端在发这个值**(C 可能拆到 #178-B)。所以这里读得动、
     *    认不出的值一律落回默认,但跨层那条边**未评估** —— 见交付物 §7。
     */
    reason: string;
}

export const REASON_ALL_EXCLUDED = 'all_excluded';
export const REASON_NO_KEYWORDS = 'no_keywords_for_lines';

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const asRows = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/**
 * 把一条被排除词从未知结构里逐字段取出来。取不到的给空串,**不抛**——
 * 客户页不能因为某一行少个字段就整页白屏(那比死胡同更糟)。
 */
function readRow(raw: unknown): ExcludedRow | null {
    if (!raw || typeof raw !== 'object') return null;
    const o = raw as Record<string, unknown>;
    const keyword = str(o.keyword);
    if (!keyword) return null;
    const row: ExcludedRow = {
        keyword,
        reason: str(o.reason),
        kind: str(o.kind),
    };
    if (typeof o.id === 'number') row.id = o.id;
    const pv = str(o.policy_version);
    if (pv) row.policy_version = pv;
    return row;
}

/**
 * 读 submit-business-lines / submit-keywords 的 200 响应(或 GET 页面数据)。
 *
 * 两个端点同名同形,所以一个函数读两边 —— 同一谓词两处,必有一处没人验。
 */
export function readAllExcluded(body: unknown): AllExcludedState {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    const rows = [
        ...asRows(o.delivery_excluded_keywords),
        ...asRows(o.delivery_excluded_custom_keywords),
    ]
        .map(readRow)
        .filter((r): r is ExcludedRow => r !== null);
    const na = (o.next_action && typeof o.next_action === 'object'
        ? o.next_action
        : {}) as Record<string, unknown>;
    return {
        allExcluded: o.all_excluded === true,
        status: str(o.status),
        notDeliverable: rows.filter((r) => r.kind !== KIND_NEEDS_CLARIFICATION),
        needsClarification: rows.filter((r) => r.kind === KIND_NEEDS_CLARIFICATION),
        total: rows.length,
        notifySent: na.notify_sent === true,
        nextActionKind: str(na.kind),
        reason: str(o.reason) || REASON_ALL_EXCLUDED,
    };
}

/**
 * 卡片标题。**认不出的 reason 落回"全被排除"那一版**,不显示原始枚举值 ——
 * 客户页上冒出一个英文枚举比说错话更糟。
 */
export function cardTitle(state: { reason: string; total: number }): string {
    if (state.reason === REASON_NO_KEYWORDS) {
        // 🔴 这一版不能说"你的问法被排除了" —— 这个方向下压根没配词,不是客户的错。
        return '这个方向下还没有配好的问法';
    }
    return `这 ${state.total} 个问法不会让 AI 推荐具体品牌，未计入本次交付`;
}

/**
 * 标题下面那句解释。**必须跟标题同源换**:
 * 改了标题却留着"它们是知识/百科类问法、投了也拿不到推荐位"这句,
 * 等于在"这个方向压根没配词"的情形下继续怪客户写错了词 —— 半句真话比整句假话更难发现。
 */
export function cardSubtitle(state: { reason: string }): string {
    if (state.reason === REASON_NO_KEYWORDS) {
        return '不是你写错了 —— 这个业务方向下还没有配好可交付的问法。你可以自己加一条，或让报价方补。';
    }
    return '它们是知识/百科类问法（比如「是什么」「怎么保养」）。用户这样问的时候，'
        + 'AI 只会讲知识、不会推荐服务商 —— 投了也拿不到推荐位，所以这里不收费。';
}

/**
 * 用 `GET /api/s/{token}` 的页面数据校准这一态 —— **刷新之后卡片还得在**。
 *
 * 🔴 为什么不是简单地 `setState(readAllExcluded(d))`:卡片最初是 POST 响应给的,
 *    而紧接着就会 `fetchData()` 重取页面。如果 GET 里**没有**这几个字段(后端补之前
 *    就是这样),无条件覆盖会把刚刚拿到的卡片当场抹掉 —— 客户看到卡片闪一下就没了,
 *    退回那个死胡同,而且没有任何报错。所以:
 *    · GET 明确带了 `all_excluded` ⇒ 以 GET 为准(true 立起来 / false 清掉,它才是落库真相);
 *    · GET 里根本没这个键 ⇒ **保留**已有的,不覆盖也不清空。
 *    这样在后端补字段之前和之后,前端都不会骗人。
 */
export function reconcileFromPage(
    prev: AllExcludedState | null,
    body: unknown,
): AllExcludedState | null {
    const o = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    if (!Object.prototype.hasOwnProperty.call(o, 'all_excluded')) return prev;
    const parsed = readAllExcluded(o);
    if (!parsed.allExcluded) return null;
    /**
     * 🔴 防御性合并 `reason`:GET 带了 `all_excluded` 却漏了 `reason` 时,
     *    整体覆盖会让卡片退回默认那一档标题 ——「这 0 个问法不会让 AI 推荐具体品牌」——
     *    那句话在"这个方向压根没配词"的情形下**是在冤枉客户**,正是本单要消灭的话。
     *    契约说 POST/GET 同名同形,所以正常不会发生;但这一格的代价不对称
     *    (漏字段只是少块信息,退回默认却是说错话),所以保住已知的那一档。
     */
    if (prev && parsed.reason === REASON_ALL_EXCLUDED
        && prev.reason && prev.reason !== REASON_ALL_EXCLUDED
        && !Object.prototype.hasOwnProperty.call(o, 'reason')) {
        return { ...parsed, reason: prev.reason };
    }
    return parsed;
}

/**
 * 出口① 的改写示例(纯文案 · 不调 AI · 不扣算力)。
 *
 * 套的是 `services/commercial_query_policy` 认的商业形态:品牌直问、选型、价格。
 * 品牌名用页面已有的 brand_name —— 政策里「文本含品牌名 ⇒ BRAND_DIRECT ⇒ 可交付」,
 * 所以「<品牌>怎么样」这种客户最爱写的词本来就该活下来(本单 C 侧 a 项修的正是它)。
 */
export function rewriteExamples(brandName: string, category?: string): string[] {
    const brand = (brandName || '').trim();
    const cat = (category || '').trim();
    const out: string[] = [];
    if (brand) {
        out.push(`${brand}怎么样`);
        out.push(`${brand}靠谱吗`);
    }
    if (cat) {
        out.push(`${cat}哪家好`);
        out.push(`${cat}价格多少`);
    } else if (brand) {
        out.push(`${brand}多少钱`);
    }
    // 一个品牌名都没有时也必须给得出东西(白卡片等于没出口)
    if (out.length === 0) {
        out.push('哪家好', '怎么选', '价格多少');
    }
    return out.slice(0, 3);
}

/**
 * 哪条出口该当主出口。
 *
 * 🔴 两档的**补救动作不同**(C 09-12 #178-B 契约原话):
 * - `all_excluded`:词有,但全是知识/百科/需澄清 ⇒ 客户**自己换个问法**就能救,
 *   主出口是「就地加一条商业问法」;
 * - `no_keywords_for_lines`:这个方向下**一条候选词都没生成** ⇒ 客户当然也能自己加,
 *   但真正该动的是报价方去配词,主出口给「让报价方补充」。
 *   (把"自己写一条"摆成主出口,等于把配词这件事推给客户。)
 *
 * 两条出口**始终都在**,这里只决定谁在前、谁是实心按钮。
 */
export function primaryExit(state: { reason: string }): 'add_keywords' | 'notify' {
    return state.reason === REASON_NO_KEYWORDS ? 'notify' : 'add_keywords';
}

export interface SubmitGate {
    disabled: boolean;
    /** 🔴 禁用**必须**配一句就地可见的原因 —— 不是 toast,不是空 tooltip。 */
    reason: string;
}

/**
 * 全排除态下那个提交键能不能点。
 *
 * 「禁用且不说为什么」和「死按钮」在客户眼里是同一件事:点不动、不知道下一步。
 * 所以本函数的返回里 reason 与 disabled 同生同死 —— disabled 为真时 reason 必非空。
 */
export function submitGateState(input: {
    allExcluded: boolean;
    deliverableCount: number;
    submitting?: boolean;
}): SubmitGate {
    if (input.submitting) {
        return { disabled: true, reason: '正在提交…' };
    }
    if (!input.allExcluded) {
        return { disabled: false, reason: '' };
    }
    if (input.deliverableCount <= 0) {
        return {
            disabled: true,
            reason: '还没有可交付的问法 —— 先在上方加一条选型/价格类问法，或让报价方补充',
        };
    }
    return { disabled: false, reason: '' };
}

/**
 * 「✅ 业务方向已提交,我们正在为您准备专属关键词方案」这条绿条能不能挂。
 *
 * 🔴 全排除时**不能**:没有人在准备方案 —— 要么客户自己补商业问法,要么报价方补词。
 *    挂着它就是 #165 F11 那一类「说的和做的不一致」,而且比死胡同更有欺骗性:
 *    客户会安心去等一个永远不来的方案。
 */
export function shouldClaimSubmitted(input: {
    status: string;
    allExcluded: boolean;
    businessLinesSubmitted: boolean;
}): boolean {
    if (input.allExcluded) return false;
    return input.businessLinesSubmitted;
}

export interface NotifyState {
    label: string;
    disabled: boolean;
    /** 已经通知过了(后端说的,或这次点完了)—— 显示态,不是按钮态。 */
    done: boolean;
}

/** 出口②「让报价方补充」的按钮态。已推过就不给再点(不制造重复打扰)。 */
export function notifyState(input: {
    notifySent: boolean;
    alreadySent?: boolean;
    inFlight?: boolean;
}): NotifyState {
    if (input.notifySent || input.alreadySent) {
        return { label: '已通知报价方补充问法', disabled: true, done: true };
    }
    if (input.inFlight) {
        return { label: '正在通知…', disabled: true, done: false };
    }
    return { label: '让报价方补充问法', disabled: false, done: false };
}
