#!/usr/bin/env node
/**
 * 判据 · WO_218-a1 前端半 —— 「生成标题这次会扣多少」的**显示口径**。
 *
 * 契约原件:`services/topic_gen_charge.py` 模块 docstring(按 sha 读原件,不按消息)。
 *
 * 本门分两件事:
 *   ① **真调**纯函数 `chargeText` / `parseTopicGenCharge`(不是读它的源码文本)——
 *      口径写没写对,只有喂值才看得出来;
 *   ② 钉住**五个入口**都挂了价,以及免费那一面没挂。
 *
 * 🔴 为什么要数五个入口:工单和消息里都只说「按钮旁显示价」,而
 *    `generateTitles` 在 `WritingHall.tsx` 里有**五个**调用点,其中一个
 *    (`runDeliveryNextAction`)的文案是后端给的,通篇 grep「生成标题」找不到它。
 *    **工单点名的实例不是缺陷类** —— 只钉一处的话另外四处静默无价,而门是绿的。
 *
 * 浏览器那一臂在 `test-topic-gen-charge-render.mjs`(真渲染、真读屏)。
 * 两臂分工写在本文件末尾的说明里,免得"哪一格去哪了"没有答案。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成。
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);

let failures = 0;
let ran = 0;
const ok = (cond, name, detail) => {
    ran += 1;
    if (!cond) failures += 1;
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${detail !== undefined ? ` — ${detail}` : ''}`);
};
function cannotRun(what, err) {
    console.log(`\n3 门没跑成:${what} —— ${String((err && err.message) || err).slice(0, 300)}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

const read = (rel) => {
    try { return readFileSync(join(ROOT, rel), 'utf8'); }
    catch (err) { cannotRun(`读不到 ${rel}`, err); return ''; }
};

const HALL = 'src/pages/Writing/WritingHall.tsx';
const MOD = 'src/pages/Writing/topicGenCharge.ts';
const MOCK = 'src/sandbox/mockData.ts';
const hallRaw = read(HALL);
/*
 * 🔴 **先把注释去掉再数。** 第一版直接在原文上数 `generateTitles`,结果被
 *    **我自己刚写的那条注释**满足了(注释里写着「它调的就是 `generateTitles()`」),
 *    分母从 5 变成 6,判据当场假红 —— 反过来也一样:
 *    某天有人在注释里提一句,就能把"少了一个入口"补成"数对了"。
 *    「某串出现过」这种锚会被**与命题无关的行**满足,而注释是最容易出现它的地方。
 * 去掉 `/* … *\/` 块,以及整行的 `//` 与 `*` 续行;JSX 文本与 URL 里的 `//` 不动。
 */
const stripComments = (src) => src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*(\/\/|\*).*$/gm, "");
const hall = stripComments(hallRaw);
const modSrc = read(MOD);
const mock = stripComments(read(MOCK));

console.log('=== WO_218-a1 · 生成标题计费显示口径 ===\n');

/* ── 一、真调纯函数 ───────────────────────────────────────────────────── */
/*
 * 🔴 用 `transpileModule` 而不是 `import` 源文件:`.ts` 里有 `interface` 与
 *    `import type`,Node 直接吃不了。type-only 的东西转译后**整段消失**,
 *    所以这个模块转完是零运行时依赖,可以用 data: URL 直接加载 ——
 *    判据因此测的是**被产品真正 import 的那一份源码**,不是我另抄的一份。
 */
let mod;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(modSrc, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    mod = await import(`data:text/javascript;base64,${Buffer.from(js, 'utf8').toString('base64')}`);
} catch (err) { cannotRun('纯函数模块加载不了', err); }

const { chargeText, parseTopicGenCharge, isChargedFace } = mod;
ok(typeof chargeText === 'function' && typeof parseTopicGenCharge === 'function'
    && typeof isChargedFace === 'function',
    'P0 分母自证:三个被测函数都真的加载进来了 —— 加载失败时下面每一格都会"全绿"',
    `chargeText=${typeof chargeText} parse=${typeof parseTopicGenCharge} isCharged=${typeof isChargedFace}`);

const card = (over = {}) => ({
    feature_code: 'topic_gen', base_points: 80, keyword_count: 3,
    estimated_points: 240, ceiling_points: 240, ...over,
});

/* ① `None` ≠ `0` —— 本单最贵的一条 */
ok(chargeText(null) === null,
    'P1 🔴 `charge` 为 `null`(本次不走计费)⇒ **不显示价**,不是显示 0',
    `得到 ${JSON.stringify(chargeText(null))}`);
