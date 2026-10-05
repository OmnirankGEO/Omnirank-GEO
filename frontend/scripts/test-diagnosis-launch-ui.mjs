/**
 * test-diagnosis-launch-ui.mjs —— 品牌体检发起页 UI 升级的锁
 * (工单 WO_DIAGNOSIS_LAUNCH_UI_2026-08-05 · 2026-08-05)
 *
 * ## 判据打在哪
 *
 * 工单 §3 明写「断言打 AST/渲染结果,不打源码串」。所以:
 *   · TSX 侧走 TypeScript 编译器的真 AST(`ts.createSourceFile` + 遍历),不 grep 源码;
 *   · Python 侧走 `ast.parse`(起一个 python 子进程吐 JSON),不 grep `server.py`;
 *   · 纯函数走**真跑**(esbuild 转译后 import 真模块,喂输入看输出);
 *   · 断点是否真编译进 CSS,读 `vite build` 的真产物 `dist/assets/index-*.css`。
 *
 * ## 每条锁配了反向对照
 *
 * 「必须命中」旁边一定有一个「必须不命中」——否则判据可能恒真。
 * (2026-08-02 起连栽多次:断言命中的是解释它的注释 / 子查询让断言变弱锁 / 恒红恒真都废。)
 *
 * 用法:node scripts/test-diagnosis-launch-ui.mjs   (在 frontend/ 下跑)
 */
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';
import esbuild from 'esbuild';

const root = process.cwd();
const PAGE = path.join(root, 'src/pages/Diagnosis/NewDiagnosis.tsx');
const STEPBAR = path.join(root, 'src/pages/Diagnosis/launch/LaunchStepBar.tsx');
const STEPS = path.join(root, 'src/pages/Diagnosis/launch/launchSteps.ts');

let failed = 0;
const results = [];
function check(name, ok, detail = '') {
    results.push({ name, ok, detail });
    if (!ok) failed++;
    console.log(`${ok ? '  ✅' : '  🔴'} ${name}${detail ? ` — ${detail}` : ''}`);
}
function section(title) { console.log(`\n=== ${title} ===`); }

// ---------------------------------------------------------------- AST 工具
function parseTsx(file) {
    const src = fs.readFileSync(file, 'utf8');
    return ts.createSourceFile(file, src, ts.ScriptTarget.Latest, /*setParentNodes*/ true, ts.ScriptKind.TSX);
}
function walk(node, fn) {
    fn(node);
    node.forEachChild(child => walk(child, fn));
}

/** 从 AST 收集 `payload.X = ...` 与 `const payload = { X: ... }` 的全部字段名。 */
function collectSubmitFields(sf) {
    const fields = new Set();
    walk(sf, node => {
        // payload.brand_display_names = ...
        if (ts.isBinaryExpression(node)
            && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
            && ts.isPropertyAccessExpression(node.left)
            && ts.isIdentifier(node.left.expression)
            && node.left.expression.text === 'payload') {
            fields.add(node.left.name.text);
        }
        // const payload: any = { brand_name: ..., ... }
        if (ts.isVariableDeclaration(node)
            && ts.isIdentifier(node.name)
            && node.name.text === 'payload'
            && node.initializer
            && ts.isObjectLiteralExpression(node.initializer)) {
            for (const prop of node.initializer.properties) {
                if (prop.name && (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name))) {
                    fields.add(prop.name.text);
                }
            }
        }
    });
    return fields;
}

/** 从 AST 收集所有 JSX 属性 `data-testid` 的字面量值。 */
function collectTestIds(sf) {
    const ids = new Set();
    walk(sf, node => {
        if (ts.isJsxAttribute(node) && node.name.getText() === 'data-testid') {
            const init = node.initializer;
            if (init && ts.isStringLiteral(init)) ids.add(init.text);
        }
    });
    return ids;
}

