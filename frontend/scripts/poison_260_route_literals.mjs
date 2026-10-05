#!/usr/bin/env node
/**
 * WO_260 反臂 —— 没毒过的锁不能当证据。
 *
 * 读四把尺子,格名带前缀防撞名:
 *   l: = test-route-literals-live.mjs(结构臂,build 链)
 *   d: = verify-dead-interactions.mjs(死交互闸,B8 = E3 删域后壳链接不死,build 链)
 *   p: = test-publish-center-layout.mjs(发布中心布局锁;锁 9 反向本单重锚过,build 链)—— 它的格是「✅/❌ 文案」行
 *   r: = test-route-literals-live-render.mjs(行为臂,真 chromium + 真登录 + 真路由表)
 *
 * 读法(不看 rc):每一发都要「**点名的那几格 OK→FAIL ∧ 其余格一个都不许动 ∧ 源文件逐字回位**」。
 *   · 下毒前先过语法尺子对照臂(原文必须判过),下毒后再判 —— 写成语法错 = 「毒没下成」,不算咬住;
 *   · 一发把整页打崩的毒也会让点名格变红 —— 「其余不动」挡的就是这种假咬。
 *
 * 十二发:
 *   P1 在役页(我的客户)塞 navigate('/m3/queue')           ⇒ l:S1 l:S2(工单点名的反臂)
 *   P2 侧栏加一项 to: '/m3/sales/today'                     ⇒ l:S1 l:S2 d:B8(今天还能重定向,E3 后必 404)
 *   P3 在役页 import '@/components/m3/parts'                ⇒ l:M
 *   P4 PlacementCenter 的 /monitoring 退回 /insights        ⇒ l:S1 l:S2 l:E1c
 *   P5 小榜助手判断里加回 isFullHeightPage                  ⇒ p:锁9 反向 …
 *   P6 BackToClientButton 返回目标退回 /m3/customer/:id     ⇒ l:E0c l:S1 l:S2
 *   P7 登录落地重新跟随后端 recommended_route               ⇒ r:L2.1 r:L3.1 r:L5.1 r:N1(结构臂看不见:值来自后端)
 *   P8 登录页社媒入口重新直落 /s                            ⇒ l:S1 l:E1c · r:L4.1 r:L4.2 r:N1
 *   P9 我的客户阶段列不再用 StagePill                       ⇒ r:W1.201 r:W1.202
 *   P10 ToolGridCard 不渲染                                 ⇒ r:B2
 *   P11 资料确认徽标换成裸 Badge                            ⇒ r:B3
 *   P12 [WO_322] 在役页经函数返回值导航到不存在的路由       ⇒ l:S3(S1 / S2 都看不见这种形状)
 *
 * 三态退出码:0 全部有牙 / 1 有一发不过 / 3 基线不干净或尺子自己坏了。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const F = (rel) => join(ROOT, 'src', ...rel.split('/'));
const ARMS = {
    l: join(ROOT, 'scripts', 'test-route-literals-live.mjs'),
    d: join(ROOT, 'scripts', 'verify-dead-interactions.mjs'),
    p: join(ROOT, 'scripts', 'test-publish-center-layout.mjs'),
    r: join(ROOT, 'scripts', 'test-route-literals-live-render.mjs'),
};
const LOCK9 = 'p:锁9 反向 小榜助手的两处判断里没有 isFullHeightPage';
const PARTS_IMPORT = "import { StagePill, CompletenessBadge, RiskDot } from '@/components/workbench/parts';";
const POISONS = [
    { id: 'P1', why: "在役页塞 navigate('/m3/queue')", file: 'pages/Brand/MyClientsPage.tsx',
        from: PARTS_IMPORT, to: `${PARTS_IMPORT}\nexport const __poisonNav = (navigate: (p: string) => void) => navigate('/m3/queue');`,
        targets: ['l:S1', 'l:S2'] },
    { id: 'P2', why: "侧栏加一项 to: '/m3/sales/today'", file: 'components/layout/AppSidebar.tsx',
        from: "  { to: '/publish', icon: Send, label: '发布投放', stepNumber: 4, module: 'publish' },",
        to: "  { to: '/publish', icon: Send, label: '发布投放', stepNumber: 4, module: 'publish' },\n  { to: '/m3/sales/today', icon: Send, label: '毒', module: 'publish' },",
        targets: ['l:S1', 'l:S2', 'd:B8'] },
    { id: 'P3', why: "在役页 import '@/components/m3/parts'", file: 'pages/Brand/MyClientsPage.tsx',
        // [开源 E3 · 前端 · 2026-10-01 · WO_322] components/m3 整删后,值导入会让行为臂整个打包失败(r 段全「消失」)——
        //   那是构建自己拦住了,不是 l:M 有牙。改成类型导入:M 判据写明「值与类型都算」,esbuild 会抹掉类型导入,打包照常
        from: PARTS_IMPORT, to: `${PARTS_IMPORT}\nimport type { ComponentProps as __M3Props } from '@/components/m3/parts';\nexport type __poisonM3 = __M3Props;`,
        targets: ['l:M'] },
    { id: 'P4', why: 'PlacementCenter 的 /monitoring 退回 /insights', file: 'pages/Placement/PlacementCenter.tsx',
        from: '<a href="/monitoring" className="text-xs text-primary hover:underline flex items-center gap-1">',
        to: '<a href="/insights" className="text-xs text-primary hover:underline flex items-center gap-1">',
        targets: ['l:S1', 'l:S2', 'l:E1c'] },
    { id: 'P5', why: '小榜助手判断里加回 isFullHeightPage', file: 'components/layout/Layout.tsx',
        from: '      {!sandboxUI && !screenshotMode && !xiaobangRequested && (',
        to: '      {!sandboxUI && !screenshotMode && !xiaobangRequested && !isFullHeightPage && (',
        targets: [LOCK9] },
    { id: 'P6', why: 'BackToClientButton 返回目标退回 /m3/customer/:id', file: 'components/workbench/BackToClientButton.tsx',
        from: '    navigate(`/my-clients/${brandId}`);', to: '    navigate(`/m3/customer/${brandId}`);',
        targets: ['l:E0c', 'l:S1', 'l:S2'] },
    { id: 'P7', why: '登录落地重新跟随后端 recommended_route', file: 'utils/postLoginRedirect.ts',
        from: '    if ((data.agent_level || 0) >= 1) {',
        to: '    if (data.recommended_route && data.recommended_route !== HOME) return data.recommended_route;\n    if ((data.agent_level || 0) >= 1) {',
        targets: ['r:L2.1', 'r:L3.1', 'r:L5.1', 'r:N1'] },
    { id: 'P8', why: '登录页社媒入口重新直落 /s', file: 'pages/Login/LoginPage.tsx',
        from: '    return determineTargetRoute(from);',
        to: "    if (entryMode === 'social') return '/s';\n    return determineTargetRoute(from);",
        targets: ['l:S1', 'l:E1c', 'r:L4.1', 'r:L4.2', 'r:N1'] },
    { id: 'P9', why: '我的客户阶段列不再用 StagePill', file: 'pages/Brand/MyClientsPage.tsx',
        from: '                    ? <StagePill stage={c.stage} />', to: '                    ? <span>{String(c.stage)}</span>',
        targets: ['r:W1.201', 'r:W1.202'] },
    { id: 'P10', why: 'ToolGridCard 不渲染', file: 'components/workbench/ToolGrid.tsx',
        from: '}: ToolGridCardProps) {\n    return (', to: '}: ToolGridCardProps) {\n    if (brandId !== undefined) return null;\n    return (',
        targets: ['r:B2'] },
    { id: 'P11', why: '资料确认徽标换成裸 Badge', file: 'pages/Brand/BrandDetailPage.tsx',
        from: '                <MaterialConfirmStatusBadge status={status.status} />',
        to: '                <Badge variant="outline" className="text-[11px]">{String(status.status)}</Badge>',
        targets: ['r:B3'] },
    // [开源 E3 · 前端 · 2026-10-01 · WO_322] S3 专属:字面量不直接进 navigate,先当函数返回值再传进去,且路由根本不存在
    //   (顾问团页那种死出口的形状)—— S1(不是 DEP)/ S2(不是直接导航语境)都看不见,只该红 S3
    { id: 'P12', why: '在役页经函数返回值导航到不存在的路由', file: 'pages/Brand/MyClientsPage.tsx',
        from: PARTS_IMPORT, to: `${PARTS_IMPORT}\nconst __poisonPath = (id: string) => \`/definitely-gone/\${id}?from=\${encodeURIComponent(id)}\`;\nexport const __poisonFlow = (navigate: (p: string) => void) => navigate(__poisonPath('x'));`,
        targets: ['l:S3'] },
];

const runArm = (path) => {
    let out = '';
    try { out = execFileSync(process.execPath, [path], { cwd: ROOT, encoding: 'utf8', timeout: 20 * 60 * 1000 }); }
    catch (e) { out = String(e.stdout || '') + String(e.stderr || ''); }
    return out;
};
const run = () => {
    const cells = {};
    for (const [k, path] of Object.entries(ARMS)) {
        const out = runArm(path);
        if (k === 'p') {
            /* 布局锁的格是「✅/❌ 文案」行;键 = 文案到第一个括号 / 冒号 / 破折号为止(后面是会变的明细) */
            for (const m of out.matchAll(/^(✅|❌)\s+(.+)$/gm)) {
                const key = m[2].split(/[((::—]/)[0].trim();
                if (key && !key.startsWith('发布中心布局')) cells[`p:${key}`] = m[1] === '✅' ? 'OK' : 'FAIL';
            }
            continue;
        }
        /* 格行缩进两格;顶格的汇总行不是格;装饰符不许跨行 */
        for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) cells[`${k}:${m[2]}`] = m[1];
    }
    return cells;
};

