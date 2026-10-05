/**
 * U-5 交付待办 + U-6 回环 + Z-4 受众边界的**纯逻辑**(零 React 依赖)。
 *
 * 与同目录 `statusVersionGuard.ts` 同一理由:判据要能直接 import 这些谓词,
 * 而不是先 bundle 一棵 React 依赖树、再从 DOM 里反推分支走没走对。
 * (浏览器级判据仍然要有 —— 见 `frontend/scripts/test-defgeo-delivery-todo.mjs`;
 *  两者分工不同:这里证明**谓词**对,那里证明**整页**对。)
 *
 * ══════════════════════════════════════════════════════════════════
 * U-5 逐字:「12~50 轮确认没有聚合页 = 验收红」
 * ══════════════════════════════════════════════════════════════════
 * 但**资金仍逐项**:一问一稿一媒体各自 preview→confirm、各自 receipt/freeze。
 * 所以这里只做**排序与计数**,不做任何"批量确认"的合并语义 ——
 * 合并一次提交会把 N 笔独立冻结变成一笔,那是改资金合同,不是改 UI。
 */

/** 后端 `GET /publish/delivery-todo` 的一项(逐字对齐 `api/defensive_publish_api.py`)。 */
export interface DeliveryTodoItem {
    planItemKey: string;
    publishSlotId: string;
    decisionSnapshotId: string | null;
    lifecycle: string | null;
    /** `awaiting_confirm` | `in_progress` | `not_previewed` —— 后端闭集。 */
    todoState: string;
    /** 人话由服务端下发,前端不自造(U-1)。 */
    todoLabel: string;
    exactPoints: number;
    publicMediaName: string | null;
    expiresAt: string | null;
    statusUrl: string | null;
}

export interface DeliveryTodoResponse {
    acceptedSnapshotId: number;
    items: DeliveryTodoItem[];
    pendingCount: number;
    totalPendingPoints: number;
}

/**
 * 呈现顺序。**等她确认的排最前** —— 她打开这一页只有一个目的。
 *
 * 🔴 同一档内按 `planItemKey` 稳定排序,不按 `expiresAt`:
 *    按到期时间排会让这一页在两次刷新之间跳来跳去(到期时间在走),
 *    她刚要点的那一项换了位置。
 */
const STATE_RANK: Record<string, number> = {
    awaiting_confirm: 0,
    not_previewed: 1,
    in_progress: 2,
};

export function orderTodoItems(items: readonly DeliveryTodoItem[]): DeliveryTodoItem[] {
    return [...items].sort((a, b) => {
        const ra = STATE_RANK[a.todoState] ?? 99;
        const rb = STATE_RANK[b.todoState] ?? 99;
        if (ra !== rb) return ra - rb;
        return a.planItemKey.localeCompare(b.planItemKey);
    });
}

/**
 * 「还有几项等你确认」。
 *
 * 🔴 **不信** `pendingCount` 一个字段就算完:它和 `items` 是同一份数据的两种说法,
 *    对不上的时候以逐项为准 —— 列表上明明有 3 项待确认、顶部却写 0,
 *    她会以为自己看错了。返回两个值让调用方能把这种不一致画出来。
 */
export function pendingSummary(res: DeliveryTodoResponse): {
    countFromItems: number;
    countFromServer: number;
    consistent: boolean;
    pointsFromItems: number;
} {
    const pending = res.items.filter((i) => i.todoState === 'awaiting_confirm');
    return {
        countFromItems: pending.length,
        countFromServer: res.pendingCount,
        consistent: pending.length === res.pendingCount,
        pointsFromItems: pending.reduce((s, i) => s + (i.exactPoints || 0), 0),
    };
}

// ══════════════════════════════════════════════════════════════════════
// U-6 · 深链
// ══════════════════════════════════════════════════════════════════════

/** 确认页深链。**同一个构造函数**给通知、待办页、过期重建三处用。 */
export function decisionDeepLink(snapshotId: string): string {
    return `/defensive-geo/publish/decision/${encodeURIComponent(snapshotId)}`;
}

export function commandDeepLink(commandId: string): string {
    return `/defensive-geo/publish/commands/${encodeURIComponent(commandId)}`;
}

/**
 * U-6 ①:审批通过的通知 → 直达**原**确认页。
 *
 * 🔴 「原」是重点:审批通过之后回到的必须是她当时看的那一份快照
 *    (GET 恢复语义),不是"再生成一份"。再生成一份 = 价格重算 + 旧的作废,
 *    而她刚刚才让负责人批过那一份。
 *
 * 通知里没有 snapshotId 时返回 `null` —— 宁可不给深链,也不要给一个
 * 落在"新建"上的链接。
 */
export function approvalDeepLink(
    notice: { decisionSnapshotId?: string | null } | null | undefined,
): string | null {
    const id = notice?.decisionSnapshotId;
    return id ? decisionDeepLink(String(id)) : null;
}

/**
 * U-6 ②:preview 过期 → 自动重建 → **价格没变时突出「直接确认」**。
 *
 * 判定只看 `exactTotalPoints` 一个数,且**严格相等**:
 * 差 1 点也算变了。她的心智是"我刚才看到的那个数还算数吗",
 * 容差会让这句"价格没变"在某天变成一句谎话。
 *
 * 🔴 任一侧拿不到数字 ⇒ 一律算「变了」(保守侧)。
 *    未知不能被说成"没变" —— 那正是把不知道谎报成好消息。
 */
export function priceUnchanged(
    before: number | null | undefined,
    after: number | null | undefined,
): boolean {
    if (typeof before !== 'number' || !Number.isFinite(before)) return false;
    if (typeof after !== 'number' || !Number.isFinite(after)) return false;
    return before === after;
}

// ══════════════════════════════════════════════════════════════════════
// Z-4 · 通知受众边界
// ══════════════════════════════════════════════════════════════════════

/**
 * 这条通知能不能进**客户** token / Portal 时间线。
 *
 * Z-4 逐字:「同角色同预算的媒体自动替换通知**只进服务商工作台操作日志**,
 * 不进客户 token/Portal 时间线;动了交付承诺的降档仍须回客户重签」。
 *
 * 🔴 判据打在**这一个谓词**上,而不是"客户页面上有没有画出来"——
 *    页面不渲染只是没画,数据照样发出去了(§9.4 的老教训)。
 *
 * 返回 true = 客户可见。默认 **false**:新加的通知种类默认不给客户看,
 * 要给必须显式登记。反过来(默认给看)会让某天新加的一条内部日志
 * 悄悄出现在客户时间线上,而没有任何判据变红。
 */
const CUSTOMER_VISIBLE_NOTICE_KINDS: ReadonlySet<string> = new Set([
    // 真降档:动了交付承诺,必须回客户重签 —— 所以客户必须看得到。
    'media_downgraded_needs_reconfirm',
    // 广告法命中要她去修,但客户侧看到的是"这一篇暂缓",仍属交付承诺变更。
    'legal_rule_hit_repairable',
]);

export function isCustomerVisibleNotice(kind: string): boolean {
    return CUSTOMER_VISIBLE_NOTICE_KINDS.has(kind);
}

/** 服务商工作台操作日志收哪些。同角色同预算替换**只**落这里。 */
export function isProviderLogNotice(kind: string): boolean {
    return kind === 'media_replaced_same_tier' || isCustomerVisibleNotice(kind);
}
