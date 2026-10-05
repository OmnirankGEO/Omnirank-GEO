#!/usr/bin/env node
/**
 * 判据 · 文章方向(user_choice)前后端契约 —— #185
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 为什么必须有这把闸:前端发一个后端不认的 `user_choice`,Pydantic 的
 *    `Literal` 会 422 —— 而且是在用户点了「重新生成标题」**之后**才炸。
 *    UI 上那一档看起来完全正常(有标签、有说明、能选中),点下去才发现是死的。
 *    #176 刚栽过同形的一次(教程沙盒没登记新接口 ⇒ 501 被折成通用错误,静默死六天)。
 *
 * 🔴 **本判据故意不进 build 链**:它要读 `../server.py`,而 build 链有一道越界闸
 *    禁止链上脚本引用后端路径 —— 构建镜像是 node:20-alpine,里面只有 frontend/。
 *    它进手跑清单与班车枚举门。
 *
 * 🔴 **未评估 ≠ 通过。** #185 的 `defensive_company` 由 C 的 c2 落后端;
 *    C 没落之前这条**不报绿也不报红**,报 rc=3「阻塞」并说清在等谁。
 *    (报绿 = 骗自己;报红 = 把"还没到"说成"做错了",下一个人会来改前端。)
 *
 * 跑法:cd frontend && node scripts/verify-article-directions-contract.mjs
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const rdRepo = (p) => readFileSync(join(ROOT, '..', p), 'utf8');

let bad = 0;
let blocked = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};
const pending = (label, detail) => {
    console.log(`  ..   未评估 ${label}${detail ? ' — ' + detail : ''}`);
    blocked += 1;
};

// ── 真调前端那份清单(不读源码字符串:那只能证明"写了这么一句") ──
let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/articleDirections.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a185-dir-'));
    const f = join(tmp, 'dirs.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL D0 🔴 前端方向清单加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

console.log('D 文章方向 · 前后端契约');

// ── 后端那份 Literal ──────────────────────────────────────────────
const server = rdRepo('server.py');
const litStart = server.indexOf('USER_CHOICE_VALUES = Literal[');
const litEnd = server.indexOf(']', litStart);
const literal = litStart >= 0 ? server.slice(litStart, litEnd + 1) : '';
const accepted = [...literal.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);

ok(accepted.length >= 6,
    'D0a 分母自证:真的从 server.py 抽出了 USER_CHOICE_VALUES'
    + '(抽空了的话下面每条都恒真 —— 那是扫描器死了,不是契约一致)',
    `${accepted.length} 个:${accepted.slice(0, 8).join(',')}…`);

const feValues = M.ARTICLE_DIRECTIONS.map((d) => d.value);
ok(feValues.length >= 7, 'D0b 分母自证:前端清单非空', `${feValues.length} 档`);

// ── D1 前端每一档后端都认(defensive_company 单独处理) ──────────────
/*
 * 🔴 [订正 · 0913d 合并树上抓到的**假红**] 这一格原来拿前端清单直接比
 *    `USER_CHOICE_VALUES` 那个 Literal,于是把 `auto`(「系统推荐」)报成
 *    「后端不认」。**它认。**
 *
 *    `auto` 不是一个文体,是**哨兵值**,走的是另一条分支:
 *      · `server.py` `_is_light_reset = (request.user_choice == "auto")`
 *        —— 显式 auto ⇒ light path,清 `user_choice=NULL`、不进 LLM、不扣费;
 *      · `writing/article_style_contract.py` `normalize_user_choice(..., allow_auto=True)`
 *        对它 `return "auto"`,并另有 `allow_auto=False` 的调用点把它挡掉。
 *    而那个 Literal typing 的是「真文体」的取值集合 —— **两个不同的指称对象**。
 *    拿 A 的清单去比 B 的集合,红的不是产品,是判据。
 *
 * ⇒ 改法:不在 Literal 里的值,必须能指出**后端处理它的那一处**;
 *    指不出来才算契约破了。并配正样本臂:编一个值必须被判成"后端不认"。
 */
const NEW_IN_185 = 'defensive_company';
const CONTRACT_SRC = rdRepo('writing/article_style_contract.py');
/** 不在 Literal 里、但后端确有专门分支处理的哨兵值 —— 每个都要给得出证据。 */
const SENTINEL_EVIDENCE = {
    auto: [
        ['server.py', server, 'request.user_choice == "auto"'],
        ['article_style_contract.py', CONTRACT_SRC, 'allow_auto'],
    ],
};
const handledAsSentinel = (v) => {
    const proofs = SENTINEL_EVIDENCE[v];
    if (!proofs) return false;
    return proofs.every(([, src, needle]) => typeof src === 'string' && src.includes(needle));
};
const legacy = feValues.filter((v) => v !== NEW_IN_185);
const rejected = legacy.filter((v) => !accepted.includes(v) && !handledAsSentinel(v));
ok(rejected.length === 0,
    'D1 🔴 前端每一档方向后端都认(在 Literal 里,或指得出处理它的那一处分支)',
    rejected.length ? `后端不认:${rejected.join(',')}` : '全部对得上');