ok(chargeText(parseTopicGenCharge(card({ estimated_points: 0, ceiling_points: 0, base_points: 0 }))) === '本次免费',
    'P1b 🔴 `0` 是「本次免费」,和 `null` 必须分得开 —— 压成一种,少写一个判断就会对客户说错话',
    JSON.stringify(chargeText(parseTopicGenCharge(card({ estimated_points: 0, ceiling_points: 0, base_points: 0 })))));

/* ② 显示上限,不是基价、不是实扣、更不是前端自己乘 */
ok(String(chargeText(parseTopicGenCharge(card()))).includes('240'),
    'P2 显示 `ceiling_points`(240),不是 `base_points`(80)',
    JSON.stringify(chargeText(parseTopicGenCharge(card()))));
ok(String(chargeText(parseTopicGenCharge(card({ ceiling_points: 300 })))).includes('300'),
    'P2b 🔴 上限与实扣不同时显示**上限** —— 工单口径:不许显示得比实扣少,客户是按那个数同意扣费的',
    JSON.stringify(chargeText(parseTopicGenCharge(card({ ceiling_points: 300 })))));
ok(parseTopicGenCharge(card({ ceiling_points: 100 })) === null,
    'P2c 🔴 `ceiling < estimated` 的回包整份作废(不显示)—— 退而求其次显示实扣,'
    + '等于把一次契约违规悄悄糊过去',
    JSON.stringify(parseTopicGenCharge(card({ ceiling_points: 100 }))));

/* ③ 同一颗按钮的免费那一面 */
/*
 * 🔴🔴 **这一格的期望在 2026-09-19 被 WO_241 丙 反转了** —— 写在原位,
 *      免得下一个人以为是我写反了,或者照着旧契约把它改回去。
 *
 *   反转前(218-a1):`isNewKwOnly` 面走逐词 `generate-topic`、整段无计费 ⇒ **不显示价**。
 *   反转后(丙 裁定):新加的词 = 全新生产 = **从没付过费** ⇒ 走批量端点带
 *     `per_keyword_plan` 子集 ⇒ **要显示价**。
 *   真正免费的是**补救**(某批次里缺题的那一条,那一批已经付过)。
 *
 * 🔴 契约原件 `43150849e:services/topic_gen_charge.py` **L40–51 是权威节**;
 *    同一文件 L84–93 的旧节结论相反且同样写着「以本文件为准」,已由 C 作废。
 *    旧节最阴的地方是**它陈述的事实仍然为真**(那条逐词路径确实不收费),
 *    过期的只是**映射** —— 读起来一点都不像错的。
 */
ok(chargeText(parseTopicGenCharge(card()), { face: 'recover-missing' }) === null,
    'P3 🔴 **补救面**(某批次里缺题的那一条,那一批已经付过)不显示价 —— '
    + '再收一次就是对同一个词收两次(退费只在"一条题都没出"时触发,部分成功一分不退)',
    JSON.stringify(chargeText(parseTopicGenCharge(card()), { face: 'recover-missing' })));
ok(String(chargeText(parseTopicGenCharge(card()), { face: 'new-keywords' })).includes('240'),
    'P3a 🔴 **新词面要显示价**(2026-09-19 被丙 反转;反转前这一格的期望是"不显示")—— '
    + '那些词从没进过任何批次、从没付过,是全新生产',
    JSON.stringify(chargeText(parseTopicGenCharge(card()), { face: 'new-keywords' })));
ok(isChargedFace('batch-all') === true
    && isChargedFace('new-keywords') === true
    && isChargedFace('recover-missing') === false,
    'P3b **三个面孔各配一格** —— 只喂一两个的话,把某一面判反也照样绿',
    `batch-all→${isChargedFace('batch-all')} new→${isChargedFace('new-keywords')} `
    + `recover→${isChargedFace('recover-missing')}`);

/* ④ 回包不合契约时一律不显示,绝不"尽量凑一个数" */
for (const [why, bad] of [
    ['缺 feature_code', card({ feature_code: '' })],
    ['字段是字符串', card({ ceiling_points: '240' })],
    ['份数 < 1', card({ keyword_count: 0 })],
    ['不是对象', 240],
    ['缺省', undefined],
]) {
    ok(parseTopicGenCharge(bad) === null,
        `P4[${why}] 不合契约的回包 ⇒ 不显示价(不凑数)`,
        JSON.stringify(parseTopicGenCharge(bad)));
}