/** 把一个 className 属性节点摊平成原子类名数组(只取字符串字面量,不碰注释)。 */
function classesOfAttr(attr) {
    const init = attr.initializer;
    const strings = [];
    if (init && ts.isStringLiteral(init)) strings.push(init.text);
    if (init && ts.isJsxExpression(init) && init.expression) {
        walk(init.expression, n => {
            if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) strings.push(n.text);
            if (ts.isTemplateHead(n) || ts.isTemplateMiddle(n) || ts.isTemplateTail(n)) strings.push(n.text);
        });
    }
    return strings.flatMap(s => s.split(/\s+/)).filter(Boolean);
}

/**
 * 收集所有 JSX className 里出现过的原子类名。
 * 🔴 只收 className 属性下的字符串,**不收注释、不收普通字符串常量** ——
 *    2026-08-05 媒体榜那单栽在这:语义红线锁抓到的是我自己写的那句注释。
 */
function collectClassNames(sf) {
    const classes = new Set();
    walk(sf, node => {
        if (!ts.isJsxAttribute(node) || node.name.getText() !== 'className') return;
        for (const c of classesOfAttr(node)) classes.add(c);
    });
    return classes;
}

/**
 * 只收**布局骨架元素**(按 data-testid 点名)身上的 className。
 * 🔴 为什么要限定范围:全文扫会把 `sm:max-w-[10rem]`(品牌下拉里那段元信息的截断宽度)
 *    也算成"布局挂在裸视口断点上" —— 那是文本截断,不是三档 layout,判成红是误报。
 *    工单 §2 管的是**三档 layout**,所以判据也只看承载 layout 的那几个元素。
 */
function collectLayoutClassNames(sf, testIds) {
    const byId = new Map();
    walk(sf, node => {
        if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return;
        let id = null; let classAttr = null;
        for (const attr of node.attributes.properties) {
            if (!ts.isJsxAttribute(attr)) continue;
            const n = attr.name.getText();
            if (n === 'data-testid' && attr.initializer && ts.isStringLiteral(attr.initializer)) id = attr.initializer.text;
            if (n === 'className') classAttr = attr;
        }
        if (id && testIds.includes(id) && classAttr) {
            byId.set(id, (byId.get(id) || []).concat(classesOfAttr(classAttr)));
        }
    });
    return byId;
}

/** 判断某个 JSX 元素是否是某个带指定 className 片段的元素的后代。 */
function isDescendantOfClass(node, needle) {
    for (let p = node.parent; p; p = p.parent) {
        if (ts.isJsxElement(p) || ts.isJsxSelfClosingElement(p)) {
            const open = ts.isJsxElement(p) ? p.openingElement : p;
            for (const attr of open.attributes.properties) {
                if (!ts.isJsxAttribute(attr) || attr.name.getText() !== 'className') continue;
                if (attr.getText().includes(needle)) return true;
            }
        }
    }
    return false;
}

// ---------------------------------------------------------------- 0. 判据可用性
section('0. 判据可用性(先证判据不是空的,再谈结论)');
for (const f of [PAGE, STEPBAR, STEPS]) {
    check(`存在:${path.relative(root, f)}`, fs.existsSync(f));
}
if (failed) { console.log('\n🔴 前端前置文件缺失,后面的检查全部会空即通过,直接 abort'); process.exit(1); }

// 🔴 [WO-LATENT-TRAPS §3 · 2026-08-17] 原来这里有个 §1(提交字段集 ⊆ server.py 的
//   DiagnosisRequest)。它引后端文件,而 Dockerfile 的 frontend-builder 阶段只
//   `COPY frontend/ ./` —— 够不到 server.py,于是它在**每次镜像构建里都大声 SKIP**:
//   打印「这不是通过,是没跑」,然后 exit 0。构建绿 ≠ 判据过,而大家只看得到构建绿。
//   §1 已整条挪到后端 pytest(前后端源码都够得到,没有 SKIP 分支可走):
//     tests/test_diagnosis_launch_request_model_lock_2026_08_17.py
//   字段抽取器同源留在 JS 侧:frontend/scripts/dump-diagnosis-submit-fields.mjs
//   本文件自此**不再引用任何后端文件**。
const pageSf = parseTsx(PAGE);
const submitFields = collectSubmitFields(pageSf);
// [⑤b 2026-09-05] 扫描范围**显式扩到题单编辑器**。
//   原来只扫 NewDiagnosis.tsx;而实时计数这个锚随「唯一题源」搬进了 QuestionPlanEditor,
//   锚在别的文件里 ⇒ 这把尺子看不见它,报的是「锚不存在」而不是「锚不对」——
//   两者读数同形。锚必须在**能被这把尺子看到的层**里验。
const EDITOR_SRC = fs.readFileSync(path.join(root, 'src/components/defensiveGeo/QuestionPlanEditor.tsx'), 'utf8');
const testIds = collectTestIds(pageSf);
for (const m2 of EDITOR_SRC.matchAll(/data-testid="([^"]+)"/g)) testIds.add(m2[1]);
const classNames = collectClassNames(pageSf);

