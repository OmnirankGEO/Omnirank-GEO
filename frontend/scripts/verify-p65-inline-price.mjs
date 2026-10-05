#!/usr/bin/env node
/**
 * 判据 · #65 三条 GEO 静默扣费路径「价格看得见」(2026-06-03 拍板第 ② 段)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 「真的显示出数字」这条**只能是「未评估」,绝不许默认绿**(见 D 段)。
 *
 * 前提演变记录(留着,因为我判断错过一次):
 *   我先按代码推断「四个 code 不在生产 feature_pricing ⇒ 渲染不出」,
 *   Deploy 只读实测推翻:四行**全部存在且 is_active=t**
 *   (autofill_brand 130 / brand_fill 40 / mktg_bundle_std 650 / mktg_bundle_pro 1040),
 *   `PricingContext` 也不过滤(直接把 66 行灌进 map)。
 *   ⇒ 「19 项」是 `PricingPage.tsx:58` 按 category 过滤后的**展示数**,与取价无关。
 *   教训:代码只能证「没有白名单」,证不了「表里有没有那一行」—— 那是 DB 事实,要去量。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const rdRepo = (p) => readFileSync(join(REPO, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const GCC = strip(rd('src/pages/GeoContentCenter.tsx'));
const BUD = strip(rd('src/pages/Brand/BatchUpgradeDialog.tsx'));
const AFD = strip(rd('src/components/brand/AiFillDialog.tsx'));
const BBK = strip(rd('src/components/customer-intake/blocks/BasicBlock.tsx'));

// ── A 🔴 跨层锁:同一条规则的三份必须一致 ────────────────────────────
/**
 * 「2k/4k ⇒ pro」这条规则服务端写了两处,我在前端写了第三处。
 * 同谓词写多处必有一处没人验 —— 这一条就是那个"人"。
 */
