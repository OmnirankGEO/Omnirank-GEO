#!/usr/bin/env node
/**
 * 判据 · #62(重算有序两步 + 签名只在算价成功写 + 过期不许确认)与 #64(真实成本禁泄)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 * 「未评估」独立成一档 —— 它与「跑了但没过」在处置上完全不同。
 *
 * 🔴 本脚本是**静态**检查(源码 + dist + git)。运行时命题(点了按钮真的又发一次
 *    run-previews、返回的 preview 绑当前题单)不放这里 —— 放这里它永远只能是"未评估"。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const PANEL = strip(rd('src/components/defensiveGeo/LaunchPanel.tsx'));
const WALLET_RAW = rd('src/pages/Wallet/WalletPage.tsx');
const WALLET = strip(WALLET_RAW);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

// ── A 纯逻辑臂 ──────────────────────────────────────────────────────
const guard = await import(new URL('../src/pages/Diagnosis/launch/planSideGuard.ts', import.meta.url).href)
    .catch(() => null);
if (!guard) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **降级 ⇒ 未评估**:没能真 import .ts,纯逻辑臂一条都没执行。');
} else {
    const { planMatchesDraft, questionSetKey } = guard;
    const a = { modeSide: 'defensive', text: '甲' };
    const b = { modeSide: 'offensive', text: '乙' };
    ok(planMatchesDraft([a, b], [b, a]),
        'A1 plan/draft 比对**顺序无关**(拖动排序不该判成对不上)');
    ok(!planMatchesDraft([a], [a, b]),
        'A1 反臂:少一道就必须判成对不上');
    ok(!planMatchesDraft([{ modeSide: 'defensive', text: '旧问法' }],
        [{ modeSide: 'defensive', text: '新问法' }]),
        'A2 🔴 **只改题文、不改条数**也必须判成对不上'
        + '(这正是 clientRequestId 只按条数编键漏掉的那一格)');
    ok(questionSetKey([a, b]) === questionSetKey([b, a]),
        'A3 内容键顺序无关(同一份题单 ⇒ 同一个键,幂等性保住)');
    ok(questionSetKey([{ modeSide: 'defensive', text: '旧问法' }])
        !== questionSetKey([{ modeSide: 'defensive', text: '新问法' }]),
        'A3 反臂:题文一变键就必须变(否则幂等会把旧 plan 取回来)');
}

// ── B 源码结构臂 ────────────────────────────────────────────────────
const previewFn = PAGE.slice(
    PAGE.indexOf('const runDefensivePlanPreview'),
    PAGE.indexOf('const repriceFromCurrentDraft'));
ok(previewFn.length > 200, 'B0 定位臂:切到了 runDefensivePlanPreview 的函数体');
ok(!/setPricedSignature/.test(previewFn),
    'B1 🔴 **缺陷本体**:题单重建路径里不得再写 pricedSignature');
ok(/clientRequestId:\s*`defgeo:\$\{brandId\}:\$\{campaignMode\}:\$\{questionSetKey\(usable\)\}`/.test(previewFn),
    'B2 clientRequestId 按**内容**取键(不是 usable.length)');
ok(/onRepriceRequested=\{repriceFromCurrentDraft\}/.test(PAGE),
    'B3 重算按钮接的是有序两步,**不是**题单生成器');
ok(!/onRepriceRequested=\{\(\)\s*=>\s*\{\s*void runDefensivePlanPreview/.test(PAGE),
    'B3 反臂:旧接法(直接接题单生成器)必须消失');
const repriceFn = PAGE.slice(PAGE.indexOf('const repriceFromCurrentDraft'),
    PAGE.indexOf('const repriceFromCurrentDraft') + 600);
ok(/planMatchesDraft\(fresh\.questions,\s*defensiveDraft\)/.test(repriceFn),
    'B4 第一步产物**亲验**绑当前题单(不信"调用成功"本身)');
ok(/onPriced=\{setPricedSignature\}/.test(PAGE),
    'B5 签名由**算价成功**路径回写');
ok(/const fresh = await onRepriceRequested\?\.\(\);[\s\S]{0,120}await runPreview\(undefined,\s*\{\s*newLogical:\s*true\s*\},\s*fresh\)/.test(PANEL),
    'B6 面板里是**有序两步**:先 await 重建,再给**新 plan** 算价');
ok(/if \(!fresh\) return;/.test(PANEL),
    'B6 反臂:第一步失败必须 return(黄条留着,不许清)');
ok(/questionPlanId:\s*usePlan\.planId/.test(PANEL) && /questionPlanRevision:\s*usePlan\.planRevision/.test(PANEL),
    'B7 算价用的是**显式传入的那份 plan**,不是闭包里的旧 props');
ok(/onPriced\?\.\(questionSetSignature\(usePlan\.questions\)\)/.test(PANEL),
    'B8 签名取自**被定价的那份 plan 自己装的题单**(不是当时的 draft)');
ok(/disabled=\{phase === 'confirming' \|\| !preview\.canConfirm \|\| !!priceIsStale\}/.test(PANEL),
    'B9 过期态**确认按钮 disabled**(与 UI 形态无关的不变量)');
ok(/data-testid="launch-confirm-blocked-stale"/.test(PANEL),
    'B9 配套:disabled 必须**写原因**(只禁用不解释 = 死按钮)');

// ── C #64 真实成本 census ───────────────────────────────────────────
const FROZEN_EXEMPT = ['src/data/china-cities.ts'];   // 经纬度 110.290 / 130.298,与价格无关
const walk = (d, out = []) => {
    for (const e of readdirSync(join(ROOT, d), { withFileTypes: true })) {
        const p = `${d}/${e.name}`;
        if (e.isDirectory()) { if (e.name !== 'node_modules') walk(p, out); }
        else if (/\.tsx?$/.test(e.name)) out.push(p);
    }
    return out;
};
const files = walk('src');
const yenHits = files.filter((f) => rd(f).includes('¥0.29'));
ok(yenHits.length === 0,
    `C1 🔴 主锚:frontend 任何文件不得出现人民币单价字面量(命中 ${yenHits.join(',') || '0'})`);
const rawHits = files.filter((f) => /0\.29/.test(rd(f)) && !FROZEN_EXEMPT.includes(f));
ok(rawHits.length === 0,
    `C2 辅锚:裸 0.29 只允许出现在**冻结豁免清单**内(清单外命中 ${rawHits.join(',') || '0'})`
    + ' —— 防有人改写成别的货币写法绕过主锚');
ok(FROZEN_EXEMPT.every((f) => /0\.29/.test(rd(f))),
    'C2 配对臂:豁免清单里的文件**确实**含 0.29(否则豁免是死条目,辅锚等于没设防)');
ok(/useFeatureCost\('scheduled_monitoring'\)/.test(WALLET),
    "C3 价从 feature_pricing 动态取(code=scheduled_monitoring · 自动监测走 batch_monitor)");
ok(/autoMonitorPoints != null\s*\?/.test(WALLET) && /算力/.test(WALLET),
    'C4 取不到价时**一个数字都不显示**(宁可少说,不可说错)');
ok(!/autoMonitorPoints\s*[*+]/.test(WALLET),
    'C5 反臂:前端**不对价格做任何算术**(前端不算钱)');

// ── D 🔴 红臂:这些锚在生产尖上都不存在 ─────────────────────────────
const PROD_TIP = 'aacc086e0';
let pPage = null; let pPanel = null; let pWallet = null;
try {
    const show = (p) => execFileSync('git', ['show', `${PROD_TIP}:${p}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    pPage = show('frontend/src/pages/Diagnosis/NewDiagnosis.tsx');
    pPanel = show('frontend/src/components/defensiveGeo/LaunchPanel.tsx');
    pWallet = show('frontend/src/pages/Wallet/WalletPage.tsx');
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ D0 **未评估**:取不到生产尖 ${PROD_TIP} 源码(${String(e.message).slice(0, 60)})`
        + ' ⇒ 红臂没跑,不能声称"改动有效"。');
}
if (pPage && pPanel && pWallet) {
    ok(/onRepriceRequested=\{\(\)\s*=>\s*\{\s*void runDefensivePlanPreview/.test(pPage),
        'D1 红臂:现网重算按钮**接的就是题单生成器**(缺陷在现网成立)');
    ok(/setPricedSignature\(questionSetSignature\(defensiveDraft\)\)/.test(pPage),
        'D2 红臂:现网在**题单重建路径**里写签名(黄条会被自己清掉)');
    ok(!/planMatchesDraft|questionSetKey|repriceFromCurrentDraft/.test(pPage),
        'D3 红臂:现网没有 planMatchesDraft / questionSetKey / repriceFromCurrentDraft');
    ok(!/priceIsStale\}/.test(pPanel.split('disabled={')[1] || ''),
        'D4 红臂:现网确认按钮的 disabled **不含** priceIsStale(过期价可确认)');
    ok(pWallet.includes('¥0.29'),
        'D5 红臂:现网 WalletPage **确实**含人民币单价(#64 缺陷在现网成立)');
    ok(/data-testid="launch-panel"/.test(pPanel) && /questionPlan/.test(pPage),
        'D6 配对臂:现网源码里本来就该有的锚确实在(否则上面的"没有"只是取到了空内容)');
}

// ── E dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ E0 **未构建 ⇒ 未评估**:没有 dist,产物锚一条都没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const e1 = js.includes('launch-confirm-blocked-stale');
    const e2 = !js.includes('¥0.29');
    ok(e1, 'E1 过期禁用说明锚在**产物**里(源码有 ≠ 产物有)');
    ok(e2, 'E2 🔴 产物里**没有**人民币单价(源码清了但产物没重建 = 线上仍在泄)');
    if (e1) ok(!js.includes('zzq-必然不存在的锚'), 'E3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ E3 未评估:E1 未成立 ⇒ 提取可能整体为空'); }
}

if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