/* ── 一之二、这个词走哪一面(`faceForKeyword`)────────────────────── */
const { faceForKeyword, keywordIdsWithoutTopics } = mod;
ok(typeof faceForKeyword === 'function' && typeof keywordIdsWithoutTopics === 'function',
    'P5-0 分母自证:两个新函数真的加载进来了', `${typeof faceForKeyword} / ${typeof keywordIdsWithoutTopics}`);

ok(faceForKeyword(7, []).face === 'new-keywords',
    'P5 没有任何选题行 ⇒ **新词面**(从没进过批次 = 从没付过 = 收费)',
    JSON.stringify(faceForKeyword(7, [])));

/*
 * 🔴 契约点名的那条细化,单独一格:「有没有选题」按 topics 行的 **keyword_id** 判,
 *    **不按标题是否为空**。受理凭据行(`optimized_title IS NULL`)也算这个词
 *    **已经进过批次** ⇒ 它是「缺题的词」走**免费补救**,不是「新词」。
 *    按标题判的话,那些词会被当成新词**再收一次钱** —— 对同一个词收两次。
 */
const ROW_NO_TITLE = { keyword_id: 7, generation_request_id: 'req-abc', optimized_title: null };
ok(faceForKeyword(7, [ROW_NO_TITLE]).face === 'recover-missing',
    'P5b 🔴 **有行但标题为空** ⇒ 仍是**补救面**(免费)—— 按标题判会把它当新词再收一次钱',
    JSON.stringify(faceForKeyword(7, [ROW_NO_TITLE])));
ok(faceForKeyword(7, [ROW_NO_TITLE]).generationRequestId === 'req-abc',
    'P5c 补救面带回**那一行的** `generation_request_id` —— 服务端靠它解到那一笔 charge',
    JSON.stringify(faceForKeyword(7, [ROW_NO_TITLE]).generationRequestId));
ok(faceForKeyword(7, [{ keyword_id: 7 }]).generationRequestId === null,
    'P5d 行上没有那个标识 ⇒ 回 `null`(**不编一个**)—— 服务端会 403,那是对的',
    JSON.stringify(faceForKeyword(7, [{ keyword_id: 7 }]).generationRequestId));
ok(JSON.stringify(keywordIdsWithoutTopics([1, 2, 3], [{ keyword_id: 2 }])) === '[1,3]',
    'P5e 子集 = 没有选题行的那些词(与 `faceForKeyword` 同一判据,不另写一遍)',
    JSON.stringify(keywordIdsWithoutTopics([1, 2, 3], [{ keyword_id: 2 }])));

/* ── [WO_254 附带] 有题的词没有"补救"可做 ─────────────────────────────── */
/*
 * 生产实测:有 topic 行**且** `optimized_title` 非空的词(380/2900)仍被判成
 * `recover-missing` ⇒ 打补救端点 ⇒ 后端 403 `TITLE_RECOVERY_NOT_AUTHORIZED`
 * ⇒ toast 叫用户"用『生成标题』重新生成",而页面上没有同名按钮 —— 死路。
 *
 * 🔴 这几格与 P5b/P5c **成对**:一边钉「缺题的仍免费补救」(不许把它当新词再收钱),
 *    一边钉「不缺题的不是补救」。只加后者会把前者的契约推翻,两格必须同时在。
 */
const ROW_TITLED = { keyword_id: 7, generation_request_id: 'req-abc', optimized_title: '一个真标题' };
ok(faceForKeyword(7, [ROW_TITLED]).face === 'has-title',
    'P5f 🔴 [WO_254] 行都有题 ⇒ **不是补救面** —— 走补救端点会 403,而那条 403 指向不存在的按钮',
    JSON.stringify(faceForKeyword(7, [ROW_TITLED])));
ok(faceForKeyword(7, [ROW_TITLED, ROW_NO_TITLE]).face === 'recover-missing',
    'P5g 🔴 **混着一条缺题的 ⇒ 仍是补救面** —— 这一格防的是"把 P5f 写成 rows.some()"那种改法:'
    + '那会让真正缺题的词补不回来(比原缺陷更贵)',
    JSON.stringify(faceForKeyword(7, [ROW_TITLED, ROW_NO_TITLE])));