const pySets = [];
for (const f of ['services/marketing/material_factory.py', 'services/marketing/geo_factory.py']) {
    let src = null;
    try { src = rdRepo(f); } catch { /* 下面按未评估处理 */ }
    if (src == null) { pySets.push(null); continue; }
    const m = src.match(/mktg_bundle_pro"\s+if\s+resolution\s+in\s+([({])([^)}]*)[)}]/);
    pySets.push(m ? m[2].split(',').map((x) => x.trim().replace(/["']/g, '')).filter(Boolean).sort() : null);
}
const mod = await import(new URL('../src/lib/marketingBundlePricing.ts', import.meta.url).href)
    .catch(() => null);

if (pySets.some((x) => x == null)) {
    notEvaluated += 1;
    console.log(`  ⚠️ A0 **未评估**:服务端规则没解析出来 (py=${JSON.stringify(pySets)})`
        + ' ⇒ 跨层锁没跑,不能声称一致。');
} else if (!mod) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **降级 ⇒ 未评估**:没能真 import marketingBundlePricing.ts,'
        + '行为锁一条都没执行(Node ≥22.6 类型剥离)。');
} else {
    const j2 = (a) => JSON.stringify(a);
    ok(j2(pySets[0]) === j2(pySets[1]),
        `A1 服务端两处规则一致 (${j2(pySets[0])} vs ${j2(pySets[1])})`);

    // 🔴 A2 是**行为锁**,不是常量锁。
    //    Review 2026-09-05 的毒:把 bundleFeatureCode 的 return 改成恒 'mktg_bundle_std'
    //    —— 常量 BUNDLE_PRO_RESOLUTIONS 一个字没动、调用形状也没动 ⇒ 旧的常量锁全绿。
    //    期望值**只从服务端规则推**,不从前端常量推(否则两边同源,漂了一起漂)。
    const expect = (r) => (pySets[0].includes(r) ? 'mktg_bundle_pro' : 'mktg_bundle_std');
    const RES = mod.BUNDLE_RESOLUTIONS || ['1k', '2k', '4k'];
    let allMatch = true;
    const detail = [];
    for (const r of RES) {
        const got = mod.bundleFeatureCode(r);
        const want = expect(r);
        detail.push(`${r}→${got}${got === want ? '' : `(应 ${want})`}`);
        if (got !== want) allMatch = false;
    }
    ok(allMatch, `A2 🔴 **行为锁**:逐 resolution 求值 == 服务端规则 [${detail.join(' ')}]`);

    // 正样本臂:两个分档都必须真的出现过,否则 A2 可能在退化数据上恒真
    const codes = new Set(RES.map((r) => mod.bundleFeatureCode(r)));
    ok(codes.has('mktg_bundle_pro') && codes.has('mktg_bundle_std'),
        `A3 正样本臂:两档都被真的取到过 (${[...codes].join(', ')})`
        + ' —— 全 std 或全 pro 时 A2 的"一致"不携带信息');
    ok(RES.length >= 2, `A4 分母非退化:resolution 至少两档 (${RES.length})`);
}

// ── B 源码结构臂:四个调用点 ────────────────────────────────────────
ok(/const bundleCode = bundleFeatureCode\(resolution\)/.test(GCC)
    && /from '@\/lib\/marketingBundlePricing'/.test(GCC),
    'B1 推广包的 code 随 resolution 反应式取(规则来自纯模块,判据才锁得住行为)');
ok(/bundleCost != null &&/.test(GCC),
    'B1 门控:取不到价**不渲染**(FeatureCostBadge 空态是「价目待配置」,给开发看的)');
ok(/featureCode=\{bundleCode\}/.test(GCC),
    'B1 badge 收的是**算出来的** code,不是字面量');
ok(/autofillCost != null &&/.test(BUD) && /featureCode="autofill_brand"/.test(BUD),
    'B2 批量升级资料:门控 + autofill_brand');
ok(/brandFillCost != null &&/.test(AFD) && /featureCode="brand_fill"/.test(AFD),
    'B3 AI 分析(AiFillDialog):门控 + brand_fill');
ok(/brandFillCost != null &&/.test(BBK) && /featureCode="brand_fill"/.test(BBK),
    'B4 AI 联网填充(BasicBlock):门控 + brand_fill —— brand_fill **有两个调用点**,不能只接一个');
for (const [n, s] of [['GCC', GCC], ['BUD', BUD], ['AFD', AFD], ['BBK', BBK]]) {
    ok(!/getCost\([^)]*\)\s*[*+]/.test(s), `B5 反臂 ${n}:前端**不对价格做算术**`);
}
ok(!/¥|人民币|元\b/.test(GCC.match(/data-testid="gcc-bundle-cost"[\s\S]{0,200}/)?.[0] || ''),
    'B6 反臂:显价区不出现人民币(#64 同款红线)');

// ── C 🔴 红臂:生产尖上四处都不存在 ─────────────────────────────────
const PROD_TIP = 'aacc086e0';
const show = (p) => execFileSync('git', ['show', `${PROD_TIP}:${p}`],
    { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
let prod = null;
try {
    prod = {
        gcc: show('frontend/src/pages/GeoContentCenter.tsx'),
        bud: show('frontend/src/pages/Brand/BatchUpgradeDialog.tsx'),
        afd: show('frontend/src/components/brand/AiFillDialog.tsx'),
        bbk: show('frontend/src/components/customer-intake/blocks/BasicBlock.tsx'),
    };
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ C0 **未评估**:取不到生产尖 ${PROD_TIP} 源码(${String(e.message).slice(0, 50)})`);
}
if (prod) {
    ok(!/FeatureCostBadge/.test(prod.gcc), 'C1 红臂:现网推广包按钮无任何显价');
    ok(!/FeatureCostBadge/.test(prod.bud), 'C2 红臂:现网批量升级无显价');
    ok(!/FeatureCostBadge/.test(prod.afd), 'C3 红臂:现网 AiFillDialog 无显价');
    ok(!/FeatureCostBadge/.test(prod.bbk), 'C4 红臂:现网 BasicBlock 无显价');
    ok(/mktg_bundle|resolution/.test(prod.gcc) && /autofill/i.test(prod.bud),
        'C5 配对臂:现网源码里本来就有的锚确实在(否则 C1-C4 只是取到空内容)');
}

// ── D 🔴 行为层:必须「未评估」,不许默认绿 ──────────────────────────
/**
 * 这一条要的是「用户真的看见了数字」。它取决于生产 `feature_pricing` 里有没有那几行,
 * 而那是 C 的一半 + DB 事实,**静态脚本永远答不了**。
 * 放一个证据文件当闸:真浏览器跑完把读数写进去,这条才可能变绿。
 */
const EVID = join(REPO, 'agent-test-artifacts', 'p65-inline-price-runtime.json');
if (!existsSync(EVID)) {
    notEvaluated += 1;
    console.log('  ⚠️ D0 **未评估 · 这是本卡的主命题**:四个按钮上真的出现了数字 —— 未验。');
    console.log('     ⚠️ 前提订正(Deploy 只读实证 2026-09-05):四个 code 在生产 feature_pricing');
    console.log('     里**全部存在且 is_active=t**(autofill_brand 130 / brand_fill 40 /');
    console.log('     mktg_bundle_std 650 / mktg_bundle_pro 1040),`PricingContext` 也不过滤');
    console.log('     ⇒ 原先"C 没补行所以渲染不出"的说法**已被推翻**,不再是阻塞原因。');
    console.log('     现在未评估的原因只剩一个:**静态脚本看不见浏览器**。');
    console.log(`     绿的条件:真浏览器跑完把读数写进 ${EVID}。`);
    console.log('     🔴 在那之前 **#65 不算修好** —— 本脚本退出码 3,不是 0。');
} else {
    const ev = JSON.parse(readFileSync(EVID, 'utf8'));
    for (const k of ['gcc-bundle-cost', 'batch-upgrade-cost', 'ai-fill-cost', 'basic-ai-fill-cost']) {
        ok(ev[k] && typeof ev[k].points === 'number' && ev[k].points > 0,
            `D:${k} 真渲染出算力数字(${ev[k]?.points ?? '无'})`);
    }
}

// ── F #81 价目表页文案如实(随 #65 同班)────────────────────────────
const PP = rd('src/pages/Pricing/PricingPage.tsx');
ok(!/项功能 · 每个功能消耗多少算力都在这查/.test(PP),
    'F1 旧文案「都在这查」消失 —— 它打印的是 category 过滤后的数(:58),读起来像"总共就这些"');
ok(/GEO 与媒体发布相关 \{items\.length\} 项/.test(PP),
    'F2 改成如实说范围(不放开 HIDDEN 过滤:那几条是 Owner 裁决下架的)');
ok(/GEO_PRICING_CATEGORIES/.test(PP) && /HIDDEN_FEATURE_CODES/.test(PP),
    'F3 反臂:过滤本身**没被顺手删掉** —— #81 只改文案,不改可见集');
if (prod) {
    let pp = null;
    try { pp = show('frontend/src/pages/Pricing/PricingPage.tsx'); } catch { /* 下面按未评估 */ }
    if (pp == null) { notEvaluated += 1; console.log('  ⚠️ F4 未评估:取不到现网 PricingPage'); }
    else ok(/项功能 · 每个功能消耗多少算力都在这查/.test(pp),
        'F4 红臂:现网**确实**是那句假文案(缺陷成立)');
}

// ── E dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ E0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const e1 = js.includes('gcc-bundle-cost');
    ok(e1, 'E1 显价锚进了产物(源码有 ≠ 产物有)');
    ok(js.includes('mktg_bundle_pro') && js.includes('mktg_bundle_std'),
        'E2 两个 code 都在产物里(只打包一个 = 另一档永远显示不出来)');
    if (e1) ok(!js.includes('zzq-必然不存在的锚'), 'E3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ E3 未评估:E1 未成立 ⇒ 提取可能整体为空'); }
}

if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估 —— #65 **尚未闭环**`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
