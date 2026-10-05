#!/usr/bin/env node
/**
 * 枚举器 · 教程阶段的**产生方 S** 与**消费方 C**(#190 §0)。
 *
 * 🔴 这不是门,是**分母**。先把两张表枚举全、打印差集,数字与手工抽查对上,
 *    再谈写门 —— 上一轮我按**一种**消费写法(`tutorialStage !== 'X'`)枚举,
 *    报出 7 个"没有引导的阶段",逐个看下来 `step4-good` / `step4-bad` 其实**有**引导,
 *    只是用了另一个变量名(`tutorial.stage === 'X'`)。
 *    枚举式太窄 ⇒ 产出**自信的假阳性**。这个文件存在的唯一理由就是防那件事。
 *
 * 🔴 每加一种写法,都要在下面的 PATTERNS 里登记,并写清它是"产生"还是"消费"。
 *    漏登记一种写法 = 分母悄悄变窄,而输出看起来一样体面。
 *
 * 跑法:cd frontend && node scripts/enumerate-tutorial-stages.mjs [--json]
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

/** 递归收集 .ts/.tsx。 */
function walk(dir, out = []) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        const st = statSync(p);
        if (st.isDirectory()) walk(p, out);
        else if (/\.(ts|tsx)$/.test(name)) out.push(p);
    }
    return out;
}

// ── 权威阶段清单:从类型联合里取,不手写 ────────────────────────────
const stageSrc = readFileSync(join(SRC, 'sandbox/tutorialStage.ts'), 'utf8');
const unionStart = stageSrc.indexOf('export type TutorialStage =');
const unionEnd = stageSrc.indexOf(';', unionStart);
const UNION = stageSrc.slice(unionStart, unionEnd);
/*
 * 🔴 取的是**联合体成员形态** `| 'x'`,不是"这一段里出现过的带引号的串"。
 *    #191 写退役说明时我在联合体里的注释里带引号提了三个**已退役**的阶段名,
 *    旧写法(/'([a-z0-9-]+)'/)当场把它们又算回分母 —— |ALL| 报 46 而真值 43,
 *    而输出看起来一样体面。同族:a-string-anchor-satisfied-by-an-unrelated-line。
 */
const ALL_STAGES = [...UNION.matchAll(/\|\s*'([a-z0-9-]+)'/g)].map((m) => m[1]);

/*
 * 🔴 **两张表必须逐项相等**:
 *    `TutorialStage`(能写进状态机的阶段)与 `VALID_STAGES`(能从 localStorage 读回的阶段)。
 *    只改一侧不会有任何东西报错,后果却是真的:
 *      · 类型里有、VALID_STAGES 里没有 ⇒ 刷新一次就被打回 'modal'(用户视角:教程自己重置);
 *      · VALID_STAGES 里有、类型里没有 ⇒ 一个没人认识的值能通过校验,全站无引导。
 *    #191 退役三个阶段时要同时改两处,所以这条自证就长在分母里。
 */
{
    const vStart = stageSrc.indexOf('const VALID_STAGES');
    const vEnd = stageSrc.indexOf('];', vStart);
    if (vStart < 0 || vEnd < 0) {
        console.log('FAIL 分母自证:找不到 VALID_STAGES 清单 ⇒ 无法与类型联合互校');
        process.exit(1);
    }
    const valid = [...stageSrc.slice(vStart, vEnd).matchAll(/'([a-z0-9-]+)'/g)].map((m) => m[1]);
    const onlyType = ALL_STAGES.filter((s) => !valid.includes(s));
    const onlyValid = valid.filter((s) => !ALL_STAGES.includes(s));
    if (ALL_STAGES.length < 20 || valid.length < 20) {
        console.log(`FAIL 分母自证:读空了(联合 ${ALL_STAGES.length} / VALID ${valid.length})`
            + ' ⇒ 下面每条都恒真');
        process.exit(1);
    }
    if (onlyType.length || onlyValid.length) {
        console.log('FAIL TutorialStage 与 VALID_STAGES 不一致 ⇒ 分母不可信:');
        if (onlyType.length) console.log(`     只在类型里:${onlyType.join(',')}`);
        if (onlyValid.length) console.log(`     只在 VALID_STAGES 里:${onlyValid.join(',')}`);
        process.exit(1);
    }
}

