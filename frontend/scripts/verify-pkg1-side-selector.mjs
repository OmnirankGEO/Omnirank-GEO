#!/usr/bin/env node
/**
 * 判据 · 包一阶段④ 侧别选择器 + 自动收敛退役(订正八①)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 这两件必须同批,不能只做删除:
 *    迁移来的题硬编码 `offensive`,防守标签下就是侧别不符 ⇒ 后端抛 `plan_side_mismatch`。
 *    自动收敛当初存在的理由就是消化这件事。只删收敛而不改侧别口径,
 *    等于把它当初修的缺陷重新打开。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const EDITOR = strip(rd('src/components/defensiveGeo/QuestionPlanEditor.tsx'));

// ── A 纯逻辑臂:侧别 → 附属字段单源 ─────────────────────────────────
const g = await import(new URL('../src/pages/Diagnosis/launch/planSideGuard.ts', import.meta.url).href)
    .catch(() => null);
if (!g) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **未评估**:没能 import planSideGuard.ts。');
} else {
    const d = g.sideDefaults('defensive');
    const o = g.sideDefaults('offensive');
    ok(d.brandExposure === 'named' && o.brandExposure === 'unnamed',
        'A1 防守=点名(named)/ 增长=没点名(unnamed)');
    ok(d.familyKey !== o.familyKey, 'A2 两侧的 familyKey 不同(否则后端分组分不开)');
    ok(!!d.familyKey && !!o.familyKey, 'A3 正样本臂:两侧都真的给出了值(空值会让 A2 恒真)');
}

// ── B 自动收敛整套退役 ──────────────────────────────────────────────
for (const [sym, why] of [
    ['autoHybridOptOut', '状态'],
    ['autoHybridApplied', '状态'],
    ['intrudingSide', '判定'],
    ['plan-auto-hybrid-undo', '撤销按钮'],
    ['plan-single-side-undo', '撤销按钮'],
]) {
    ok(!PAGE.includes(sym), `B1 收敛${why} ${sym} 已删`);
}
ok(!existsSync(join(ROOT, 'src/pages/Diagnosis/launch/planSideConverge.ts')),
    'B2 收敛模块文件已删(它已零消费方;留着会被下一个人当成"还在用的东西")');
ok(!/setCampaignMode\('hybrid'\)/.test(PAGE),
    'B3 🔴 页面里**不再有**「系统替你切模式」—— 模式只由用户点选');
ok(/onChange=\{setCampaignMode\}/.test(PAGE),
    'B3 配套:标签的 onChange 就是直接设模式,没有副作用');

// ── C 🔴 「题 vs 关键词」彻底分家(⑤b 返修后重锚)────────────────────
/**
 * 🔴 原 C1/C2 锁的是「关键词迁移**只在 hybrid**」。⑤b 返修把**迁移整个拆了**,
 *    于是这两条的前提被一个**正确的修复**推翻 —— 这不是缺陷,是重锚。
 *    (存在锁的前提被正确修复推翻 ⇒ 它会把对的代码判红。)
 *
 *    换锚不换意图,而且两条都比原来**强**:
 *    · C1 意图「防守标签下不许混进 offensive 侧的题」
 *      → 承接为「任何模式下关键词都不再变成题」(限制 ⊂ 不存在)。
 *    · C2 意图「她的输入不许静默消失」
 *      → 承接为「每个写入控件都必须有可见落点」—— 正好钉住返修时抓到的真缺陷:
 *        删掉关键词框之后「使用示例」仍写 formData.keywords,那个字段已无任何 UI,
 *        **她点了屏幕上什么都不会发生**。点了没反应的按钮比没有这个按钮更糟。
 *    · keywords 写入点的分母枚举放在 ⑤b 判据里,不在这里重写一遍 ——
 *      同一个谓词写两处,必有一处没人验。
 */
ok(!/keywordsMigrationOn|migratedFromKeywords/.test(PAGE),
    'C1 🔴 关键词→题的迁移**整个不存在**(比"只在 hybrid"更强:三个标签下都不会发生)');