ok(faceForKeyword(7, [{ keyword_id: 7, optimized_title: '   ' }]).face === 'recover-missing',
    'P5h 只有空白的标题**算没题** —— 否则一行空格就把这个词挡在补救之外',
    JSON.stringify(faceForKeyword(7, [{ keyword_id: 7, optimized_title: '   ' }])));
ok(isChargedFace('has-title') === true,
    'P5i `has-title` **不在免费面里** —— 免费只有补救那一面;'
    + '新面孔默认落到收费侧,不会悄悄多出一个免费入口',
    String(isChargedFace('has-title')));
/*
 * 🔴 静态那一格钉的是**根因**,不是症状:
 *    按钮的闸原来读 `kwTopics`(= `filteredByKeyword`,当前页签过滤后的子集),
 *    而面孔判据读未过滤的 `topics` —— 两个数看的是不同集合,各自单独看都对。
 *    在「待写」页签上,所有选题都已 completed 的词读出来是"一条都没有"⇒ 按钮出现。
 */
ok(/\{canGenerateTitlesNow && \(/.test(hall),
    'P5j 🔴 [WO_254] 每词那颗「立即生成标题」的闸是 `canGenerateTitlesNow`(由唯一判据 `faceForKeyword` 定)',
    /\{canGenerateTitlesNow && \(/.test(hall) ? '闸已改' : '🔴 没找到那个闸');
ok(/const canGenerateTitlesNow = kwFace === 'new-keywords' && plannedPosts\(kw\) > 0;/.test(hall),
    'P5k 🔴 那个闸**只在「真的一行都没有」且这个词要出题(plannedPosts > 0)时为真** —— 有题 / 缺题 / 0 槽都不给这颗按钮'
    + '(缺题的补救入口在选题行上;0 槽是有意不出题,WO_317 第三笔)',
    /const canGenerateTitlesNow/.test(hall) ? '由 new-keywords 决定' : '🔴 定义不见了');
ok(!/\{hasNoTopics && \(\s*<Button/.test(hall),
    'P5l 🔴 反臂:**不许**再用过滤后的 `hasNoTopics` 当按钮的闸 —— 那正是 380/2900 的成因',
    /\{hasNoTopics && \(\s*<Button/.test(hall) ? '🔴 又读回过滤集合了' : '没有');
{
    /* 生产方那一格:行类型没有这一列的话,每行都读作"缺题",这个修**静默失效**。 */
    const modSrc = read(MOD);
    ok(/optimized_title\?: string \| null;/.test(modSrc),
        'P5m 🔴 `TopicRowForFace` 声明了 `optimized_title` —— 少了这一列,'
        + '每一行都读作「缺题」,行为退回改动前而判据 P5f 会**静默**变绿不了',
        /optimized_title\?: string \| null;/.test(modSrc) ? '声明在' : '🔴 没声明');
}

/* ── 二、五个入口都挂了价 ─────────────────────────────────────────────── */
/*
 * 🔴 分母从**调用点**来,不是从我记得的按钮来:
 *    凡是会打到 `generateTitles` 的地方都要有价。
 *    这里先把调用点数出来当分母,再断言徽标数不少于它 ——
 *    分母自己也要有一格,否则"分母缩了"会让覆盖率凭空变好看。
 */
const callSites = [...hall.matchAll(/generateTitles\b/g)].length - 1; // 减去定义那一处
ok(callSites === 5,
    'E0 分母自证:`generateTitles` 的调用点恰好 5 处 —— 变了就要重新分配价的挂点,'
    + '而不是让新入口静默无价',
    `${callSites} 处`);

const badges = [...hall.matchAll(/data-testid="topic-gen-charge"/g)].length;
/*
 * 🔴 [WO_241 丙 2026-09-19 反转] 原来是 **5 处**(五个入口各一)。
 *    裁定之后两处重试翻成**免费补救面**、撤掉价 ⇒ 收费入口只剩 **3 处**:
 *    三元按钮 / 「为这些词生成标题」/ 交付「下一步」。
 *    写成等号而不是 `>=`:少一处要红,**多一处也要红** ——
 *    多出来的那一处极可能是"把价挂回免费面"。
 */
ok(badges === 3, 'E1 🔴 **收费入口恰好 3 处**各挂一处价徽标(重试两处已翻免费面,不挂)', `${badges} 处`);

/* 逐个入口点名,免得"总数够了"掩盖"某一处挂了两个、另一处一个没有" */
const ENTRIES = [
    ['批量/为新词(三元那颗)', /data-testid="sandbox-generate-titles"[\s\S]{0,1600}?topic-gen-charge/],
    ['为这些词生成标题', /为这些词生成标题[\s\S]{0,900}?topic-gen-charge/],
    ['交付「下一步」', /next_action\?\.kind === 'generate_titles'[\s\S]{0,400}?topic-gen-charge/],
];
for (const [name, re] of ENTRIES) {
    ok(re.test(hall), `E2[${name}] 这个入口旁边确实有价`, re.test(hall) ? '命中' : '🔴 没找到');
}
/*
 * 🔴🔴 **这一格整个反过来了(2026-09-19 WO_241 丙)。**
 *    反转前:「两处重试都要挂价」(理由:generate-titles 先扣后退,重试是全新全额扣费)。
 *    反转后:重试按钮**长在选题行里** ⇒ 有行 ⇒ 后端 `if not topics:` 的全额退
 *    **没触发** ⇒ 这个词的钱**还在** ⇒ 免费补救,**不许挂价**。
 *    全新收费只剩主按钮。
 * 🔴 三件一起钉,少一件就能造出最贵的那种状态:
 *    ①不挂价 ②不打 `generateTitles`(整批重跑=再收一次) ③必须打补救端点。
 *    只钉①的话 = **照收钱而屏幕上没有任何数字**,比不改更糟。
 */
const retryBlocks = [...hall.matchAll(/data-testid="retry-title-generation"[\s\S]{0,2200}?<\/Button>/g)]
    .map((m) => m[0]);
ok(retryBlocks.length === 2, 'E2a 分母自证:两处重试都找到了', `${retryBlocks.length} 处`);
ok(retryBlocks.every((b) => !b.includes('topic-gen-charge')),
    'E2[重试 ×2] 🔴 **免费补救面不许挂价** —— 那个词的钱已经付过,再显示一个数等于说要再收一次',
    `${retryBlocks.filter((b) => b.includes('topic-gen-charge')).length} 处仍挂着价`);
ok(retryBlocks.every((b) => !/generateTitles\s*\(/.test(b)),
    'E2b 🔴 免费面**不许打 `generateTitles`**(整批重跑 = 对同一个词再收一次)—— '
    + '徽标与端点必须同笔翻,只去价不改端点是最贵的那种状态',
    `${retryBlocks.filter((b) => /generateTitles\s*\(/.test(b)).length} 处还在打收费端点`);
ok(retryBlocks.every((b) => /generateTopicForKeyword\s*\(/.test(b)),
    'E2c2 🔴 免费面**必须打补救入口** —— 只证"没打收费端点"不够,什么都不打也满足那一条',
    `${retryBlocks.filter((b) => /generateTopicForKeyword\s*\(/.test(b)).length}/2`);

/*
 * 🔴 [注毒 M11 打出来的,反转后依然成立] 只钉「传了 face」不够,**反过来也要钉**:
 *    把某一处偷偷改成 `face: 'recover-missing'`,testid 还在、离得也近,
 *    E1/E2 全绿,而价**静默消失**。E1/E2 数的是"有没有那个格子",
 *    数不到"那个格子被喂了什么"。
 * 🔴 反转后**方向换了**:以前防的是"免费面被挂上价",现在防的是
 *    "**收费面被伪装成免费面**" —— 后者更贵:客户会在没有任何数字的情况下被扣钱。
 */
const freeFaceArgs = [...hall.matchAll(/face:\s*'recover-missing'/g)].length;
ok(freeFaceArgs === 0,
    "E2c 🔴 写作大厅里**没有任何一处**把 `face: 'recover-missing'` 写死 —— "
    + '免费那一面不在这颗按钮上;在这里写死它 = 把会收费的一面伪装成免费面,价静默消失而 testid 还在',
    `${freeFaceArgs} 处`);

/* 三元那颗必须把**面孔**传进去,否则子集面会按默认(batch-all)取价 */
ok(/chargeText\(titleGenCharge, \{ face: titleGenFace \}\)/.test(hall),
    'E3 🔴 三元那颗按钮把**面孔**传进口径函数 —— 不传的话两面会共用同一个默认面',
    /chargeText\(titleGenCharge, \{ face: titleGenFace \}\)/.test(hall) ? '命中' : '🔴 没传');
ok(/const titleGenCharge = isNewKwOnly \? topicGenChargeNewOnly : topicGenCharge;/.test(hall),
    'E3b 🔴 **新词面读新词面那一份价**(`charge_new_keywords_only`),不是整表价 —— '
    + '拿整表价显示 = 10 个词的项目加 2 个新词时说 10 份而实扣 2 份',
    '命中');
ok(/const titleGenFace: TitleGenFace = isNewKwOnly \? 'new-keywords' : 'batch-all';/.test(hall),
    "E3a 🔴 两面**都**落在收费面上(`new-keywords` / `batch-all`)—— "
    + '反转前新词面是免费的,这一格钉住它已经不是了',
    '命中');

/*
 * 🔴 **这条锁被重新瞄准过一次,原因值得写下来。**
 *    第一版找的是字面量 `face: 'recover-missing'`。而后半的实现是用
 *    `faceForKeyword()` **在运行期定面孔**,那个字面量**一次都不出现** ——
 *    于是锁绿着,却**咬不到任何东西**。写锁时我以为免费面会以字面量出现,
 *    那是对「将来的代码长什么样」的猜测,而锁不该建立在猜测上。
 *    ⇒ 改成按**行为**认免费面:谁调了补救入口 `generateTopicForKeyword`,谁就是免费面。
 */
const miswired = (src) => {
    const out = [];
    for (const m of src.matchAll(/generateTopicForKeyword\s*\(/g)) {
        const win = src.slice(Math.max(0, m.index - 900), m.index + 900);
        if (/generateTitles\s*\(/.test(win)) out.push(src.slice(Math.max(0, m.index - 60), m.index + 40));
    }
    return out;
};

const freeEntries = [...hall.matchAll(/generateTopicForKeyword\s*\(/g)].length;
ok(freeEntries >= 3,
    `R1 分母自证:免费补救入口 **${freeEntries} 处**(定义 1 + 调用点)—— `
    + '0 处说明我认免费面的方式又失效了(实现换个写法它就瞎)',
    `${freeEntries} 处`);

ok(miswired(hall).length === 0,
    'R1a 🔴 调补救入口(免费)的地方,附近不许出现 `generateTitles(` —— '
    + '标免费却打收费端点 = **照收钱而屏幕上没有任何数字**,比不改更糟',
    miswired(hall).map((x) => x.replace(/\s+/g, ' ')).join(' | ') || '零处违规');

/* 🔴 正样本:真实违规为 0 时 R1a 天然全绿 —— 这一格是它的牙证。 */
const SYNTHETIC_MISWIRE = `
    <Button onClick={() => { void generateTitles(); }}>
        重新生成标题 {void generateTopicForKeyword(1, 'x')}
    </Button>`;
ok(miswired(SYNTHETIC_MISWIRE).length === 1,
    'R1b 🔴 探测器正样本:喂一段"免费入口旁边还调 generateTitles"的合成代码,它**必须**报违规',
    `合成样本命中 ${miswired(SYNTHETIC_MISWIRE).length} 处(必须 = 1)`);

/* ── 二之三、后半接线 ───────────────────────────────────────────────── */
ok(/per_keyword_plan: perKeywordPlan/.test(hall),
    'E5 🔴 新词面真的发 `per_keyword_plan` 子集 —— 不发的话后端按整表出题整表收费',
    '命中');
ok(/slots: onlyKeywordIds\.includes\(kw\.id\) \? \(kw\.required_articles \?\? 0\) : 0/.test(hall),
    'E5b 🔴 子集外的词**显式置 0**,不是省略 —— 显式 0 = 「这个词这次不出题」,省略会被当缺省(照出照收);'
    + '子集内发槽数 `slots`(后端读的键,见 tests/per_keyword_plan_contract_2026_09_28),缺值才兜底 0,0 槽不被吃成 1',
    '命中');
ok(/generateTitles\(keywordIdsWithoutTopics\(/.test(hall),
    'E5c 🔴 新词面发的子集来自**同一个判据**(`keywordIdsWithoutTopics`),不是就地再筛一遍',
    '命中');
ok(/generation_request_id: generationRequestId/.test(hall),
    'E5d 🔴 补救请求带 `generation_request_id` —— 不带的话补救闸第一行就 403',
    '命中');
ok(/code === 'TITLE_RECOVERY_NOT_AUTHORIZED'/.test(hall),
    'E6 🔴 403 按 **code** 分支不按文案 —— 补救闸对所有拒绝理由一律同一句话,按文案判是把会改的话当协议',
    '命中');
ok(!/const generateTopicsForMissingKws/.test(hall),
    'E7 🔴 `generateTopicsForMissingKws` **已退役** —— 它把新词发给补救端点,上线后每词 403,'
    + '而它的循环把失败吞进 console.error 再报成功',
    '定义点 0 处');

/* ── 三、前端不许自己乘 ───────────────────────────────────────────────── */
/*
 * 🔴 这一格是**黑名单**,天生对"换个写法"是瞎的(本仓
 *    a-blacklist-lock-is-blind-to-the-thing-it-exists-to-catch)。
 *    所以它只当第二道:第一道是 P2/P2b —— 显示的必须**逐字等于**某个后端字段,
 *    自己乘出来的数过不了那两格。
 */
const mulHits = [...hall.matchAll(/base_points\s*\*|\*\s*[A-Za-z_.]*keyword_count/g)].map((m) => m[0]);
ok(mulHits.length === 0,
    'E4 🔴 写作大厅里没有 `base × keyword_count` 这种自算 —— 契约逐字写着「前端不要自己乘」:'
    + '倍率口径一变两边分家,而分家时两边各自都对',
    mulHits.join(' | ') || '零处');
ok(!/base_points/.test(modSrc.split('export function chargeText')[1] || ''),
    'E4b 口径函数里也没碰 `base_points` —— 要显示什么就读什么字段',
    '命中零处');

/* ── 四、沙盒是第二个后端 ─────────────────────────────────────────────── */
ok(/function sandboxTopicGenCharge/.test(mock) && /charge: sandboxTopicGenCharge\(/.test(mock),
    'S1 🔴 教程沙盒的详情夹具也带 `charge` —— 沙盒是第二个后端,不跟上的话教程那一步'
    + '**安静地没有价**:不报错、不变红,与生产分家而无人看见',
    `${[...mock.matchAll(/charge: sandboxTopicGenCharge\(/g)].length} 个分支挂了`);
ok([...mock.matchAll(/charge: sandboxTopicGenCharge\(/g)].length === 2,
    'S1b 详情夹具的**两个** return 分支都挂了(in_hall 与主分支)—— '
    + '只挂一个的话教程在另一半路上无价,而那正是"存在≠可达"的形状',
    `${[...mock.matchAll(/charge: sandboxTopicGenCharge\(/g)].length}/2`);

/* ── 五、「不知道余额」不许显示成一个钱数 ─────────────────────────────── */
/*
 * 这一条是复审在核我的桩时抓出来的**生产真缺陷**,同笔一起修:
 * `walletStatus` 以前只守了动作(4323),没守显示,而那一行的注释自称它守了显示。
 */
const insufficientLines = [...hall.matchAll(/insufficient=\{[^}]*\}/g)].map((m) => m[0]);
ok(insufficientLines.length === 2, 'W0 分母自证:`insufficient` 恰好两处', `${insufficientLines.length} 处`);
ok(insufficientLines.every((l) => l.includes("walletStatus === 'ready'")),
    "W1 🔴 两处 `insufficient` 都要 `walletStatus === 'ready'` —— 否则余额还没拿到时 "
    + '`totalPoints` 是空钱包的 0,徽标显示「只够 0 篇」:**「不知道你的余额」被显示成「你只够写 0 篇」**',
    insufficientLines.filter((l) => !l.includes("walletStatus === 'ready'")).join(' | ') || '两处都有');
ok(/if \(!isAdmin && walletStatus !== 'ready'\)/.test(hall),
    'W2 动作那一侧的同源判断还在 —— 显示与动作必须同源,'
    + '两边各写一套的话会出现"徽标说够、点下去说不知道"',
    '命中');

console.log('');
console.log('两臂分工(免得"哪一格去哪了"没有答案):');
console.log('  · 本门:口径函数真调 + 五个入口的挂点 + 不自算 + 沙盒 + 余额未知');
console.log('  · test-topic-gen-charge-render.mjs:真渲染写作工作台,读屏上那个数');
console.log('    (份数 3→5 数要跟着变 · 后端没给价要零显示 · 上限小于实扣要零显示)');
console.log('  🔴 浏览器臂只覆盖到**两个**入口(重试与交付下一步需要失败选题/next_action 夹具,');
console.log('     那两处由本门的 E2 从源码钉);说出来,免得浏览器臂"全绿"被读成"五个入口都在浏览器里验过"。');
console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