console.log('=== 基线(四把尺子逐格读数;不看 rc)===');
const base = run();
const ids = Object.keys(base);
const red = ids.filter((k) => base[k] !== 'OK');
const per = Object.keys(ARMS).map((k) => `${k} ${ids.filter((i) => i.startsWith(`${k}:`)).length} 格`).join(' · ');
console.log(`  ${ids.length} 格(${per})· 红 ${red.length}${red.length ? `(${red.join(',')})` : ''}`);
const missingTargets = POISONS.flatMap((p) => p.targets).filter((t) => !(t in base));
if (!ids.length || red.length || missingTargets.length) {
    console.log(`🔴 基线不干净 / 点名格不在基线里(${missingTargets.join(',') || '—'})—— 注毒读数无意义`);
    process.exit(3);
}

let bad = 0;
for (const p of POISONS) {
    console.log(`\n=== ${p.id} ${p.why}(点名 ${p.targets.join(' / ')})===`);
    const path = F(p.file);
    const original = readFileSync(path, 'utf8');
    const hits = original.split(p.from).length - 1;
    if (hits !== 1) { console.log(`  🔴 毒没下成:${p.file} 锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`); bad += 1; continue; }
    try { assertRulerWorks(path); } catch (err) { console.log(String(err.message || err)); process.exit(3); }
    let after = {};
    let landed = true;
    try {
        writeFileSync(path, original.replace(p.from, p.to), 'utf8');
        if (!syntaxOk(path)) { console.log('  🔴 毒没下成:写成了语法错'); landed = false; }
        else after = run();
    } finally {
        writeFileSync(path, original, 'utf8');
    }
    const back = readFileSync(path, 'utf8') === original;
    if (!landed || !back) { bad += 1; if (!back) console.log('  🔴 源文件没回位'); continue; }
    const flipped = p.targets.filter((k) => base[k] === 'OK' && after[k] === 'FAIL');
    const missed = p.targets.filter((k) => !flipped.includes(k));
    const drift = ids.filter((k) => !p.targets.includes(k) && after[k] !== base[k]);
    const good = !missed.length && !drift.length;
    if (!good) bad += 1;
    console.log(`  点名格:${p.targets.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(' · ')}`);
    console.log(`  其余格漂移:${drift.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(',') || '无'} · 源文件逐字回位`);
    console.log(`  ${good ? '✅ 有牙' : `🔴 不过${missed.length ? `(没红:${missed.join(',')})` : ''}${drift.length ? '(别的格跟着动了)' : ''}`}`);
}
console.log(bad ? `\n🔴 ${bad} 发不过` : `\n全部通过:${POISONS.length} 发,每发只红点名的格`);
process.exit(bad ? 1 : 0);