check('AST 抽出的提交字段集非空', submitFields.size >= 5, `${submitFields.size} 个:${[...submitFields].join(',')}`);
check('AST 抽出的 data-testid 非空', testIds.size >= 5, `${testIds.size} 个`);
check('AST 抽出的 className 原子类非空', classNames.size >= 50, `${classNames.size} 个`);
// 抽取器自检:注释里的类名不能被当成真类名收进来。
// 🔴 这里必须是**真自检**,不是写一句"口径说明"就算 —— 2026-08-05 已经栽过
//    「判据后跟死结论串 = 自造恒真」。所以拿一段合成源码真跑一遍抽取器。
{
    const probe = ts.createSourceFile('probe.tsx', `
        // 这行注释里写了 lg:sticky 和 lg:grid-cols-2,抽取器不许把它们当类名
        /* 块注释里也写一次 lg:sticky */
        const X = () => <div className="真类名-A @container">{/* JSX 注释里 lg:sticky */}</div>;
    `, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const probed = collectClassNames(probe);
    check('抽取器自检:注释里的 lg:sticky 不被收', !probed.has('lg:sticky'), [...probed].join(' '));
    check('抽取器自检:className 里的类确实被收(否则上一条是空即通过)',
        probed.has('真类名-A') && probed.has('@container'));
}

// ---------------------------------------------------------------- 2. 响应式走容器查询,不走裸视口
section('2. 响应式必须走容器查询(工单 §2)');
check('页面里有 @container 查询锚点', classNames.has('@container'));
// 🔴 「页面里某处有 @container」是弱锁:主列自己也带 @container,摘掉外层锚点它照样绿
//    (变异 M2 第一版就是这么活下来的)。真判据是:限宽容器 / 两栏栅格 **各自的祖先里**
//    必须有一个 @container —— 否则它们身上那些 @min-[880px] 全是没人应答的死类。
{
    const needAncestor = ['diagnosis-launch-shell', 'launch-grid'];
    const found = new Map();
    walk(pageSf, node => {
        if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return;
        for (const attr of node.attributes.properties) {
            if (!ts.isJsxAttribute(attr) || attr.name.getText() !== 'data-testid') continue;
            if (!attr.initializer || !ts.isStringLiteral(attr.initializer)) continue;
            if (needAncestor.includes(attr.initializer.text)) {
                found.set(attr.initializer.text, isDescendantOfClass(node, '@container'));
            }
        }
    });
    for (const id of needAncestor) {
        check(`${id} 的祖先里有 @container 查询锚点`, found.get(id) === true,
            found.has(id) ? '' : '连这个元素都没找到 —— 判据空即通过');
    }
}
const containerVariants = [...classNames].filter(c => /^@(min|max)-\[\d+px\]:/.test(c));
check('用了 @min-/@max- 容器断点', containerVariants.length >= 6, `${containerVariants.length} 条`);

// 布局骨架(限宽容器 / 两栏栅格 / 右栏 / CTA)不许再挂裸视口断点
const LAYOUT_IDS = ['diagnosis-launch-shell', 'launch-grid', 'launch-aside', 'launch-cta'];
const layoutClasses = collectLayoutClassNames(pageSf, LAYOUT_IDS);
check('四个布局骨架元素都抓到了(否则下面恒真)', LAYOUT_IDS.every(id => (layoutClasses.get(id) || []).length > 0),
    LAYOUT_IDS.map(id => `${id}:${(layoutClasses.get(id) || []).length}`).join(' '));
const LAYOUT_RE = /^(sm|md|lg|xl|2xl):(grid-cols-|max-w-|sticky$|top-)/;
const viewportLayout = [...layoutClasses.values()].flat().filter(c => LAYOUT_RE.test(c));
check('布局骨架没有挂在裸视口断点上', viewportLayout.length === 0,
    viewportLayout.length ? `还挂着:${viewportLayout.join(', ')}` : '0 条');
for (const id of LAYOUT_IDS) {
    const cs = layoutClasses.get(id) || [];
    check(`${id} 用的是容器断点`, cs.some(c => c === '@container' || /^@(min|max)-\[\d+px\]:/.test(c)),
        cs.filter(c => c.startsWith('@')).slice(0, 3).join(' ') || '一条容器类都没有');
}
// 🔴 [#125 裁定乙 2026-09-06 重锚] 原锁是「CTA 贴底相关类**都带 pointer-fine 守卫**」,
//    它的前提(存在贴底类)被一个**决定**推翻了:那组类已整组删除,CTA 在任何档、
//    任何指针类型下都留在流里。原锁作者防了空分母(「一条都没有 ⇒ 判据会空即通过」),
//    所以它现在诚实地红 —— 但红的是**过期的锚**,不是缺陷。
//    承接后的锁**方向反过来,而且更强**:一条贴底类都不许有。
//    删它的理由不是它有害,是它让首屏读数有两个解释(2026-09-06 生产实测:
//    容器 y=642 h=125 ⇒ bottom 正好 767,是贴底不是自然流 —— 说不清是重排还是 sticky 的功劳)。
{
    const cta = layoutClasses.get('launch-cta') || [];
    const PIN_RE = /(^|:)(sticky|fixed|bottom-0)$/;
    const pinned = cta.filter(c => PIN_RE.test(c));
    check('CTA className 一条贴底/固定类都没有(任何变体前缀都算)', pinned.length === 0,
        pinned.join(' ') || `0 条 · 共扫 ${cta.length} 个 class`);
    check('正样本臂:确实抓到了 launch-cta 的 class(抓 0 会让上一条恒真)', cta.length >= 2,
        cta.slice(0, 4).join(' '));
    check('反向对照:判据认得出带守卫的贴底类(它曾经就长这样)',
        ['@max-[880px]:pointer-fine:sticky', '@max-[880px]:pointer-fine:bottom-0', 'fixed']
            .every(c => PIN_RE.test(c)));
    check('反向对照:判据不误伤普通类', !PIN_RE.test('@max-[880px]:order-2') && !PIN_RE.test('space-y-2'));
}
// 反向对照:判据认得出裸视口布局类(拿底那一版真实存在过的 lg:sticky 试)
check('反向对照:LAYOUT_RE 认得 lg:sticky / lg:grid-cols-[x] / md:max-w-xl',
    LAYOUT_RE.test('lg:sticky') && LAYOUT_RE.test('lg:grid-cols-[minmax(0,1fr)_340px]') && LAYOUT_RE.test('md:max-w-xl'));
// 反向对照:判据不该把非布局类(如 md:p-6 / sm:flex-row)误判成布局类
check('反向对照:LAYOUT_RE 不误伤 md:p-6 / sm:flex-row',
    !LAYOUT_RE.test('md:p-6') && !LAYOUT_RE.test('sm:flex-row'));

// ---------------------------------------------------------------- 3. IA 六处锚点
section('3. IA 六处锚点在位(工单 §1)');
const REQUIRED_IDS = [
    ['diagnosis-launch-shell', '限宽内容容器'],
    ['launch-grid', '两栏栅格'],
    ['launch-aside', '右栏'],
    ['launch-checklist', '启动检查'],
    ['outcome-list', '本次会得到'],
    ['launch-cta', 'CTA 区'],
    ['launch-submit', '开始品牌体检按钮'],
    // [⑤b 2026-09-05] 原锚 `question-count` 挂在旧「核心搜索问题」框上,那个框已删 ⇒
    //   锚的**前提**被一次正确的改动推翻。意图不变(她要随时看得见这次会跑几道题),
    //   继任锚 = 题单编辑器里的 `plan-running-count`,且它比原锚**强**:
    //   原锚数的是输入框里的行数,继任锚数的是**实际要跑的题数**(订正十二三分支之后那个数)。
    ['plan-running-count', '实时计数(实际要跑的题数)'],
    ['collapsed-rows', '高级选项+补充材料两条折叠行'],
    ['advanced-row', '高级选项折叠行'],
    ['materials-row', '补充材料折叠行'],
];
for (const [id, label] of REQUIRED_IDS) check(`锚点 ${id}(${label})`, testIds.has(id));
// 反向对照:不存在的锚点必须判红
check('反向对照:虚构锚点不在集合里', !testIds.has('anchor-that-must-not-exist'));

// 三步进度条 / 示例卡是子组件,查它们被真引用(AST:JSX 元素名)
const jsxNames = new Set();
walk(pageSf, node => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        jsxNames.add(node.tagName.getText());
    }
});
check('页面里真渲染了 LaunchStepBar(三步进度条)', jsxNames.has('LaunchStepBar'));
// 🔴 [#149 2026-09-08] 「页面里真渲染了 QuestionExampleCard」**按 Review #149 裁定退役**。
//    L3 不变式中「示例卡必须真渲染」随本单退役:AI 出的题直接进题单之后,
//    示例卡的职责被题单本身接管,组件与其模块一并删除。
//    这是**被裁定退役**,不是改代码时顺手拿掉一条挡路的判据。
check('反向对照:虚构组件名不在 JSX 名字集合里', !jsxNames.has('ComponentThatMustNotExist'));

