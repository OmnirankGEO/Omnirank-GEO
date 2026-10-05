#!/usr/bin/env node
/**
 * 判据 · 包三 §G「我的客户」页收口。三态退出码:0 全过 / 1 有失败 / 3 有未评估。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const REL = 'src/pages/Brand/MyClientsPage.tsx';
const RAW = readFileSync(join(ROOT, REL), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const SRC = strip(RAW);
/** 三种形状:引号串 / 独占一行的 JSX 文本 / 带 {插值} 的 JSX 文本(前两形各盲过一次)。 */
const copyStrings = (s) => [
    ...(s.match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(s.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || []),
];

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['E0', 'F0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 筛选档位的含义要说出来 ─────────────────────────────────────
/**
 * 🔴 五个 chip 看起来是并列五档,其实「日常客户」= 除已归档外的全部,
 *    与其余三档是**包含关系** ⇒ 那几个数字加起来不等于第一个。
 *    她看到对不上的数字,第一反应是"系统算错了"。
 */
ok(/data-testid="client-filter-meaning"/.test(SRC), 'A1 筛选条下有一句「当前这档是什么意思」');
const mIdx = SRC.indexOf('client-filter-meaning');
const mBlock = SRC.slice(mIdx, mIdx + 520);
ok(mBlock.length > 120, 'A0 正样本臂:切到了那块(切空会让下面两条不可解读)');
ok(/getClientStatusMeta\(statusFilter\)\.description/.test(mBlock),
    'A2 🔴 非 daily 档的说法**取自 clientStatus.ts 的 description(SSOT)**,不在页里另写一份'
    + ' —— 两处必漂,而漂开那天不会有任何判据变红');
ok(/不是相加关系|包含|除已归档外的全部/.test(mBlock),
    'A3 🔴 daily 那档明说它是**包含**关系 —— 这正是数字对不上的原因');

// ── B 🔴 空态必须给得出出口 ─────────────────────────────────────────
/**
 * 🔴 改前搜不到时只有「无匹配结果」一句,**没有任何可点的东西**:
 *    她盯着空列表,不知道是没这个客户还是被筛选挡住了,只能退出去。
 */
ok(/data-testid="clients-empty-exits"/.test(SRC), 'B1 有客户但筛不出来时,给出口');
const eIdx = SRC.indexOf('clients-empty-exits');
const eBlock = SRC.slice(eIdx, eIdx + 1400);
ok(eBlock.length > 400, 'B0 正样本臂:切到了出口块');
for (const [needle, why] of [
    ["setSearch('')", 'B2 清空搜索'],
    ["setStatusFilter('daily')", 'B3 换回日常客户这一档'],
    ["setStatusFilter('archived')", 'B4 到已归档里找 —— 归档是最常见的"人明明在却搜不到"'],
    ['setShowAdd(true)', 'B5 直接新建'],
]) ok(eBlock.includes(needle), `${why}`);
ok(!/无匹配结果/.test(SRC), 'B6 🔴 旧的那句无动作文案已不存在');

// ── C 🔴 对客文案不说内部机制 ───────────────────────────────────────
const cs = copyStrings(SRC);
const JARGON = ['并发', 'token', 'draft', 'is_test', 'SOV', 'stage'];
ok(cs.length >= 60, `C0 正样本臂:抽到 ${cs.length} 条对客文案`);
const hit = JARGON.filter((j) => cs.some((c) => c.includes(j)));
ok(hit.length === 0, `C1 🔴 零工程术语(实得 ${JSON.stringify(hit)})`);
/**
 * 🔴 C3 `owner_user_id` 不进禁词表,换一条**更精确**的断言。
 *    它是内部 id,但只渲染给 admin —— admin 是内部用户,要靠这个 id 追溯归属。
 *    笼统禁掉会逼我删掉一个对内有用的信息;真正该钉的是**它不许漏给代理**。
 *    所以:凡是渲染 ownerText 的地方,必须在 isAdmin 分支里。
 *    (豁免要显式且可证,不能靠"我记得那是 admin 才看得到"。)
 */
const ownerRenders = SRC.split('ownerText').slice(1)
    .map((seg, i) => ({ i, adminGated: /isAdmin/.test(SRC.slice(0, SRC.indexOf('ownerText') + 1)) }));
const ownerRenderLines = SRC.split(String.fromCharCode(10))
    .filter((l) => /\{ownerText\}|ownerText &&/.test(l));
ok(ownerRenderLines.length >= 1,
    `C3 正样本臂:确实抓到 ${ownerRenderLines.length} 处 ownerText 渲染点(抓 0 会让下一条恒真)`);
ok(ownerRenderLines.every((l, k) => {
    const idx = SRC.indexOf(l);
    return /isAdmin\s*&&/.test(SRC.slice(Math.max(0, idx - 260), idx + l.length));
}), 'C4 🔴 内部账号 id 的每一处渲染都在 isAdmin 分支里 —— 它对 admin 有用、对代理是噪音');
void ownerRenders;
ok(JARGON.some((j) => '并发 3 · 失败自动跳过'.includes(j)),
    'C2 反向对照:词表对样例串确实命中(否则 C1 的"零"只是词表坏了)');

// ── D 🔴 红臂:改动前都不成立 ───────────────────────────────────────
const BASE = 'f8a62e7ce';
let baseSrc = null;
try {
    baseSrc = execFileSync('git', ['show', `${BASE}:frontend/${REL}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1; skipped.push('E0');
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (baseSrc) {
    ok(/无匹配结果/.test(baseSrc), 'D1 红臂:改动前确实只有那句无动作文案');
    ok(/并发 3/.test(baseSrc), 'D2 红臂:改动前确实印着「并发 3」');
    ok(!/client-filter-meaning/.test(baseSrc), 'D3 红臂:改动前没有筛选含义说明');
    ok(/statusCounts/.test(baseSrc),
        'D4 配对臂:基线里本来就有的锚确实在(否则上面的"没有"只是取到空内容)');
}

// ── E dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('clients-empty-exits'), 'E1 空态出口进了产物');
    ok(!js.includes('并发 3'), 'E2 🔴 旧术语**不在产物里**(源码改了产物没重建 = 线上还在)');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