ok(/const editableQuestions: DraftQuestion\[\] = manualQuestions;/.test(PAGE),
    'C1a 🔴 「她自己的题」只有一个来源 = 题单编辑器,没有第二段被 concat 进来');
const draftCompose = (PAGE.match(/const rawDefensiveDraft: DraftQuestion\[\] = \[[^\]]*\]/) || [])[0] || '';
ok(draftCompose.length > 40,
    'C1b 正样本臂:确实切到了防守草稿的构成式(切空会让下一条恒真)');
ok(!/migrated|formData\.keywords/.test(draftCompose),
    `C1c 🔴 防守草稿的构成里没有任何关键词来源(实得 ${JSON.stringify(draftCompose.slice(0, 110))})`);
/**
 * 🔴 C2 打**行为**不打存在:切出 fillExamples 的实现,看它往哪写。
 *    写 setManualQuestions ⇒ 出现在题单里(看得见 / 计价 / 会被拿去问);
 *    写 setFormData ⇒ 进那个无 UI 的搜索词字段 ⇒ 点了没反应。
 */
const feIdx = PAGE.indexOf('const fillExamples');
const feBlock = feIdx >= 0 ? PAGE.slice(feIdx, feIdx + 800) : '';
ok(feBlock.length > 150, 'C2 正样本臂:切到了「使用示例」的实现(切空会让下面两条不可解读)');
ok(/setManualQuestions/.test(feBlock),
    'C2a 🔴 「使用示例」写进**题单编辑器** —— 点一下就看得见');
ok(!/setFormData/.test(feBlock),
    'C2b 🔴 反臂:它**不再**写那个没有 UI 的 keywords 字段(写了 = 她点了屏幕上什么都不发生)');

// ── C3 🔴 死控件普查:每个输入控件都必须接线 ──────────────────────────
/**
 * 🔴 C2 钉的是「使用示例」**那一个**控件。但它是一**类**缺陷,不是一个:
 *    删掉一个字段之后,任何还在写它的控件都会变成「点了没反应」——
 *    不报错、不空指针、屏幕上也看不出异常,只是什么都不做。
 *    我在返修里已经吃过一次(写进一个没人读的地方),同一天注释里也写清楚了,
 *    但**注释传不出去,门才传得出去**。所以这里做机械枚举,不做点名。
 *
 * 🔴 豁免必须**显式且非死**:`<input type="file">` 结构上不存在受控 value,
 *    是唯一豁免;而豁免项自己必须有 onChange —— 否则「豁免」会退化成
 *    「这一格永远不看」,而那正好是死控件最爱藏的地方。
 */
function scanControls(src) {
    const out = [];
    const re = /<(?:Textarea|Input|textarea|input|Select|select)(?![A-Za-z])/g;
    let m;
    while ((m = re.exec(src)) !== null) {
        let depth = 0; let j = m.index;
        while (j < src.length) {
            const ch = src[j];
            if (ch === '{') depth += 1;
            else if (ch === '}') depth -= 1;
            else if (ch === '>' && depth === 0) break;
            j += 1;
        }
        out.push(src.slice(m.index, j + 1));
    }
    return out;
}
const wired = (t) => (/value=|checked=|defaultValue=/.test(t))
    && (/onChange=|onValueChange=|onCheckedChange=/.test(t));
const isFileInput = (t) => /type="file"/.test(t);

const allControls = [...scanControls(PAGE), ...scanControls(EDITOR)];
ok(allControls.length >= 8,
    `C3 正样本臂:扫到 ${allControls.length} 个输入控件(扫成 0 会让下面两条恒真)`);
const fileInputs = allControls.filter(isFileInput);
const unwired = allControls.filter((t) => !wired(t) && !isFileInput(t));
ok(unwired.length === 0,
    `C3a 🔴 没有任何**无绑定**输入控件 —— 有就是「她往里打字/点它,屏幕上什么都不发生」`
    + `(实得 ${JSON.stringify(unwired.map((t) => t.replace(/\s+/g, ' ').slice(0, 90)))})`);
ok(fileInputs.length === 1 && fileInputs.every((t) => /onChange=/.test(t)),
    `C3b 🔴 唯一豁免(文件输入)自己没死:恰 1 个且带 onChange(实得 ${fileInputs.length} 个)`);