// ---------------------------------------------------------------- 4. fixed 弹窗不能落进 @container 子树
section('4. 全屏弹窗必须留在 @container 之外(contain:layout 会让 fixed 相对容器定位)');
let fixedInsideContainer = [];
walk(pageSf, node => {
    if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return;
    for (const attr of node.attributes.properties) {
        if (!ts.isJsxAttribute(attr) || attr.name.getText() !== 'className') continue;
        const txt = attr.getText();
        if (!/\bfixed\b/.test(txt) || !/\binset-0\b/.test(txt)) continue;
        if (isDescendantOfClass(node, '@container')) {
            const { line } = pageSf.getLineAndCharacterOfPosition(node.getStart());
            fixedInsideContainer.push(line + 1);
        }
    }
});
check('没有 fixed inset-0 元素落在 @container 子树里', fixedInsideContainer.length === 0,
    fixedInsideContainer.length ? `行号:${fixedInsideContainer.join(',')}` : '0 处');
// 反向对照:页面里确实存在 fixed inset-0 元素(否则上面是"空即通过")
let fixedTotal = 0;
walk(pageSf, node => {
    if (!ts.isJsxOpeningElement(node) && !ts.isJsxSelfClosingElement(node)) return;
    for (const attr of node.attributes.properties) {
        if (ts.isJsxAttribute(attr) && attr.name.getText() === 'className'
            && /\bfixed\b/.test(attr.getText()) && /\binset-0\b/.test(attr.getText())) fixedTotal++;
    }
});
check('反向对照:页面里确实有 fixed inset-0 弹窗可供判定', fixedTotal >= 2, `${fixedTotal} 个`);

