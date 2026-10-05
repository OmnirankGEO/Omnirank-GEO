#!/usr/bin/env node
/**
 * 判据 · 包一阶段⑤b 唯一题源(订正七① / 二十四)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const rdRepo = (p) => readFileSync(join(REPO, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
/**
 * 🔴 未评估的**清单**,不只是计数。
 *    今天栽过一次:C4 重构后 E8 的解析锚失效、静静掉进未评估,
 *    而 F0 早已让退出码是 3 ⇒ **新增的未评估在退出码上完全不可见**。
 *    3 只说"有未评估",不说"有几个、是哪几个"。
 *    所以冻结一份预期清单:出现清单外的未评估 = **失败**(1),不是 3。
 */
const skipped = [];
/**
 * 🔴 两类未评估必须分开,它们的处置正好相反:
 *   · ENV_SKIPS  = **环境缺东西**(没库 / 没 dist / 取不到基线)——
 *     跑的人补上环境就能评,不是判据坏了。允许,但每条都要大声打印。
 *   · 其余(D0 / E8.0 / E9.0 / E10.0)= **锚失效**:解析器一个都没抓到,
 *     通常是对方重构把我的锚挪走了。这类必须红。
 *
 * 第一版把两类并成一档 ⇒ Review 在**没有 dist** 的环境跑,H0 被报成"清单外未评估"。
 * 一个在正当环境里也会响的警报,会把人训练成忽略它 —— 那比没有警报更糟。
 */
const ENV_SKIPS = ['F0', 'G0', 'H0', 'G3'];
const EXPECTED_SKIPS = ENV_SKIPS;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const EDITOR = strip(rd('src/components/defensiveGeo/QuestionPlanEditor.tsx'));
const OWNQ = strip(rd('src/pages/Diagnosis/launch/ownQuestions.ts'));

// ── A 🔴 三同一:同一个变量,不是三份拷贝 ────────────────────────────
/**
 * 后端拿 `custom_questions` 去比 `pricePreviewId` 的哈希。取价用一份、提交用另一份
 * ⇒ 要么每次 409,要么(更糟)按 A 算价按 B 扣钱。所以三处必须读**同一个变量**。
 */
ok(/const ownQuestions = collectOwnQuestions\(manualQuestions\);/.test(PAGE),
    'A1 唯一题源 = 题单编辑器的 manualQuestions,经一次归一化');
const Q_HITS = (PAGE.match(/questions: ownQuestions\.unique/g) || []).length;
ok(Q_HITS === 2, `A2 🔴 取价的两处(首次 + 409 重取)都读它(实得 ${Q_HITS})`);
ok(/payload\.custom_questions = ownQuestions\.unique;/.test(PAGE),
    'A3 🔴 提交体也读它');
ok(!/customParsed/.test(PAGE),
    'A4 反臂:旧的第二份解析(customParsed / formData.customQuestions)已彻底消失');
ok(!/formData\.customQuestions/.test(PAGE),
    'A4 反臂二:页面不再读那个字段');

