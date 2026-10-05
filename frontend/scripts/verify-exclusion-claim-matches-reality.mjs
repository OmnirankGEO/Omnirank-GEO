#!/usr/bin/env node
/**
 * 判据 · #236-c1a 前端半:「口径已排除『直接问本品牌名』」这句声明必须由**本次实算**驱动。
 *
 * 🔴 缺陷原状:这句是 JSX 里**无条件**写死的一段文字 —— 它陈述的是「本代码打算排除」,
 *    不是「这次到底排没排」。真客户 #700 / #726 这次**一条都没排**,脚注照样对客户宣称排过。
 *
 * 🔴 **拆两半的风险**(Review 点名两边各堵一侧):后端交了、前端没接,或字段名对不上
 *    ⇒ **两边各自绿,页面照旧骗客户**。所以这门除了钉渲染规则,还钉**字段名本身**。
 *
 * 🔴 判据**真调**纯函数,不做文本匹配:文本匹配对「守卫写了但那句仍渲染」天生没分辨力
 *    (本轮同一件事咬过三次)。为此把判定抽成零运行时依赖的 `exclusionClaim.ts`。
 *
 * 只读 frontend/ —— 引用仓外文件的门不许进 build 链(构建阶段只有 frontend/)。
 * 两态退出:0 全过 / 1 有失败。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

console.log('#236-c1a 排除声明:说不说、说几条,都由本次实算决定');

/* 真调:transpile + data URL,不落临时文件;该模块零运行时依赖(只有类型,转译后擦除)。 */
let mod;
try {
    const ts = require_('typescript');
    const src = readFileSync(join(ROOT, 'src/features/publicReportPremium/exclusionClaim.ts'), 'utf8');
    const out = ts.transpileModule(src, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    if (/from\s+['"]\.{1,2}\//.test(out)) throw new Error('转译后仍有运行时相对 import');
    mod = await import('data:text/javascript;base64,' + Buffer.from(out, 'utf8').toString('base64'));
} catch (e) {
    console.log('  FAIL E0 🔴 取不到 `exclusionClaim.ts` ⇒ 本门一条都没跑成:'
        + String((e && e.message) || e).slice(0, 160));
    console.log('       这是**失败**不是未评估:环境不具备就不许放行。');
    process.exit(1);
}
const { shouldClaimExclusion, exclusionClaimText, platformExclusionClause } = mod;

/* ── E1 没排就不许说(含缺省 / null) ─────────────────────────────── */
for (const [v, how] of [[0, '本次实算为 0'], [null, '后端给 null'], [undefined, '老报告没有这个字段']]) {
    ok(shouldClaimExclusion(v) === false && exclusionClaimText(v) === '',
        `E1[${String(v)}] 🔴 ${how} ⇒ **不说**那句 —— 缺省按 0 处理,`
        + '不许回落成「按老样子说」(那等于继续宣称排过)',
        JSON.stringify(exclusionClaimText(v)));
}

/* ── E2 排了就要说,且句中的数字**就是那个字段** ───────────────────── */
for (const n of [1, 5, 12]) {
    const t = exclusionClaimText(n);
    /* 🔴 不只查「含这个数字」:含 5 的句子也可能是「排除 15 条」。
          钉的是**紧邻量词**的那个数,而且全句里不许出现别的数字。 */
    const nums = (t.match(/\d+/g) || []);
    ok(shouldClaimExclusion(n) === true
        && t.includes(`本次排除 ${n} 条回答`)
        && nums.length === 1 && nums[0] === String(n),
        `E2[${n}] 🔴 排了就要说,且句中数字**只有**这一个、就是该字段的值 —— `
        + '另算一个的话会出现「脚注说 5 条、明细里 8 条」而两边各自看起来都对',
        `${t.slice(0, 46)}… · 句中数字 [${nums.join(',')}]`);
}

/* ── E3 反臂:异常值不许被当成「排过」 ────────────────────────────── */
for (const v of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
    ok(shouldClaimExclusion(v) === false,
        `E3[${String(v)}] 反臂:异常值不算「排过」—— 负数/NaN/Infinity 走进正分支就会印出一句鬼话`);
}

/* ── E4 接线:组件真的用了这两个函数,而不是自己又写了一遍 ─────────── */
{
    const comp = readFileSync(
        join(ROOT, 'src/features/publicReportPremium/components/sections/CompetitiveLandscape.tsx'), 'utf8');
    const decommented = comp.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '');
    ok(/shouldClaimExclusion\(/.test(decommented) && /exclusionClaimText\(/.test(decommented),
        'E4 🔴 组件**真的调**这两个函数 —— 判据钉纯函数,而组件另写一份的话,'
        + '纯函数绿着、页面照旧骗客户');
    /* 🔴 反臂:那句文案不许再以**字面量**形式留在组件里。
          留着的话,把守卫删掉它就又无条件显示了,而 E1/E2 照样绿(它们只测纯函数)。 */
    ok(!/口径已排除/.test(decommented),
        'E4b 🔴 反臂:那句文案**不在组件里写死** —— 文案只有一个家(纯函数);'
        + '组件里留一份字面量的话,守卫一删就又无条件显示,而上面几格测不到');
    /*
     * 🔴 [F1 2026-09-17 的同构风险提醒逼出来的] E4 只查**函数名出现过** ——
     *    把 `ownMentionCount` 传进去,E1–E5 **全绿而页面照旧骗客户**:
     *    纯函数测得再全,喂错参数就全白测。钉「调用了谁」不等于钉「拿什么调的」。
     */
    const FIELD = 'excludedBrandDirectedCount';
    const argOk = new RegExp(`shouldClaimExclusion\\(\\s*${FIELD}\\s*\\)`).test(decommented)
        && new RegExp(`exclusionClaimText\\(\\s*${FIELD}\\s*\\)`).test(decommented);
    ok(argOk,
        `E6 🔴 两处调用喂进去的都是 \`${FIELD}\` 本人 —— `
        + '喂错字段(比如 ownMentionCount)时上面每一格都还是绿的,而页面又开始宣称排过',
        (decommented.split('\n').filter((l) => l.includes('ExclusionClaim') || l.includes('ClaimExclusion'))
            .map((l) => l.trim().slice(0, 56)).join(' | ')) || '(没找到调用)');
}

/* ── E11 平台表那半句:同一个判定、同一个数、两个方向 ────────────────── */
{
    /* 🔴 值挑 2/7/13,**避开 0/1/5/10/100** —— 毒最可能写死的就是那几个;
          今天实测过:毒写死 5 时,只喂 5 的那一格照样绿。 */
    for (const n of [2, 7, 13]) {
        const t = platformExclusionClause(n);
        const nums = (t.match(/\d+/g) || []);
        ok(t.includes('已排除品牌定向问答') && t.includes(`本次排除 ${n} 条回答`)
            && nums.length === 1 && nums[0] === String(n),
            `E11[${n}] 🔴 排了就说,且句中数字只有这一个、就是该字段的值`, t);
    }
    for (const v of [0, null, undefined, -3, Number.NaN]) {
        const t = platformExclusionClause(v);
        ok(!t.includes('已排除'),
            `E11r[${String(v)}] 🔴 反臂:没排(含缺省/异常值)⇒ 那半句**整段不出现**`, t);
    }
    /* 🔴 不许返回空串:这半句在句子中间,空串会让整段读成
          「识别率看全部问题;「被推荐」是…」,缺一个连接词。 */
    ok(platformExclusionClause(0).length > 0,
        'E11c 不说那句时返回的是**替代措辞**不是空串 —— 空串会让抬头断成半句话',
        JSON.stringify(platformExclusionClause(0)));
}

/* ── E5 跨窗契约:字段名两边必须是同一个 ──────────────────────────── */
{
    const types = readFileSync(join(ROOT, 'src/features/publicReportPremium/contract/types.ts'), 'utf8');
    const dto = readFileSync(join(ROOT, 'src/features/publicReportPremium/transport/mapDto.ts'), 'utf8');
    const F = 'excludedBrandDirectedCount';
    ok(types.includes(F) && dto.includes(F),
        `E5 🔴 字段名 \`${F}\` 在类型与 DTO 校验两处都在 —— `
        + '拆两半最贵的失败是**字段名对不上**:后端交了、前端没接,两边各自绿而页面照旧骗客户',
        `types=${types.includes(F)} dto=${dto.includes(F)}`);
}

/* ── E7-E10 [#238 甲] 同一句谎话长在**两个屏**上 ────────────────────────
 *
 * 🔴 #236 修的是竞争格局那一屏,而**平台表**(`PlatformPerformance.tsx`)上有一句
 *    一模一样的无条件宣称。真客户 #700 的 brand_directed_valid = 0,一条都没排,
 *    平台表照样说「已排除品牌定向问答」。
 *
 * 🔴 复审自己的话:**这是工单缺陷不是执行缺陷** —— 当时点的是那**一个实例**,
 *    没要求扫全类。所以这里补的关键一格是 **E10 分母锁**:
 *    那句文案在 **frontend/src** 下只许有一个家(纯函数)。
 *    它才是当初能一次抓住这一类的那格 —— 钉实例只能钉住已知的那个。
 */
{
    const readSrc = (rel) => readFileSync(join(ROOT, rel), 'utf8');
    const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
        .replace(/^\s*\/\/.*$/gm, '');

    const PP = strip(readSrc('src/features/publicReportPremium/components/sections/PlatformPerformance.tsx'));
    ok(!/已排除品牌定向问答[^（]/.test(PP) || /shouldClaimExclusion/.test(PP),
        'E7 🔴 平台表那句不再是**无条件字面量** —— 它原来陈述的是「本代码打算排除」,'
        + '不是「这次到底排没排」');
    ok(/platformExclusionClause\(\s*excludedBrandDirectedCount\s*\)/.test(PP),
        'E8 🔴 平台表用的是**同一个判定 + 同一个字段**(不是自己另写一份)—— '
        + '两处各自算就会出现「脚注说 5 条、平台表说 8 条」而两边各自看起来都对');

    const RP = strip(readSrc('src/features/publicReportPremium/components/ReportPage.tsx'));
    ok(/excludedBrandDirectedCount=\{/.test(RP)
        && /competitive\.data\.excludedBrandDirectedCount/.test(RP),
        'E9 🔴 接线:`ReportPage` 真的把**竞争格局那一节的同一个数**传给了平台表 —— '
        + '组件收了 prop 而调用方没传(或传了别的),上面两格照样绿');

    /*
     * 🔴 E10 分母锁:**frontend/src** 下,那句文案只许有一个家。
     *
     * 🔴 [2026-09-18 复审点名] 分母就是 frontend/src,**不是「全站」**。
     *    我第一版抬头写「整个 src」而 walk 的根是 frontend/src —— **门自称的范围比它实际扫的大**,
     *    下一个人会以为这一类已经全扫过。(这正是我同一天写进记忆的那条,写完当天又犯一次。)
     *    后端 services/public_report_presentation.py:884 有**第三个家**
     *    (页面上「样本口径 · 已排除 N 条品牌定向问答」那半句)。它**不是缺陷** ——
     *    有条件、且与前端同源同变量。本门读不到它也不该读:
     *    进 build 链的门不许伸手到 frontend/ 之外(构建阶段只有 frontend/)。
     *    这一格若当初就有,#238 根本不会发生 —— 而 E7/E8 这种按实例点名的格,
     *    只能钉住已经知道的那一处。
     */
    const BS = String.fromCharCode(92);
    const homes = [];
    (function walk(dir) {
        for (const n of readdirSync(dir)) {
            const p = join(dir, n);
            if (statSync(p).isDirectory()) { walk(p); continue; }
            if (!/\.tsx?$/.test(n)) continue;
            const body = strip(readFileSync(p, 'utf8'));
            if (/排除.*(品牌定向|直接问本品牌名)/.test(body)) {
                homes.push(p.slice(ROOT.length + 1).split(BS).join('/'));
            }
        }
    })(join(ROOT, 'src'));
    ok(homes.length === 1 && /exclusionClaim\.ts$/.test(homes[0]),
        'E10 🔴 **分母锁**:那句「已排除品牌定向问答」在 **frontend/src** 下只有**一个家**(纯函数)—— '
        + '#238 就是因为同一句话长在两个屏上、而判据只钉了其中一屏;'
        + '按实例点名的格永远只能钉住已知的那一处',
        `${homes.length} 处:${homes.join(' / ')}`);
}

/* ── E12-E13 [#238 乙] 同一个判定不许在对客面上有两套词 ──────────────────
 *
 * 🔴 233-c2 把 `recommended` 档对客改称「提及」,但**只改了一半**:
 *    平台表列头仍叫「推荐率」、说明仍说「被推荐」、展开行仍说「N 条推荐」、
 *    竞品行仍说「被推荐 N 次」。客户对同一个底层判定看到两套词。
 *    而不能叫「推荐」的理由(该判定会把「目前无法推荐甲公司」判成 recommended)
 *    在这几处**没生效**。
 *
 * 🔴 这一格是**类锁**不是实例锁:#238 的教训就是按实例点名只钉得住已知那一处。
 */
{
    const BS2 = String.fromCharCode(92);
    const strip2 = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
        .replace(/^\s*\/\/.*$/gm, '');
    /*
     * 🔴 **显式排除,写出来给人质疑**(悄悄 filter 掉的排除没人看得见):
     *    `明确推荐` / `条件推荐` 是 `target_outcome` **取值**的标签,
     *    归 233-a1d'(`ed86681ba`)那条分支改,不在本笔范围 ——
     *    在这里一起改会变成同内容两笔、合车时重复应用。
     *    夹具(`fixtures/`)里的演示正文同理由那条分支处理。
     */
    /*
     * 🔴 [2026-09-18 复审一发毒打穿了上一版] 上一版是
     *    `BANNED2 = ['推荐率','被推荐','条推荐']` —— **名字叫类锁,机制是三个串的黑名单**。
     *    复审注入「AI 推荐了你 3 次,推荐次数越多说明越受青睐」⇒ 判据**仍绿**。
     *    换个说法它就瞎。(同族 `a-blacklist-lock-is-blind-to-the-thing-it-exists-to-catch`)
     *    ⇒ 改成:**含「推荐」即红** + **显式豁免名单,每条带理由/归属/何时**。
     *    加黑名单串是在扩大盲区;加豁免条目是在 diff 里留下痕迹让人质疑。
     */
    const EXEMPT = {
        /*
         * 🔴 [2026-09-18 复审第二发毒] 上一版豁免是**整文件的** ——
         *    往豁免文件里注入「AI 已经明确推荐了你,推荐率遥遥领先」⇒ E12 仍绿。
         *    **豁免一个文件等于把那个文件整片让出去。**
         *    ⇒ 每条豁免声明**恰好几行命中**,多一行就红,且数字必须在同一笔里抬。
         *    (这手法我在 E12c 上用过,只是当时没想到往文件内再沉一层。)
         */
        'components/sections/KeyFindings.tsx': {
            hits: 1,
            reason: '`recommendation_blocker`「推荐阻断」是**另一套分类法**(什么在阻碍被推荐),'
                + '不是 target_outcome 的档位标签,与本单要治的「拿会认错的判定做正面承诺」不是一件事。',
            owner: 'A(本单作者)', since: '#238 乙 2026-09-18',
        },
        /*
         * 🔴 [#238 戊 2026-09-18] 这里原来还有 `EvidenceMatrix.tsx` / `primitives.tsx` 两条,
         *    已删 —— **它们在我写的时候是对的,是合并让它们空了**:
         *    那两处的「明确推荐/条件推荐」归 233-a1d'(`ed86681ba`),我核过它不是我树的祖先才豁免;
         *    而它早已随 0913j 上线,合到生产尖 `c254b925f` 之后标签没了,豁免就指空了。
         *
         * 🔴 照出它的正是我昨夜刚加的丁(陈旧条目格)—— 建那一格时谁都没想到
         *    **它第一个抓到的会是合并本身**,而不是某次疏忽。
         *
         * 🔴 教训比条目本身重要:**我那棵树上跑绿,不代表合到生产尖上绿**。
         *    豁免条目描述的是「某文件当前有几行」,而那是**随合并变化的事实**;
         *    凡这类条目,验收必须在**合到现役尖之后**再跑一次。
         */
    };
    const hits2 = [];
    const visited = new Set();
    (function walk2(dir) {
        for (const n of readdirSync(dir)) {
            const p = join(dir, n);
            if (statSync(p).isDirectory()) { walk2(p); continue; }
            if (!/\.tsx?$/.test(n)) continue;
            if (/fixtures[\\/]/.test(p)) continue;          /* 见上:夹具归另一条分支 */
            /* 🔴 连**行尾注释**一起剥:`types.ts` 那行是契约键带一句中文注释,不是渲染串。 */
            const body = strip2(readFileSync(p, 'utf8')).replace(/\/\/.*$/gm, '');
            const rel0 = p.slice(ROOT.length + 1).split(BS2).join('/')
                .replace('src/features/publicReportPremium/', '');
            /*
             * 🔴 [2026-09-18 复审第三发] 上一版在这里就 `if (!body.includes('推荐')) continue;`
             *    —— **零命中的文件在查豁免表之前就被跳过**。把 KeyFindings 那唯一一行删掉
             *    (实际 0 ≠ 声明 1)⇒ 门仍绿。后果不是当场出错,是豁免条目**静默变陈旧**:
             *    E12c 仍数 3 个、夸大真实豁免面,而将来谁往那文件重新加一行「推荐」,
             *    **陈旧条目会静默放行**。
             *    ⇒ 先记「这个豁免键被访问过」,再决定跳不跳;walk 完断言**每个键都被比对过**。
             */
            if (EXEMPT[rel0]) visited.add(rel0);
            if (!body.includes('推荐')) {
                /* 声明了 N>0 却一行都没命中 ⇒ 陈旧条目,当场说出来 */
                if (EXEMPT[rel0] && EXEMPT[rel0].hits !== 0) {
                    hits2.push(`${rel0} 实际 0 行,而豁免声明 ${EXEMPT[rel0].hits} 行(陈旧条目)`);
                }
                continue;
            }
            const rel = rel0;
            const lines = body.split('\n').filter((l) => l.includes('推荐'));
            const ex = EXEMPT[rel];
            if (ex) {
                /* 🔴 豁免是**按条数**的:多出来的那几行不在豁免范围内。 */
                if (lines.length !== ex.hits) {
                    hits2.push(`${rel} 命中 ${lines.length} 行,而豁免只认 ${ex.hits} 行`);
                }
                continue;
            }
            hits2.push(`${rel} → ${lines[0].trim().slice(0, 60)}`);
        }
    })(join(ROOT, 'src/features/publicReportPremium'));
    /* 🔴 豁免键必须**全部被访问到**:文件被删掉 / 改名 / 移走时,条目会留在表里空指,
          而空指的豁免既夸大豁免面,又会在文件回来时静默放行。 */
    for (const k of Object.keys(EXEMPT)) {
        if (!visited.has(k)) hits2.push(`${k} 豁免条目**指不到文件**(已删/改名/移走)⇒ 陈旧`);
    }
    ok(hits2.length === 0,
        'E12 🔴 **类锁**:对客渲染面上**凡出现「推荐」二字即红**(豁免名单除外)—— '
        + '上一版是三个串的黑名单,复审换个说法「AI 推荐了你 3 次」就穿过去了;'
        + '黑名单对它本该抓的东西天生是瞎的',
        hits2.join(' / ') || '零处(豁免 ' + Object.keys(EXEMPT).length + ' 个文件)');

    /* 🔴 豁免条目必须带**理由 + 归属 + 何时** —— 没有理由的豁免等于把锁关掉。 */
    const badEx = Object.entries(EXEMPT)
        .filter(([, v]) => !v.reason || !v.owner || !v.since || v.reason.length < 25
            || typeof v.hits !== 'number')
        .map(([k]) => k);
    ok(badEx.length === 0,
        'E12b 每条豁免都写清**为什么不是同一件事 / 归谁 / 何时加的** —— '
        + '「没人注意到」和「声明过」是两回事,后者才允许留着',
        badEx.join(',') || `${Object.keys(EXEMPT).length} 条都带了`);

    /* 🔴 豁免表长度钉死:加一条就必须在 diff 里看得见,并同笔说明理由。 */
    ok(Object.keys(EXEMPT).length === 1,
        'E12c 🔴 豁免名单**恰好 1 个文件**(只剩 KeyFindings)—— 加第二条就是在开口子,'
        + '必须同一笔把这个数抬上去并写清理由与责任人',
        `${Object.keys(EXEMPT).length} 个:${Object.keys(EXEMPT).map((k) => k.split('/').pop()).join(' ')}`);

    /*
     * 🔴 反臂(复审点名):**契约键一个都不许改**。
     *    `recommended` / `recommendCount` / `recommendRatePct` 在**认 / 发 / 比**
     *    三个角色上各出现过;改显示名是对客口径,改键下游取数就断。
     */
    const keyFiles = {
        '认(类型)': 'src/features/publicReportPremium/contract/types.ts',
        '发(DTO)': 'src/features/publicReportPremium/transport/mapDto.ts',
        '比(组件)': 'src/features/publicReportPremium/components/sections/CompetitiveLandscape.tsx',
    };
    for (const [role, rel] of Object.entries(keyFiles)) {
        const s2 = readFileSync(join(ROOT, rel), 'utf8');
        ok(/recommendCount|recommendRatePct|'recommended'/.test(s2),
            `E13[${role}] 反臂:契约键(recommendCount / recommendRatePct / 'recommended')**原样不动** —— `
            + '改的是对客显示名,不是键;改键后端对表就断',
            rel.split('/').pop());
    }
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