// ---------------------------------------------------------------- 5. 纯函数真跑
section('5. 示例问题 / 解析 / 步骤 的纯函数行为(真 import 真跑)');
const outDir = path.join(root, 'node_modules/.cache/diaglaunch-lock');
fs.mkdirSync(outDir, { recursive: true });

// 🔴 [#149 裁定退役] 原 5.1/5.2/5.3 共 12 条 —— 靶是 `searchQuestionExamples.ts`
//    (5.1 R6 fail-soft:登记名录整串不许拼进题面;5.2 解析不静默丢行;5.3 一键填入去重)。
//    该模块随示例卡一并删除。普查结论:全前端**没有别的路径**把行业登记名拼进题面
//    (`distillTradeWord`/`QUESTION_MAX_LEN` 只此一处;另两条产题面的路径 ——
//     防守模板拼 brandName、记录回填/手输 —— 都不碰 industry)⇒ 随模块退役,不重锚。
//    🔴 另:5.2/5.3 测的 `parseSearchQuestions`/`applyExamples` 在 src 里**零调用点**
//    (只被 import 从不被用),它们测的是死代码。

// 5.4 resolveLaunchStep 三态(纯逻辑单独一个模块,不用 bundle React 依赖树)
const stepBundle = path.join(outDir, 'launchSteps.mjs');
esbuild.buildSync({ entryPoints: [STEPS], outfile: stepBundle, format: 'esm', bundle: true, platform: 'neutral' });
const stepMod = await import(pathToFileURL(stepBundle).href + `?t=${fs.statSync(stepBundle).mtimeMs}`);
check('步骤:品牌名缺 → 第 1 步', stepMod.resolveLaunchStep({ brandName: ' ', industry: '科技', questionCount: 3 }) === 1);
check('步骤:基础信息齐但没问题 → 第 2 步', stepMod.resolveLaunchStep({ brandName: 'A', industry: '科技', questionCount: 0 }) === 2);
check('步骤:都齐 → 第 3 步', stepMod.resolveLaunchStep({ brandName: 'A', industry: '科技', questionCount: 1 }) === 3);
check('反向对照:三步不是恒定同一个值',
    new Set([
        stepMod.resolveLaunchStep({ brandName: '', industry: '', questionCount: 0 }),
        stepMod.resolveLaunchStep({ brandName: 'A', industry: 'B', questionCount: 0 }),
        stepMod.resolveLaunchStep({ brandName: 'A', industry: 'B', questionCount: 2 }),
    ]).size === 3);

