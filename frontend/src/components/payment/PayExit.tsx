/**
 * PayExit — 三个支付面共用的「怎么出去付款」区块。
 * [WO #169 §2.1 · 2026-09-10]
 *
 * 🔴 **本单的解释换过三次,最终以工单 §7 为准 —— 前两条别再引用:**
 *    · §0 初版「手机+虎皮椒历史成功 0」→ §5.1 **撤回**
 *      (`actual_payment_channel` 全表 109/120 为 NULL,对这个问题零区分力);
 *    · §6「App 内置浏览器拦 weixin:// 拉起」→ §7 **撤回**
 *      (Deploy 取到 UA:08-07 **付成**那次与 09-10 **失败**那次逐字符相同,
 *       都是 iPhone Safari;客户自述 Safari,站点无 PWA)。
 *    · §7 最终:**同一环境、同一条路、时好时坏**。用户已到收银台、
 *      按「微信支付」5 秒零反应;我方在收银台之后零可见度。
 *
 * ⇒ 既然赌不出哪条能成,就**同时给两条**:主 = 再试一次(重开同一 payment_url_mobile),
 *   次 = 复制链接到微信里打开(带 `?resume_order=<id>`,微信内打开直接恢复弹窗)。
 *   支付弹窗与「还没付成?」区块**用同一对按钮、同一个顺序**。
 *
 * 🔴 渲染什么由 `payExitMatrix.payExits()` **单点决定**,本文件只负责画。
 *   在这之前同一份口径在三个面各写一版,#169 矩阵实跑出 14/48 格零可点。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import QRCode from 'qrcode';
import { ExternalLink, Copy, Check, MessageCircle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { copyToClipboard } from '@/lib/copyUtils';
import { isMobileDevice, isMobileWechatUa, payLinkTargetProps, shouldOpenPayLinkInNewTab } from '@/lib/paymentEnv';
import { trackPayLinkClicked, trackPayReturnUnpaid, type PayContext } from '@/lib/paymentTelemetry';
import { payExits, canStartJsapi, shouldOfferRetry, buildResumeUrl } from './payExitMatrix';

export interface PayExitInfo {
    order_id: string;
    actual_channel: string;
    // 🔴 一律 `| null`:后端这几个字段是**可空列**,发过来就是 null 不是 undefined。
    //    只写 `?:` 会让调用方 tsc 红,然后有人用 `as any` 把它压下去 —— 那才是真的坏。
    code_url?: string | null;
    payment_url_qrcode?: string | null;
    payment_url_mobile?: string | null;
    needs_openid?: boolean | null;
}

interface Props {
    info: PayExitInfo;
    status: string;
    payCtx: PayContext;
    /** 点击支付出口时调一次:从这一刻起离页算「跳转成功」 */
    armJumpTracking: () => void;
    /** 调起微信官方支付层(各面自己的实现) */
    onStartJsapi?: (info: PayExitInfo) => void;
    /** 重新下单(降级路用:端点还没回 payment_url_mobile 时) */
    onReorder?: () => void;
    /**
     * 🔴 恢复出来的单**一个支付出口都没有**时置 true(不是「没有 H5」)。
     *    这是**永久**分支:C 的 058 只加列不回填历史,
     *    058 之前建的 pending 单永远没有 URL(它的列注释就写着
     *    `NULL = 本迁移之前建的单`)。这时明说「要重新获取支付链接」并给入口,
     *    **不静默**、也不推去「请联系客服」。判据 R3 钉着它。
     */
    degradedNoMobileUrl?: boolean;
}

// 🔴 剪贴板走仓内既有的 `copyToClipboard` —— 我最初在这里又写了一份,
//    被 `test-ios-touch-ux` 锁1 当场抓住:我那份的 `execCommand` 落在 `await` **之后**,
//    iOS 上手势凭证那时已过期,兜底会静默失效。仓内那份是白名单里唯一有理由的兜底本体。
//    同一个谓词写两处,必有一处没人验 —— 这次是那把锁替我验了。
//    (本处链接是同步就有的,不需要 `copyAsyncText`;那条是"先问后端再复制"用的。)

