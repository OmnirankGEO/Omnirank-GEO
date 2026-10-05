#!/usr/bin/env node
/**
 * 判据 · #165 说的和做的要一致(F3 / F4 / F11)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 三条表面上是三个页面的小文案,根其实是**同一个**:
 *    前端把「系统预填 / 没有问题」说成「**你**填了 / 你可以先看价」。
 *      F3 「点下面的按钮看这次要花多少算力」—— 那个分支里本面板一颗按钮都没有,
 *          她眼里"下面的按钮"是页面自己那颗「开始品牌体检 · 650 算力」,一点就扣;
 *      F4 离开体检页弹浏览器原生空白框 —— 判据是"品牌名有没有值",
 *          而品牌名是系统预填的,她一个字没动也算"未保存";
 *      F11 「题单 已填」在 0 道时显示(ok 判的是"没有超长题"),
 *          「高级选项 已填」在别名/竞品是从客户档案带进来时显示。
 *
 * 🔴 **F3 没有按工单原话做**,理由钉在这里:工单写「按元指令 2 加一步
 *    『确认扣 650 算力』」,而元指令 2 的原文(CLAUDE.md)是
 *    「**不弹 Dialog**;按钮直接标价;大额(>=1000 分)走 toast 5s 反悔」——
 *    它禁止的正是那一步确认,且 650 < 1000 连 toast 档都不到。
 *    那颗按钮本来就把价写在脸上,已经合规;骗人的是那句指路。
 *    所以本单只改文案。D3 把「按钮自带价」这个**元指令 2 的正面要求**钉住,
 *    而不是去钉「不许有 dialog」—— 后者会把 Owner 将来改口径的路堵死。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/^\s*\/\/.*$/gm, blank);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const PANEL = decomment(rd('src/components/defensiveGeo/LaunchPanel.tsx'));
const LAUNCH = decomment(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));

// ══ D F3:不许指着一颗"点了就扣"的按钮说"点它看价" ══════════════════════
console.log('D F3 指路词');
{
    ok(!/点下面的按钮看这次/.test(PANEL),
        'D1 🔴 `!plan` 分支里没有「点下面的按钮看…」—— 那个分支本面板不渲染任何按钮');
    ok(/题单生成后,这里显示这次/.test(PANEL),
        'D2 换上的是"价会出现在哪"而不是"去点什么"(空着不说 = 另一种不告诉她)');
    ok(/看看要花多少算力/.test(PANEL),
        'D3a 反臂:真实存在的那颗**免费预览**按钮标签没被顺手改掉(它被 10+ 处浏览器判据点名)');
    ok(/data-testid="launch-price-points"/.test(LAUNCH),
        'D3b 🔴 元指令 2 的正面要求仍成立:启动按钮**自带价**('
        + '不弹 Dialog / 按钮直接标价 / >=1000 才走 toast)');
}

// ══ E F4:守卫只在"她真动过手"之后生效 ═════════════════════════════════
console.log('E F4 未保存守卫');
{
    const m = LAUNCH.match(/const hasUnsaved = ([^;]+);/);
    ok(!!m, 'E1a 取得到 hasUnsaved 的表达式');
    const expr = m ? m[1] : '';
    ok(/userEditedForm/.test(expr),
        'E1b 🔴 守卫条件含"她动过手",不是只看字段**有没有值**(值是系统预填的)', expr.trim());
    // 🔴 分母同集合:hasUnsaved 用到哪些 formData 字段,就得有哪些字段的输入把手置位。
    //    少一个 ⇒ 那个字段改了却不守卫。这条比"有没有写 setUserEditedForm"强。
    const fields = [...expr.matchAll(/formData\.([A-Za-z][\w]*)/g)].map((x) => x[1]);
    ok(fields.length >= 2, 'E2a 分母非空(抽不到字段 = 表达式形状变了,回来重看)', fields.join(','));
    const missing = fields.filter((f) => {
        const re = new RegExp('setUserEditedForm\\(true\\)[\\s\\S]{0,220}' + f + ':\\s*e\\.target\\.value');
        return !re.test(LAUNCH);
    });
    ok(missing.length === 0,
        'E2b 🔴 hasUnsaved 用到的每个字段,它的用户输入把手都会置位(漏一个 = 改了不守卫)',
        missing.join(',') || fields.join(','));
    ok(/useUnsavedWarning\(hasUnsaved\)/.test(LAUNCH), 'E3 守卫仍接在 hasUnsaved 上');
}

// ══ F F11:状态词只陈述状态,不替用户宣称"你填了" ═══════════════════════
console.log('F F11 状态词');
{
    ok(/okLabel: liveQuestionCount === 0 \? '等 AI 出题'/.test(LAUNCH),
        'F1 🔴 「题单」0 道时说「等 AI 出题」,不说「已填」');
    ok(/ok: ownQuestions\.tooLong\.length === 0/.test(LAUNCH),
        'F2 🔴 反臂:ok 的**判据没动** —— 空题集本来就合法(#143④ 撤回订正),'
        + '这一单改的是词不是判据');
    ok(/okLabel/.test(LAUNCH) && /\('okLabel' in it && it\.okLabel\)/.test(LAUNCH),
        'F3 渲染真的用了 okLabel(否则 F1 只是写了个没人读的字段)');
    // 高级选项那处:别名/竞品是从客户档案带进来的
    const adv = LAUNCH.slice(LAUNCH.indexOf('data-testid="advanced-row"'),
        LAUNCH.indexOf('data-testid="advanced-row"') + 1600);
    ok(adv.length > 100, 'F4a 取到「高级选项」那一段(取不到 = 锚没了,回来重看)');
    ok(!/已填/.test(adv), 'F4b 🔴 「高级选项」不再说「已填」(内容多半是档案带进来的)');
    ok(/有内容/.test(adv), 'F4c 换上的词只陈述状态');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`\n未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('\n全部通过');
    process.exitCode = 0;
}
