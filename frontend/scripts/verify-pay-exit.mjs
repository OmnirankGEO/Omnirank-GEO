#!/usr/bin/env node
/**
 * 判据 · #169 支付出口。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 本单真因不在渲染矩阵里(Deploy 取证 39c08ad9…):用户 125 用 iPhone
 *    **非微信浏览器**进货,虎皮椒下单成功、三埋点齐全、跳出去 96 秒后回来仍 pending。
 *    她那一格**有 CTA**;卡点在收银台之后,而「手机外部浏览器 → 虎皮椒 H5」
 *    这条路**历史成功数 = 0**。同一个人同一条路第二次(08-07 案例 B 就是她)。
 *    ⇒ 所以 B 段钉的是「手机外部浏览器那一格必须给『进微信』这条主路」,
 *      而不是再去钉一遍「按钮在不在」——按钮一直都在,它就是不管用。
 *
 * 🔴 矩阵**真调**(`payExitMatrix.ts` 是零 import 纯函数,用 data: URL 加载),
 *    不读 JSX 猜渲染条件。#165 那次的教训:读出来的条件和跑出来的条件会漂。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/^\s*\/\/.*$/gm, blank);
const require_ = createRequire(import.meta.url);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

async function importTs(relPath) {
    try {
        const ts = require_('typescript');
        const src = rd(relPath);
        if (/from\s+['"]\.{1,2}\//.test(src)) return { ok: false, err: `${relPath} 有相对 import` };
        const out = ts.transpileModule(src, {
            compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
            fileName: relPath,
        }).outputText;
        return { ok: true, mod: await import('data:text/javascript;base64,' + Buffer.from(out, 'utf8').toString('base64')) };
    } catch (e) { return { ok: false, err: String((e && e.message) || e) }; }
}

// ══ M 元判据:先证 .ts 能加载,别让下面每条业务锁各自 SKIP ═══════════════
const MOD_PATH = 'src/components/payment/payExitMatrix.ts';
const probe = await importTs(MOD_PATH);
if (!probe.ok) {
    console.log('  FAIL M0 🔴 加载不了矩阵模块,下面的行为锁一条都跑不了:' + probe.err);
    console.log('       修法:确认 devDependency `typescript` 已装(纯 JS 转译);');
    console.log('       **不要**改回直接 import(".ts") —— 构建镜像是 node:20-alpine。');
    process.exitCode = 1;
    process.exit(1);
}
const M = probe.mod;
console.log(`  OK  M1 元判据:矩阵模块可加载(typescript ${require_('typescript').version})`);

// ══ A 矩阵契约:每格至少一个出口或一句原因(#169 §4.1) ═══════════════════
const CH = ['wechat_jsapi', 'xunhupay', 'wechat_native', 'other'];
const B = [true, false];
// 🔴 环境形态**显式枚举**,不用布尔积 —— 布尔积会造出「微信内但不是手机」这种
//    根本不存在的格,拿它凑分母等于自欺。五种是真实存在的:
const ENVS = [
    { name: '桌面',        isMobileWechat: false, isMobile: false },
    { name: '手机微信',    isMobileWechat: true,  isMobile: true,  isInApp: false },
    { name: '手机浏览器',  isMobileWechat: false, isMobile: true,  isInApp: false },
];
const cells = [];
for (const channel of CH) for (const e of ENVS) for (const oid of B) for (const mu of B) for (const qr of B) {
    cells.push({ channel, isMobileWechat: e.isMobileWechat, isMobile: e.isMobile, needsOpenid: oid, hasMobileUrl: mu, hasQr: qr, _env: e.name });
}
console.log(`A 矩阵契约(${cells.length} 格 · ${ENVS.length} 种真实环境形态显式枚举)`);
{
    const empty = cells.filter((c) => M.payExits(c).length === 0);
    // 🔴 分母写成**推导式**而不是拍一个数:4 通道 × 3 种(微信内/手机外/桌面)
    //    × needsOpenid 2 × hasMobileUrl 2 × hasQr 2 = 96。
    //    加一个通道枚举值它会当场红 —— 那正是要人回来重看矩阵的时刻。
    const expectCells = CH.length * ENVS.length * 2 * 2 * 2;
    ok(cells.length === expectCells,
        'A0 分母 = 推导格数(对不上 = 枚举塌了或状态空间变了,当场喊)',
        `${cells.length} / 应 ${expectCells}`);
    ok(empty.length === 0,
        'A1 🔴 每格恒有 >=1 个出口或一句原因 —— 「弹窗渲染了但屏幕上零解释」不许再出现',
        empty.length ? JSON.stringify(empty[0]) : '0 格为空');
    const noClickable = cells.filter((c) => M.payExits(c).every((e) => e.kind === 'reason'));
    const noReason = noClickable.filter((c) => !M.payExits(c).some((e) => e.reason));
    ok(noReason.length === 0,
        'A2 🔴 零可点的格必须带**一句人话的原因**(照 InventoryCenter 原红字口径)',
        noReason.length ? JSON.stringify(noReason[0]) : `零可点 ${noClickable.length} 格,全部有原因`);
}

// ══ B 🔴 本单真因那一格:手机外部浏览器必须有「进微信」主路 ═══════════════
console.log('B 手机外部浏览器(本单事故那一格)');
{
    const incident = {
        channel: 'xunhupay', isMobileWechat: false, isMobile: true,
        needsOpenid: false, hasMobileUrl: true, hasQr: false,
    };
    const ex = M.payExits(incident);
    const copy = ex.find((e) => e.kind === 'wechat_open_copy');
    const h5 = ex.find((e) => e.kind === 'mobile_h5');
    // 🔴 **本段口径被工单 §5.1 订正过一次,两条都钉住,免得又被翻回去。**
    //    初版依据「这条路历史成功 0」把 H5 降为次要 —— 那条证据已撤回
    //    (actual_payment_channel 109/120 NULL,零区分力;用户 125 重下 52 秒付成)。
    //    现在钉的是**不随证据变的那部分**:两条路都必须在。
    ok(!!copy && !!h5,
        'B1 🔴 事故格(iPhone 非微信 · xunhupay · 有 H5)**两条路都在** —— '
        + '只给一条等于赌它这次能成');
    ok(!!h5 && h5.primary === true,
        'B2 🔴 §5.1 订正:直达 H5 是**主路**(它成功过,形态是间歇不是死路)');
    ok(!!copy && copy.primary === false,
        'B3 「在微信里打开」是第二条路(保留,但不抢主按钮)');
    // 分母是**所有**手机外部浏览器格,不只工单点名那一格
    const mobExt = cells.filter((c) => c.isMobile && !c.isMobileWechat && (c.hasMobileUrl || c.hasQr));
    const missing = mobExt.filter((c) => !M.payExits(c).some((e) => e.kind === 'wechat_open_copy'));
    ok(missing.length === 0,
        'B4 🔴 所有手机外部浏览器格都有「在微信里打开」这条备路',
        missing.length ? JSON.stringify(missing[0]) : `${mobExt.length} 格全覆盖`);
    // 拿不到 H5 时「进微信」必须顶上来当主按钮,否则那一格没有主路
    const noH5 = cells.filter((c) => c.isMobile && !c.isMobileWechat && !c.hasMobileUrl && c.hasQr);
    const noPrimary = noH5.filter((c) => !M.payExits(c).some((e) => e.primary));
    ok(noPrimary.length === 0,
        'B4b 没有 H5 的手机外部格,「进微信」顶上来当主按钮(不留无主路的格)',
        noPrimary.length ? JSON.stringify(noPrimary[0]) : `${noH5.length} 格`);
    // 反臂:桌面不许被塞这条(桌面扫码是正路,弹这句是噪音)
    const desktopPolluted = cells.filter((c) => !c.isMobile && M.payExits(c).some((e) => e.kind === 'wechat_open_copy'));
    ok(desktopPolluted.length === 0,
        'B5 反臂:桌面格不许出现「在微信里打开」(桌面扫码本来就能付,弹它是噪音)',
        desktopPolluted.length ? JSON.stringify(desktopPolluted[0]) : '0 格');
    // 反臂:手机上不许把二维码当出口(§1:手机扫不了自己)
    const mobQr = cells.filter((c) => c.isMobile && !c.isMobileWechat && M.payExits(c).some((e) => e.kind === 'qr'));
    ok(mobQr.length === 0,
        'B6 反臂:手机外部浏览器不把二维码当出口(自己扫不了自己)',
        mobQr.length ? JSON.stringify(mobQr[0]) : '0 格');
}

// ══ C JSAPI 闸口统一(#169 §4.3) ══════════════════════════════════════
console.log('C JSAPI 闸口只有一处');
{
    ok(M.canStartJsapi({ channel: 'wechat_jsapi', isMobileWechat: true, needsOpenid: false }) === true,
        'C1 jsapi 单在手机微信内可调起(BuyCredit 原口径)');
    ok(M.canStartJsapi({ channel: 'xunhupay', isMobileWechat: true, needsOpenid: true }) === true,
        'C2 needs_openid 的单也可调起(InventoryCenter 原口径)—— 两种口径已并成一条');
    ok(M.canStartJsapi({ channel: 'wechat_jsapi', isMobileWechat: false, needsOpenid: true }) === false,
        'C3 🔴 反臂:不在手机微信内一律 false(在别处调 JSAPI 只会静默失败)');
    const PAGES = ['src/pages/Customer/BuyCredit.tsx', 'src/pages/Agent/InventoryCenter.tsx'];
    for (const p of PAGES) {
        const src = decomment(rd(p));
        ok(!/needs_openid && isInWechatBrowser\(\)\s*&&|actual_channel === 'wechat_jsapi' && isWeChat/.test(src),
            `C4 ${p.split('/').pop()} 不再自带第二份 JSAPI 闸口`);
    }
}

// ══ D 回来没付成 + 恢复链接(#169 §2.1.2/2.1.3) ═════════════════════════
console.log('D 回来没付成 / resume_order');
{
    ok(M.shouldOfferRetry('pending', 96) === true, 'D1 事故那一单(离开 96 秒仍 pending)会出「还没付成?」');
    ok(M.shouldOfferRetry('pending', 3) === false,
        'D2 🔴 反臂:刚跳走 3 秒不弹 —— 弹窗被拦时 secondsAway 很小,不能拿它吓人');
    ok(M.shouldOfferRetry('paid', 999) === false, 'D3 反臂:已付成不弹');
    ok(M.shouldOfferRetry('pending', null) === false, 'D4 反臂:没离开过不弹');
    const u = M.buildResumeUrl('https://omnirank.top', '/agent/inventory', 'ORD-1');
    ok(u === 'https://omnirank.top/agent/inventory?resume_order=ORD-1', 'D5 恢复链接形态', u);
    const dirty = M.buildResumeUrl('https://x.cn', '/p', 'A B&c');
    ok(!dirty.includes(' ') && dirty.includes('%20'), 'D6 订单号进 query 前转义(带空格/&的单不会截断链接)', dirty);
    ok(M.readResumeOrderId('?resume_order=ORD-1&x=2') === 'ORD-1', 'D7 读得回来');
    ok(M.readResumeOrderId('?code=abc&state=1') === null,
        'D8 🔴 反臂:微信 OAuth 回跳残留的 code/state 不会被误读成订单号');
}

// ══ E §5.1 裁定:「还没付成?」区块里「再试一次」是主按钮 ═══════════════
console.log('E 还没付成区块(§5.1 裁定)');
{
    const C = decomment(rd('src/components/payment/PayExit.tsx'));
    ok(/data-testid="pay-exit-retry-again"/.test(C),
        'E1 🔴 区块里有「再试一次」(重跳同一链接)—— §5.1:已知有效的动作就是再来一次');
    ok(/data-testid="pay-exit-retry"/.test(C), 'E2 「还没付成?」区块本身在');
    ok(/trackPayReturnUnpaid/.test(C), 'E3 回来未付成有埋点(前三点断在 left,这条补后半段)');
    ok(/wechat_open_copy/.test(C) && /buildResumeUrl/.test(C),
        'E4 「复制链接到微信打开」这条备路仍在(§7:两条路同时给)');
    ok(/data-testid="pay-exit-retry-copy"/.test(C),
        'E5 🔴 §7:「还没付成?」区块里**两条路都在**(不是只给一条)');
    ok(/收银台里点「微信支付」没反应/.test(C),
        'E6 🔴 §7 指定的第一句在(问句 —— 我方在收银台之后零可见度,不许写成断言)');
    ok(/link_kind: 'retry_mobile_h5'/.test(C), 'E7 §7 指定的 link_kind 逐字一致');
    // 🔴 §6 的内置浏览器假设已被 §7 撤回:代码里不许留残档
    ok(!/isInApp|inAppBrowser/.test(C), 'E8 🔴 §6 撤回后无残档(UA 分支已整条移除)');
}

// ══ Q §4.2 二维码不许再依赖第三方 ═══════════════════════════════════════
console.log('Q 二维码本地渲染');
{
    const SURFACES = [
        'src/pages/Customer/BuyCredit.tsx',
        'src/pages/Agent/InventoryCenter.tsx',
        'src/components/payment/PayExit.tsx',
    ];
    // [开源 E3 · 前端 · 2026-10-01 · WO_322] 社媒充值弹窗随 components/social 整删,
    //   支付面从四个变三个(两个在役支付面 + 抽出来的组件);Q3 的社媒那一半随之去掉。
    // 🔴 分母是**全部**支付面(含抽出来的组件),不是工单点名那几个:
    //    第三方依赖会跟着复制粘贴走,只钉被点名的那几处就是等它从别处回来。
    const polluted = SURFACES.filter((f) => /api\.qrserver\.com/.test(decomment(rd(f))));
    ok(polluted.length === 0,
        'Q1 🔴 支付面不再渲染第三方二维码服务(被墙/超时 = 空白框,与后端给没给无关)',
        polluted.join(', ') || `${SURFACES.length} 个面全清`);
    const localRender = SURFACES.filter((f) => /QRCode\.toDataURL/.test(rd(f)));
    // [开源 E3 · 前端] 原门槛 ≥ 2 = PayExit + 社媒充值弹窗;后者随 components/social 删,在役本地渲染只在 PayExit(另两个面复用它)
    ok(localRender.length >= 1,
        'Q2 正样本臂:确实改成了本地渲染(而不是把二维码整块删了)',
        `${localRender.length} 处本地渲染`);
    const fallback = /pay-exit-qr-fallback/.test(rd('src/components/payment/PayExit.tsx'));
    ok(fallback, 'Q3 🔴 本地渲染失败也要说话(空白框是同一类病,换个成因而已)');
}

// ══ R §2.1.2 resume_order 恢复(复制出去的链接必须真能用) ═══════════════
console.log('R resume_order 恢复');
{
    const H = rd('src/components/payment/useResumeOrder.ts');
    const PAGES = ['src/pages/Customer/BuyCredit.tsx', 'src/pages/Agent/InventoryCenter.tsx'];
    // 🔴 分母 = 会发出恢复链接的每一个面。发链接的面若不接恢复,那条链接就是死链 ——
    //    「按钮在、动作不在」正是本窗这几天反复抓的同一类病。
    // 🔴 **钉的是那条边,不是那个名字。** 第一版写成「源码里出现 useResumeOrder」——
    //    注毒时删掉 import,函数名仍在函数体里,锁照样绿(存在锁没有牙)。
    //    而真实的回归形状是:hook 照调、返回值没人用 —— **那是合法代码,tsc 不会响**。
    //    所以要求两件事同时成立:调了它,**并且**把结果喂给了 setPayInfo。
    const notWired = PAGES.filter((f) => {
        const src = decomment(rd(f));
        // [#169 返修换锚] 去掉 `as T` 后不再有显式类型参数,改判「调了它并传了 mapper」
        const called = /useResumeOrder\(\s*to\w+PayInfo\s*\)/.test(src);
        const consumed = /setPayInfo\(\s*resumedOrder\.info\s*\)/.test(src);
        return !(called && consumed);
    });
    ok(notWired.length === 0,
        'R1 🔴 发出 resume 链接的面都**真接上了**(调了 hook 且把结果喂给弹窗)——'
        + '不接 = 复制出去是死链',
        notWired.join(', ') || `${PAGES.length} 面全接`);
    ok(/order-status/.test(H), 'R2a 恢复走 order-status');
    ok(!/method:\s*'POST'/.test(H) && !/\/api\/wallet\/recharge/.test(H),
        'R2b 🔴 恢复只**重取**,绝不新下单(否则复制一次链接就多一张单)');
    // 🔴 **R3 重锚(2026-09-10)**:原文写的是「C 的字段上线后撤掉这条」。
    //    去核 C 的 058 才发现前提是错的 —— 那支迁移自己写着「零 DML:只加列,
    //    **不回填历史**」,列注释是 `NULL = 本迁移之前建的单`。
    //    ⇒ 降级路是**永久**分支,不是脚手架:058 之前建的 pending 单永远没有 URL,
    //      包括本单事故里用户 125 那两张。撤掉它 = 把受影响的那个人推去
    //      「请联系客服」而不是「重新获取支付链接」。**换锚不换意图:它必须在。**
    ok(/degraded/.test(H) && PAGES.every((f) => /degraded/.test(rd(f))),
        'R3 🔴 降级路在,并一路传到 PayExit(058 只加列不回填 ⇒ 老单永远无出口)');
    ok(/不回填历史|本迁移之前建的单/.test(H),
        'R3b 🔴 降级路的**理由**写在代码里 —— 下一个人来撤它之前会先读到为什么不能撤');
    // [#169 返修换锚] 谓词已搬进 resumeShape.hasNoExit —— 从 grep 升成**真调**,
    // 这比原来强:换个等价写法 grep 会红,而真调只在**行为**变了才红。
    const probeR = await importTs('src/components/payment/resumeShape.ts');
    ok(probeR.ok, 'R3c-0 resumeShape 可加载', probeR.ok ? '' : probeR.err);
    if (probeR.ok) {
        const RS = probeR.mod;
        ok(RS.hasNoExit({ order_id: 'x', status: 'pending' }) === true,
            'R3c 🔴 三个出口都没有 ⇒ 降级(058 之前建的老单)');
        ok(RS.hasNoExit({ order_id: 'x', status: 'pending', code_url: 'weixin://x' }) === false,
            'R3d 🔴 反臂:native 单只有 code_url 也是**能付的**,不许说成要重新获取链接');
        ok(RS.hasNoExit({ order_id: 'x', status: 'pending', payment_url_qrcode: 'https://q' }) === false,
            'R3e 反臂:只有 payment_url_qrcode 同理');
    }
    ok(/404/.test(H) && /找不到了/.test(H),
        'R4 单不存在/不是你的 ⇒ 明说,不静默(404 不许当成"暂时读不到")');
    ok(/status === 'paid'/.test(H) && /已经付成了/.test(H),
        'R5 已付成的单不渲染任何支付出口,直接告诉她好了');
    ok(/encodeURIComponent\(id\)/.test(H), 'R6 订单号进 URL 前转义');
}

// ══ S 恢复时的形状映射(生产实测「¥NaN」那一格) ═══════════════════════
console.log('S order-status → 各面 PayInfo 的映射');
{
    const probeS = await importTs('src/components/payment/resumeShape.ts');
    if (!probeS.ok) {
        console.log('  FAIL S0 🔴 加载不了 resumeShape:' + probeS.err);
        bad += 1;
    } else {
        const R = probeS.mod;
        // 后端逐字回的那一行(api/wallet_api.py 的 pending 分支)
        const ROW = {
            order_id: 'AIP600D61E064DF4070', status: 'pending',
            amount_yuan: 1, total_points: 1000, base_points: 900, bonus_points: 100,
            actual_channel: 'xunhupay', payment_url_mobile: 'https://x/y',
            payment_url_qrcode: null, code_url: null, needs_openid: false,
        };
        const inv = R.toInventoryPayInfo(ROW);
        // 🔴 生产实测那一格:进货面按**分**记账,端点给的是**元**。
        //    原来整包 `as T` 过去 ⇒ amount_cents 是 undefined ⇒ formatCents 渲出「¥NaN」。
        ok(inv.amount_cents === 100,
            'S1 🔴 进货面拿到的是**分**(端点给元)—— 这一格生产上渲出过「¥NaN」', String(inv.amount_cents));
        const bc = R.toBuyCreditPayInfo(ROW);
        ok(bc.amount_yuan === 1, 'S2 买算力面按元,原样带过去', String(bc.amount_yuan));

        // 🔴 **逐字段核对**,不是只看金额:少任何一个必填字段都会在页面上变成
        //    NaN / undefined / "null" —— 而它们都长得像"页面渲染正常"。
        const INV_REQUIRED = ['order_id', 'amount_cents', 'base_points', 'bonus_points',
            'total_points', 'actual_channel'];
        const BC_REQUIRED = ['order_id', 'amount_yuan', 'base_points', 'bonus_points',
            'total_points', 'tier_label', 'actual_channel'];
        const missInv = INV_REQUIRED.filter((k) => inv[k] === undefined || (typeof inv[k] === 'number' && !Number.isFinite(inv[k])));
        const missBc = BC_REQUIRED.filter((k) => bc[k] === undefined || (typeof bc[k] === 'number' && !Number.isFinite(bc[k])));
        ok(missInv.length === 0, 'S3 🔴 进货面必填字段无 undefined / 无 NaN', missInv.join(',') || 'ok');
        ok(missBc.length === 0, 'S4 🔴 买算力面必填字段无 undefined / 无 NaN(含端点不回的 tier_label)',
            missBc.join(',') || 'ok');

        // 反臂:端点少给字段时给的是**看得见的默认值**,不是 undefined 漏进 formatter
        const bare = R.toInventoryPayInfo({ order_id: 'x', status: 'pending' });
        ok(Number.isFinite(bare.amount_cents) && bare.amount_cents === 0,
            'S5 反臂:端点少给 amount_yuan ⇒ 0,不是 NaN(NaN 会一路渲到屏幕上)');
        ok(bare.actual_channel === '',
            'S6 反臂:channel 为 null(老单 109/120 如此)⇒ 空串,不是字面 "null"');

        // 🔴 浮点:1.15 * 100 = 114.99999999999999,截断会少收一分钱 ——
        //    比 NaN 更难发现,因为它看起来完全正常。
        ok(R.yuanToCents(1.15) === 115 && R.yuanToCents(0.07) === 7 && R.yuanToCents(19.99) === 1999,
            'S7 🔴 元转分四舍五入(截断会少收一分,而那看起来很正常)',
            `${R.yuanToCents(1.15)}/${R.yuanToCents(0.07)}/${R.yuanToCents(19.99)}`);
        ok(R.yuanToCents(null) === 0 && R.yuanToCents(undefined) === 0 && R.yuanToCents('x') === 0,
            'S8 反臂:非数字不产出 NaN');
    }
    // 🔴 钉住那个让缺陷溜过去的东西本身:hook 里不许再整包 cast
    const H2 = decomment(rd('src/components/payment/useResumeOrder.ts'));
    /*
     * 🔴 [#195 09-13] 这条正则**原来是坏的**:末尾那个 `\b` 在某次改写中被写成
     *    退格字符(0x08),于是 `!/…/` 这半边**恒真恒绿** —— 它本来是要拦整包 `as T` 的,
     *    而那句断言正是「¥NaN」能溜过 tsc 的原因。全仓扫出同族三处,都改成不用 `\b`
     *    并就地加正负样本;另加 `verify-no-control-chars` 把这一类变成红。
     */
    const castsWhole = (t) => /\}\s*as T(?![A-Za-z0-9_])/.test(t);
    ok(castsWhole('return { ...row } as T;') && !castsWhole('return row as TableRow;'),
        'S9a 判据自证:抓得到整包 `} as T`,且不把 `as TableRow` 误判成它');
    ok(!castsWhole(H2) && /mapper\(row\)/.test(H2),
        'S9 🔴 hook 不再 `as T` 整包断言,改由调用方 mapper 逐字段构造 ——'
        + ' 那句断言正是「¥NaN」能溜过 tsc 的原因');
    for (const f of ['src/pages/Agent/InventoryCenter.tsx', 'src/pages/Customer/BuyCredit.tsx']) {
        ok(/useResumeOrder\((toInventoryPayInfo|toBuyCreditPayInfo)\)/.test(decomment(rd(f))),
            `S10 ${f.split('/').pop()} 传了自己的 mapper`);
    }
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`\n未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('\n全部通过');
    process.exitCode = 0;
}