/* 哨兵那一档的证据要逐条打印 —— 不打印的话"它是哨兵"就只是一句我自己说的话 */
for (const [v, proofs] of Object.entries(SENTINEL_EVIDENCE)) {
    if (!feValues.includes(v)) continue;
    ok(handledAsSentinel(v),
        `D1b 🔴 哨兵值 \`${v}\` 在后端有专门分支(不是靠 Literal 放行)`,
        proofs.map(([f, src, needle]) => `${f}:${(typeof src === 'string' && src.includes(needle)) ? '有' : '**没有**'} ${needle}`).join(' · '));
}
/* 正样本臂:编一个值必须被判成"后端不认",否则 D1 是空断言 */
{
    const fake = 'zzq_不存在的方向';
    const fakeRejected = !accepted.includes(fake) && !handledAsSentinel(fake);
    ok(fakeRejected, 'D1c 正样本臂:编造的方向值**会**被判成后端不认(否则 D1 恒绿)');
}

// ── D2 #185 那一档:**按"后端有没有 × 前端露不露"的组合判** ──────────
{
    /*
     * 🔴 [Review 09-13 裁定] 这一格原来是"后端没有 ⇒ 未评估"。
     *    不够 —— 未评估挡不住**这一档已经暴露在界面上**这件事:
     *    0913b 这班带我的前端、不带 C 的 c2,用户选了就是 422,
     *    而界面上那一档看起来完全正常(#176 那个形状)。
     *    ⇒ 改判**组合**:
     *      后端有 + 前端露  ⇒ 绿(正常上线态)
     *      后端没有 + 前端藏 ⇒ 绿(本班的状态:开关关着,用户根本选不到)
     *      后端没有 + 前端露 ⇒ **红**(会 422 的死入口)
     *      后端有 + 前端藏  ⇒ 未评估(C 已经上了、我还没翻开关,提醒翻)
     */
    const backendHas = accepted.includes(NEW_IN_185);
    const frontendShows = feValues.includes(NEW_IN_185);
    ok(typeof M.DEFENSIVE_DIRECTION_ENABLED === 'boolean',
        'D2a 开关存在且是布尔(不是靠注释或约定)',
        String(M.DEFENSIVE_DIRECTION_ENABLED));
    ok(M.DEFENSIVE_DIRECTION_ENABLED === frontendShows,
        'D2b 🔴 开关与对外清单**一致** —— 开关说关、清单里却还有那一档,'
        + '就是开关没接上(过滤漏了一处)',
        `开关=${M.DEFENSIVE_DIRECTION_ENABLED} 清单里有=${frontendShows}`);
    if (backendHas && frontendShows) {
        ok(true, `D2 🔴 ${NEW_IN_185}:后端认、前端露 —— 正常上线态`);
    } else if (!backendHas && !frontendShows) {
        ok(true, `D2 🔴 ${NEW_IN_185}:后端还不认,前端**也没露** —— 用户选不到,不会 422`);
    } else if (!backendHas && frontendShows) {
        ok(false, `D2 🔴 ${NEW_IN_185}:后端不认、前端却露着 ⇒ 用户选了就是 **422**,`
            + '而界面上那一档看起来完全正常(#176 同形)。把开关关掉,或与 C 的 c2 同班');
    } else {
        pending(`D2 ${NEW_IN_185}:后端已经认了、前端还藏着`,
            '把 DEFENSIVE_DIRECTION_ENABLED 翻成 true 就转绿');
    }
    /* 前端藏着时,页面上不许还留着这一档的字样(挡"清单过滤了、页面另写一份") */
    if (!frontendShows) {
        const hall2 = rd('src/pages/Writing/WritingHall.tsx');
        const dlg2 = rd('src/components/writing/DistributionConfigDialog.tsx');
        ok(!/'defensive_company'/.test(hall2) && !/'defensive_company'/.test(dlg2),
            'D2c 🔴 开关关着 ⇒ 页面里没有另写一份这一档(过滤只在唯一清单那一层)');
    }
}

// ── D3 两处入口同源(挡"配比里能选、单篇下拉里没有") ────────────────
const hall = rd('src/pages/Writing/WritingHall.tsx');
const dlg = rd('src/components/writing/DistributionConfigDialog.tsx');
ok(/ARTICLE_DIRECTIONS\.map/.test(hall) && /distributableDirections\(\)/.test(dlg),
    'D3 🔴 两处入口都从**唯一清单**派生 —— 改前是两份手写清单,'
    + '加一档只改一处就会出现「配比里能选、单篇下拉里没有」,而两边各自都看起来对');
