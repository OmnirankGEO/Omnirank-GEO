#!/usr/bin/env node
/**
 * 防御型 GEO 呈现层 · 前端静态契约闸(CUR-02 / CUR-03 / CUR-05 + §0.5.5 U-1)。
 *
 * 为什么是静态闸而不是单测:这四条债务的形态都是「源码里**不许再出现**某种写法」——
 * 负向存在性断言。真浏览器测不出"有没有人又把阈值抄回去了",源码扫描才行。
 *
 * 🔴 每条「必须命中」都配一条「必须不命中」。`--selftest` 会把每把锁**逐把注毒**,
 *    证明它真的能转红 —— 没有自证的负向锁与恒绿无法区分
 *    (本仓 2026-08-20 记过:`0x08` 假 \\b 负向锁恒绿顶了两轮复审)。
 */

import { spawnSync } from 'node:child_process';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, posix } from 'node:path';
import ts from 'typescript';

const SELF = fileURLToPath(import.meta.url);
const ROOT = join(dirname(SELF), '..');
const SRC = join(ROOT, 'src');

/** 剥掉 // 行注释 —— 病历里引用旧写法不该把自己判红。
 *  (本仓记过这个形态:引用裁决原文会让裸串结构锚判红。) */
const stripComments = (text) =>
    text.split('\n').filter((line) => !line.trim().startsWith('//')).join('\n');

const read = (rel) => stripComments(readFileSync(join(SRC, rel), 'utf8'));