// ── B 🔴 出题位恰 1,且是编辑器 ──────────────────────────────────────
ok(!/id="keywords"/.test(PAGE), 'B1 🔴 旧「核心搜索问题」Textarea 零渲染');
ok(!/id="customQuestions"/.test(PAGE), 'B2 🔴 高级选项那个自定义题 Textarea 零渲染');
const editorMounts = (PAGE.match(/<QuestionPlanEditor/g) || []).length;
ok(editorMounts === 1, `B3 题单编辑器恰挂 1 次(实得 ${editorMounts})`);
ok(!/\{isDefensiveFlow && \(\s*\n\s*<div ref=\{planEditorRef\}/.test(PAGE)
    && !/\{isDefensiveFlow && \($/m.test(PAGE.split('<QuestionPlanEditor')[0].split('\n').slice(-6).join('\n')),
    'B4 🔴 编辑器**不再挂在 isDefensiveFlow 门后** —— 默认线(增长)上必须出现,'
    + '否则她在默认线上根本没有出题的地方');

// ── C 🔴 空题集是合法路径,不许被当成缺项 ────────────────────────────
ok(/const questionsValid = ownQuestions\.tooLong\.length === 0;/.test(PAGE),
    'C1 🔴 合法性只看「有没有超长题」,**没有 >= 1** —— 空题集要跑 AI 8 道');
ok(!/questionCount >= 1/.test(PAGE),
    'C1 反臂:任何地方都不再要求题数 >= 1(要求了就把主路径堵死)');
ok(/题可以不填/.test(PAGE) || /题不填也能开始/.test(PAGE),
    'C2 灰按钮/错误文案明说「题可以不填」——否则她以为自己漏了必填项');

// ── D 🔴 100 字上限:跨层锁(锚 validator **函数名**,不锚行号)──────
let pyMax = null;
try {
    const py = rdRepo('server.py');
    const i = py.indexOf('def validate_custom_questions');
    if (i >= 0) {
        const body = py.slice(i, i + 1200);
        const m = body.match(/len\(q\)\s*>\s*(\d+)/);
        pyMax = m ? parseInt(m[1], 10) : null;
    }
} catch { /* 下面按未评估处理 */ }
const tsMax = (OWNQ.match(/CUSTOM_QUESTION_MAX_CHARS\s*=\s*(\d+)/) || [])[1];
if (pyMax == null || tsMax == null) {
    notEvaluated += 1;
    skipped.push('D0');
    console.log(`  ⚠️ D0 **未评估**:上限没解析出来(py=${pyMax} ts=${tsMax}) ⇒ 跨层锁没跑。`);
} else {
    ok(parseInt(tsMax, 10) === pyMax,
        `D1 🔴 前端上限 == 服务端 validate_custom_questions 的上限 (${tsMax} vs ${pyMax})`);
    ok(pyMax >= 50, `D2 正样本臂:解析到的上限像个真数字(${pyMax})`);
}
ok(/data-testid="question-too-long"/.test(EDITOR),
    'D3 超长在**打字时**就说(归一化会静默丢弃它,不出声就是「写了 12 道只跑 10 道」)');

// ── E 🔴 订正二十五:前端**一个字都不发** keywords ────────────────────
/**
 * 🔴 这条从「题不许进 keywords」升级成「前端根本没有 keywords 这个字段」。
 *    理由是结构性的,不是洁癖:只要前端还留着一个写 keywords 的口子,
 *    就会有下一个人把**题**写进去 —— 而题在服务端过的是上限 50 的 `validate_keywords`,
 *    编辑器镜像的却是上限 100 的题 ⇒ 她在一个叫「问题」的框里打字,
 *    收到一条说「关键词」的 422(C 在你我接缝上抓到的就是这一格)。
 *    两边判据都照不到:我的跨层锁比的是 100 ≡ 100(恒绿),
 *    C 的判据只看 custom_questions、不看被写回的 keywords。
 *
 * 🔴 **分母归零之后「里面没有坏的」是恒真的**,所以这里不写 `.every(...)`:
 *    ① 断言计数恰为 0;
     *    ⚠️ G6/G7 必须用**同一个正则常量**，不允许各写一份 ——
     *       两份必漂，而漂开那天 G6 在验另一把尺子，E1 的 0 仍无人担保。
 *    ② 同一条正则在**基线**上必须抓到东西(G6 / G7)—— 否则这个 0 可能只是正则坏了。
 *    尺子的有效性,要在它照得见的地方证。
 */
const KW_WRITE_RE = /keywords:\s*[^,\n]*/g;
const KW_PAYLOAD_RE = /payload\.keywords[^;\n]*|^\s*keywords,\s*$/gm;
const kwWrites = PAGE.match(KW_WRITE_RE) || [];
const kwPayload = PAGE.match(KW_PAYLOAD_RE) || [];
ok(kwWrites.length === 0,
    `E1 🔴 页面里**没有任何** formData.keywords 写入点(实得 ${JSON.stringify(kwWrites)})`);
/**
 * 🔴 [订正二十九] E2 的前提被**裁定**改了,不是被代码破坏了。
 *    原来钉「提交体一个 keywords 都不带」。Deploy 的正确分母(首诊非空 207/207 ·
 *    ≥2 次诊断的品牌上次非空 61/61)显示那样会整体丢掉她填的业务词,
 *    Owner 铁律是「影响诊断质量的不能丢」⇒ 乙案:高级里保留一个折叠、可选、
 *    **默认空且不预填**的搜索词输入。
 *
 *    换锚不换意图。原意图是「**题**不许进 keywords」(那才是 100 字 vs 50 字那格 422 的根),
 *    不是「前端不许有 keywords」。承接成三条,合起来比原来那一条更严:
 *      E2  ── 值**恒等于** searchTerms.terms(写同一性不写包含:短路/三元能绕过包含判定);
 *      E2a ── 题的任何状态都不流向它(反写锁,订正二十九 明令保留);
 *      E2b ── 只在**非空**时赋值 ⇒ 空的时候字段整个不出现,服务端一处派生。
 */
ok(kwPayload.length === 1,
    `E2 提交体里对 keywords 恰一处赋值(实得 ${JSON.stringify(kwPayload)})`);
/**
 * 🔴 [订正三十① 重锚] 前提被**裁定**改了两次,这是第二次:
 *    二十九「空不带、非空带她填的」→ 三十①「**带屏幕上显示的那一份**」。
 *    理由是 Owner 的「看到的必须就是跑的」:留空让服务端在**提交时刻**再派生一次,
 *    那一刻可能已有新的已发布诊断 ⇒ 她看到 A、系统跑 B。
 *
 *    换锚不换意图。原意图是「提交体的值不许是页面自己拼的、必须与屏幕同源」,
 *    承接为:值恒等于 `keywordsForSubmit` 的结果,而三态判定在那个纯函数里(已注毒)。
 */
ok(kwPayload.length === 1
    && /^payload\.keywords = searchTermsSubmit\.terms$/.test(kwPayload[0].trim()),
    'E2 🔴 数据流锁:值**恒等于**三态决策的结果,不是页面另拼一份');
ok(kwPayload.every((x) => !/ownQuestions|manualQuestions|editableQuestions|custom/.test(x)),
    'E2a 🔴 反写锁不变:题的任何状态都不流向 keywords');
const kwIdx = PAGE.indexOf('payload.keywords');
ok(kwIdx > 0 && /if \(searchTermsSubmit\.send\)/.test(PAGE.slice(Math.max(0, kwIdx - 60), kwIdx + 60)),
    'E2b 🔴 由三态决策的 `send` 决定带不带 —— 页面不再自己判空'
    + '(它曾把「取到空数组」与「没取到」压成同一档,而两者处置相反)');
ok(/const searchTermsSubmit = keywordsForSubmit\(prefill, searchTermsDirty, searchTerms\.terms\);/.test(PAGE),
    'E2c 配套:三个入参都真的喂进去了(少喂 dirty 就等于永远按"没改过"处理)');
ok(!/if \(searchTerms\.terms\.length > 0\) payload\.keywords/.test(PAGE),
    'E2d 反臂:订正二十九那版"非空才带"已消失');

/**
 * 🔴 E7 「不预填」必须是**结构性**的,不是纪律。
 *    搜索词刻意不放进 `formData`(那里有品牌档案 / AI 补全 / 事件补填 / 重置四类写入者)。
 *    锁法:它的 setter 全仓**恰一个**调用点 —— 多一个就说明有人开始往里预填了,
 *    而预填过的框会让她以为那是她填的,改一个字就变成了她的输入。
 */
const setterCalls = (PAGE.match(/setSearchTermsRaw\(/g) || []).length;
/**
 * 🔴 [订正三十① 重锚] 「setter 恰 1」这条**结构性不预填**的保证被裁定作废了 ——
 *    现在就是要预填。Review 的替代口径是「写入点恰 2:端点回填 + 用户输入」,
 *    实码是 3(多一个:换品牌时清空,那也是写入)。
 *
 *    ⚠️ 弱化必须说清:计数**证不了位置**。所以三处**分别**在各自的块里验;
 *    而「回填不许冲掉她的字」那条语义,由 verify-pkg1-prefill-semantics 的 H3
 *    单独钉(同一谓词只在一处验,不在这儿重写一遍)。
 */
ok(setterCalls === 3, `E7 搜索词 setter 恰 3 个写入点(实得 ${setterCalls}):输入框 / 回填 / 换品牌清空`);
ok(/onChange=\{\(e\) => \{ setSearchTermsRaw\(e\.target\.value\); setSearchTermsDirty\(true\); \}\}/.test(PAGE),
    'E7a 🔴 ①输入框那处是受控绑定,且**同时置 dirty**(不置就等于她改了也当没改)');
const refillIdx = PAGE.indexOf('fetchSearchTermSuggestion(selectedBrandId');
ok(refillIdx > 0 && /setSearchTermsRaw\(st\.terms\.join/.test(PAGE.slice(refillIdx, refillIdx + 620)),
    'E7b 🔴 ②回填那处在取数回调里(不在别处偷偷写)');
const swIdx = PAGE.indexOf('brandSwitchNotice(');
ok(swIdx > 0 && /setSearchTermsRaw\(''\)/.test(PAGE.slice(Math.max(0, swIdx - 80), swIdx + 260)),
    'E7c 🔴 ③换品牌那处**清空**(把上一个客户的词带进新客户是明确错的)');
const stIdx = PAGE.indexOf('const [searchTermsRaw, setSearchTermsRaw]');
ok(stIdx > 0 && /useState\(''\)/.test(PAGE.slice(stIdx, stIdx + 90)),
    'E7d 🔴 初值是空串 —— 「默认空」也钉住,别哪天从 props 带个初值进来');

/**
 * 🔴 E8 跨层锁:两条上限与服务端同解。
 *
 * ⚠️ [C4 后重锚] C4 把数字从 `validate_keywords` 里搬到了**唯一定义处**
 *    `services/diagnosis_keyword_source.py` 的 `MAX_KEYWORDS` / `MAX_KEYWORD_CHARS`,
 *    validator 改成 import 它们。我原来的解析器认的是 `len(kw) > <数字>`,
 *    重构之后**一个都抓不到** ⇒ 这条静静地掉进「未评估」。
 *    而当时 F0 已经让退出码是 3 —— **新增的未评估在退出码上完全不可见**。
 *    所以下面 E8d 加了一道未评估**计数**守卫。
 *
 * 🔴 期望值必须来自 validator **实际读的那个地方**:所以先证 validator 确实
 *    从那个模块 import(否则我锁的是一个没人用的常量 —— 锚要在证据层)。
 */
let pyLen = null; let pyCount = null; let importsSSOT = false;
try {
    const py = rdRepo('server.py');
    const i = py.indexOf('def validate_keywords');
    if (i >= 0) {
        const body = py.slice(i, i + 1200);
        importsSSOT = /from services\.diagnosis_keyword_source import/.test(body);
    }
    const ssot = rdRepo('services/diagnosis_keyword_source.py');
    pyCount = (ssot.match(/^MAX_KEYWORDS\s*=\s*(\d+)/m) || [])[1];
    pyLen = (ssot.match(/^MAX_KEYWORD_CHARS\s*=\s*(\d+)/m) || [])[1];
} catch { /* 下面按未评估处理 */ }
const ST = strip(rd('src/pages/Diagnosis/launch/searchTerms.ts'));
const tsLen = (ST.match(/SEARCH_TERM_MAX_CHARS\s*=\s*(\d+)/) || [])[1];
const tsCount = (ST.match(/SEARCH_TERMS_MAX_COUNT\s*=\s*(\d+)/) || [])[1];
if (!pyLen || !pyCount || !tsLen || !tsCount) {
    notEvaluated += 1;
    skipped.push('E8.0');
    console.log(`  ⚠️ E8.0 **未评估**:上限没解析全(py=${pyLen}/${pyCount} ts=${tsLen}/${tsCount})⇒ 跨层锁没跑。`);
} else {
    ok(importsSSOT,
        'E8 🔴 证据层:`validate_keywords` 确实从 diagnosis_keyword_source import 上限 —— '
        + '否则我锁的是一个没人读的常量');
    ok(tsLen === pyLen, `E8a 🔴 单条字数上限 == 唯一定义处 (${tsLen} vs ${pyLen})`);
    ok(tsCount === pyCount, `E8b 🔴 条数上限 == 唯一定义处 (${tsCount} vs ${pyCount})`);
    ok(Number(pyLen) >= 10 && Number(pyCount) >= 5,
        `E8c 正样本臂:解析到的是真数字(${pyLen}/${pyCount}),不是把两个 undefined 判成相等`);
}
ok(/data-testid="search-terms-too-long"/.test(PAGE) && /data-testid="search-terms-overflow"/.test(PAGE),
    'E8e 🔴 两种超限都在**打字时**就说 —— 不出声就是静默丢弃');

/**
 * 🔴 E9 行为臂:直接跑归一化函数。常量对不代表逻辑对,三态各打一发。
 */
const st = await import(new URL('../src/pages/Diagnosis/launch/searchTerms.ts', import.meta.url).href)
    .catch(() => null);
if (!st) {
    notEvaluated += 1;
    skipped.push('E9.0');
    console.log('  ⚠️ E9.0 **未评估**:没能 import searchTerms.ts,行为臂没跑。');
} else {
    const empty = st.collectSearchTerms('');
    ok(empty.terms.length === 0 && empty.valid === true,
        'E9 🔴 空是**合法**的(不填就走服务端派生,不是缺项)');
    const long = st.collectSearchTerms('a'.repeat(st.SEARCH_TERM_MAX_CHARS + 1));
    ok(long.tooLong.length === 1 && long.terms.length === 0 && long.valid === false,
        'E9a 🔴 超长:留在 tooLong 里、不进提交、且拦住提交 —— 不是静默丢掉');
    const many = st.collectSearchTerms(
        Array.from({ length: st.SEARCH_TERMS_MAX_COUNT + 1 }, (_, i) => `w${i}`).join('\n'));
    ok(many.overflow === true && many.valid === false,
        'E9b 🔴 超条数:报 overflow 且拦住 —— **不截断**(截断 = 填 21 条只跑 20 条而屏幕上没字解释)');
    ok(st.collectSearchTerms(' a \n\n b ').terms.join(',') === 'a,b',
        'E9c trim + 去空行(服务端拒空串,先去掉免得提交必 422)');
}

/**
 * 🔴 E10 门的**行为**锁。E8c 只证「界面上有红字」,证不了「点不下去」——
 *    两者差着一整格:红字挂在那儿而按钮能点,她照点,照样撞服务端 422,
 *    而那条 422 长得像系统故障、也不指是哪一条词。
 */
const gate = await import(new URL('../src/pages/Diagnosis/launch/defensiveLaunchGate.ts', import.meta.url).href)
    .catch(() => null);
if (!gate) {
    notEvaluated += 1;
    skipped.push('E10.0');
    console.log('  ⚠️ E10.0 **未评估**:没能 import defensiveLaunchGate.ts,门的行为臂没跑。');
} else {
    const base = {
        mode: 'offensive', isFormComplete: true, totalDiagnosisCost: 650,
        defensiveQuestionCount: 0, hasBrandId: true, loading: false, planPending: false,
    };
    ok(gate.isLaunchBlocked({ ...base, searchTermsInvalid: false }) === false,
        'E10 正样本臂:搜索词合法时同一份输入能启动(否则下一条不携带信息)');
    ok(gate.isLaunchBlocked({ ...base, searchTermsInvalid: true }) === true,
        'E10a 🔴 只把 searchTermsInvalid 翻成 true ⇒ **真的挡住**');
}
ok(/searchTermsInvalid: !searchTerms\.valid/.test(PAGE),
    'E10b 🔴 页面把**归一化算出来的**合法性喂给门 —— 不是另算一遍(两处算必漂)');

/**
 * 🔴 E11 对客文案门 —— 本页此前**没有任何**文案门覆盖。
 *    全前端有 4 个脚本做禁词/对客文案检查(test-gap-plan-ui / test-publish-center-scope /
 *    test-unknown-denominator-disclosure / verify-publish-center-scope-ui),
 *    它们的文件分母里**都没有 NewDiagnosis.tsx** —— 我刚往这页加了对客文案,
 *    「禁词照旧」在这一页上此前是句没有执行者的话。
 *
 * 🔴 分母不能是"整个源码":页里唯一的 `draft` 是 prop 名 `draftCount`,不是给人看的字。
 *    按源码扫会把**标识符**和**文案**压成一档 ⇒ 要么误红,要么为了不误红而放宽到没用。
 *    改按「含中文的字符串字面量 + JSX 文本节点」取分母 —— 这一页的对客文案都带中文,
 *    而标识符不带。
 */
    // 🔴 用 **strip 后**的源:未 strip 会把我自己的注释当成对客文案(注释里出现 campaignMode 就误红)。
const COPY_SRC = PAGE;
const copyStrings = [
    ...(COPY_SRC.match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(COPY_SRC.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || []),
];
const COPY_FORBIDDEN = [
    'SOV', 'target_share', '净化测度', 'qwen3-max', 'deepseek-v3',   // 内部技术参数/供应商
    'portal', 'draftCount', 'campaignMode',                          // 工程术语直接露给用户
    '保排名', '几乎每次', '保证上榜',                                  // 绝对化承诺
];
ok(copyStrings.length >= 30,
    `E11 正样本臂：抽到 ${copyStrings.length} 条对客文案（抽成 0 会让下一条恒真）`);
/**
 * 🔴 按**形状**验抽取器，不按条数。
 *    第一版只断言「抽到 >= 30 条」，而它在 243 条的情况下
 *    依然看不见 JSX 文本节点（那条正则不允许换行，而文案常常独占一行）——
 *    注毒往 <Label> 里塞 SOV 竟然不红。**数量证不了覆盖**：
 *    要每一种形状各钉一条已知样本。
 */
ok(copyStrings.some((c) => c.includes('不填也行')),
    'E11-shape1 形状臂：**引号串**（placeholder）被抽到了');
ok(copyStrings.some((c) => c.includes('搜索词(选填)')),
    'E11-shape2 🔴 形状臂：**JSX 文本节点**（<Label> 里独占一行的那种）也被抽到了');
const copyHits = COPY_FORBIDDEN.filter((t) => copyStrings.some((c) => c.includes(t)));
ok(copyHits.length === 0, `E11a 🔴 对客文案里零内部术语/零绝对化承诺(实得 ${JSON.stringify(copyHits)})`);
ok(COPY_FORBIDDEN.some((t) => '我们保排名,SOV 稳定'.includes(t)),
    'E11b 反向对照:词表对样例串确实命中 —— 否则 E11a 的"零"只是词表坏了');
ok(copyStrings.some((c) => c.includes('不填也行,AI 会按这个品牌已有的信息来规划')),
    'E11c 🔴 订正二十九 指定的占位文案原样在页里 —— '
    + '它对首诊/复诊都为真(旧稿「沿用上次诊断的词」对首诊用户是句不成立的承诺)');

// ── F 🔴 行为臂:空 keywords 必须被 DTO 接受(依赖 C 同班那笔)──────
/**
 * 🔴 缺席锁(「Field 里没有 min_length」)只能证「那个写法没了」,
 *    证不了「那件事能做了」—— C 换个地方拒空,缺席锁照绿而提交照样 422。
 *    所以这里打**行为**:构造一个空 keywords 的 DTO,不抛才算数。
 * 🔴 跑不了 ⇒ **未评估**,不是绿;且必须把**异常原文**带进报文 ——
 *    否则哪天它因别的原因跑不了(import 变了、模型改名),我还是报 3,
 *    而那个 3 的含义已经变了。(C 的提醒。)
 */
const PY = `
import json
try:
    from server import DiagnosisRequest
except Exception as e:
    print(json.dumps({"ran": False, "why": type(e).__name__ + ": " + str(e)[:200]})); raise SystemExit(0)
try:
    DiagnosisRequest(brand_name="x", industry="y", keywords=[])
    print(json.dumps({"ran": True, "accepted": True}))
except Exception as e:
    print(json.dumps({"ran": True, "accepted": False, "why": type(e).__name__ + ": " + str(e)[:200]}))
`;
const r = spawnSync('python', ['-c', PY], {
    cwd: REPO, encoding: 'utf8', timeout: 120000,
    env: { ...process.env, DATABASE_URL: process.env.DATABASE_URL || 'postgresql://u:p@127.0.0.1:5432/none' },
});
let verdict = null;
try { verdict = JSON.parse((r.stdout || '').trim().split('\n').pop() || ''); } catch { /* below */ }
if (!verdict || verdict.ran === false) {
    notEvaluated += 1;
    skipped.push('F0');
    const why = verdict?.why || `python 没给出可解析的结果(exit=${r.status}) stderr=${(r.stderr || '').slice(-200)}`;
    console.log('  ⚠️ F0 **未评估**:空 keywords 的行为臂**没跑成**,原因原文如下 ——');
    console.log(`     ${why}`);
    console.log('     ⇒ 这条耦合在本环境**未验**。必须在有库处跑(Deploy 预烤 / C 的包)。');
    console.log('     ⚠️ 不要把这条读成"通过":跑不起来与通过在退出码上同形,所以它落在 3 不落在 0。');
} else {
    ok(verdict.accepted === true,
        `F1 🔴 行为锁:空 keywords 的 DiagnosisRequest 必须能构造(C 同班那笔)`
        + (verdict.accepted ? '' : ` —— ${verdict.why}`));
}

// ── G 🔴 红臂:基线上都不成立 ────────────────────────────────────────
const BASE = 'e19fe1496';
let basePage = null;
try {
    basePage = execFileSync('git', ['show', `${BASE}:frontend/src/pages/Diagnosis/NewDiagnosis.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1;
    skipped.push('G0');
    console.log(`  ⚠️ G0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (basePage) {
    ok(/id="keywords"/.test(basePage), 'G1 红臂:改动前那个「核心搜索问题」框确实在');
    ok(/id="customQuestions"/.test(basePage), 'G2 红臂:改动前高级选项那个框也在(两个题入口)');
    ok(/questionCount >= 1/.test(basePage), 'G3 红臂:改动前确实要求题数 >= 1');
    ok(!/ownQuestions\.unique/.test(basePage),
        'G5 红臂:基线上没有归一化后的唯一题集(那时取价/提交/判定各解析一份)');
    /**
     * 🔴 G6 / G7 = E1 / E2 的**尺子有效性臂**。E1/E2 断言"计数为 0",
     *    而一条坏掉的正则也会返回 0 —— 两者在读数上完全同形。
     *    所以把同一条正则拿到基线上跑:那里这些写入点**确实存在**,
     *    抓不到就说明是尺子坏了,不是代码干净。
     */
    const baseKwWrites = (basePage.match(KW_WRITE_RE) || []).length;
    const baseKwPayload = (basePage.match(KW_PAYLOAD_RE) || []).length;
    ok(baseKwWrites >= 5,
        `G6 🔴 同一条正则在基线上抓到 ${baseKwWrites} 处写入 —— 证明 E1 的 0 是真 0`);
    ok(baseKwPayload >= 1,
        `G7 🔴 同一条正则在基线上抓到 ${baseKwPayload} 处提交体赋值 —— 证明 E2 那一处是尺子真抽到了`);
    ok(/manualQuestions/.test(basePage),
        'G4 配对臂:基线里本来就有的锚确实在(否则上面几条"有"只是取到空内容)');
}

// ── H dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    skipped.push('H0');
    console.log('  ⚠️ H0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('also-run-ai-toggle'), 'H1 双轨开关锚进了产物(它从高级面板搬到了编辑器旁)');
    ok(!js.includes('id="customQuestions"'), 'H2 🔴 已删的题框**不在产物里**(源码删了但产物没重建 = 线上还在)');
}

/**
 * 🔴 清单外的未评估算**失败**,不算 3。
 *    退出码 3 只说"有未评估",不说"有几个、是哪几个" ——
 *    今天 C4 重构让 E8 的锚失效、静静掉进未评估,而 F0 早已把码顶在 3 上,
 *    新增的那个在退出码上**完全不可见**。冻结清单之后它会红。
 */
const envSkips = skipped.filter((t) => ENV_SKIPS.includes(t));
if (envSkips.length) {
    console.log(`  ⚠️ SKIP-GUARD 环境类未评估:${JSON.stringify(envSkips)} —— 补上环境(库 / dist / 基线)才算评过,**别读成通过**。`);
}
const unexpectedSkips = skipped.filter((t) => !EXPECTED_SKIPS.includes(t));
if (unexpectedSkips.length) {
    bad += 1;
    console.log(`  🔴 SKIP-GUARD 出现清单外的未评估:${JSON.stringify(unexpectedSkips)}`);
    console.log(`     预期只有 ${JSON.stringify(EXPECTED_SKIPS)}(本机无库)。多出来的是**锚失效**,不是环境问题。`);
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