export function PayExit({
    info, status, payCtx, armJumpTracking, onStartJsapi, onReorder, degradedNoMobileUrl,
}: Props) {
    const isMobileWechat = isMobileWechatUa(typeof navigator === 'undefined' ? '' : navigator.userAgent);
    const isMobile = isMobileDevice();
    const payInNewTab = shouldOpenPayLinkInNewTab();

    const qrSource = info.code_url || info.payment_url_qrcode || '';
    const exits = useMemo(() => payExits({
        channel: info.actual_channel ?? null,
        isMobileWechat,
        isMobile,
        needsOpenid: Boolean(info.needs_openid),
        hasMobileUrl: Boolean(info.payment_url_mobile),
        hasQr: Boolean(qrSource),
    }), [info.actual_channel, info.needs_openid, info.payment_url_mobile, qrSource, isMobileWechat, isMobile]);

    // ── 本地二维码(#169 §4.2)────────────────────────────────────────────
    // 🔴 原来三个面都渲染 `https://api.qrserver.com/...` —— 第三方被墙/超时时
    //    <img> 静默失败,用户看到一个空框,而这与后端给没给 code_url 无关。
    //    仓内本来就有 `qrcode` 依赖(M3 门户二维码组件 等三处已在用),改本地渲染,
    //    并且**渲染失败要说话**:退成 code_url 文本 + 复制按钮。
    const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
    const [qrFailed, setQrFailed] = useState(false);
    useEffect(() => {
        if (!qrSource) { setQrDataUrl(null); setQrFailed(false); return; }
        let alive = true;
        setQrFailed(false);
        QRCode.toDataURL(qrSource, { width: 240, margin: 1 })
            .then((url) => { if (alive) setQrDataUrl(url); })
            .catch(() => { if (alive) { setQrDataUrl(null); setQrFailed(true); } });
        return () => { alive = false; };
    }, [qrSource]);

    // ── 「还没付成?」(#169 §2.1.3)───────────────────────────────────────
    const hiddenAtRef = useRef<number | null>(null);
    const clickedRef = useRef(false);
    const reportedRef = useRef(false);
    const [secondsAway, setSecondsAway] = useState<number | null>(null);
    useEffect(() => {
        if (status !== 'pending') return;
        const onVis = () => {
            if (document.visibilityState === 'hidden') {
                if (clickedRef.current) hiddenAtRef.current = Date.now();
                return;
            }
            const t = hiddenAtRef.current;
            if (t === null) return;
            hiddenAtRef.current = null;
            const away = (Date.now() - t) / 1000;
            setSecondsAway(away);
            // 🔴 每单只报一次 —— 她可能来回切好几次,不能把漏斗刷成噪音
            if (!reportedRef.current && shouldOfferRetry('pending', away)) {
                reportedRef.current = true;
                trackPayReturnUnpaid(payCtx, { seconds_away: Math.round(away) });
            }
        };
        document.addEventListener('visibilitychange', onVis);
        return () => document.removeEventListener('visibilitychange', onVis);
    }, [status, payCtx]);
    const offerRetry = shouldOfferRetry(status, secondsAway);

    // ── 「在微信里打开」──────────────────────────────────────────────────
    const resumeUrl = typeof window === 'undefined'
        ? ''
        : buildResumeUrl(window.location.origin, window.location.pathname, info.order_id);
    const [copyState, setCopyState] = useState<'idle' | 'ok' | 'failed'>('idle');
    const doCopy = useCallback(async () => {
        trackPayLinkClicked(payCtx, { new_tab: false, link_kind: 'wechat_open_copy' });
        const ok = await copyToClipboard(resumeUrl);
        setCopyState(ok ? 'ok' : 'failed');
    }, [payCtx, resumeUrl]);

    if (status !== 'pending') return null;

    const has = (k: string) => exits.some((e) => e.kind === k);
    const reason = exits.find((e) => e.kind === 'reason')?.reason;

    return (
        <div className="space-y-3" data-testid="pay-exit">
            {offerRetry && (
                <div data-testid="pay-exit-retry"
                     className="rounded border border-amber-300 bg-amber-50 p-3 text-left text-sm space-y-2">
                    <p className="font-medium">还没付成?收银台里点「微信支付」没反应?</p>
                    {/* 🔴 [§5.1 订正] 这里**不能**说「手机浏览器里付不了款」——
                        那句话依据的是「这条路历史成功 0」,已被撤回
                        (actual_payment_channel 109/120 NULL,零区分力)。
                        真实情况是同一个用户重下同一条路 52 秒就付成了 ⇒ 形态是间歇。
                        所以主按钮是**再试一次**,不是把她赶去微信。 */}
                    <p className="text-xs text-muted-foreground">
                        你离开了约 {Math.round(secondsAway || 0)} 秒,这一单还没到账。
                        支付页有时会中途断掉 —— 再试一次多数就过了。关闭弹窗订单也会保留。
                    </p>
                    {/* 🔴 [§6③] 两个动作的主次**随环境翻转**:
                        · 内置浏览器 ⇒ 「换个地方打开」是主 —— 在这里重试还是会被同一个 App 拦;
                        · 普通浏览器 ⇒ 「再试一次」是主 —— 同一条路有人重下 52 秒付成(§5.1)。
                        写死任何一边都会在另一半用户身上变成一句没用的话。 */}
                    {info.payment_url_mobile && (
                        <Button asChild
                                className="w-full h-11" data-testid="pay-exit-retry-again">
                            <a href={info.payment_url_mobile} {...payLinkTargetProps()}
                               onClick={() => {
                                   clickedRef.current = true;
                                   reportedRef.current = false;   // 再走一轮,下次回来还要报
                                   armJumpTracking();
                                   trackPayLinkClicked(payCtx, { new_tab: payInNewTab, link_kind: 'retry_mobile_h5' });
                               }}>
                                <RefreshCw className="mr-1 h-4 w-4" />再试一次
                            </a>
                        </Button>
                    )}
                    <Button
                        variant="outline"
                        className="w-full h-11" data-testid="pay-exit-retry-copy"
                        onClick={() => void doCopy()}
                    >
                        {copyState === 'ok' ? <Check className="mr-1 h-4 w-4" /> : <Copy className="mr-1 h-4 w-4" />}
                        {copyState === 'ok' ? '已复制 · 换个地方打开' : '复制链接 · 换浏览器或微信打开'}
                    </Button>
                    {copyState === 'failed' && (
                        <input readOnly value={resumeUrl} onFocus={(e) => e.currentTarget.select()}
                               data-testid="pay-exit-retry-copy-fallback"
                               className="w-full rounded border px-2 py-1 text-[11px]" />
                    )}
                </div>
            )}

            {/* 🔴 手机外部浏览器的**主路**:先进微信,再付 */}
            {has('wechat_open_copy') && (
                <div data-testid="pay-exit-wechat-open"
                     className="rounded border border-emerald-300 bg-emerald-50 p-3 space-y-2 text-left">
                    <p className="text-sm font-medium flex items-center gap-1">
                        <MessageCircle className="h-4 w-4" />
                        收银台里点「微信支付」没反应?
                    </p>
                    {/* 🔴 [§7] 这一句是**问句**不是断言 —— 我方在收银台之后零可见度,
                        说「手机上付不了款」是假话(同一 UA 同一条路 08-07 付成过)。
                        问出来,然后把两条路都摆上,让她自己挑。 */}
                    <p className="text-xs text-muted-foreground">
                        复制下面这条链接,粘贴到微信里任意聊天窗口再点开即可。
                    </p>
                    <Button className="w-full h-11" onClick={() => void doCopy()} data-testid="pay-exit-copy-link">
                        {copyState === 'ok' ? <Check className="mr-1 h-4 w-4" /> : <Copy className="mr-1 h-4 w-4" />}
                        {copyState === 'ok' ? '已复制 · 去微信粘贴打开' : '复制链接'}
                    </Button>
                    {/* 🔴 复制失败必须给可见兜底:手机上剪贴板 API 会因非安全上下文/权限直接拒。
                        只 toast 一句「复制失败」等于什么都没给 —— 把链接摊出来让她自己长按选。 */}
                    {copyState === 'failed' && (
                        <div data-testid="pay-exit-copy-fallback" className="space-y-1">
                            <p className="text-xs text-red-600">复制没成功 · 请长按下面这行地址手动复制</p>
                            <input readOnly value={resumeUrl} onFocus={(e) => e.currentTarget.select()}
                                   className="w-full rounded border px-2 py-1 text-[11px]" />
                        </div>
                    )}
                </div>
            )}

            {has('jsapi') && onStartJsapi && canStartJsapi({
                channel: info.actual_channel ?? null, isMobileWechat, needsOpenid: Boolean(info.needs_openid),
            }) && (
                <Button
                    className="w-full h-12 bg-emerald-500 hover:bg-emerald-600 text-white text-base"
                    data-testid="pay-exit-jsapi"
                    onClick={() => {
                        trackPayLinkClicked(payCtx, { new_tab: false, link_kind: 'jsapi' });
                        onStartJsapi(info);
                    }}
                >调起微信支付</Button>
            )}

            {has('wechat_inline_h5') && info.payment_url_mobile && (
                <Button asChild className="w-full h-12 bg-emerald-500 hover:bg-emerald-600 text-white text-base">
                    <a href={info.payment_url_mobile} data-testid="pay-exit-wechat-h5"
                       onClick={() => {
                           armJumpTracking();
                           trackPayLinkClicked(payCtx, { new_tab: false, link_kind: 'wechat_inline_h5' });
                       }}>点这里完成支付 →</a>
                </Button>
            )}

            {has('mobile_h5') && info.payment_url_mobile && (
                <Button asChild variant="outline" className="w-full h-12 text-base" data-testid="pay-exit-mobile-h5">
                    <a href={info.payment_url_mobile} {...payLinkTargetProps()}
                       onClick={() => {
                           clickedRef.current = true;
                           armJumpTracking();
                           trackPayLinkClicked(payCtx, { new_tab: payInNewTab, link_kind: 'mobile_h5' });
                       }}>
                        <ExternalLink className="w-4 h-4 mr-1" />
                        {isMobile ? '或试试直接跳转支付页' : (payInNewTab ? '新窗口打开支付页' : '前往支付页完成付款')}
                    </a>
                </Button>
            )}

            {has('qr') && (
                qrDataUrl ? (
                    <div>
                        <img src={qrDataUrl} alt="支付二维码" data-testid="pay-exit-qr"
                             className="w-60 h-60 mx-auto border rounded" />
                        <p className="text-xs text-muted-foreground mt-2">微信扫码完成支付 · 系统每 3 秒自动检查</p>
                    </div>
                ) : qrFailed ? (
                    // 二维码画不出来也要给出路 —— 空白框是本单矩阵抓到的同一类病
                    <div data-testid="pay-exit-qr-fallback" className="space-y-1 text-left">
                        <p className="text-xs text-red-600">二维码没能生成 · 请复制下面的支付链接在微信里打开</p>
                        <input readOnly value={qrSource} onFocus={(e) => e.currentTarget.select()}
                               className="w-full rounded border px-2 py-1 text-[11px]" />
                    </div>
                ) : (
                    <p className="text-xs text-muted-foreground" data-testid="pay-exit-qr-loading">正在生成二维码…</p>
                )
            )}

            {/* 降级路:端点还没回 payment_url_mobile(C 的字段未上线)—— 明说,不静默 */}
            {degradedNoMobileUrl && (
                <div data-testid="pay-exit-degraded"
                     className="rounded border border-amber-300 bg-amber-50 p-3 text-left text-sm space-y-2">
                    <p>这一单是早前创建的,系统里没有留下支付链接 —— 重新获取一条就能继续付。</p>
                    {onReorder && (
                        <Button variant="outline" className="w-full" onClick={onReorder}
                                data-testid="pay-exit-reorder">
                            <RefreshCw className="mr-1 h-4 w-4" />重新获取支付链接
                        </Button>
                    )}
                </div>
            )}

            {reason && (
                <p className="text-sm text-red-600" data-testid="pay-exit-reason">
                    {reason} {info.order_id}
                </p>
            )}
        </div>
    );
}

export default PayExit;