// ── D 侧别选择器 ────────────────────────────────────────────────────
ok(/data-testid="question-side-select"/.test(EDITOR), 'D1 全面测试下自己加的题有侧别选择器');
ok(/isManual && mode === 'hybrid'/.test(EDITOR),
    'D1 门控:只在 hybrid 且是她自己的题时出现(系统题的侧别由生成器定)');
/**
 * 🔴 D2 第一版是**文件级**存在锁:`sideDefaults(side)` 在 addManual 里也有一份,
 *    所以把选择器 onChange 里那半删掉后**全文件仍匹配 ⇒ 仍绿**。
 *    「字符串在文件里」证明不了「它在该在的那个位置」。
 *    改成:①总数必须恰 2(两个使用点各一);②**分别**在各自的块里验。
 */
const SD_RE = /\.\.\.sideDefaults\(side\)/g;
const SD_HITS = (EDITOR.match(SD_RE) || []).length;
ok(SD_HITS === 2, `D2 单源映射恰用在两处(实得 ${SD_HITS}:新建题 + 改侧别)`);
const selIdx = EDITOR.indexOf('question-side-select');
const selBlock = selIdx >= 0 ? EDITOR.slice(selIdx, selIdx + 900) : '';
const amIdx = EDITOR.indexOf('const addManual');
const amBlock = amIdx >= 0 ? EDITOR.slice(amIdx, amIdx + 500) : '';
ok(/\.\.\.sideDefaults\(side\)/.test(selBlock),
    'D2a **改侧别那一处**用了单源映射 —— 只改 modeSide 会让那道题带着上一侧的分组值');
ok(/\.\.\.sideDefaults\(side\)/.test(amBlock),
    'D2b **新建题那一处**也用同一份映射(两处各写一份必漂,而屏幕上都看起来正常)');
ok(selBlock.length > 100 && amBlock.length > 100,
    'D2c 正样本臂:两个块都真的切到了内容(切空会让上面两条不可解读)');
ok(!/familyKey: side === 'defensive' \? 'trust_reliability'/.test(EDITOR),
    'D2d 反臂:内联的那份映射已删(否则单源等于没建立)');

// ── E 🔴 红臂:基线 e19fe1496 上都不成立 ─────────────────────────────
const BASE = 'e19fe1496';
let basePage = null; let baseEditor = null;
try {
    const show = (p) => execFileSync('git', ['show', `${BASE}:${p}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    basePage = show('frontend/src/pages/Diagnosis/NewDiagnosis.tsx');
    baseEditor = show('frontend/src/components/defensiveGeo/QuestionPlanEditor.tsx');
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (basePage && baseEditor) {
    ok(/plan-auto-hybrid-undo/.test(basePage), 'E1 红臂:改动前那两颗撤销按钮确实在');
    ok(/setCampaignMode\('hybrid'\)/.test(basePage), 'E2 🔴 红臂:改动前确实有「系统替你切模式」');
    ok(/const keywordsMigrationOn = isDefensiveFlow/.test(basePage), 'E3 红臂:改动前迁移口径是旧的');
    ok(!/question-side-select/.test(baseEditor), 'E4 红臂:改动前没有侧别选择器');
    ok(/keywords: applyExamples\(/.test(basePage),
        'E6 红臂:基线上「使用示例」确实写 formData.keywords —— '
        + '那时关键词框还在,写了看得见;框删掉之后同样的写法就变成点了没反应');
    ok(/manualIndex/.test(baseEditor),
        'E5 配对臂:基线里本来就有的锚确实在(否则 E4 的"没有"只是取到空内容)');
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const f1 = js.includes('question-side-select');
    ok(f1, 'F1 侧别选择器锚进了产物');
    ok(!js.includes('plan-auto-hybrid-undo'), 'F2 🔴 退役的按钮**不在产物里**(源码删了但产物没重建 = 线上还在)');
    if (f1) ok(!js.includes('zzq-必然不存在的锚'), 'F3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ F3 未评估:F1 未成立 ⇒ 提取可能整体为空'); }
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