/**
 * 写法登记表。
 * kind: 'produce' = 把状态机推到这个阶段;'consume' = 读这个阶段决定显示什么。
 *
 * 🔴 `re` 里的捕获组 1 必须是 stage 名。
 */
const PATTERNS = [
    // ── 产生方 ──────────────────────────────────────────────────
    { kind: 'produce', why: 'setTutorialStage(…) 直接推进', re: /setTutorialStage\(\s*'([a-z0-9-]+)'/g },
    { kind: 'produce', why: 'setStage(…) 底层 API', re: /\bsetStage\(\s*'([a-z0-9-]+)'/g },
    { kind: 'produce', why: '侧栏导航契约的 stage / nextStage', re: /(?:^|\s)(?:stage|nextStage):\s*'([a-z0-9-]+)'/gm },
    { kind: 'produce', why: 'OnboardingChecklist 的 stepId→stage 映射', re: /:\s*'(step[a-z0-9-]+|sidebar|modal|page-intro|brand-name|autofill|submit|done)'\s*,?\s*$/gm },
    // ── 消费方 ──────────────────────────────────────────────────
    { kind: 'consume', why: 'tutorialStage === / !==', re: /tutorialStage\s*(?:===|!==)\s*'([a-z0-9-]+)'/g },
    { kind: 'consume', why: 'tutorial.stage === / !==(另一个变量名 —— 上轮漏的就是它)', re: /tutorial(?:\?)?\.stage\s*(?:===|!==)\s*'([a-z0-9-]+)'/g },
    { kind: 'consume', why: 'stage === / !==(裸变量)', re: /(?<![.\w])stage\s*(?:===|!==)\s*'([a-z0-9-]+)'/g },
    { kind: 'consume', why: 'getStage() 比较', re: /getStage\(\)\s*(?:===|!==)\s*'([a-z0-9-]+)'/g },
    { kind: 'consume', why: '数组/集合包含判断', re: /\[[^\]]*'([a-z0-9-]+)'[^\]]*\]\s*\.includes\(\s*(?:tutorialStage|stage)\s*\)/g },
    /*
     * 🔴 下面这条是**手工抽查抽出来的**,不是我一开始想到的 —— 正是 #190 §0 要防的那种漏。
     *    `getSandboxTutorialNavItem(stage)` 做的是
     *    `SANDBOX_TUTORIAL_NAV_ITEMS.find(item => item.stage === stage)`:
     *    **按阶段查表**。所以侧栏契约里每一个 `stage:` 值都是被消费的,
     *    而源码里**没有任何一处出现那个字符串的比较**。
     *    漏了它,`step4-sidebar` 就会被报成"推得到却没人消费"(我第一版就是这么报的)。
     */
    {
        kind: 'consume',
        why: '侧栏契约按阶段查表(getSandboxTutorialNavItem)—— 泛型消费,源码里没有字面比较',
        re: /(?:^|\s)stage:\s*'([a-z0-9-]+)'/gm,
        onlyIn: /tutorialLayoutContract\.ts$/,
        requires: { file: 'src/sandbox/tutorialLayoutContract.ts', needle: 'item.stage === stage' },
    },
];

const files = walk(SRC).filter((f) => !f.endsWith('tutorialStage.ts'));
/*
 * 🔴 `requires`:泛型消费这条的**前提**是那个查表函数真的按 stage 查。
 *    前提不成立就不该把它算成消费 —— 否则哪天查表改成按别的字段,
 *    这条会继续把一堆阶段算成"有人消费",而屏幕上其实没人管。
 */
