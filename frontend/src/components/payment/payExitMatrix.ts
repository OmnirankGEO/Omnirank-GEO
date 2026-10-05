/**
 * payExitMatrix — 「拿到支付信息之后,给用户哪些出口」的**唯一**一份行为矩阵。
 * [WO #169 §2.1.1 · 2026-09-10]
 *
 * 🔴 **为什么必须只写一遍**:在这之前同一件事在三个支付面各写了一版,
 *    #169 的矩阵实跑结果是 BuyCredit 48 格里 14 格零可点(10 格连解释都没有),
 *    InventoryCenter 同样 48 格只有 6 格、且全部落到一句红字。
 *    同一个后端响应两个页面结论相反 —— 那不是两个 bug,是**一份口径写了两遍**。
 *
 * 🔴 **本单真因不在这张矩阵里**(Deploy 取证):用户 125 用 iPhone 非微信浏览器进货,
 *    虎皮椒下单成功、三埋点齐全、跳出去 96 秒后回来仍 pending。她那一格**是有 CTA 的**。
 *
 * 🔴 **这里曾经写着「这条路历史成功数 = 0」,那句话已被工单 §5.1 撤回,别再引用。**
 *    撤回理由:`actual_payment_channel` 全表 109/120 为 NULL,对「走没走过 xunhupay」
 *    **零区分力**,拿它当证据是错的。真实时间线是同一个用户 125 在 08-07
 *    失败 3 分钟后**重下同一条路、52 秒付成**。
 *    ⇒ 形态是**间歇 / 时序**,不是结构性死路;埋点覆盖内手机弹窗样本量 = 1。
 *    ⇒ 所以矩阵**不改路由、不废 H5**:直达 H5 仍是主路,「在微信里打开」作第二条路,
 *      而「还没付成?」区块里**「再试一次」是主按钮**(§5.1)——
 *      因为已知有效的动作就是「再来一次」。
 *
 * 本模块**零 import**:判据用 data: URL 加载它真调,不靠读 JSX 猜渲染条件。
 */

/** 一次支付所处的环境 + 后端给了什么。全部由调用方传入,本模块不读 navigator。 */
export interface PayEnv {
    /** 后端实际路由到的通道:'wechat_jsapi' | 'xunhupay' | 其它 */
    channel: string | null;
    /** 手机微信内置浏览器(桌面微信不算 —— 它扫码就行) */
    isMobileWechat: boolean;
    /** 手机浏览器(含手机微信) */
    isMobile: boolean;
    /** 后端说这一单要先换 openid */
    needsOpenid: boolean;
    /** 有没有 payment_url_mobile */
    hasMobileUrl: boolean;
    /** 有没有二维码可渲(code_url 或 payment_url_qrcode) */
    hasQr: boolean;
}

export type PayExitKind =
    | 'jsapi'              // 调起微信官方支付层
    | 'wechat_inline_h5'   // 微信内直接跳虎皮椒 H5(配合 BuyCredit 的 1 秒自动跳)
    | 'wechat_open_copy'   // 手机外部浏览器的**第二条路**:复制链接去微信里打开(§5.1 后不再是主路)
    | 'mobile_h5'          // 通用 H5 跳转(桌面;手机外部浏览器的**主路** —— 它成功过)
    | 'qr'                 // 扫码(桌面场景)
    | 'reason';            // 没有任何出口时,必须给的一句原因

export interface PayExit {
    kind: PayExitKind;
    /** true = 主按钮;false = 次要出口 / 说明 */
    primary: boolean;
    /** kind==='reason' 时必填:告诉用户为什么没有出口、下一步找谁 */
    reason?: string;
}

/**
 * 给出这一格该渲染的出口列表。
 *
 * 🔴 **本函数的契约:返回值长度恒 >= 1。**
 *    没有任何可点的出口时必须返回一条 `reason` —— 照 InventoryCenter 原来那句
 *    「未拿到支付链接 · 请联系客服处理订单 X」的口径。
 *    「弹窗渲染了但屏幕上一个字都不解释」是本单矩阵抓到的最大一类,不许再出现。
 */
