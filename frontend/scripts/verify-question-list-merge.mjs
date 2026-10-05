#!/usr/bin/env node
/**
 * 判据 · 题单与核心搜索问题合并为一个列表(WO §3.2 · 第六趟)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 * 「未评估」独立成一档 —— 下一个人(含 Deploy 预检)只看退出码,
 * 而把「跳过」印成绿是本仓已经栽过的形状。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PAGE = readFileSync(join(ROOT, 'src/pages/Diagnosis/NewDiagnosis.tsx'), 'utf8');
const EDITOR = readFileSync(join(ROOT, 'src/components/defensiveGeo/QuestionPlanEditor.tsx'), 'utf8');
const PANEL_RAW = readFileSync(join(ROOT, 'src/components/defensiveGeo/LaunchPanel.tsx'), 'utf8');
/**
 * 🔴 **剥掉注释再判**。第一版直接 grep 整个文件,而我在注释里写了
 * 「原来这里是一句死占位『先把问题填好…』」—— **我自己的解释性散文触发了我自己的锁**。
 * 这是本仓 `feedback_prose_mention_trips_string_anchored_locks` 的又一例:
 * 任何"某个词在不在"的检查,分母里必然包含**解释这个检查的文字**。
 */
const stripComments = (src) => src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
const PANEL = stripComments(PANEL_RAW);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, extra = '') => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}${extra ? ' · ' + extra : ''}`);
    if (!cond) bad += 1;
};

// ── A 纯逻辑臂 ──────────────────────────────────────────────────────
const guard = await import(new URL('../src/pages/Diagnosis/launch/planSideGuard.ts', import.meta.url).href)
    .catch(() => null);
if (!guard) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **降级 ⇒ 未评估**:没能真 import .ts,纯逻辑臂一条都没执行。'
        + ' 解法:Node ≥22.6 的 --experimental-strip-types 或 tsx。');
} else {
    const { sideCounts } = guard;
    const D = { modeSide: 'defensive', text: '甲' };
    const O = { modeSide: 'offensive', text: '乙' };
    ok(sideCounts([D, O, O]).offensive === 2, 'A1 合并后按 modeSide 正确分侧');
    ok(sideCounts([D, { modeSide: 'offensive', text: '   ' }]).offensive === 0,
        'A1 反臂:空白题不计数(后端也不认)');
}

// ── B 源码结构臂 ────────────────────────────────────────────────────
// B1 「核心搜索问题」只在 legacy 渲染
ok(/\{!isDefensiveFlow && \(/.test(PAGE) && /核心搜索问题(\(提交字段仍为 keywords\))?/.test(PAGE),
    'B1 「核心搜索问题」整块被 !isDefensiveFlow 包住(hybrid/defensive 不渲染)');
ok(/id="keywords"/.test(PAGE),
    'B1 反臂:legacy 那个输入框**仍在源码里**(把它删了就是动了主动线)');

// B2 迁移:keywords 的行并入时一律标 offensive,且写回各自的源
ok(/fromKeywords: true/.test(PAGE), 'B2 并入的行打了 fromKeywords 标记');
ok(/modeSide: 'offensive' as const/.test(PAGE), 'B2 并入的行一律标 offensive');
ok(/const kw = next\.filter\(\(q\) => q\.fromKeywords\)/.test(PAGE)
    && /const mine = next\.filter\(\(q\) => !q\.fromKeywords\)/.test(PAGE),
    'B2 编辑写回**各自的源**(fromKeywords → keywords,其余 → manualQuestions)');
ok(!/setManualQuestions\(\[\.\.\.migratedFromKeywords/.test(PAGE),
    'B2 反臂:不得把并入的行复制进 manualQuestions(那会造出第二个真相源)');

// ── B3 侧别标签无编辑入口 ───────────────────────────────────────────
/**
 * 🔴 第一版写的是 `/question-side-badge[\s\S]{0,900}(onChange|…)/` —— **只朝后看**。
 *    把 `onChange` 放在 `data-testid` **之前**就整个逃掉(Review 实测:那一发毒 rc=3 不红)。
 *    属性在开标签里是无序的,任何"从某个属性往后数 N 个字符"的窗口都会漏掉它前面的。
 * ⇒ 改成取**整个元素**:从含该 testid 的开标签起点,到与之配对的闭合标签。
 */
function elementAt(src, needle) {
    const at = src.indexOf(needle);
    if (at < 0) return null;
    const open = src.lastIndexOf('<', at);
    if (open < 0) return null;
    const tag = (src.slice(open + 1).match(/^[A-Za-z][A-Za-z0-9]*/) || [])[0];
    if (!tag) return null;
    const gt = src.indexOf('>', at);
    if (gt < 0) return null;
    if (src[gt - 1] === '/') return src.slice(open, gt + 1);          // 自闭合
    let depth = 1;
    let i = gt + 1;
    // 🔴 `'[\s>]'` 在 JS **单引号字符串**里不是合法转义,`\s` 会被吃成 `s`
    //    ⇒ 正则变成 `/<span[s>]/`,**匹配不到带属性的开标签 `<span `**,
    //    深度计数漏数嵌套 ⇒ 元素被截短 ⇒ 藏在后半截的毒逃掉。
    //    当前徽标没有嵌套同名标签,所以自证仍全绿 —— 典型的**潜伏型不咬人**。
    const openRe = new RegExp('<' + tag + '[\\s>]', 'g');
    const closeRe = new RegExp('</' + tag + '>', 'g');
    while (i < src.length && depth > 0) {
        openRe.lastIndex = i; closeRe.lastIndex = i;
        const o = openRe.exec(src);
        const c = closeRe.exec(src);
        if (!c) return src.slice(open);
        if (o && o.index < c.index) { depth += 1; i = o.index + 1; }
        else { depth -= 1; i = c.index + 1; if (depth === 0) return src.slice(open, c.index + tag.length + 3); }
    }
    return src.slice(open);
}
const EDIT_HANDLERS = /onChange|onInput|onKeyDown|onKeyPress|contentEditable|<select|<input|<textarea/;

// 自证:锁必须抓住 Review 那一发(onChange 放在 testid **之前**)。
const POISON_BEFORE = '<span onChange={() => {}} data-testid="question-side-badge">会问</span>';
const POISON_AFTER = '<span data-testid="question-side-badge" onChange={() => {}}>会问</span>';
const CLEAN_BADGE = '<span data-testid="question-side-badge" className="x">会问</span>';
const lockCatches = (src) => EDIT_HANDLERS.test(elementAt(src, 'question-side-badge') ?? '');
ok(lockCatches(POISON_BEFORE), 'B3 自证:onChange 在 testid **之前**必须被抓(第一版就漏在这)');
ok(lockCatches(POISON_AFTER), 'B3 自证:onChange 在 testid 之后必须被抓');
ok(!lockCatches(CLEAN_BADGE), 'B3 自证配对臂:干净徽标必须放行(否则锁是"永远红"不是锁)');

ok(/data-testid="question-side-badge"/.test(EDITOR), 'B3 侧别标签存在');
const badgeEl = elementAt(EDITOR, 'question-side-badge');
ok(badgeEl !== null, 'B3 能取到徽标整个元素(取不到就不是"通过",是没验)');
ok(badgeEl !== null && !EDIT_HANDLERS.test(badgeEl),
    'B3 反臂:徽标元素**整体**无编辑入口(属性顺序无关)');
// 父容器也不能是可编辑的 —— 徽标本身干净但被塞进 contentEditable 里同样等于给了入口。
const liEl = elementAt(EDITOR, '<li key={`${q.source}');
ok(liEl === null || !/contentEditable/.test(liEl),
    'B3 徽标的父容器不是可编辑元素');

