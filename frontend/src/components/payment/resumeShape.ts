/**
 * resumeShape — `order-status` 的响应形状 → 各支付面自己的 PayInfo。
 * [#169 返修 · 2026-09-10]
 *
 * 🔴 **为什么要有这个文件**:生产实测(Review QA 账号,1 元真单
 *    AIP600D61E064DF4070)带 `?resume_order=` 打开 /agent/inventory,
 *    弹窗出来了、二维码也出来了,**金额显示「¥NaN」**。
 *
 * 🔴 是我把类型系统关掉造成的。原来的恢复代码是:
 *        const info = { ...d, order_id: d.order_id || id } as T;
 *    `as T` 是一句**没有根据的断言** —— 它告诉 tsc「别管形状,我说它是 T」。
 *    而端点回 `amount_yuan`(数字 1),InventoryCenter 的 PayInfo 要的是
 *    `amount_cents`,`formatCents(undefined)` 就是 NaN。
 *    泛型 `useResumeOrder<T>` 承诺的是「我给你一个 T」,实际交付的是
 *    「端点说什么就是什么,只是换了个名字」。**tsc 本来能拦住,是我让它闭嘴的。**
 *
 * ⇒ 所以现在:hook 不再 cast,由**调用方给一个 mapper**;
 *   而两种量纲(分 / 元)的换算**只在 `yuanToCents` 一处**。
 *
 * 本模块**零 import**:判据用 data: URL 加载它真调两个 mapper,
 * 逐字段核对产出,而不是 grep 源码猜。
 */

/** `GET /api/wallet/order-status/{id}` 的 `data`。字段名与后端逐字一致。 */
export interface OrderStatusRow {
    order_id: string;
    status: string;
    paid_at?: string | null;
    /** 🔴 后端给的是**元**(`float(Decimal(cents)/100)`),不是分 */
    amount_yuan?: number | null;
    total_points?: number | null;
    base_points?: number | null;
    bonus_points?: number | null;
    /** 🔴 可为 null:`actual_payment_channel` 老单大量为空 */
    actual_channel?: string | null;
    payment_url_mobile?: string | null;
    payment_url_qrcode?: string | null;
    code_url?: string | null;
    needs_openid?: boolean | null;
}

/**
 * 元 → 分。**全仓这一处换算**。
 *
 * 🔴 必须 `Math.round`:`1.15 * 100` 在 IEEE754 下是 114.99999999999999,
 *    截断会变成 114 分 —— 少收一分钱的 bug 比 NaN 更难发现,因为它看起来很正常。
 */
export function yuanToCents(yuan: number | null | undefined): number {
    const n = Number(yuan);
    if (!Number.isFinite(n)) return 0;
    return Math.round(n * 100);
}

/** 这一单一个支付出口都没有(058 之前建的老单)。 */
export function hasNoExit(row: OrderStatusRow): boolean {
    return !row.payment_url_mobile && !row.payment_url_qrcode && !row.code_url;
}

/** 进货面(`AgentInventoryPurchaseResponse`):它按**分**记账。 */
export function toInventoryPayInfo(row: OrderStatusRow) {
    return {
        order_id: row.order_id,
        amount_cents: yuanToCents(row.amount_yuan),
        base_points: Number(row.base_points) || 0,
        bonus_points: Number(row.bonus_points) || 0,
        total_points: Number(row.total_points) || 0,
        actual_channel: row.actual_channel || '',
        code_url: row.code_url ?? null,
        payment_url_qrcode: row.payment_url_qrcode ?? null,
        payment_url_mobile: row.payment_url_mobile ?? null,
        needs_openid: Boolean(row.needs_openid),
    };
}

/** 买算力面(`BuyCredit` 的 `PayInfo`):它按**元**记账。 */
export function toBuyCreditPayInfo(row: OrderStatusRow) {
    return {
        order_id: row.order_id,
        amount_yuan: Number(row.amount_yuan) || 0,
        base_points: Number(row.base_points) || 0,
        bonus_points: Number(row.bonus_points) || 0,
        total_points: Number(row.total_points) || 0,
        // 🔴 端点不回 tier_label(它是下单时的档位文案,恢复时无从得知)。
        //    给空串而不是 `as` 掉 —— 本页当前不渲染它;将来若要渲染,
        //    空串会当场看得见,而 `undefined` 只会变成一句 "undefined"。
        tier_label: '',
        actual_channel: row.actual_channel || '',
        code_url: row.code_url ?? undefined,
        payment_url_qrcode: row.payment_url_qrcode ?? undefined,
        payment_url_mobile: row.payment_url_mobile ?? undefined,
        needs_openid: Boolean(row.needs_openid),
    };
}