for (const pat of PATTERNS) {
    if (!pat.requires) continue;
    const src = readFileSync(join(ROOT, pat.requires.file), 'utf8');
    if (!src.includes(pat.requires.needle)) {
        console.log(`FAIL 枚举前提不成立:${pat.why}`);
        console.log(`     期望在 ${pat.requires.file} 里看到 \`${pat.requires.needle}\`,没看到。`);
        console.log('     🔴 这条写法的前提变了,枚举结果不可信 —— 先核对再跑。');
        process.exit(1);
    }
}
const S = new Map();   // stage -> [{file,line,why}]
const C = new Map();
const add = (map, stage, rec) => {
    if (!map.has(stage)) map.set(stage, []);
    map.get(stage).push(rec);
};

for (const f of files) {
    const text = readFileSync(f, 'utf8');
    const lines = text.split('\n');
    for (const pat of PATTERNS) {
        /* onlyIn:这条写法只在某个文件里成立(泛型查表那条) */
        if (pat.onlyIn && !pat.onlyIn.test(f.replace(/\\/g, '/'))) continue;
        pat.re.lastIndex = 0;
        let m;
        while ((m = pat.re.exec(text)) !== null) {
            const stage = m[1];
            if (!ALL_STAGES.includes(stage)) continue;   // 只认真实阶段名
            const line = text.slice(0, m.index).split('\n').length;
            const rec = {
                file: relative(ROOT, f).split('\\').join('/'),
                line,
                why: pat.why,
                text: (lines[line - 1] || '').trim().slice(0, 100),
            };
            add(pat.kind === 'produce' ? S : C, stage, rec);
        }
    }
}

const sKeys = [...S.keys()].sort();
const cKeys = [...C.keys()].sort();
const sMinusC = sKeys.filter((k) => !C.has(k));
const cMinusS = cKeys.filter((k) => !S.has(k));

if (process.argv.includes('--json')) {
    console.log(JSON.stringify({
        allStages: ALL_STAGES,
        S: Object.fromEntries(S), C: Object.fromEntries(C),
        sMinusC, cMinusS,
    }, null, 2));
    process.exit(0);
}

console.log('教程阶段枚举(#190 §0 · 这是分母,不是门)');
console.log(`  类型联合里的阶段总数 |ALL| = ${ALL_STAGES.length}`);
console.log(`  有产生方的阶段     |S| = ${sKeys.length}`);
console.log(`  有消费方的阶段     |C| = ${cKeys.length}`);
console.log(`  登记的写法         = ${PATTERNS.length} 种`
    + `(产生 ${PATTERNS.filter((p) => p.kind === 'produce').length} / 消费 ${PATTERNS.filter((p) => p.kind === 'consume').length})`);
console.log('');
console.log(`S−C(**推得到、却没人消费** ⇒ 教程走到这儿没有下文)${sMinusC.length} 个:`);
for (const k of sMinusC) {
    console.log(`  🔴 ${k}`);
    for (const r of S.get(k)) console.log(`       产生于 ${r.file}:${r.line}  ${r.text}`);
}
console.log('');
console.log(`C−S(**有人读、却从不产生** ⇒ 死引导)${cMinusS.length} 个:`);
for (const k of cMinusS) {
    console.log(`  🔴 ${k}`);
    for (const r of C.get(k)) console.log(`       消费于 ${r.file}:${r.line}  ${r.text}`);
}
console.log('');
console.log('从未出现在任何一侧的阶段(既不产生也不消费):');
const orphan = ALL_STAGES.filter((k) => !S.has(k) && !C.has(k));
for (const k of orphan) console.log(`  .. ${k}`);
console.log('');
console.log('逐阶段明细(手工抽查用):');
for (const k of ALL_STAGES) {
    const s = (S.get(k) || []).length;
    const c = (C.get(k) || []).length;
    console.log(`  ${k.padEnd(26)} 产生 ${String(s).padStart(2)} · 消费 ${String(c).padStart(2)}`);
}