// B4 面板不算钱
ok(/draftCount/.test(PANEL), 'B4 常显面板显示题数(即时反馈)');
ok(!/先把问题填好/.test(PANEL), 'B4 「先把问题填好…」那句死占位已从**代码**里移除');
ok(/先把问题填好/.test(PANEL_RAW),
    'B4 配对臂:那句话仍出现在**注释**里(证明剥的是注释,不是把文件读空了)');
ok(!/650|每题\s*100|basePoints\s*\*|\*\s*100/.test(PANEL.split('if (!plan)')[1]?.split('return (')[2] ?? ''),
    'B4 反臂:idle 面板**不含任何写死价钱或算钱表达式**(前端不算钱)');

// ── B5 计价规则:只渲染,不算钱 ─────────────────────────────────────
ok(/pricingRule/.test(PANEL), 'B5 面板渲染服务端下发的计价规则');
ok(!/pricingRule\.[a-zA-Z]+\s*[*+\-/]|\*\s*pricingRule/.test(PANEL),
    'B5 反臂:**不对规则里的数做任何算术**(前端不算钱,总价只来自 run-previews)');
ok(/setPricingRule\(null\)/.test(PAGE) && /\[campaignMode, formData\.brandName\]/.test(PAGE),
    'B5 品牌/模式一变即清缓存(留着旧的 = 显示别的品牌的价)');
ok(/if \(plan\.pricingRule\) setPricingRule/.test(PAGE),
    'B5 只在服务端真给了规则时才缓存(422 不带 ⇒ 不写脏值)');
/**
 * 🔴 **B6 已从这里移走 —— 它放错了仪器。**
 *
 * 「pricingRule 四值来自一次真实响应」是个**运行时**命题;而本脚本是**静态**检查
 * (只读源码与 dist)。放在这里它永远只能是"未评估" —— 那不是诚实,那是**把一条
 * 永远无法在此满足的判据挂在这儿**,读的人还以为总有一天它会变绿。
 *
 * ⇒ 已移到**生产 harness**(`C:\AI-Test\gate8_sweep\prod_sweep_gate8.mjs`),
 *    在那里它能真被满足:打一次真实 preview,抓响应里的四个值并记录观测值。
 * ⇒ 本脚本只保留它能证的那部分(B5 四臂:渲染 / 不做算术 / 变更即清 / 不写脏值)。
 */

// ── C dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ C0 **未构建 ⇒ 未评估**:没有 dist,产物锚一条都没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const c1 = js.includes('question-side-badge');
    const c2 = js.includes('launch-panel-draft-count')
        && js.includes('launch-panel-pricing-rule');
    // 🔴 legacy 那段必须**还在产物里** —— 这是 Review 点名要加的那一臂:
    //    「hybrid/defensive 不渲染」与「legacy 也没了」在源码 diff 上很像。
    const c3 = js.includes('核心搜索问题');
    ok(c1, 'C1 侧别标签锚在产物里');
    ok(c2, 'C2 常显面板题数锚 + 计价规则锚都在产物里');
    ok(c3, 'C3 **legacy 的「核心搜索问题」文案仍在产物里**(证明只是不渲染,不是被删了)');
    if (c1 && c2 && c3) {
        ok(!js.includes('zzq-必然不存在的锚'), 'C4 反臂:编造的锚必须不命中');
    } else {
        notEvaluated += 1;
        console.log('  ⚠️ C4 未评估:C1-C3 未全成立 ⇒ 提取可能整体为空,'
            + '此时「编造锚不命中」不携带任何信息');
    }
}

if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估(要绿必须先构建 / 让 A 组真 import)`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
