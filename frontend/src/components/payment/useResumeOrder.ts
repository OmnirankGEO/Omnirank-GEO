/**
 * useResumeOrder — 带 `?resume_order=<id>` 打开页面时,把那一单的支付弹窗恢复出来。
 * [WO #169 §2.1.2 · 2026-09-10]
 *
 * 🔴 **没有这个 hook,「复制链接到微信里打开」就是一条死链** ——
 *    她照做了、粘贴了、点开了,然后看到一个什么都没有的页面。
 *    那比不给这个按钮更糟:她会以为自己做错了,而不是知道系统没接上。
 *    (这正是本窗这几天在别处反复抓的同一类病:提示存在、动作不存在。)
 *
 * 🔴 **重取,不重新下单**:恢复只读 `/api/wallet/order-status/{id}`,
 *    绝不触发任何向虎皮椒/微信的新下单 —— 否则用户复制一次链接就多一张单。
 *
 * 🔴 **降级路是永久分支,不是脚手架 —— 别撤。**
 *    最初写它是因为「C 的字段还没上线」,那个理由已经过期;但**结论没变、原因换了**:
 *    C 的 058 迁移自己写着「**零 DML:只加列,不回填历史**……
 *    NULL 在这里是诚实的:它表示『这张单建于本迁移之前,我们没有留下出口』」,
 *    列注释也写着 `NULL = 本迁移之前建的单`。
 *    ⇒ 058 之前建的 pending 单**永远**拿不到 URL —— 包括本单事故里
 *      用户 125 那两张(08-07 与 09-10)。撤掉降级路,正好把受影响的那个人
 *      推去一句更差的话(「请联系客服」而不是「重新获取支付链接」)。
 *
 * 🔴 `degraded` 判的是**一个出口都没有**,不是「没有 H5」:
 *    wechat_native 的单只有 `code_url`(扫码),`payment_url_mobile` 本来就是空 ——
 *    只看 H5 会把一张桌面上完全能扫的单说成「要重新获取链接」。
 */
import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { readResumeOrderId } from './payExitMatrix';
import { hasNoExit, type OrderStatusRow } from './resumeShape';

export interface ResumedOrder<T> {
    info: T;
    /** 这一单**一个支付出口都没有**(058 之前建的老单)⇒ 必须明说,不静默 */
    degraded: boolean;
}

/**
 * 🔴 **必须由调用方给 mapper,本 hook 不再 `as T`。**
 *    原来写的是 `const info = { ...d, order_id: ... } as T` —— 一句没有根据的断言,
 *    等于让 tsc 闭嘴。生产实测的后果:进货面拿 `amount_cents` 渲染,
 *    而端点回的是 `amount_yuan` ⇒ 金额显示 **「¥NaN」**。
 *    网络边界上「把 JSON 当成某个类型」本来就无法在编译期证明,
 *    但**逐字段构造**能把「少了一个字段」变成一个看得见的默认值,
 *    而不是让 undefined 一路漏进 formatter。
 *
 * @param mapper  order-status 的行 → 本页自己的 PayInfo(见 `resumeShape.ts`)
 * @param enabled 页面准备好了没(比如已登录 / 已选客户)
 * @returns 恢复出来的订单;没有 `resume_order` 或恢复失败时为 null
 */
export function useResumeOrder<T>(mapper: (row: OrderStatusRow) => T, enabled = true): {
    resumed: ResumedOrder<T> | null;
    /** 恢复失败的原因(给页面显示;null = 没失败) */
    error: string | null;
    clear: () => void;
} {
    const [resumed, setResumed] = useState<ResumedOrder<T> | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!enabled) return;
        if (typeof window === 'undefined') return;
        const id = readResumeOrderId(window.location.search);
        if (!id) return;
        let cancelled = false;

        void (async () => {
            try {
                const r = await authFetch(`/api/wallet/order-status/${encodeURIComponent(id)}`);
                if (cancelled) return;
                if (!r.ok) {
                    // 🔴 404 = 这单不是你的 / 不存在。不猜、不重试、如实说。
                    setError(r.status === 404
                        ? '这条支付链接对应的订单找不到了 · 请重新下单'
                        : '暂时读不到这一单的支付状态 · 请稍后再试');
                    return;
                }
                const body = await r.json().catch(() => null);
                const d = (body && body.data) || null;
                if (!d) { setError('暂时读不到这一单的支付状态 · 请稍后再试'); return; }
                if (d.status === 'paid') {
                    // 已付成:不渲染任何支付出口,只告诉她好了
                    setError('这一单已经付成了 · 算力已入账');
                    return;
                }
                // 网络边界上只能断言一次「这是 order-status 的行」;
                // 之后由 mapper **逐字段构造**本页要的形状,不再整包 cast。
                const row: OrderStatusRow = { ...(d as OrderStatusRow), order_id: (d as OrderStatusRow).order_id || id };
                // 一个出口都没有才算降级(见抬头:native 单只有 code_url 也是能付的)
                setResumed({ info: mapper(row), degraded: hasNoExit(row) });
            } catch {
                if (!cancelled) setError('网络异常 · 这一单的支付状态暂时读不到');
            }
        })();
        return () => { cancelled = true; };
    }, [enabled, mapper]);

    return {
        resumed,
        error,
        clear: () => { setResumed(null); setError(null); },
    };
}