ok(!/const USER_CHOICE_LABELS: \{ value: string; label: string; emoji: string \}\[\] = \[\s*\{/.test(dlg),
    'D3b 反臂:对话框里那份**手写**清单真的不在了(留着就会再分家)');

// ── D4 配比器不含 auto(它不是一类) ───────────────────────────────
const dist = M.distributableDirections().map((d) => d.value);
ok(!dist.includes('auto') && dist.length === feValues.length - 1,
    'D4 配比器不含「系统推荐」(配比是给每一类分篇数,它不是一类)', dist.join(','));

// ── D5 防御型的判定只有一处 ────────────────────────────────────────
ok(M.isDefensiveDirection(NEW_IN_185) === true
    && M.isDefensiveDirection('company_facts') === false
    && M.isDefensiveDirection(null) === false,
    'D5 🔴 防御型判定走 isDefensiveDirection —— 各处自己写 `=== \'defensive_company\'` '
    + '就会有一处漏掉(徽章有、提示行没有)');
ok(!/=== 'defensive_company'/.test(hall),
    'D5b 反臂:页面里没有裸比较(必须走那个函数)');

// ── D6 防御型说明必须讲清与 company_facts 的区别 ────────────────────
/* 用**全量**清单:文案对不对与开关无关(开关管的是能不能选)。 */
const defOpt = M.ALL_ARTICLE_DIRECTIONS.find((d) => d.value === NEW_IN_185);
ok(!!defOpt && /公司名|品牌/.test(defOpt.desc),
    'D6 🔴 防御型那句说明点出"标题围绕公司名" —— 它和「企业事实与品牌说明」'
    + '听起来像同一件事,但一个改写法、一个改**题的主语**,用户选错拿到的东西完全不同',
    defOpt ? defOpt.desc : '(缺)');

// ── D8 徽章与提示行**只在一处**实现(挡"三个调用点漏一个") ──────────
{
    const selStart = hall.indexOf('function TopicStyleSelector(');
    const selEnd = hall.indexOf(String.fromCharCode(10) + 'function ', selStart + 10);
    const comp = selStart >= 0 ? hall.slice(selStart, selEnd > 0 ? selEnd : selStart + 4000) : '';
    ok(comp.length > 200, 'D8a 分母自证:定位到了 TopicStyleSelector', `${comp.length} 字符`);
    ok(/topic-defensive-badge/.test(comp) && /defensiveMissingFactsLine\(/.test(comp),
        'D8 🔴 徽章与缺事实提示在**组件内部**实现 —— 本页有三处渲染它,'
        + '在调用点各加一遍必然漏一处(徽章有、提示行没有)');
    const callSites = (hall.match(/<TopicStyleSelector/g) || []).length;
    ok(callSites >= 3,
        'D8b 分母自证:确实有多个调用点(只有一处的话 D8 没有意义)', `${callSites} 处`);
    ok(!/topic-defensive-badge/.test(hall.slice(selEnd > 0 ? selEnd : hall.length)),
        'D8c 反臂:组件外面没有第二份徽章实现');
}

// ── D7 缺事实提示:说缺什么,没有就不编 ─────────────────────────────
{
    /*
     * 🔴 D7 按 C 09-13 交底的**真形状**验:
     *    `missing_facts: [{ question, fields[] }]`(不是整句,也不是纯字符串数组)。
     *    C 给结构不给整句的理由:整句少一个占位符会渲染成用户可见的乱码,
     *    而那种错代码里不报错。
     */
    const real = M.defensiveMissingFactsLine([
        { question: '资质·案例', fields: ['资质证书', '客户案例'] },
        { question: '价格·收费', fields: ['价格区间'] },
    ]);
    ok(/资质·案例/.test(real) && /资质证书/.test(real) && /价格区间/.test(real),
        'D7 🔴 缺事实提示按**问**说缺了哪些**档案字段**(不是一句"资料不足" ——'
        + '后者用户不知道该去补哪一项)', real);
    ok(M.defensiveMissingFactsLine([]) === ''
        && M.defensiveMissingFactsLine(null) === ''
        && M.defensiveMissingFactsLine([{}]) === '',
        'D7b 一个都没给(或给了空壳)时返回空串 —— **不编**');
    ok(M.defensiveMissingFactsLine(['资质']).includes('资质'),
        'D7c 老形状(纯字符串数组)也吃得下 —— 契约落地前我自己的桩是那个形状');
    /*
     * 🔴 D7d:八问逐字。C 特意点出 Review 转述里第六问写成「适合谁」,
     *    而 WO 原文是「适合谁·不适合谁」。两边差半句,徽章文案与后端出的题就对不上。
     *    这条把**原文**钉住 —— 引规则不能凭转述。
     */
    ok(Array.isArray(M.DEFENSIVE_QUESTIONS) && M.DEFENSIVE_QUESTIONS.length === 8,
        'D7d0 八问就是八条', String((M.DEFENSIVE_QUESTIONS || []).length));
    ok((M.DEFENSIVE_QUESTIONS || []).includes('适合谁·不适合谁')
        && !(M.DEFENSIVE_QUESTIONS || []).includes('适合谁'),
        'D7d 🔴 第六问逐字是「适合谁·不适合谁」,不是转述里那个「适合谁」');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
if (blocked > 0) {
    console.log(`未评估 ${blocked} 项(见上面 ".." 行)—— **不是通过**,退出码 3`);
    process.exit(3);
}
console.log('全部通过:前后端方向契约一致');
process.exit(0);
