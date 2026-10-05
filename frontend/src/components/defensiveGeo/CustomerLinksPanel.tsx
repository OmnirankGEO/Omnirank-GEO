/**
 * U-9 · 「发给客户」统一面板。
 *
 * §0.5.5 U-9 逐字:四类 token 链接(报告/报价/监测/portal)从统一面板生成,
 * 自动带人话前缀(【诊断报告】【报价单·请确认】…);过期/撤销时
 * 一键「重新签发同对象新链接」。
 *
 * 每屏三问:
 * - **发生了什么?** 每一类都写着"可以发了 / 已过期 / 已收回 / 还没到时候",
 *   不是四个灰色的图标让她猜。
 * - **点哪?** 可发的给「复制链接和话术」;过期/收回的给「重新签发一个新链接」;
 *   还没到时候的给一句人话说明为什么(不给死按钮)。
 * - **敢等吗?** 重签发那一句明写「不额外扣算力」—— 她不会因为怕扣钱
 *   而把一个过期链接发给客户。
 *
 * 🔴 前缀、状态词、说明**全部服务端下发**。前端这里没有一个自造的中文名 ——
 *    U-1「同一概念全站唯一叫法」在这一屏的具体形态。
 */

import { useCallback, useEffect, useState } from 'react';
import { Copy, Link2, Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { formatApiErrorForDisplay } from '@/lib/api';
// 🔴 复制**必须**走这个现役 helper,不自己写一份。
//    自写的那份被 test-ios-touch-ux.mjs 锁1 当场判红:`document.execCommand`
//    是 iOS 上的手势链断裂点,全仓只允许 lib/copyUtils.ts 这**一处**兜底
//    (白名单里写着理由)。第二份 execCommand = 第二条没人验的降级路径。
import { copyToClipboard } from '@/lib/copyUtils';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import {
    fetchCustomerLinks, reissueCustomerLink,
    type CustomerLink, type CustomerLinksResponse,
} from '@/lib/defensiveGeoAssistApi';

function absolute(url: string): string {
    return typeof window !== 'undefined' ? `${window.location.origin}${url}` : url;
}

function LinkRow({
    link, brandId, onReissued,
}: { link: CustomerLink; brandId: number; onReissued: (l: CustomerLink) => void }) {
    const [busy, setBusy] = useState(false);

    const copy = useCallback(async () => {
        if (!link.wechatScript) return;
        // 复制的是**整段话术**(前缀 + 品牌名 + 绝对链接),不是光秃秃一个 URL。
        const script = link.url
            ? link.wechatScript.replace(link.url, absolute(link.url))
            : link.wechatScript;
        // 🔴 只调一次。第一版把复制写进了三元的两边 —— 会跑两遍,
        //    而且两次结果可能不同(第二次可能因为焦点丢失失败),
        //    于是"复制成功了却提示失败"。
        const ok = await copyToClipboard(script);
        if (ok) toast.success('已复制，直接粘到微信就行');
        else toast.error('这次没能复制，请长按手动复制');
    }, [link]);

    const reissue = useCallback(async () => {
        setBusy(true);
        try {
            // 🔴 [工单 V3-A · Codex 三审 P1-8] 把**这一行显示的那个对象**原样带回去。
            //    服务端不再自己"取最新":两次取数之间新建一份报价的话,
            //    她看的是 A、被换掉的会是 B(A 还是坏的,客户手上好好的 B 被作废)。
            //    对象漂移时服务端回 409,提示她刷新后再操作。
            const res = await reissueCustomerLink(brandId, link.kind, link.objectRef);
            onReissued(res.link);
            toast.success('已经换成新链接了');
        } catch (e) {
            toast.error(formatApiErrorForDisplay(
                e, '这次没能重新签发；没有扣除任何算力，稍后再试一次。', 'agent'));
        } finally {
            setBusy(false);
        }
    }, [brandId, link.kind, link.objectRef, onReissued]);

    return (
        <li
            data-testid="customer-link-row"
            data-link-kind={link.kind}
            data-link-status={link.status}
            className="flex flex-col gap-2 rounded-lg border border-border p-3 lg:flex-row lg:items-center lg:gap-4"
        >
            <div className="min-w-0 flex-1">
                <p className="flex items-center gap-1.5 text-[14px] font-medium">
                    <Link2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                    {/* 前缀由服务端下发 —— 前端不拼第二份 */}
                    <span data-testid="customer-link-prefix">{link.prefix}</span>
                </p>
                <p className="text-[12px] text-muted-foreground">
                    <span data-testid="customer-link-status">{link.statusLabel}</span>
                    {link.blockedReason ? ` · ${link.blockedReason}` : ''}
                </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
                {link.url && (
                    <Button type="button" size="sm" variant="outline"
                            data-testid="customer-link-copy" onClick={() => void copy()}>
                        <Copy className="mr-1 h-3.5 w-3.5" aria-hidden />
                        {DEFGEO_COPY.copyCustomerLink}
                    </Button>
                )}
                {link.reissuable && (
                    <Button type="button" size="sm" disabled={busy}
                            data-testid="customer-link-reissue" onClick={() => void reissue()}>
                        {busy
                            ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" aria-hidden />
                            : <RefreshCw className="mr-1 h-3.5 w-3.5" aria-hidden />}
                        {DEFGEO_COPY.reissueCustomerLink}
                    </Button>
                )}
            </div>
        </li>
    );
}

export function CustomerLinksPanel({ brandId }: { brandId: number }) {
    const [data, setData] = useState<CustomerLinksResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let alive = true;
        const ctl = new AbortController();
        (async () => {
            setLoading(true);
            try {
                const res = await fetchCustomerLinks(brandId, ctl.signal);
                if (alive) { setData(res); setError(null); }
            } catch (e) {
                if (alive) {
                    setError(formatApiErrorForDisplay(
                        e, '这次没能把客户链接取出来，稍后再试一次。', 'agent'));
                }
            } finally {
                if (alive) setLoading(false);
            }
        })();
        return () => { alive = false; ctl.abort(); };
    }, [brandId]);

    const replace = useCallback((fresh: CustomerLink) => {
        setData((prev) => (prev
            ? { ...prev, links: prev.links.map((l) => (l.kind === fresh.kind ? fresh : l)) }
            : prev));
    }, []);

    return (
        <section data-testid="customer-links-panel" className="space-y-3" aria-live="polite">
            <header className="space-y-0.5">
                <h3 className="text-[15px] font-semibold">{DEFGEO_COPY.openCustomerLinks}</h3>
                {data && <p className="text-[12px] text-muted-foreground">{data.hint}</p>}
            </header>
            {loading && (
                <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />正在取链接
                </p>
            )}
            {error && !loading && (
                <p role="alert" className="text-[13px] text-amber-700">{error}</p>
            )}
            {data && !loading && !error && (
                <ul className="space-y-2">
                    {data.links.map((l) => (
                        <LinkRow key={l.kind} link={l} brandId={brandId} onReissued={replace} />
                    ))}
                </ul>
            )}
        </section>
    );
}

export default CustomerLinksPanel;