/** 每把锁:{ id, file, pattern, max, why }。max=0 即负向锁。 */
const LOCKS = [
    {
        id: 'CUR-03/brandlist-no-client-thresholds',
        file: 'pages/Brands/BrandList.tsx',
        pattern: /score\s*>=\s*\d+/g,
        max: 0,
        why: '前端不得自己按分数算等级(实测曾与后端 SSOT 逐档不一致:阈值全错、领先/成熟颜色互换、缺「待提升」档)。等级读服务端 level_meta。',
    },
    {
        id: 'CUR-03/historylist-no-string-level-guess',
        file: 'pages/History/HistoryList.tsx',
        pattern: /level\.includes\(/g,
        max: 0,
        why: '按等级字符串 includes 判色是全站第三套口径。改读服务端 level_meta。',
    },
    {
        id: 'CUR-02/branddetail-no-score-zero-coercion',
        file: 'pages/Brands/BrandDetail.tsx',
        pattern: /_score\s*\|\|\s*0/g,
        max: 0,
        why: '未测量被强制成 0 分,与真的 0 分无法区分 —— 等于把「不知道」谎报成「很差」。用 measured() 保 null。',
    },
    {
        id: 'CUR-05/newdiagnosis-no-hardcoded-capability',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /4\s*大引擎|约\s*5\s*分钟|5\s*分钟出报告/g,
        max: 0,
        why: '引擎名单与耗时必须来自服务端运行计划;写死会在平台增减/降级时对用户说谎(§9.2)。',
    },
    {
        id: 'U-1/no-retired-four-state-words',
        file: 'lib/defensiveGeoPresentation.ts',
        pattern: /['"](?:稳定|有缺口|未建立|未测过|主动获客|主动推荐)['"]/g,
        max: 0,
        why: '§0.5.5 U-1 废除品牌列表第二套四态词与进攻侧两个旧别名。',
    },
    {
        id: 'ASM/defensive-plan-must-not-write-legacy-keywords',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /setFormData\(\s*\{[^}]*keywords:\s*(?:defensiveDraft|usable|manualQuestions)/g,
        max: 0,
        why: '防御题单**不得**写回 formData.keywords。写回会污染 legacy 的条数/字数校验(questionCount 喂 resolveLaunchStep、keywords 喂 legacy payload)。',
    },
    {
        id: 'ASM/exactly-one-defensive-fork-in-submit',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /if \(isDefensiveFlow\) \{\s*\n\s*await runDefensivePlanPreview\(\);/g,
        max: 1,
        why: '提交路径只允许**一处**分流。出现第二处意味着 legacy 主体被切开了,ACT-01「legacy 行为零漂移」不再可由阅读证明。',
    },
    {
        id: 'ASM/legacy-start-call-untouched',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /diagnosisApi\.start\(/g,
        max: 1,
        why: 'legacy 启动仍是唯一的 diagnosisApi.start 调用点。多一处 = 有人给防御流也接了 legacy 启动(会双跑)。',
    },
    {
        id: 'WIRE/no-client-side-price-arithmetic',
        file: 'components/defensiveGeo/LaunchPanel.tsx',
        pattern: /(basePoints|extraPoints)\s*[+\-*/]|\+\s*(basePoints|extraPoints)/g,
        max: 0,
        why: '前端不算钱(§9.1)。三个数字与那句人话全部由服务端下发;前端一旦做加法就有了第二套算价。',
    },
    {
        id: 'WIRE/no-frontend-error-code-translation',
        file: 'lib/defensiveGeoApi.ts',
        pattern: /['"](PREVIEW_EXPIRED|SNAPSHOT_CHANGED|INSUFFICIENT_POINTS|APPROVAL_REQUIRED)['"]\s*:/g,
        max: 0,
        why: 'code→中文的映射表必须只在服务端 copy_registry(§0.5.5 U-1)。前端建表 = 第二套口径,服务端改文案前端不跟。',
    },
    // 🔴 原 `WIRE/idempotency-key-not-regenerated-per-retry`(裸串存在锁)已下线,
    //    换成下方的 AST 接线锁 IDEM_*。原因见那一段的抬头注释。
];

/** 正样本:这些必须**存在**,否则说明改造被整段回退了(负向锁会因此假绿)。 */
const PRESENCE = [
    {
        id: 'presence/asm-mode-chooser-mounted',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        // [包一 · 订正三 2026-09-05] 原锚是 ModeRadioCards。三张卡已被三标签取代 ⇒
        // 该锚的**前提**被一次正确的改动推翻了，继续锁它会把对的代码判红。
        // 锁的**意图**没变（用户必须能选到防御模式），所以换锚不换意图。
        pattern: /<ModeTabs/,
        why: '模式选择器(三标签)必须真的挂在发起页上。组件建好但没装配 = 用户永远选不到防御模式。',
    },
    {
        id: 'presence/asm-launch-panel-mounted',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /<LaunchPanel/,
        why: '两阶段启动面板必须真的挂上,否则防御模式下没有任何 preview→confirm 入口。',
    },
    {
        id: 'presence/asm-fork-defaults-to-legacy',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /const isDefensiveFlow = campaignMode !== 'offensive'/,
        why: "分流键必须是「!== 'offensive'」这一个形态:offensive 为默认且落 legacy。改成 '=== defensive' 会让 hybrid 静默走 legacy。",
    },
    {
        id: 'presence/level-meta-consumed',
        file: 'pages/Brands/BrandList.tsx',
        pattern: /readLevelMeta\(/,
        why: 'BrandList 必须真的在读服务端 level_meta —— 只删旧阈值不接新口径,负向锁照样全绿。',
    },
    {
        id: 'presence/measured-used',
        file: 'pages/Brands/BrandDetail.tsx',
        pattern: /measured\(/,
        why: 'BrandDetail 必须真的用 measured() 区分 null 与 0。',
    },
    {
        id: 'presence/capability-hint-from-plan',
        file: 'pages/Diagnosis/NewDiagnosis.tsx',
        pattern: /buildCapabilityHint\(/,
        why: '能力文案必须由运行计划驱动。',
    },
    {
        id: 'presence/idempotency-conflict-handled-silently',
        file: 'components/defensiveGeo/LaunchPanel.tsx',
        pattern: /IDEMPOTENCY_CONFLICT/,
        why: 'U-2 逐字:该 code **永不上屏**,必须由前端静默处理。没有这段接线,它会走进通用错误框。',
    },
    {
        id: 'presence/no-charge-sentence-on-money-failure',
        file: 'components/defensiveGeo/LaunchPanel.tsx',
        pattern: /没有扣除任何算力/,
        why: '涉钱动作失败必须显式说这半句(协议 §1-3 安全感专项)。',
    },
    {
        id: 'presence/enter-does-not-submit-on-radio',
        file: 'components/defensiveGeo/ModeRadioCards.tsx',
        pattern: /case 'Enter':/,
        why: "§9.8:Enter 不得在 radio 上偷跑提交,必须显式 preventDefault。",
    },
];

// ════════ AST 接线锁:preview 幂等键的**两种**生命周期 ════════════════════
//
// 【为什么从裸串存在锁改成 AST 接线锁 · 2026-08-30 门八】
// 旧锁是 `/newIdempotencyKey\(\)/g` max 0 —— 一条"带括号调用一次都不许有"的存在锁。
// 它成立的前提是"这个组件的键只在惰性初始化时签发一次"。V5-A(Codex fix-of-fix2
// P2-1)之后这个前提不再成立:键有**两种**生命周期,
//   · **网络重试**(同一次逻辑预览没发出去/没回来)→ 键必须**不变**。
//     换一把 = 同一次预览在后端变成两条命令,「她看到的那一版」有了分身。
//   · **用户重新发起**(她点重新预览,或旧 preview 已过期)→ 这是**另一次**逻辑预览,
//     必须换新键。沿用旧键会命中后端幂等唯一约束,而 `insert_run_preview` 冲突时是
//     `DO NOTHING` + 回读**原来那一行** —— 于是"重新发起"重放的是那条已经过期的
//     preview,她点多少次都出不来。
// 存在锁分不清这两者:它只会数"有没有括号",于是**正确的那一次**签发也被判红。
// 实测代价:整锅 `docker build` 在 `npm run build` 这一步炸 —— 生产镜像从候选
// 构不出来 = 发车阻断。五轮复审 + 真 Chromium + 17 个后端判据包都没跑 `npm run build`,
// 所以谁都没看见。
//
// 把 max 从 0 改成 1 是**错的**:那恰好放行要禁的那一次(原锁 why 里已记过同款误标定)。
// 判据必须改成回答"调用点在哪儿",而不是"有没有调用点":
//   **恰好一处**带括号调用,且它必须落在 `newLogical` 的**真**分支里。
// 其余形态一律红:落在重试分支、多出第二处、把函数转手给别的变量、改名引入。
//
// 用 AST 不用正则的第二个理由:`lib/defensiveGeoApi.ts` 的块注释里就写着
// `useState(newIdempotencyKey)`,而本仓的 stripComments 只剥 `//` 行注释 ——
// 裸串锁会被散文触发(本仓记过这个形态)。AST 只看代码节点,注释天然不算数。

const KEY_FN = 'newIdempotencyKey';
const KEY_MODULE = 'lib/defensiveGeoApi';
const NEW_LOGICAL = 'newLogical';

/** 冻结分母:从 `lib/defensiveGeoApi` 引入 KEY_FN 的文件。当前恰好 1 个。
 *
 *  🔴 分母按**模块解析**取,不按名字全局取:`pages/DefensivePublish/api.ts` 另有一个
 *     **同名但不同**的 `newIdempotencyKey(prefix)`(发布链自己的,5 处调用)。
 *     按名字全局扫会把那 5 处一起判红 —— 那是另一条链的事,不在本锁作用域内。
 *  🔴 set 相等,不是包含:多一个文件 = 出现了没人验的新签发点(它可以在自己的
 *     重试路径上随便换键);少一个 = 接线被整段摘掉,下面的计数会因此恒绿。 */
const KEY_IMPORTERS = ['components/defensiveGeo/LaunchPanel.tsx'];

/** src 下全部 .ts/.tsx(相对 SRC 的 posix 路径)。 */
function listSources(rel = '') {
    const out = [];
    for (const ent of readdirSync(join(SRC, rel), { withFileTypes: true })) {
        const child = rel ? posix.join(rel, ent.name) : ent.name;
        if (ent.isDirectory()) out.push(...listSources(child));
        else if (/\.tsx?$/.test(ent.name)) out.push(child);
    }
    return out;
}

const parseSource = (rel) => ts.createSourceFile(
    rel,
    readFileSync(join(SRC, rel), 'utf8'),
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
);

const lineOf = (sf, node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;

/** import 说明符 → 相对 SRC 的模块路径(无扩展名);解析不了的返回 null。 */
function resolveSpec(spec, fromRel) {
    let p;
    if (spec.startsWith('@/')) p = spec.slice(2);
    else if (spec.startsWith('.')) p = posix.normalize(posix.join(posix.dirname(fromRel), spec));
    else return null;
    return p.replace(/\.tsx?$/, '').replace(/\/index$/, '');
}

/** 这个调用是不是落在 `newLogical` 的**真**分支里(三元的 whenTrue / if 的 then)。 */
function insideNewLogicalTrueBranch(node) {
    let cur = node;
    let p = node.parent;
    while (p) {
        if (ts.isConditionalExpression(p) && p.whenTrue === cur
            && p.condition.getText().includes(NEW_LOGICAL)) return true;
        if (ts.isIfStatement(p) && p.thenStatement === cur
            && p.expression.getText().includes(NEW_LOGICAL)) return true;
        cur = p;
        p = p.parent;
    }
    return false;
}

function checkIdempotencyWiring() {
    const failures = [];
    const add = (id, msg) => failures.push(`[${id}] ${msg}`);

    // 0) 锚点正样本:被锁的那个函数必须真的还在。它没了,下面全是空断言(恒绿)。
    const apiRel = `${KEY_MODULE}.ts`;
    const apiSf = parseSource(apiRel);
    const declared = apiSf.statements.some(
        (n) => ts.isFunctionDeclaration(n) && n.name && n.name.text === KEY_FN);
    if (!declared) {
        add('IDEM/anchor-gone',
            `${apiRel} 里找不到 ${KEY_FN} 的函数声明 —— 锚点过期,本锁的作用域已不成立。`
            + '在确认新形态之前不许当绿灯用。');
        return failures;
    }

    // 1) 分母:谁从这个模块引入了它。
    //    文本预筛是**保守**的:引入了这个名字的文件必然含有这个字符串,
    //    所以预筛只可能少 parse、不可能漏掉真引入者(968 个源文件里当前命中 5 个)。
    const importers = new Set();
    for (const rel of listSources()) {
        if (!readFileSync(join(SRC, rel), 'utf8').includes(KEY_FN)) continue;
        const sf = parseSource(rel);
        for (const st of sf.statements) {
            if (!ts.isImportDeclaration(st) || !ts.isStringLiteral(st.moduleSpecifier)) continue;
            if (resolveSpec(st.moduleSpecifier.text, rel) !== KEY_MODULE) continue;
            const nb = st.importClause && st.importClause.namedBindings;
            if (!nb || !ts.isNamedImports(nb)) continue;
            for (const el of nb.elements) {
                if ((el.propertyName || el.name).text !== KEY_FN) continue;
                importers.add(rel);
                if (el.propertyName) {
                    add('IDEM/alias-import',
                        `${rel}:${lineOf(sf, el)} 把 ${KEY_FN} 改名成 ${el.name.text} 引入 —— `
                        + '改名之后本锁按名字找不到调用点,等于把这把锁整个关掉。');
                }
            }
        }
    }
    for (const rel of [...importers].sort()) {
        if (KEY_IMPORTERS.includes(rel)) continue;
        add('IDEM/denominator',
            `${rel} 新引入了 ${KEY_FN},但它不在本锁的冻结分母里 —— 多出来的签发点没人验`
            + '它会不会在自己的重试路径上换键。要么把它加进 KEY_IMPORTERS 并为它补调用点判据,'
            + '要么别在那儿签发。');
    }
    for (const rel of KEY_IMPORTERS) {
        if (importers.has(rel)) continue;
        add('IDEM/denominator',
            `${rel} 不再引入 ${KEY_FN} —— 接线被整段摘掉了(或分母写错了)。`
            + '分母空掉之后下面每一条都是空断言。');
    }

    // 2) 定义模块自己不许调用它:那等于在 API 层又开了一个没人管的签发点。
    const apiCalls = [];
    (function walk(n) {
        if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === KEY_FN) {
            apiCalls.push(n);
        }
        ts.forEachChild(n, walk);
    })(apiSf);
    if (apiCalls.length) {
        add('IDEM/extra-call',
            `${apiRel}:${apiCalls.map((n) => lineOf(apiSf, n)).join(',')} 在定义模块里就调用了 ${KEY_FN}`
            + ' —— 组件之外的签发点本锁的调用点规则管不到。');
    }

    // 3) 分母内逐文件:每一处引用都要能归到一个**允许的形态**里。
    for (const rel of KEY_IMPORTERS.filter((f) => importers.has(f))) {
        const sf = parseSource(rel);
        const calls = [];
        let lazyInit = 0;
        (function walk(node) {
            if (ts.isIdentifier(node) && node.text === KEY_FN) {
                const p = node.parent;
                if (ts.isImportSpecifier(p)) {
                    // 引入本身,不是使用点。
                } else if (ts.isCallExpression(p) && p.expression === node) {
                    calls.push(node);
                } else if (ts.isCallExpression(p) && ts.isIdentifier(p.expression)
                    && p.expression.text === 'useState') {
                    lazyInit += 1;   // useState(newIdempotencyKey) 惰性初始化,签发一次
                } else {
                    add('IDEM/reference-kind',
                        `${rel}:${lineOf(sf, node)} 把 ${KEY_FN} 转手给了别处(既不是惰性初始化`
                        + '实参,也不是直接调用)—— 转手之后它在哪儿被调用,本锁看不见。');
                }
            }
            ts.forEachChild(node, walk);
        })(sf);

        if (lazyInit < 1) {
            add('IDEM/lazy-init-gone',
                `${rel} 里没有 useState(${KEY_FN}) 形态的惰性初始化 —— 跨重试冻结的那把键没了。`);
        }
        if (calls.length === 0) {
            add('IDEM/new-logical-missing',
                `${rel} 里 ${KEY_FN} 一次都没被调用 —— 「用户重新发起」不再换新键。`
                + '沿用旧键会命中后端幂等唯一约束(冲突时 DO NOTHING 并回读原来那一行),'
                + '于是重新发起重放的是那条已经过期的 preview,她点多少次都出不来。');
        } else if (calls.length > 1) {
            add('IDEM/extra-call',
                `${rel}:${calls.map((n) => lineOf(sf, n)).join(',')} ${KEY_FN} 被调用 `
                + `${calls.length} 次(只许 1 次,且必须在 ${NEW_LOGICAL} 真分支里)。`
                + '多出来的那次多半落在**网络重试**路径上 —— 那样同一次逻辑预览'
                + '在后端会变成两条命令。');
        } else if (!insideNewLogicalTrueBranch(calls[0])) {
            add('IDEM/call-outside-new-logical',
                `${rel}:${lineOf(sf, calls[0])} 唯一那处 ${KEY_FN}() 不在 ${NEW_LOGICAL} 的真分支里 ——`
                + '它落在了网络重试那一侧,每次重试都会换一把新键(计数规则看不出这一种:'
                + '调用点还是 1 处,只是站错了边)。');
        }
    }

    return failures;
}

// ════════ AST 接线锁:确认之后跳的那个进度页,URL 里放的是哪把键 ════════
//
// 【门八第二发现 · 2026-08-30】
// 防御体检 confirm 200(算力真冻结、run 真在跑)之后,`NewDiagnosis` 那句
// `navigate(\`/diagnosis/progress/${…}\`)` 放进去的是 **runId**。
// 可 `runId` 是 run_token,而 `auth.session_access.authorize_session` 查的是
// `diagnosis_runs.session_id`(= "defgeo_" + run_token)—— 两者不相等,
// 归属查询一条都命中不到,fail-closed 直接 403:
// **她刚付完钱,看到的是「无权访问该诊断进度」**。
//
// 后端在门三 G8 就把 `progressSessionId` 交出来了,是前端类型没收、调用点没用。
// 门三那条判据打的是 API(直接调 `authorize_session`),证的是"后端这把键对得上",
// 证不了"前端把哪把键放进了 URL" —— 所以前半条链一直没有任何判据。
//
// 这把锁管**静态那一半**:全站构造进度路由的地方是不是都放了 session 键。
// 运行时那一半在 `test-defgeo-pkgh-ux.mjs`(真 NewDiagnosis 点到底 + 落地页
// 第一发 /status 必须 200)。两边不是同一个断言的两份实现:
// 这里管**枚举完整性**(浏览器判据看不见"别处还有几个站点"),那边管**真行为**。

const PROGRESS_ROUTE = '/diagnosis/progress/';

/** 冻结分母:每个文件里构造进度路由的模板字面量**条数**。
 *  用"文件→条数"而不是"文件:行号",行号会因为无关改动漂;条数变了必须有人解释。 */
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 三条随宿主整删(M3 路由隔离件 1 · M3 诊断确认弹窗 1 · M3 新建诊断页 1)——
//   components/m3/** 与 pages/M3/** 整目录删除,不是站点被改没了。
const PROGRESS_SITES = {
    'pages/Brand/BrandDetailPage.tsx': 1,
    'pages/Diagnosis/NewDiagnosis.tsx': 2,
    'services/m3/stagePrimaryButtonMap.ts': 2,
};

/** 冻结豁免:按名字分不出来、但已逐个看过的站点。每条都要带理由。
 *  [开源 E3 · 前端 · 2026-10-01 · WO_322] 唯一一条(M3 路由隔离件的路由改写透传)随文件删除,现为空。 */
const PROGRESS_EXEMPT = {};

// 大小写两种驼峰都要认:`session_id` / `sessionId` / `diagnosisSessionId` / `progressSessionId`。
// (第一版写成 /session_?[iI]d/,漏了 `diagnosisSessionId` 里那个大写 S ——
//  两个本来合规的站点被判成"分不出来",注毒时才露出来。)
const SESSION_KEYED = /[sS]ession_?[iI]d/;
const RUN_TOKEN_KEYED = /\brun_?([iI]d|[tT]oken)\b/;

function checkProgressRouteKey() {
    const failures = [];
    const add = (id, msg) => failures.push(`[${id}] ${msg}`);

    const sites = [];
    for (const rel of listSources()) {
        const text = readFileSync(join(SRC, rel), 'utf8');
        if (!text.includes(PROGRESS_ROUTE)) continue;
        const sf = parseSource(rel);
        (function walk(n) {
            if (ts.isTemplateExpression(n) && n.head.text.endsWith(PROGRESS_ROUTE)) {
                sites.push({
                    rel,
                    line: lineOf(sf, n),
                    // 第一个插值就是路由的 :id 段
                    expr: n.templateSpans[0].expression.getText().trim(),
                });
            }
            ts.forEachChild(n, walk);
        })(sf);
    }

    // ① 分母:文件→条数 set 相等。多一个站点 = 有人新开了一条跳进度的路,没人验它放的是哪把键。
    const got = {};
    for (const s of sites) got[s.rel] = (got[s.rel] || 0) + 1;
    for (const rel of new Set([...Object.keys(got), ...Object.keys(PROGRESS_SITES)])) {
        const a = got[rel] || 0;
        const b = PROGRESS_SITES[rel] || 0;
        if (a !== b) {
            add('PROG/denominator',
                `${rel} 构造进度路由的地方有 ${a} 处,冻结分母写的是 ${b} 处。`
                + '新增的那一处必须说清它放的是哪把键(session_id 还是 run_token),'
                + '并把它加进 PROGRESS_SITES;整段删掉的话这把锁就失去了它守的东西。');
        }
    }
    // (刻意**没有**单独一条 "anchor-gone":站点全没了的话上面每个文件都会 count 0≠N,
    //  PROG/denominator 会连红 6 条。再加一条只会多一个我注不了毒的规则。)

    // ② 逐站分类。分不出来的一律红:"看不懂"不等于"没问题"。
    for (const s of sites) {
        const ex = PROGRESS_EXEMPT[s.rel];
        if (ex && ex.expr === s.expr) continue;
        if (RUN_TOKEN_KEYED.test(s.expr)) {
            add('PROG/run-token-in-route',
                `${s.rel}:${s.line} 拿 \`${s.expr}\` 拼进度路由 —— 那是 run_token。`
                + '进度端点/WS 校验的是 diagnosis_runs.session_id(= "defgeo_" + run_token),'
                + '两者不相等,归属查询一条都命中不到 ⇒ fail-closed 403。'
                + '她刚付完钱,看到的是「无权访问该诊断进度」。用 progressSessionId。');
        } else if (!SESSION_KEYED.test(s.expr)) {
            add('PROG/unclassified-route',
                `${s.rel}:${s.line} 拼进度路由用的 \`${s.expr}\` 按名字判不出是不是 session 键。`
                + '要么改成名字里带 sessionId 的字段,要么进 PROGRESS_EXEMPT 并写清理由。');
        }
    }

    // ③ 正样本:被修的那个站点必须真的在,且取的就是 confirm 响应里那把键。
    const confirmSite = sites.find(
        (s) => s.rel === 'pages/Diagnosis/NewDiagnosis.tsx' && /progressSessionId/.test(s.expr));
    if (!confirmSite) {
        add('PROG/defensive-confirm-site-gone',
            '发起页里找不到"用 progressSessionId 跳进度"的那一处 —— 门八第二发现的修复没了,'
            + '或者整段被挪走了(挪走的话上面的分母也会红,两条一起看)。');
    }

    return failures;
}

// ════════ AST 锁:进度页那个 WS/轮询 effect 不许读**陈旧的** state ════════
//
// 【门八第三发现 · 2026-08-30】
// `DiagnosisProgress` 里 WS 的 `onmessage` 有一支「收到正常消息后清除之前的临时错误」,
// 写的是 `else if (error) { setError(null); }`。可 `error` 是**这个 effect 闭包里的值**,
// 而 effect 的依赖数组是 `[id, navigate, initialCheckDone]` —— 不含 `error`。
// effect 建立时 `error` 恒为 `null`,于是**这一支从来没执行过**:
// 「未找到此诊断任务…请返回重新发起」的红色横幅挂上之后,
// WS 推 50%、实时日志都来了,横幅照样粘着直到终态。她刚付过 7800。
//
// 这不是一处笔误,是一整类:effect 闭包里读任何不在依赖里的 state,读到的都是
// 建立那一刻的快照。所以锁的形态是**类级**的,不是"别写 error 这个词":
//   那个 effect 里,凡是本组件 `useState` 出来的值,只要不在依赖数组里,读了就红。
// 当前该 effect 的这类读 = **0 处**,所以豁免集是空的。

const STALE_EFFECT_FILE = 'pages/Diagnosis/DiagnosisProgress.tsx';
/** 锚:靠依赖数组认那个 effect。依赖变了 = 闭包语义变了,必须由人重新确认。 */
const STALE_EFFECT_DEPS = 'id,navigate,initialCheckDone';

function checkStaleClosureReads() {
    const failures = [];
    const add = (id, msg) => failures.push(`[${id}] ${msg}`);
    const sf = parseSource(STALE_EFFECT_FILE);

    // ① 本组件 useState 出来的值名(分母从代码现取,不手抄)
    const states = new Set();
    (function walk(n) {
        if (ts.isVariableDeclaration(n) && n.initializer && ts.isCallExpression(n.initializer)
            && ts.isIdentifier(n.initializer.expression)
            && n.initializer.expression.text === 'useState'
            && ts.isArrayBindingPattern(n.name)) {
            const first = n.name.elements[0];
            if (first && ts.isBindingElement(first) && ts.isIdentifier(first.name)) {
                states.add(first.name.text);
            }
        }
        ts.forEachChild(n, walk);
    })(sf);

    // ② 锚:那个 effect 还在不在
    let target = null;
    (function walk(n) {
        if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === 'useEffect'
            && n.arguments.length === 2 && ts.isArrayLiteralExpression(n.arguments[1])
            && n.arguments[1].elements.map((e) => e.getText()).join(',') === STALE_EFFECT_DEPS) {
            target = n;
        }
        ts.forEachChild(n, walk);
    })(sf);

    if (states.size < 5 || !target) {
        add('STALE/anchor-gone',
            `${STALE_EFFECT_FILE} 里没找到依赖为 [${STALE_EFFECT_DEPS}] 的那个 effect`
            + `(或 useState 分母只有 ${states.size} 个)—— 锚点过期。`
            + '依赖数组变了就等于闭包语义变了,必须由人重新确认这把锁该打在哪儿,'
            + '在那之前不许当绿灯用。');
        return failures;
    }

    // ③ 闭包里读到的、不在依赖里的 state
    const deps = new Set(STALE_EFFECT_DEPS.split(','));
    (function walk(n) {
        if (ts.isIdentifier(n) && states.has(n.text) && !deps.has(n.text)) {
            const p = n.parent;
            const isProp = ts.isPropertyAccessExpression(p) && p.name === n;   // data.error
            const isKey = ts.isPropertyAssignment(p) && p.name === n;          // { error: … }
            const isBind = ts.isBindingElement(p) && p.name === n;
            const isDecl = ts.isVariableDeclaration(p) && p.name === n;
            if (!isProp && !isKey && !isBind && !isDecl) {
                add('STALE/state-read-outside-deps',
                    `${STALE_EFFECT_FILE}:${lineOf(sf, n)} 在那个 effect 里读了 \`${n.text}\`,`
                    + `而它不在依赖 [${STALE_EFFECT_DEPS}] 里 —— 读到的是 effect 建立那一刻的`
                    + '快照,不是当前值。这条分支多半从来没执行过(而且看起来完全正常)。'
                    + '改成读 ref / 无条件执行 / 用不会陈旧的判据(如 isDoneRef)。');
            }
        }
        ts.forEachChild(n, walk);
    })(target.arguments[0]);

    return failures;
}

function runLocks() {
    const failures = [
        ...checkIdempotencyWiring(),
        ...checkProgressRouteKey(),
        ...checkStaleClosureReads(),
    ];
    for (const lock of LOCKS) {
        const hits = (read(lock.file).match(lock.pattern) || []).length;
        if (hits > lock.max) {
            failures.push(`[${lock.id}] ${lock.file} 命中 ${hits} 次(上限 ${lock.max})。${lock.why}`);
        }
    }
    for (const p of PRESENCE) {
        if (!p.pattern.test(read(p.file))) {
            failures.push(`[${p.id}] ${p.file} 缺少必需接线。${p.why}`);
        }
    }
    return failures;
}

/** AST 接线锁的注毒:每一条规则一发,**两个方向都要**。
 *
 * 断言用的是**错误签名相等**(只看 IDEM/* 那一族的 id 集合恰好等于期望的那一个),
 * 不是"随便红一下就算"—— 否则一发注毒把别的规则打红也会被记成 ok,
 * 而真正该管这一形态的那条规则可能根本没牙。
 *
 * 🔴 每一发还要证**结果进得了退出码**:在毒还没还原的时候,把本文件当子进程再跑一遍
 *    (走非 --selftest 那条真路径),要求 `status === 1` 且 stderr 里有那个 id。
 *    只在进程内调判据函数,证的是"谓词有牙",证不了"它被 runLocks 收走、并变成非零退出码"
 *    —— 本仓 2026-08-29 记过这个形态:调用点被摘掉之后 209 条结构锁全绿存活。 */
const AST_POISONS = [
    // ① 工单点名的方向一:**在网络重试路径上塞一个签发**。
    //    这就是要禁的那一种;旧的 max:1 存在锁恰好放行它。
    ['IDEM/extra-call', 'components/defensiveGeo/LaunchPanel.tsx',
        (s) => s.replace('opts?.newLogical ? newIdempotencyKey() : previewIdempotencyKey',
            'opts?.newLogical ? newIdempotencyKey() : newIdempotencyKey()')],
    // ② 只换边不换数量:调用点仍然恰好 1 处,但站到了重试那一侧。
    //    纯计数锁(不管 max 写几)对这一发**永远是绿的**。
    ['IDEM/call-outside-new-logical', 'components/defensiveGeo/LaunchPanel.tsx',
        (s) => s.replace('opts?.newLogical ? newIdempotencyKey() : previewIdempotencyKey',
            'opts?.newLogical ? previewIdempotencyKey : newIdempotencyKey()')],
    // ③ 工单点名的方向二:**把 newLogical 那处签发去掉**。
    //    行为后果 = 重新发起重放那条已过期的 preview(V5-A P2-1 原样复发)。
    ['IDEM/new-logical-missing', 'components/defensiveGeo/LaunchPanel.tsx',
        (s) => s.replace('opts?.newLogical ? newIdempotencyKey() : previewIdempotencyKey',
            'previewIdempotencyKey')],
    // ④ 转手:`const mint = newIdempotencyKey` 之后再调用 —— 按名字找调用点的锁全瞎。
    ['IDEM/reference-kind', 'components/defensiveGeo/LaunchPanel.tsx',
        (s) => `${s}\nconst zHandoff = newIdempotencyKey;\n`],
    // ⑤ 分母:另一个组件也开始签发键 —— 它的重试路径没人验。
    ['IDEM/denominator', 'components/defensiveGeo/ModeRadioCards.tsx',
        (s) => `${s}\nimport { newIdempotencyKey } from '@/lib/defensiveGeoApi';\n`],
    // ⑥ 改名引入:调用点还在,但本锁按名字已经找不到它。
    ['IDEM/alias-import', 'components/defensiveGeo/LaunchPanel.tsx',
        (s) => s.replace('createRunPreview, newIdempotencyKey,',
            'createRunPreview, newIdempotencyKey as mintKey,')],
    // ⑦ 锚点:函数改名/挪走之后,上面每一条都会变成空断言 —— 必须先红。
    ['IDEM/anchor-gone', 'lib/defensiveGeoApi.ts',
        (s) => s.replace('export function newIdempotencyKey(): string {',
            'export function newIdempotencyKeyRenamed(): string {')],

    // ── 门八第二发现:进度路由那把键 ──────────────────────────────
    // ⑧ 缺陷原样复发:拿 runId(run_token)拼路径 ⇒ 她付完钱看到 403。
    //    这一发**同时**打红正样本(progressSessionId 那处没了),两条一起是对的:
    //    "换成了错的键"与"对的键不在了"本来就是同一件事的两面。
    [['PROG/run-token-in-route', 'PROG/defensive-confirm-site-gone'],
        'pages/Diagnosis/NewDiagnosis.tsx',
        (s) => s.replace('${result.progressSessionId}', '${result.runId}')],
    // ⑨ 换一个**看起来很合理**的字段:diagnosisCommandId。它在后端恰恰也等于
    //    run_token(`command_id = run_token or …`),所以照样 403 —— 但名字里
    //    既没有 run 也没有 session,按名字分不出来。分不出来一律红。
    [['PROG/unclassified-route', 'PROG/defensive-confirm-site-gone'],
        'pages/Diagnosis/NewDiagnosis.tsx',
        (s) => s.replace('${result.progressSessionId}', '${result.diagnosisCommandId}')],
    // ⑩ 正样本单独一发:换成另一个 session 名的字段 —— 分类过得去,
    //    但"用 progressSessionId 那一处"没了,必须有人红。
    ['PROG/defensive-confirm-site-gone', 'pages/Diagnosis/NewDiagnosis.tsx',
        (s) => s.replace('${result.progressSessionId}', '${result.sessionId}')],
    // ⑪ 分母:别处新开一条跳进度的路。键写得再对也要红 ——
    //    没进枚举 = 下次它改成 runId 时没有任何判据会知道。
    ['PROG/denominator', 'components/defensiveGeo/ModeRadioCards.tsx',
        (s) => `${s}\nconst zRoute = \`/diagnosis/progress/\${sessionId}\`;\n`],

    // ── 门八第三发现:effect 闭包里的陈旧 state ────────────────────────
    // ⑫ 缺陷原样复发:把清横幅那一支改回 `else if (error)` ——
    //    读的是 effect 建立那一刻的 error(恒 null),分支永不执行,横幅粘死。
    ['STALE/state-read-outside-deps', 'pages/Diagnosis/DiagnosisProgress.tsx',
        (s) => s.replace('} else if (!isDoneRef.current) {', '} else if (error) {')],
    // ⑬ 同一类的另一副面孔:轮询里按 connectionState 判"WS 是不是已经在工作"。
    //    它同样不在依赖里,同样恒为建立时的值 —— 证明这把锁管的是**一类**,不是一个词。
    ['STALE/state-read-outside-deps', 'pages/Diagnosis/DiagnosisProgress.tsx',
        (s) => s.replace('if (prev.progress > 0) {', "if (prev.progress > 0 || connectionState === 'live') {")],
    // ⑭ 锚:依赖数组一改,这把锁就不知道该打在哪儿了 —— 必须先红,由人重锚。
    ['STALE/anchor-gone', 'pages/Diagnosis/DiagnosisProgress.tsx',
        (s) => s.replace('}, [id, navigate, initialCheckDone]);',
            '}, [id, navigate, initialCheckDone, error]);')],
];

function astSelftest() {
    let ok = true;
    for (const [expectedRaw, rel, mutate] of AST_POISONS) {
        // 期望可以是一条 id,也可以是一组 —— 有的注毒本来就该同时打红两条
        //(例如"换成了错的键"同时意味着"对的键不在了")。写成集合相等,
        // 不写成"包含":多红一条也要当场看见,那说明我少想了一种后果。
        const expectedSet = [...new Set([].concat(expectedRaw))].sort();
        const expected = expectedSet.join(' + ');
        const family = expectedSet[0].split('/')[0] + '/';
        const path = join(SRC, rel);
        const original = readFileSync(path, 'utf8');
        const poisoned = mutate(original);
        if (poisoned === original) {
            console.log(`  DEAD ${expected}(注毒无效:要改的那段源码没命中,锚点已漂)`);
            ok = false;
            continue;
        }
        let got;
        let child;
        try {
            writeFileSync(path, poisoned, 'utf8');
            // 走 runLocks(),不是直接调 checkIdempotencyWiring() —— 顺带证明它真的被收走了。
            got = [...new Set(runLocks()
                .map((f) => f.slice(1, f.indexOf(']')))
                .filter((id) => id.startsWith(family)))].sort();
            child = spawnSync(process.execPath, [SELF], { encoding: 'utf8' });
        } finally {
            writeFileSync(path, original, 'utf8');
        }
        // 🔴 还原自证:本仓记过"变异残留穿着合法 dirty 的衣服"。写回之后逐字节复核。
        if (readFileSync(path, 'utf8') !== original) {
            console.log(`  DEAD ${expected}(还原失败:${rel} 已被污染,后面每一发都不可信)`);
            return 1;
        }
        const hit = got.length === expectedSet.length && got.every((id, i) => id === expectedSet[i]);
        const wired = child.status === 1
            && expectedSet.every((id) => String(child.stderr).includes(`[${id}]`));
        console.log(`${hit && wired ? '  ok  ' : '  DEAD'} ${expected}`
            + (hit ? '' : ` —— 实际红的是 [${got.join(', ') || '无'}]`)
            + (wired ? '' : ` —— 没进退出码(子进程 status=${child.status})`));
        if (!hit || !wired) ok = false;
    }
    return ok;
}

/** 判别力自证:逐把锁注毒,证明它会红;再还原。 */
function selftest() {
    const POISON = {
        'ASM/defensive-plan-must-not-write-legacy-keywords': ['pages/Diagnosis/NewDiagnosis.tsx', 'setFormData({ keywords: defensiveDraft });'],
        'ASM/exactly-one-defensive-fork-in-submit': ['pages/Diagnosis/NewDiagnosis.tsx', 'if (isDefensiveFlow) {\n            await runDefensivePlanPreview();'],
        'ASM/legacy-start-call-untouched': ['pages/Diagnosis/NewDiagnosis.tsx', 'const zz = diagnosisApi.start({});'],
        'WIRE/no-client-side-price-arithmetic': ['components/defensiveGeo/LaunchPanel.tsx', 'const z = 1 + basePoints;'],
        'WIRE/no-frontend-error-code-translation': ['lib/defensiveGeoApi.ts', 'const z = { "PREVIEW_EXPIRED": "过期了" };'],
        'CUR-03/brandlist-no-client-thresholds': ['pages/Brands/BrandList.tsx', 'if (score >= 81) return 1;'],
        'CUR-03/historylist-no-string-level-guess': ['pages/History/HistoryList.tsx', 'const z = level.includes("领先");'],
        'CUR-02/branddetail-no-score-zero-coercion': ['pages/Brands/BrandDetail.tsx', 'const z = d.web_search_score || 0;'],
        'CUR-05/newdiagnosis-no-hardcoded-capability': ['pages/Diagnosis/NewDiagnosis.tsx', 'const z = "4 大引擎";'],
        'U-1/no-retired-four-state-words': ['lib/defensiveGeoPresentation.ts', 'const z = "主动获客";'],
    };
    let ok = true;
    for (const [lockId, [rel, poison]] of Object.entries(POISON)) {
        const path = join(SRC, rel);
        const original = readFileSync(path, 'utf8');
        try {
            writeFileSync(path, original + '\n' + poison + '\n', 'utf8');
            const caught = runLocks().some((f) => f.startsWith(`[${lockId}]`));
            console.log(`${caught ? '  ok  ' : '  DEAD'} ${lockId}`);
            if (!caught) ok = false;
        } finally {
            writeFileSync(path, original, 'utf8');
        }
    }
    // 正样本反向自证:**通用**地把接线摘掉,PRESENCE 必须红。
    // 🔴 原来这里是写死的 if/else 三选一,新增正样本会掉进 else 分支做无效替换,
    //    于是"注毒后仍绿" —— 本轮实测三条新正样本因此全是恒绿锁。
    //    改成从 pattern 反推要摘的字面量:凡是能被 pattern 命中的片段一律替换掉。
    for (const p of PRESENCE) {
        const path = join(SRC, p.file);
        const original = readFileSync(path, 'utf8');
        try {
            const global = new RegExp(p.pattern.source, 'g');
            const poisoned = original.replace(global, '__WIRE_REMOVED__');
            if (poisoned === original) {
                console.log(`  DEAD ${p.id}(注毒无效:pattern 在源码里没命中)`);
                ok = false;
                continue;
            }
            writeFileSync(path, poisoned, 'utf8');
            const caught = runLocks().some((f) => f.startsWith(`[${p.id}]`));
            console.log(`${caught ? '  ok  ' : '  DEAD'} ${p.id}`);
            if (!caught) ok = false;
        } finally {
            writeFileSync(path, original, 'utf8');
        }
    }

    console.log('— AST 接线锁(IDEM=幂等键生命周期 · PROG=进度路由那把键 · STALE=闭包陈旧 state)—');
    if (!astSelftest()) ok = false;

    return ok ? 0 : 1;
}

const isSelftest = process.argv.includes('--selftest');
if (isSelftest) {
    console.log('— 判别力自证(每把锁注毒必须转红)—');
    const rc = selftest();
    console.log(rc === 0 ? '✅ 全部锁具备判别力' : '❌ 有锁注毒后仍绿 = 恒绿锁');
    process.exit(rc);
} else {
    const failures = runLocks();
    if (failures.length) {
        console.error('❌ 防御型 GEO 呈现层契约闸失败:');
        failures.forEach((f) => console.error('  · ' + f));
        process.exit(1);
    }
    console.log(`✅ 防御型 GEO 呈现层契约闸通过(${LOCKS.length} 负向锁 + ${PRESENCE.length} 正样本`
        + ` + ${AST_POISONS.length} 条 AST 接线规则)`);
}
