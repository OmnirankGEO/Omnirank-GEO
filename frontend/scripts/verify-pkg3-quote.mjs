#!/usr/bin/env node
/**
 * 判据 · 包三 §G「报价方案」页收口。三态退出码:0 全过 / 1 有失败 / 3 有未评估。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const REL = 'src/pages/Quote/OnlineQuoteFlow.tsx';
const RAW = readFileSync(join(ROOT, REL), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const SRC = strip(RAW);
const copyStrings = (s) => [
    ...(s.match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(s.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || []),
];

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['D0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 超出服务范围的词不许默认勾选(钱的缺陷)────────────────────
/**
 * 🔴 改前 `default_selected === true` 短路在最前面 ⇒ 后端给了 true,
 *    一个客户根本不服务的地区的词也默认勾上、直接进报价。
 *    代理未必逐条看那一长串词 ⇒ 把不该卖的东西默认卖了出去。
 *    锁打**顺序**:否决必须在短路之前。
 */
const fnIdx = SRC.indexOf('function isDefaultSelectedKeyword');
const fn = fnIdx >= 0 ? SRC.slice(fnIdx, fnIdx + 420) : '';
ok(fn.length > 80, 'A0 正样本臂:切到了默认勾选谓词(切空会让下面几条不可解读)');
ok(/isOutOfScopeKeyword\(keyword\)/.test(fn),
    'A1 🔴 谓词里问了「是不是超出服务范围」');
const vetoIdx = fn.indexOf('isOutOfScopeKeyword');
const shortIdx = fn.indexOf('default_selected === true');
ok(vetoIdx >= 0 && shortIdx >= 0 && vetoIdx < shortIdx,
    `A2 🔴 否决**排在** default_selected 短路之前(实得 ${vetoIdx} < ${shortIdx})`
    + ' —— 排在后面等于没写:第一支为真就返回了');
ok(/reason_group[^\n]*geo_outside/.test(SRC) && /scope_match === false/.test(SRC),
    'A3 🔴 新旧两种口径都认(新后端给 reason_group,老后端只有 scope_match 布尔)');
/**
 * 🔴 A4 比 A2 更精确:断言函数体的**第一条语句**就是否决。
 *    A2 只比下标先后 —— 若哪天有人在中间插一条 `if (x) return true;`,
 *    下标顺序不变而否决实际上被绕过了。第一条语句这个位置是唯一的,绕不过去。
 *    (原来这里是一条"用正则剥掉守卫再判旧形态"的锁,脆且没有区分力,已换掉 ——
 *     换的是尺子不是意图:意图仍是「旧的无条件短路形态不许回来」。)
 */
const bodyIdx = fn.indexOf('{');
const firstStmt = bodyIdx >= 0 ? fn.slice(bodyIdx + 1).trim().split(String.fromCharCode(10))[0].trim() : '';
ok(firstStmt === 'if (isOutOfScopeKeyword(keyword)) return false;',
    `A4 🔴 函数体第一条语句就是否决(实得 ${JSON.stringify(firstStmt)})`);