export function payExits(env: PayEnv): PayExit[] {
    const out: PayExit[] = [];
    const isJsapi = env.channel === 'wechat_jsapi';

    if (env.isMobileWechat) {
        // ① 手机微信内 —— 唯一能可靠调起微信官方付款层的形态
        if (isJsapi || env.needsOpenid) out.push({ kind: 'jsapi', primary: true });
        // 微信内 + xunhupay:既有口径(BuyCredit 那条 1 秒自动跳 08-07 锁着,逐字保留)
        if (env.hasMobileUrl && env.channel === 'xunhupay') {
            out.push({ kind: 'wechat_inline_h5', primary: out.length === 0 });
        } else if (env.hasMobileUrl) {
            out.push({ kind: 'mobile_h5', primary: out.length === 0 });
        }
        // 微信内扫码没有意义(手机扫不了自己),但没有别的出口时它总比空白强
        if (out.length === 0 && env.hasQr) out.push({ kind: 'qr', primary: true });
    } else if (env.isMobile) {
        // ② 手机外部浏览器 —— 本单事故那一格(iPhone Safari)。
        //
        // 🔴 **这一格的口径三天里换过三次,每次都是证据换了。最终以工单 §7 为准:**
        //    Deploy 取到 UA:用户 125 在 08-07 **付成**那次与 09-10 **失败**那次
        //    UA **逐字符相同**(iPhone Safari),客户自述 Safari,站点无 PWA。
        //    ⇒ 之前两版的解释都不成立:既不是「这条路从没成功过」
        //      (`actual_payment_channel` 109/120 NULL,零区分力,§5.1 撤回),
        //      也不是「App 内置浏览器拦 weixin://」(§6 撤回,UA 不支持)。
        //    ⇒ 同一环境同一条路时好时坏 ⇒ **同时给两条路,别替她赌哪条能成**:
        //      主 = 再试一次(重开同一 payment_url_mobile);次 = 复制链接去微信打开。
        if (env.hasMobileUrl) out.push({ kind: 'mobile_h5', primary: true });
        if (env.hasMobileUrl || env.hasQr) {
            out.push({ kind: 'wechat_open_copy', primary: !env.hasMobileUrl });
        }
        // 手机上不渲二维码当出口:自己扫不了自己(§1)。
    } else {
        // ③ 桌面(含桌面微信)—— 扫码是这里的正路
        if (env.hasQr) out.push({ kind: 'qr', primary: true });
        if (env.hasMobileUrl) out.push({ kind: 'mobile_h5', primary: out.length === 0 });
        if (isJsapi && !env.hasQr && !env.hasMobileUrl) {
            out.push({
                kind: 'reason',
                primary: false,
                reason: '这一单需要在手机微信里完成支付 · 请用手机微信打开本页',
            });
        }
    }

    if (out.length === 0) {
        out.push({
            kind: 'reason',
            primary: false,
            reason: '未拿到支付链接 · 请联系客服处理本订单',
        });
    }
    return out;
}

/**
 * 调起微信官方支付层的**唯一**闸口(#169 §4.3)。
 *
 * 🔴 在这之前两个页面两种口径:BuyCredit 判 `channel==='wechat_jsapi' && isWeChat`,
 *    InventoryCenter 判 `needs_openid` —— 于是一张 jsapi 但 needs_openid=false 的单,
 *    一个页面给按钮、另一个不给。同一个谓词写两处,必有一处没人验。
 */
export function canStartJsapi(env: Pick<PayEnv, 'channel' | 'isMobileWechat' | 'needsOpenid'>): boolean {
    if (!env.isMobileWechat) return false;
    return env.channel === 'wechat_jsapi' || env.needsOpenid === true;
}

/**
 * 「还没付成?」区块该不该出(§2.1.3)。
 * 用户从收银台回来、单仍 pending、且距点击已超过 `thresholdSec` 秒。
 *
 * 🔴 阈值判的是**离开时长**不是「回没回来」:她可能压根没跳走(弹窗被拦),
 *    那种情况 secondsAway 很小,不该拿「还没付成」去吓她。
 */
export function shouldOfferRetry(
    status: string,
    secondsAway: number | null,
    thresholdSec = 20,
): boolean {
    if (status !== 'pending') return false;
    if (secondsAway === null || !Number.isFinite(secondsAway)) return false;
    return secondsAway > thresholdSec;
}

/**
 * 拼「在微信里打开」要复制的那条链接。
 * `?resume_order=<id>` 让微信内打开时直接恢复这一单的支付弹窗,不重新下单。
 *
 * 🔴 **不带走原有 query**:原页面可能带着 `code`/`state`(微信 OAuth 回跳残留)
 *    或别人的 `resume_order`,原样复制过去会恢复错的单。只保留 path。
 */
export function buildResumeUrl(origin: string, pathname: string, orderId: string): string {
    const id = String(orderId || '').trim();
    if (!id) return origin + pathname;
    return origin + pathname + '?resume_order=' + encodeURIComponent(id);
}

/** 从 URL query 里取 resume_order(取不到回 null)。 */
export function readResumeOrderId(search: string): string | null {
    const s = String(search || '');
    const m = s.match(/[?&]resume_order=([^&#]+)/);
    if (!m) return null;
    try {
        const v = decodeURIComponent(m[1]).trim();
        return v || null;
    } catch {
        return null;
    }
}
