#!/usr/bin/env node
/**
 * 判据 · #153-A 超长题必须出声,并且拦得住。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 缺陷的形态(C 取证):超长题在防守线**整单进不去**,而且**不在提交时拦** ——
 *    后台派发时静默重试到退款。用户点确认 ⇒ 冻算力 ⇒ 什么都没发生 ⇒ 退款,
 *    **全程屏幕上没有一个字**。而前端本来就有那句提示,却写着 `{isManual && …}`,
 *    把自己挡在系统/AI 题之外。
 *
 * 🔴 所以本单钉两件事,缺一不可:
 *    ① 提示对**任何来源**的题都出声(不再只对手写题);
 *    ② 有超长题时**冻结 CTA 不可点**,并显示**同一句**话。
 *    只做 ① 是「红字挂在那儿没人理,她照点、照冻、照退款」。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const require_ = createRequire(import.meta.url);

let bad = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

async function importTs(relPath) {
    try {
        const ts = require_('typescript');
        const src = readFileSync(join(ROOT, relPath), 'utf8');
        if (/from\s+['"]\.{1,2}\//.test(src)) return { ok: false, err: `${relPath} 有相对 import` };
        const out = ts.transpileModule(src, {
            compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
            fileName: relPath,
        }).outputText;
        return { ok: true, mod: await import('data:text/javascript;base64,' + Buffer.from(out, 'utf8').toString('base64')) };
    } catch (e) { return { ok: false, err: String((e && e.message) || e) }; }
}

// ══ M 元判据 ═════════════════════════════════════════════════════════
{
    const probe = await importTs('src/pages/Diagnosis/launch/questionTooLong.ts');
    if (!probe.ok) {
        console.log('  FAIL M0 🔴 加载不了 .ts 判据模块,下面的行为锁一条都跑不了:' + probe.err);
        console.log('       修法:确认 devDependency `typescript` 已装(纯 JS 转译);');
        console.log('       **不要**改回直接 import(".ts") —— 构建镜像是 node:20-alpine。');
        process.exitCode = 1;
        process.exit(1);
    }
    console.log(`  OK  M1 元判据:.ts 可加载(typescript ${require_('typescript').version})`);
}

const EDITOR = strip(rd('src/components/defensiveGeo/QuestionPlanEditor.tsx'));
const PANEL = strip(rd('src/components/defensiveGeo/LaunchPanel.tsx'));
const COPY = rd('src/lib/defensiveGeoCopy.ts');
const M = (await importTs('src/pages/Diagnosis/launch/questionTooLong.ts')).mod;

// ══ A 🔴 提示对任何来源的题都出声 ═════════════════════════════════════
console.log('A 提示不再只对手写题');
{
    ok(/\{isQuestionTooLong\(q\.text\) && \(/.test(EDITOR),
        'A1 🔴 超长提示的条件里**没有** isManual —— 系统/AI 出的题超长也要标红');
    ok(!/isManual && isQuestionTooLong/.test(EDITOR),
        'A2 反臂:不许退回 `isManual && …`(那正是把提示挡在系统题之外的那一版)');
    ok(/isManual \? \(|\{isManual &&/.test(EDITOR),
        'A3 正样本臂:`isManual` 本身还在别处用(否则 A2 可能只是因为这个词整个消失了)');
}

// ══ B 🔴 有超长题 ⇒ 冻结 CTA 不可点 + 同一句话 ═════════════════════════
console.log('B 拦得住,并且说得出为什么');
{
    ok(/\|\| !!tooLongHit\}/.test(PANEL),
        'B1 🔴 CTA 的 disabled 里含 tooLongHit —— **能拦住就别只提示**');
    ok(/data-testid="question-too-long-block"/.test(PANEL) && /\{tooLongMsg\}/.test(PANEL),
        'B2 灰按钮旁边有一句人话(灰按钮 + 零文案是这条链上原本的样子)');
    ok(/DEFGEO_COPY\.questionTooLong/.test(PANEL),
        'B3 🔴 句子取自**跨层注册表**,不是本文件自己写的 —— 两份文案必漂');
    ok(!/太长了|上限 100 字/.test(PANEL),
        'B4 反臂:面板里**没有**自带的句子(自带就是第二份真相)');
    ok(/CUSTOM_QUESTION_MAX_CHARS/.test(PANEL),
        'B5 上限取单源常量(与服务端 validator 同解),不在这里抄一个数');
}

// ══ C 🔴 注册表键与模板在(生成物,不手改) ════════════════════════════
console.log('C 跨层文案');
{
    ok(/DEFGEO_COPY_REGISTRY_VERSION = "defensive-geo-copy-v11"/.test(COPY),
        'C1 生成物版本 v11(与后端 registry 同步声明)');
    ok(/questionTooLong:/.test(COPY), 'C2 键在');
    const m = COPY.match(/questionTooLong:\s*"([^"]*)"/);
    ok(!!m, 'C3 取得到模板');
    if (m) {
        const tpl = m[1];
        for (const ph of ['{ordinal}', '{chars}', '{limit}', '{excerpt}']) {
            ok(tpl.includes(ph), `C4 模板含占位符 ${ph}`);
        }
    }
}

// ══ D 🔴 纯逻辑行为(真调) ═══════════════════════════════════════════
console.log('D 行为臂');
{
    const tplM = COPY.match(/questionTooLong:\s*"([^"]*)"/);
    const tpl = tplM ? tplM[1] : '';
    const hit = M.findTooLongQuestion([{ text: '短' }, { text: 'x'.repeat(120) }], 100);
    ok(!!hit && hit.ordinal === 2 && hit.chars === 120,
        'D1 找到**第一道**超长题,序号从 1 起(给人看的序号)', JSON.stringify(hit && hit.ordinal));
    ok(M.findTooLongQuestion([{ text: '短' }, { text: 'y'.repeat(100) }], 100) === null,
        'D2 🔴 边界:恰好 100 字**不算**超(上限是 <=,与服务端同解)');
    ok(M.findTooLongQuestion([], 100) === null, 'D3 空题单 ⇒ null');
    const msg = M.questionTooLongMessage(tpl, hit, 100);
    ok(!M.hasUnresolvedPlaceholder(msg),
        '🔴 D4 成句里**没有残留占位符** —— 少给一个,用户就会看到字面的 {excerpt}', msg.slice(0, 40));
    ok(M.hasUnresolvedPlaceholder(M.formatCopyTemplate(tpl, { ordinal: 1 })),
        'D5 正样本臂:少给占位符时**检得出来**(否则 D4 是空断言)');
    ok(msg.includes('120') && msg.includes('100'),
        'D6 真实数字进了句子(不是把模板原样显示)');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过`);
    process.exitCode = 1;
} else {
    console.log('\n全部通过');
    process.exitCode = 0;
}