// ── B 🔴 重算只有一个入口、一套文案 ─────────────────────────────────
const recalcHandlers = (SRC.match(/generate-quote`, \{\}, \{ timeout: 600000 \}\)/g) || []).length;
ok(recalcHandlers >= 1, `B0 正样本臂:确实抓到了重算调用(实得 ${recalcHandlers})`);
ok(/onClick=\{handleRecalculate\}/.test(SRC),
    'B1 🔴 那颗按钮指向**唯一**的 handleRecalculate,而不是自己内联第二套');
const confirmBlocks = (SRC.match(/confirmLabel: '重新算一遍'/g) || []).length;
ok(confirmBlocks === 1, `B2 🔴 确认文案只有一处(实得 ${confirmBlocks});两处必漂,而改文案时只会改一处`);

// ── C 🔴 对客文案不说内部机制 ───────────────────────────────────────
/**
 * 🔴 [Review 裁 ①] 「审计」也归工程术语:对代理来说「AI 复核报价」才说得清做什么。
 *    ⚠️ 只管**对客文案**——API 路径 `audit-keyword` 与变量名不在此列,
 *    改它们是另一回事(会动接口),而把它们卷进文案词表只会制造假红。
 */
const JARGON = ['LLM 审计', 'LLM 已重新审计', '三维评分引擎', '审计', 'token', 'SOV', 'draft'];
const cs = copyStrings(SRC);
ok(cs.length >= 50, `C0 正样本臂:抽到 ${cs.length} 条对客文案(抽空会让下一条恒真)`);
const hit = JARGON.filter((j) => cs.some((c) => c.includes(j)));
ok(hit.length === 0, `C1 🔴 按钮/提示说**做什么**不说内部机制(实得 ${JSON.stringify(hit)})`);
ok(JARGON.some((j) => '重新触发三维评分 + LLM 审计'.includes(j)),
    'C1 反向对照:词表对样例串确实命中(否则 C1 的"零"只是词表坏了)');
ok(cs.some((c) => c.includes('重新算一遍价格')), 'C2 按钮说的是她要做的那件事');
/**
 * 🔴 C5 形状臂三:带 {插值} 的 JSX 文本。抽取器在这一形上盲过一次
 *    (Review 抓到「复核前 ¥…」那一处不在我数的 21 处里)。
 *    三种形状各钉一条已知样本:引号串 / 独占一行的 JSX 文本 / 带插值的 JSX 文本。
 *    修这条盲区时我先放宽了正则,又引入 => 的假阳 —— 同一根轴上放宽必然从一端
 *    滑到另一端;最终靠「折叠插值 + 排除代码标点」两步分开,不是靠放宽。
 */
ok(cs.some((c) => c.includes('复核前')),
    'C5 🔴 形状臂三:带插值的 JSX 文本也被抽到了');
ok(cs.some((c) => c.includes('复核')),
    'C3 正样本臂:替代说法确实在页里(否则 C1 的「零审计」可能只是把整块功能删了)');
/**
 * 🔴 第一版写的是 `RAW.includes('audit-keyword')` —— 而 `audit-keyword-RENAMED`
 *    **包含**它,注毒当场不红。我正是在写「别用包含判定」的注释时犯了包含判定。
 *    改成带分隔符的路径形:改名会同时破坏前后两个边界。
 */
ok(/\/audit-keyword\/\$\{/.test(RAW),
    'C4 🔴 只改文案不改接口:API 路径原样保留 —— 改词的同时动接口,回归面就从一页变成前后端两侧');

// ── D 🔴 生成链接后说清「客户看到什么」──────────────────────────────
ok(/data-testid="quote-link-what-customer-sees"/.test(SRC), 'D1 链接旁有一块说明');
const box = SRC.slice(SRC.indexOf('quote-link-what-customer-sees'), SRC.indexOf('quote-link-what-customer-sees') + 900);
ok(/不会看到/.test(box) && /成本/.test(box),
    'D2 🔴 明说客户**看不到**成本 —— 不知道对方看到什么,她就不敢发,这条链路就断在这儿');
ok(/不需要注册|不需要登录|不用登录/.test(box), 'D3 说清客户不用注册登录(token-only 是产品口径)');

// ── E 🔴 红臂:改动前都不成立 ───────────────────────────────────────
const BASE = 'f05bc1da4';
let baseSrc = null;
try {
    baseSrc = execFileSync('git', ['show', `${BASE}:frontend/${REL}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1; skipped.push('D0');
    console.log(`  ⚠️ D0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (baseSrc) {
    ok(/重新算价 \+ LLM 审计/.test(baseSrc), 'E1 红臂:改动前按钮确实说「LLM 审计」');
    ok(!/isOutOfScopeKeyword/.test(baseSrc), 'E2 红臂:改动前没有超范围否决');
    ok(!/quote-link-what-customer-sees/.test(baseSrc), 'E3 红臂:改动前链接旁没有那块说明');
    ok(/isDefaultSelectedKeyword/.test(baseSrc),
        'E4 配对臂:基线里本来就有的锚确实在(否则上面几条"没有"只是取到空内容)');
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('H0');
    console.log('  ⚠️ H0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('quote-link-what-customer-sees'), 'F1 新说明进了产物');
    ok(!js.includes('重新算价 + LLM 审计'), 'F2 🔴 旧按钮文案**不在产物里**(源码改了产物没重建 = 线上还在)');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t) && t !== 'H0');
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