// ---------------------------------------------------------------- 6. 断点真进了编译产物
section('6. 容器断点真编译进了 dist CSS(不是只写在 TSX 里)');
const distDir = path.join(root, 'dist/assets');
const cssFile = fs.existsSync(distDir)
    ? fs.readdirSync(distDir).filter(f => /^index-.*\.css$/.test(f)).map(f => path.join(distDir, f))[0]
    : null;
if (!cssFile) {
    check('dist CSS 存在', false, '没有 dist/assets/index-*.css —— 先跑 vite build。SKIP 不等于 PASS');
} else {
    const css = fs.readFileSync(cssFile, 'utf8');
    const need = [
        '@container (min-width:880px)',
        '@container (min-width:1600px)',
        '@container (min-width:2200px)',
        '@container (min-width:3000px)',
        'container-type:inline-size',
    ];
    for (const n of need) check(`CSS 里有 ${n}`, css.includes(n));
    // 🔴 [#125 裁定乙 重锚] 原来这里要求 CSS **必须含** `(pointer:fine)` —— 那是给 CTA 贴底守卫用的,
    //    唯一的生产者已被删除 ⇒ 该锚过期。承接为**反向**断言:整份 CSS 里不许再出现它。
    //    这样一来「有人把 pointer-fine 贴底条重新加回去」会当场红,比原来那条强。
    check('CSS 里**没有** (pointer:fine) —— 贴底守卫已整组退役,重新加回即红',
        !css.includes('(pointer:fine)'));
    check('反向对照:CSS 里没有虚构断点 @container (min-width:9999px)',
        !css.includes('@container (min-width:9999px)'));
    for (const w of ['1240px', '1440px', '1760px', '2000px']) {
        check(`CSS 里有 max-width:${w}(四档限宽)`, css.includes(`max-width:${w}`));
    }
}

// ---------------------------------------------------------------- 汇总
console.log(`\n${'='.repeat(64)}`);
console.log(`总计 ${results.length} 条 · 通过 ${results.length - failed} · 失败 ${failed}`);
if (failed) {
    console.log('🔴 体检发起页 UI 锁未通过');
    process.exit(1);
}
console.log('✅ 体检发起页 UI 锁全部通过');
