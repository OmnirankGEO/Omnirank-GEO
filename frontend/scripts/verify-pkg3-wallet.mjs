#!/usr/bin/env node
/**
 * 判据 · 包三 §G「我的算力 / 我的钱包」收口。三态退出码:0 / 1 / 3。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const rd = (p) => strip(readFileSync(join(ROOT, p), 'utf8'));
const WALLET = 'src/pages/Wallet/WalletPage.tsx';
const SRC = rd(WALLET);

/** 边界同 pkg3-imagecontent:引号串那半可靠;JSX 那半滤掉明显是代码的片。声明,不是门。 */
const stripInterp = (t) => t.replace(/\$\{[^{}]*\}/g, '~');
const looksLikeCode = (t) => /\|\||&&|=>|!==|return |throw /.test(t);
const copyOf = (t) => [
    ...(stripInterp(t).match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(t.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || [])
        .filter((x) => !looksLikeCode(x)),
];

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['E0', 'F0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 被占用的算力对**所有人**可见(FH-032)────────────────────────
/**
 * 🔴 原来这块挂 `!isAgent` —— 恰好把「有多少算力正被占用」对**主用户(服务方)**
 *    藏起来了:她看到的可用余额比实际少,却找不到少的那部分去哪了。
 *    算力被占用与她是不是服务方无关。
 */
ok(!/\{!isAgent && frozenPoints > 0/.test(SRC),
    'A1 🔴 那块**不再**挂 `!isAgent` —— 藏的恰好是主用户');
ok(/\{frozenPoints > 0 && \(/.test(SRC), 'A2 只按"有没有被占用"决定显不显示');
const aIdx = SRC.indexOf('frozenPoints > 0 && (');
const aBlock = SRC.slice(aIdx, aIdx + 700);
ok(aBlock.length > 200, 'A0 正样本臂:切到了那一块');
ok(/使用中/.test(aBlock), 'A3 说清那是"正在被用掉的"而不是丢了');
ok(/退回|退还/.test(aBlock),
    'A4 🔴 明说**跑失败会退回来** —— 不说,她会以为那笔已经花掉了');

// ── B 🔴 文案铁律:对客不出现「冻结 / 积分 / 额度」──────────────────
/**
 * 🔴 「冻结」是内部记账词。Review 裁:**状态要可见,词要人话** ——
 *    「先占用 … 失败退回」。两件事不矛盾:可见的是那笔算力,不是那两个字。
 *    单位一律「算力」,禁「积分 / 额度」(资金链 SSOT)。
 */
const FILES = [
    WALLET,
    'src/pages/Wallet/ServiceFeeHistoryPage.tsx',
    'src/components/wallet/ServiceFeeWalletCard.tsx',
    'src/components/wallet/WalletAddonTab.tsx',
];
const BAD = ['冻结', '积分', '额度'];
/**
 * 🔴 **中文禁词不需要 JSX 解析。**
 *
 * 我先按老路子走了抽取器那条:折叠 `{...}` → 取 JSX 文本 → 逐条判。
 * 结果注毒时发现 WalletAddonTab 的「…11 维基础额度…」在折叠后的源里
 * `indexOf` = **-1** —— 折叠把整段真文案吃掉了,判据绿而那一页根本没被看过(假阴)。
 *
 * 然后我意识到走错了轴:**中文只可能出现在文案或注释里**,而注释已经剥掉。
 * 所以「去注释后的源码是否包含这个中文词」本身就是可靠判据 ——
 * 抽取器那套只有拉丁术语(token / draft,它们也会出现在标识符里)才需要。
 * 复杂的仪器不是更强的仪器;这里简单的那个才是对的。
 */
let scanned = 0;
const violations = [];
for (const rel of FILES) {
    const src = rd(rel);          // 已去注释
    scanned += (src.match(/[一-龥]/g) || []).length;
    for (const b of BAD) {
        const i = src.indexOf(b);
        if (i >= 0) violations.push([rel.split('/').pop(), b, src.slice(Math.max(0, i - 18), i + 18).replace(/\s+/g, ' ')]);
    }
}
ok(scanned >= 500, `B0 正样本臂:四个文件去注释后共 ${scanned} 个中文字(为 0 会让下一条恒真)`);
ok(violations.length === 0, `B1 🔴 对客文案零「冻结/积分/额度」(实得 ${JSON.stringify(violations.slice(0, 3))})`);
ok(BAD.some((b) => '余额已冻结'.includes(b)), 'B2 反向对照:词表对样例串确实命中');

// ── C 🔴 流水标签说那笔钱发生了什么 ─────────────────────────────────
ok(/freeze:\s+\{ label: '先占用'/.test(SRC), 'C1 freeze → 先占用');
ok(/release:\s+\{ label: '占用退回'/.test(SRC), 'C2 release → 占用退回(与 C1 成对,她能对上账)');
ok(/withdrawal_freeze:\s+\{ label: '提现处理中'/.test(SRC), 'C3 withdrawal_freeze → 提现处理中');

// ── E 🔴 红臂 ───────────────────────────────────────────────────────
const BASE = '505fb33ca';
try {
    const base = execFileSync('git', ['show', `${BASE}:frontend/${WALLET}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    ok(/\{!isAgent && frozenPoints > 0/.test(base), 'E1 红臂:改动前那块确实挂着 !isAgent');
    ok(/label: '冻结'/.test(base), 'E2 红臂:改动前流水标签确实是「冻结」');
    ok(/frozenPoints/.test(base), 'E3 配对臂:基线里本来就有的锚确实在');
} catch (e) {
    notEvaluated += 1; skipped.push('E0');
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('先占用'), 'F1 新说法进了产物');
    ok(!js.includes("label:'冻结'") && !js.includes('label: "冻结"'),
        'F2 🔴 旧标签不在产物里(源码改了产物没重建 = 线上还在)');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
