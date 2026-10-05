#!/usr/bin/env node
/**
 * 判据 · #190 G2 —— 逐阶段**真浏览器可见性**臂。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * G1 钉的是结构(S−C / C−S / 锚点存在)。但"阶段有人消费"不等于
 * "用户真的看得见一张引导卡":消费者可能被别的条件挡住(#180-B 那发毒
 * `|| true` 就是这一格 —— 引导还在源码里,屏幕上永远不出现)。
 * 所以这一层把状态机**逐个推到每个阶段**,渲染对应页面,断言:
 *   · `[data-coach-card]` 真的在 DOM 里,且
 *   · `getBoundingClientRect()` 非零、不被 hidden ⇒ **看得见**。
 *
 * 🔴 **未评估不是通过。** 44 个阶段里有 22 个的消费者住在我还没有 harness 的页面
 *    (体检 NewDiagnosis / 监测 Monitoring / AwaitingConfirmDialog)。
 *    那些**不报绿也不报红**,逐个列出缺什么,rc=3。
 *    报绿 = 骗自己;报红 = 把"还没测"说成"坏了",下一个人会去改没坏的代码。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-tutorial-coachmark-render.mjs
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a190-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
let failed = 0;
let pending = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
const skip = (m, d = '') => {
    console.log(`  ..   未评估 ${m}${d ? ` — ${d}` : ''}`);
    pending++;
    /* 🔴 记下是哪个阶段:事后要逐个核对它在 PENDING_DECLARED 里有没有声明。 */
    const m2 = /^G2\[([^\]]+)\]/.exec(m);
    if (m2) skippedStages.add(m2[1]);
};
const section = (t) => console.log(`\n=== ${t} ===`);
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1500));
    process.exit(1);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

// ── 分母来自枚举器 ────────────────────────────────────────────────
let data;
try {
    data = JSON.parse(execFileSync(process.execPath,
        [join(ROOT, 'scripts/enumerate-tutorial-stages.mjs'), '--json'],
        { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
} catch (e) { unusable('枚举器跑不起来 ⇒ 没有分母', e); }

/**
 * 阶段 → 挂哪个页面。
 * 🔴 这张表**只列我真有 harness 的页面**;没列到的阶段一律报未评估,
 *    并说清缺什么。硬凑一个"差不多的页面"去跑,只会得到一堆假红。
 */
const SURFACE_OF = {
    quote: ['step2-sidebar', 'step2-page', 'step2-online-form'],
    writing: [
        'step3-sidebar', 'step3-page', 'step3-explain-modes', 'step3-show-competitors',
        'step3-upload-kb', 'step3-upload-kb-click', 'step3-gen-titles', 'step3-expand-titles',
        'step3-show-titles', 'step3-start-writing', 'step3-show-articles', 'step3-go-publish',
        /* [#194] 这两个的消费者也在写作大厅里(补发闭环的写作那一段) */
        'step4-opt-write', 'step4-opt-gopublish',
    ],
    publish: [
        'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart',
        'step3-pub-batch-send',
        /* [#194] 补发闭环最后一步的消费者在发布中心 */
        'step4-opt-batch',
    ],
    /*
     * [#194] 体检页。第一步那四个阶段的消费者都在 `NewDiagnosis.tsx` 里。
     * 🔴 `sidebar` 不列在这儿:它的消费者一半在体检页、一半在侧栏外壳(泛型查表),
     *    挂单页证不了侧栏那一半 —— 列进来会把"证了一半"说成"证过了"。
     */
    diagnosis: ['page-intro', 'brand-name', 'autofill', 'submit'],
    /*
     * [#194] 监测页。step4 这一串的消费者分布在 index.tsx / KeywordTable / ActionCards,
     * 三个都长在这一页里,所以同一个面就够。
     */
    /*
     * [#194] `step4-opt-*` 原来报「消费者在 WritingHall / PublishCenter」——
     * 🔴 那不是"缺 harness",是**我没把它们归到已有的面上**。理由写错了:
     *    照着它去补 harness 会补出第三个写作页 harness,而真正缺的只是这三行映射。
     */
    monitoring: [
        'step4-page', 'step4-intro', 'step4-rate', 'step4-good', 'step4-bad',
        'step4-fix', 'step4-send', 'step4-opt-confirm', 'step4-recovered',
        'step4-token-card', 'step4-token-share', 'step4-finish',
    ],
};
/**
 * [#194] **前置动作**:有几个阶段的引导只在用户先做了一步之后才出现
 * (选中关键词之后才有底部操作栏、点开弹窗之后才有弹窗里那段)。
 *
 * 🔴 每一步都要**自证点到了**:点不到就报未评估并说清是哪一步没点上,
 *    而不是把"我没点到"说成"引导不见了"。
 */
const SURFACE_PREPARE = {
    /*
     * 🔴 写作大厅的大部分引导长在**项目详情**里:没选项目时那一片根本不渲染,
     *    于是 12 个 step3 阶段全报「0 张卡」。这不是引导坏了,是我的桩停在列表页。
     *    沙盒客户固定是「一路顺风出行服务」(`src/sandbox/mockData.ts` DEMO_BRAND_NAME),
     *    夹具名字**从产品自己的沙盒数据里取**,不是我编的。
     */
    writing: [
        /* 🔴 顺序要紧:**先选客户,再选项目**。项目列表按当前客户过滤,
           不先选客户时大厅显示「暂无项目」—— 第一版就卡在这儿(debug 打出来才看见)。 */
        { byText: '切换客户', why: '展开左上角客户切换器', optional: true },
        { byText: '一路顺风出行服务', why: '在左上角选中沙盒客户,项目列表才有东西' },
        /*
         * 🔴 这一步**不能再按客户名找**:DOM 里第一个命中的是左上角那条,
         *    于是"点项目"变成了"又点一次客户",而页面纹丝不动 ——
         *    而判据看见的是那张与阶段无关的「点这个项目卡进入」,一度报了 6 个假绿。
         *    改成按项目卡上那颗按钮的文字找,并把左上角排除在搜索范围外。
         */
        /*
         * 🔴 [WO_235] 这颗按钮的**文案随项目状态变**(`WritingHall.tsx:5536-5541`):
         *    pending →「开始创作」· titles_ready →「检查标题」· 其余 →「查看详情」。
         *    钉死「创作」二字,等于只有项目在 pending 时才点得到 —— 补发那几个阶段
         *    要求项目已经走到 submitted,于是前置动作当场失手。
         *    **钉名字不是钉命题**:要钉的是"进工作台那颗按钮",不是它今天叫什么。
         */
        { byText: ['创作', '检查标题', '查看详情'], notWithin: '[data-testid="real-sidebar"]',
            why: '点项目卡上进工作台那颗按钮(文案随状态变)—— step3 的引导长在工作台里' },
    ],
};

/**
 * [#194] 跑完前置动作之后**要把阶段按回去**的那些。
 *
 * 🔴 为什么不是一刀切:进项目工作台那一下,产品会
 *    `setTutorialStage('step3-explain-modes')`(`WritingHall.tsx:5147-5159`,
 *    "首次进这个项目把 mode/competitors/stage 都拉到起点")。
 *    所以**工作台里的**阶段必须按回去,否则屏幕上永远是 explain-modes 那张。
 * 🔴 但**列表页那几个**阶段不能按回去:它们的引导只在"还没选项目"时存在,
 *    强行按回阶段而 UI 已经进了工作台,反而把本来看得见的那张弄没了
 *    —— 我一刀切地加这一步,当场把 5 个冻结表里的阶段打红,
 *    而那 5 条红**是我造的**,不是产品回退。冻结表在这里起了作用。
 */
const RESTAGE_AFTER_PREP = [
    'step3-show-competitors', 'step3-upload-kb', 'step3-upload-kb-click',
    'step3-gen-titles', 'step3-start-writing', 'step3-go-publish',
    'step4-opt-write', 'step4-opt-gopublish',
];

/**
 * [WO_235] **挂载之前**就要成立的后端状态。
 *
 * 🔴 和 `PREPARE` 的区别是时机,不是手段:补发那一串要求项目详情
 *    **第一次拉回来时**就已经是补发态 —— 页面挂上去之后再改沙盒状态,
 *    组件不会自己重拉,于是屏幕停在「0 个选题」,而读数是「0 张卡」,
 *    与"这个阶段的消费点死了"完全同形。
 * 🔴 仍然走产品自己的 authFetch(沙盒那条路),不去碰 `sandboxWriting` 模块变量。
 */
const PRE_MOUNT = {
    'step4-opt-write': [
        { method: 'POST', url: '/api/writing/optimize-generate', body: { keyword_id: 40003 },
            why: '把沙盒项目推进补发态(mockData.ts:1788)' },
    ],
    'step4-opt-gopublish': [
        { method: 'POST', url: '/api/writing/optimize-generate', body: { keyword_id: 40003 },
            why: '把沙盒项目推进补发态' },
    ],
};

const PREPARE = {
    /*
     * [#194] 工作台里的引导是**一步一步长出来**的:进了工作台先看到「找同行」那张,
     * 后面几张要先把前一步做掉。每一步都自证点到了,点不到就报未评估并说清是哪一步。
     */
    'step3-show-competitors': [
        { bySelector: '[data-testid="peer-research-btn"]', waitMs: 1800,
            why: '先点「找同行」——同行列表出来了,那张讲解才挂得上' },
    ],
    /*
     * 🔴 [#234-a1 2026-09-17] 上传区在**抽屉里**,不点开抽屉就没有那张卡。
     *    不加这一步的话,本阶段永远落在「未评估」桶里 —— 而未评估桶常年十几项,
     *    「消费点是死的」和「桩没把页面推到位」在读数上完全同形。
     *    这一件就是那么藏了这么久:它的消费点原本住在一个零调用点的函数里。
     */
    'step3-upload-kb-click': [
        { bySelector: '[data-testid="knowledge-open-drawer"]', waitMs: 1200,
            why: '打开资料抽屉 —— 上传区(以及它的引导卡)长在抽屉里' },
    ],
    /*
     * 🔴 报价页空列表上那张「点这里新建报价」(`sandbox_quote_new_btn`)
     *    的 disabled 只有 `!isSandboxActive()` —— **与阶段无关**。
     *    它一度让 step2 的三个阶段一起报绿(归因加上之后才看见)。
     *    这一阶段自己的引导长在**新建之后**那张表单里,所以先点进去。
     */
    /*
     * [WO_235] 「开始写作」那颗按钮长在**标题工具条**里,没有标题就没有那一条,
     * 于是本阶段长期躺在未评估桶里 —— 而"消费点死了"和"我的桩停在没标题的那一步"
     * 读数完全同形。缺的只是把项目推到"标题已生成":点产品自己的那颗按钮。
     */
    'step3-start-writing': [
        { bySelector: '[data-testid="sandbox-generate-titles"]', waitMs: 2500,
            why: '先点「批量生成标题」—— 标题出来了才有「开始写作」那一条工具栏' },
    ],
    /*
     * [WO_235] 「进入发布中心」那颗按钮长在**已完成页签**的工具条里,
     * 要 6 篇都写完才有。沙盒按 elapsed 推(`mockData.ts:966` 8 秒满),
     * 所以这里走用户的真路径:生成标题 → 开始写作 → 等写完 → 切页签。
     * 🔴 等的时间比 8 秒多一点,不是掐着点 —— 掐点的等法在慢一档必然截断。
     */
    'step3-go-publish': [
        { event: 'wallet:refresh', waitMs: 1200,
            why: '余额那一次请求发在鉴权落定前、被静默丢弃 ⇒ 余额恒 0、写作被预检拦死' },
        { bySelector: '[data-testid="sandbox-generate-titles"]', waitMs: 2500,
            why: '先生成标题' },
        /* 🔴 等 17 秒不是随手写的:6×390=2340 越过 `CHARGE_CONFIRM_THRESHOLD=2000`
              (`WritingHall.tsx:2501`)⇒ 先走 **5 秒反悔 toast**,写作才真开始,
              沙盒再按 elapsed 跑满 8 秒(`mockData.ts:966`)。5+8 是下界,留一点余量。 */
        { byText: '开始写作', waitMs: 17000, why: '让 6 篇文章真写完(含 5 秒反悔窗)—— 没有已完成的文章就没有那条工具栏' },
        { byText: '已完成', optional: true, waitMs: 1200, why: '切到「已完成」页签' },
    ],
    /*
     * [WO_235] 补发闭环那两张卡长在**优化页签**里,而优化页签只有在
     * `optimizeStage !== 'none'` 时才有东西(`mockData.ts:986`)。
     * 那个状态由产品自己的 `POST /api/writing/optimize-generate` 推
     * (`mockData.ts:1788`),所以这里就调它 —— 走 authFetch,与用户在监测页
     * 点「智能补足」触发的那一次同一条路。
     */
    'step4-opt-write': [
        { byText: '优化', optional: true, waitMs: 1200, why: '切到「优化」页签(补发态由 PRE_MOUNT 摆好)' },
    ],
    'step4-opt-gopublish': [
        { byText: '优化', optional: true, waitMs: 1200, why: '切到「优化」页签' },
    ],
    'step2-online-form': [
        { byText: '新建报价', why: '进新建报价表单,「选诊断报告」那张引导才在' },
    ],
    'step4-send': [
        { byText: '选中未达标', why: '选中未达标的词,底部才会出现「智能补足」操作栏' },
    ],
    'step4-opt-confirm': [
        { byText: '选中未达标', why: '先选中未达标的词' },
        { byText: '智能补足', why: '点开补发弹窗,那一段引导长在弹窗里' },
    ],
    'step4-token-share': [
        { byText: '客户门户', why: '点开 Token 弹窗 —— 这一阶段的讲解长在弹窗里面' },
    ],
};

/**
 * [#194] 🔴 **有些阶段的引导根本不是 coach mark。**
 *
 *    `step4-token-share` 是 TokenDialog 里 `tutorialMode` 那一段说明,
 *    `step4-finish` 是一整个收尾总结弹窗 —— 两者都不经过 `FeatureTooltip`,
 *    所以 `[data-coach-card]` 这个探针**永远**看不到它们。
 *    以前把它们报成「未评估 · 缺 harness」是**一个错的理由**:harness 补齐了也照样看不见。
 *    ⇒ 这类阶段换一个探针:断言那段文字**真的可见**(rect 非零、不被 hidden)。
 *    它们不进 coach mark 的冻结表,单独记在 `DIALOG_BASELINE` 里,免得两种证据混成一笔。
 */
const DIALOG_PROBE = {
    'step4-token-share': {
        needle: '白标链接',
        why: 'TokenDialog 的 tutorialMode 说明段',
    },
    'step4-finish': {
        needle: '你已掌握',
        why: '沙盒 step4 收尾的总结弹窗(不是 coach mark)',
    },
    'step4-opt-confirm': {
        needle: '智能补足文章',
        why: '补发弹窗本身 —— 这一阶段的引导是「锁住弹窗强制走完」,不是一张卡',
        /*
         * @@R@@ 只断言"弹窗开着"是不够的:**任何**阶段点开它都会显示这句标题,
         *    那条锚会被一件无关的事满足。这一阶段真正的行为是**锁**:
         *    点背景关不掉。所以这里点一下背景,再断言它**还在**。
         */
        lockCheck: true,
    },
};
/** 用 DIALOG_PROBE 证过的阶段(只许变长,同 VISIBLE_BASELINE)。 */
const DIALOG_BASELINE = ['step4-token-share', 'step4-finish', 'step4-opt-confirm'];

/**
 * 🔴 **已经证明能渲染出可见引导卡的阶段**(冻结表)。
 *    这几个是硬要求:一旦不再可见就是 **FAIL**,不是"未评估"。
 *
 *    为什么需要它:G2 对"没看见卡"默认报未评估(因为多半是我的桩没喂够页面数据)。
 *    但那样一来,**已经能看见的阶段回退了也只报未评估** —— 门就抓不住回归。
 *    冻结表把这两件事分开:表里的必须可见,表外的才允许未评估。
 *    新增能跑通的阶段就往表里加(只许变长,不许变短)。
 */
const VISIBLE_BASELINE = [
    /* #190 建表时的四个(`step2-online-form` 见下) */
    'step2-sidebar', 'step2-page', 'step3-pub-pick-media', 'step3-pub-add-cart',
    /*
     * 🔴 `step2-online-form` 当初的绿是**借来的** —— 屏幕上那张是与阶段无关的
     *    `sandbox_quote_new_btn`(空列表上的「新建报价」)。加归因之后露了馅,
     *    补上前置动作(先点新建报价)才重新进表。
     */
    'step2-online-form',
    /* [#194] 体检页 */
    'page-intro', 'brand-name', 'autofill', 'submit',
    /* [#194] 写作大厅:列表页那几个 */
    'step3-sidebar', 'step3-page', 'step3-explain-modes',
    'step3-expand-titles', 'step3-show-titles', 'step3-show-articles',
    /* [#194] 写作工作台:要先进工作台**并把阶段按回去**(进项目会把阶段拉到起点) */
    'step3-show-competitors', 'step3-gen-titles',
    /*
     * 🔴 [#234-a1 2026-09-17] `step3-upload-kb` 进表。
     *
     *    它此前**没在表里**,而这正是这个缺陷藏了这么久的原因:
     *    它的消费点住在 `renderKnowledgePanel`(全仓零调用点)里 ⇒ 屏幕上永远没有卡,
     *    而本门对「没看见卡」的默认处置是**未评估**(多半是我的桩没喂够页面数据)——
     *    于是「消费点是死的」和「桩缺数据」在读数上**完全同形**,而未评估桶常年 11–22 项,
     *    多一项不显眼。G1a/G1b 那边也是绿的,因为它们数的是源码引用。
     *    ⇒ 修好并实测可见之后必须进表:表里的不可见就是 FAIL,不再允许退回未评估。
     *    `-click` 那一格还额外要一条**按阶段的前置动作**(先点开抽屉,见 PREPARE)——
     *    不加的话它同样永远停在未评估里,而那正是本缺陷的藏身形态。
     */
    'step3-upload-kb', 'step3-upload-kb-click',
    /*
     * 🔴 [WO_235 2026-09-18] `step3-start-writing` 烧单进表(声明表 10 → 9)。
     *    它缺的只是一步:**先点「批量生成标题」**——「开始写作」那颗按钮长在标题工具条里,
     *    没有标题就没有那一条。补上前置动作之后卡当场可见。
     *    进表的意义在于**从此不许退回未评估**:烧掉一条却让它悄悄滑回桶里,
     *    等于这个数白降了(G4d 管声明表那一侧,这张表管读数那一侧,两边都要钉)。
     */
    'step3-start-writing',
    /* [#194] 监测页(coach mark 那一类) */
    'step4-page', 'step4-intro', 'step4-rate', 'step4-good', 'step4-bad',
    'step4-fix', 'step4-send', 'step4-recovered', 'step4-token-card',
];

/** 缺 harness 的页面 → 缺什么(报未评估时逐条说清)。 */
/**
 * 🔴 [WO_235 · 2026-09-18] **未评估必须被声明**。
 *
 *    本门对「没看见卡」的默认处置是「未评估」(多半是我的桩没把页面推到位)——
 *    这个默认**通常是对的**,但它让「消费点是死的」与「桩缺数据」读数同形。
 *    #234 就是这么藏了很久:`step3-upload-kb` 不在冻结表里,于是它的死消费点
 *    和我的桩缺数据长得一模一样,而桶里常年十几项,多一项不显眼。
 *
 *    ⇒ 从现在起:**每一项未评估都要在这张表里有一条**,写清
 *      kind(为什么没评估)/ reason / owner / since。没声明的未评估**当场红**。
 *      这不等于桶清零(那是后续),但桶里**不能再静默躺东西**。
 *
 * kind 三类,处置各不相同:
 *   · no-card-by-design —— 这个阶段本来就不该有卡,**永久**豁免;
 *   · needs-shell-harness —— 缺的是**整壳/整页** harness,工作量大,单独立卡;
 *   · needs-stage-prep —— 页面 harness 已有,缺的是**把页面推到那一步的前置动作**,
 *     这一类是可以逐个补掉的,是 WO_235 真正要烧掉的那部分。
 */
const PENDING_DECLARED = {
    done: {
        kind: 'no-card-by-design',
        reason: '教程终态:走完之后没有下一步可引导,本来就不该有卡。',
        owner: '永久豁免', since: 'WO_235 2026-09-18',
    },
    sidebar: {
        kind: 'needs-shell-harness',
        reason: '消费者一半在体检页、一半在侧栏外壳(泛型查表)——挂单页证不了侧栏那一半,'
            + '列进来会把「证了一半」说成「证过了」。要整壳 harness。',
        owner: 'A · 另立卡', since: 'WO_235 2026-09-18',
    },
    'step4-sidebar': {
        kind: 'needs-shell-harness',
        reason: '同上:侧栏外壳按 stage 查表,源码里没有字面比较,只有整壳 harness 能验。',
        owner: 'A · 另立卡', since: 'WO_235 2026-09-18',
    },
    'step3-go-publish': {
        kind: 'needs-stage-prep',
        /* 🔴 [WO_235 2026-09-18 实测] 原来写「缺前置动作」太笼统。真正卡住的是**余额**:
              `/api/wallet` 的响应被 WalletContext 按 user-id 竞态静默丢弃
              (`WalletContext.tsx:379-381`,请求发在鉴权落定之前),余额恒 0 ⇒
              「开始写作」被余额预检拦死 ⇒ 永远没有已完成的文章 ⇒ 这张卡的工具栏不存在。
              屏幕上只写「只够 0 篇」——**读起来像"这个服务商没钱",不像"我丢了那次响应"**。 */
        reason: '卡在余额:钱包响应被 user-id 竞态静默丢弃(WalletContext:379),余额恒 0、写作被预检拦死,于是没有已完成文章、没有那条工具栏。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
    'step4-opt-write': {
        kind: 'needs-stage-prep', reason: '补发态已能在挂载前用产品自己的 optimize-generate 摆好(PRE_MOUNT),但优化页签仍 0 张卡,尚未定性 —— 不写"缺前置动作",那会掩盖"还没查清"。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
    'step4-opt-gopublish': {
        kind: 'needs-stage-prep', reason: '同 step4-opt-write:补发态已摆好,优化页签仍 0 张卡(消费点在 WritingHall:7026),尚未定性。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
    'step3-pub-pick-article': {
        kind: 'needs-stage-prep', reason: '消费点在 PublishCenter,发布中心 harness 已有,缺「文章列表非空」那一步。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
    'step3-pub-batch-send': {
        kind: 'needs-stage-prep', reason: '消费点在 PublishCenter:1778,触发条件是购物车 >= 6 条,缺把车填满那一步。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
    'step4-opt-batch': {
        kind: 'needs-stage-prep', reason: '消费点在 PublishCenter:1718/3278,缺补发闭环最后一步的前置状态。',
        owner: 'A · WO_235 烧单', since: 'WO_235 2026-09-18',
    },
};

/** 被 skip 过的阶段(用于事后核对:每一项都必须在上表里声明)。 */
const skippedStages = new Set();

const NO_HARNESS = {
    'NewDiagnosis.tsx': '体检页(/diagnosis/new)还没有 harness · 要品牌档案 + 诊断桩',
    'index.tsx': '监测页(/monitoring)还没有 harness · 要关键词表 + 排名数据桩',
    'KeywordTable.tsx': '监测页关键词表还没有 harness · 同上',
    'ActionCards.tsx': '监测页动作卡还没有 harness',
    'mockData.ts': '终态 done · 本来就不该有引导卡',
    'tutorialLayoutContract.ts': '侧栏外壳(泛型查表)· 要整壳 harness',
};

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));
writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from ${q('src/context/AuthContext.tsx')};
import { OrganizationProvider } from ${q('src/context/OrganizationContext.tsx')};
import { UserModeProvider } from ${q('src/context/UserModeContext.tsx')};
import { WalletProvider } from ${q('src/context/WalletContext.tsx')};
import { OnboardingProvider } from ${q('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import { WritingHall } from ${q('src/pages/Writing/WritingHall.tsx')};
import OnlineQuoteFlow from ${q('src/pages/Quote/OnlineQuoteFlow.tsx')};
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
/* [#194] 体检页与监测页 —— G2 的 36 条未评估里最大的两块。 */
import { NewDiagnosis } from ${q('src/pages/Diagnosis/NewDiagnosis.tsx')};
import MonitoringPage from ${q('src/pages/Monitoring/index.tsx')};
/* [#194] 真的左上角:写作大厅的项目列表按**当前客户**过滤,不先选客户它就是空的。
   用真控件建立前置状态,判据才不是拿我自己造的状态自证。 */
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};
/* [WO_235] 前置动作要用**产品自己的** authFetch 推进沙盒状态 ——
   它第一件事就是 tryFetchSandboxMockLazy,与用户点出来的那一次走同一条路。
   自己 new 一个 fetch 或直接改 sandboxWriting 模块变量都是拿我造的状态自证。 */
import { authFetch as __authFetch } from ${q('src/lib/api.ts')};
(globalThis as any).__authFetch = __authFetch;

const Stack = ({ children, entry }: any) => (
    <MemoryRouter initialEntries={[entry]}>
      <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
        <OnboardingProvider><PricingProvider><ClientProvider>
          <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
          {children}
        </ClientProvider></PricingProvider></OnboardingProvider>
      </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
    </MemoryRouter>
);
const PAGES: Record<string, { el: any; entry: string }> = {
    quote: { el: <OnlineQuoteFlow />, entry: '/pricing' },
    publish: { el: <PublishCenter />, entry: '/publish' },
    writing: { el: <WritingHall />, entry: '/writing' },
    /* [#194] 两个新面 */
    diagnosis: { el: <NewDiagnosis />, entry: '/diagnosis/new' },
    monitoring: { el: <MonitoringPage />, entry: '/monitoring' },
};
(globalThis as any).__mount = function (el: HTMLElement, surface: string) {
    const p = PAGES[surface] || PAGES.writing;
    createRoot(el).render(<Stack entry={p.entry}>{p.el}</Stack>);
};
`, 'utf8');

const rawSuffixPlugin = {
    name: 'vite-raw-suffix',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (args) => {
            const bare = args.path.replace(/\?raw$/, '');
            const abs = bare.startsWith('@/') ? join(ROOT, 'src', bare.slice(2)) : join(args.resolveDir, bare);
            return { path: abs, namespace: 'vite-raw' };
        });
        build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({
            contents: readFileSync(args.path, 'utf8'), loader: 'text',
        }));
    },
};
try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl',
            '.png': 'dataurl', '.webp': 'dataurl', '.svg': 'dataurl',
        },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) {
    unusable('取不到真 CSS —— 没有它,"看得见"这件事量不了(裸 DOM 上什么都没有尺寸)', err);
}
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a190</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try {
        const b = readFileSync(join(outDir, n));
        res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' });
        res.end(b);
    } catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

const ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};

/**
 * [#194] 🔴 **归因表**:每个阶段的引导卡对应哪个 `featureId`。
 *
 *    为什么必须有:G2 原来只问「屏幕上有没有可见的引导卡」,而那一问会被
 *    **与阶段无关**的卡满足 —— 写作大厅那张「点这个项目卡进入」只由
 *    `!selectedProject` 控制,于是 6 个 step3 阶段一起报绿,
 *    而它们各自的引导一张都没出现。看见 ≠ 看见的是**它**。
 *
 *    🔴 表是**从源码推导**的,不手写:在每个 `<FeatureTooltip` 块里找
 *    `featureId="…"` 与 `tutorialStage === / !== '<stage>'` 的共现。
 *    手写的映射会随重构悄悄过期,而过期的归因比没有归因更难发现。
 */
function buildStageFeatureMap() {
    const out = {};
    const walkSrc = (dir, acc = []) => {
        for (const name of readdirSync(dir)) {
            const full = join(dir, name);
            const st = statSync(full);
            if (st.isDirectory()) walkSrc(full, acc);
            else if (/\.tsx?$/.test(name)) acc.push(full);
        }
        return acc;
    };
    for (const f of walkSrc(join(ROOT, 'src'))) {
        const text = readFileSync(f, 'utf8');
        let i = text.indexOf('<FeatureTooltip');
        while (i >= 0) {
            /*
             * 🔴 只取**这一个** `<FeatureTooltip` 的开标签(到它自己那个 `>` 为止)。
             *    第一版取的是固定 1400 字符窗口 —— 窗口会跨进**隔壁**那个 tooltip,
             *    于是 `step4-recovered` 被绑到了 `sandbox_step4_intro` 上,
             *    判据把本来对的绿demote 成未评估(理由还写得头头是道)。
             *    🔴 找结尾的 `>` 要**带花括号深度**:属性里全是 `{...}`,
             *    里面有 `=>` 和 `>=`,按第一个 `>` 截会截在半路(#188 C2 栽过同一跤)。
             */
            let depth = 0;
            let end = -1;
            for (let k = i; k < text.length; k += 1) {
                const ch = text[k];
                if (ch === '{') depth += 1;
                else if (ch === '}') depth -= 1;
                else if (ch === '>' && depth === 0 && text[k - 1] !== '=' && text[k + 1] !== '=') {
                    end = k;
                    break;
                }
            }
            /*
             * @@R@@ 还要往**前**看一段:很多 coach mark 的阶段条件写在包着它的
             *    条件表达式里,而不是自己的属性里 —— 例如 KeywordTable:
             *    `{tutorial && tutorial.stage === 'step4-good' && … ? (<FeatureTooltip …`。
             *    只读开标签的话,这类阶段会被判成"没有 featureId",
             *    而我差点据此断言"它们的引导不是 coach mark"(那是错的)。
             * @@R@@ 往前看**会**有窗口跨到隔壁的风险,但那一类错的方向是**更严**
             *    (要求了别人的 id ⇒ 报未评估/红),不会造出假绿 —— 与"看见任意一张卡
             *    就算过"那种放宽相比,这个方向是安全的。
             */
            const back = text.slice(Math.max(0, i - 300), i);
            const seg = (end > i ? text.slice(i, end) : '') + back;
            const idm = seg.match(/featureId="([a-z0-9_]+)"/);
            if (idm) {
                /*
                 * @@R@@ 比较的**变量名不止一个**:`tutorialStage`(多数页面)、
                 *    `tutorial.stage`(KeywordTable 用的是传进来的 ctx 对象)、
                 *    裸 `stage`。第一版只认第一种 ⇒ 23 个阶段"查不到 featureId",
                 *    我差点据此断言"它们的引导不是 coach mark"——**又一次分母比现实窄**
                 *    (#190 的枚举器早就登记过这三种写法,我自己新写的这段却没跟上)。
                 */
                for (const m of seg.matchAll(
                    /(?:tutorialStage|tutorial\??\.stage|(?<![.\w])stage)\s*(?:===|!==)\s*'([a-z0-9-]+)'/g)) {
                    (out[m[1]] ||= new Set()).add(idm[1]);
                }
            }
            i = text.indexOf('<FeatureTooltip', i + 1);
        }
    }
    return out;
}
const STAGE_FEATURES = buildStageFeatureMap();

const browser = await playwright.chromium.launch();
try {
    section('G2 逐阶段可见性(分母 = 枚举器的 |S|)');
    const produced = Object.keys(data.S);
    check(produced.length >= 40, 'G2a0 分母自证:拿到了有产生方的阶段清单', `${produced.length} 个`);

    const stageToSurface = {};
    for (const [surface, list] of Object.entries(SURFACE_OF)) {
        for (const st of list) stageToSurface[st] = surface;
    }

    let covered = 0;
    for (const stage of produced) {
        const surface = stageToSurface[stage];
        if (!surface) {
            const files = [...new Set((data.C[stage] || []).map((r) => r.file.split('/').pop()))];
            const why = files.map((f) => NO_HARNESS[f] || `消费者在 ${f}`).join(' · ');
            skip(`G2[${stage}]`, why || '没有消费者(终态/废弃)');
            continue;
        }
        const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
        await page.addInitScript((st) => {
            localStorage.setItem('omnirank_token', 'qa-token');
            localStorage.setItem('omnirank_sandbox_active', '1');
            localStorage.setItem('omnirank_sandbox_tutorial_stage', st);
            /*
             * 🔴 [#194] 沙盒的**写作阶段**是另一套状态:`getSandboxWritingProjects()`
             *    在 `sandboxWriting.stage === 'idle'` 时直接返空列表,于是写作大厅永远
             *    「暂无项目」,12 个 step3 阶段全报 0 张卡。
             *    解锁靠产品自己的那把钥匙:`omnirank_onboarding_state.completed_steps`
             *    里有 `first_quote`(= 第二步走完)——
             *    见 `src/sandbox/mockData.ts` 的 `isStep2CompletedFromLocalStorage`。
             *    这不是我编的夹具,是用户走到第三步时**本来就有**的那个状态。
             */
            localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
                completed_steps: ['first_quote'],
            }));
        }, stage);
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
            if (process.argv.includes(`--debug=${stage}`)) console.log(`  [api ${stage}] ${route.request().method()} ${path}`);
            if (path.includes('/auth/me')) return json(ME);
            /*
             * [WO_235] 价目表:`pricingReady = articleGenCost != null`
             * (`WritingHall.tsx:2500`),价目没回来就**禁用**「开始写作」。
             * 🔴 沙盒**不拦** `/api/wallet/pricing`(interceptor 里没有这条规则),
             *    教程里它走的是真后端 —— 所以生产上按钮是活的,而我的桩没有后端,
             *    于是按钮常年灰着,下游几个阶段一直躺在未评估桶里。
             *    这是**我的桩缺一份响应**,不是产品坏了,所以补桩不是放水。
             * 🔴 390 不是我编的:`WritingHall.tsx:2501` 注释里「6×390=2340」就是这个价。
             */
            /*
             * [WO_235] 余额:`startWriting()` 会做余额预检,余额 0 时按钮上写
             * 「只够 0 篇」并不让写 —— 于是下游两个阶段永远到不了。
             * 🔴 这是**浏览器里的一份响应桩**,不是往任何账本写数
             *    (本仓「不造余额」说的是别在系统里造;这里没有系统,只有一个渲染桩)。
             * 🔴 顺带一件产品事实,记下来给复审:`GET /api/wallet` 在沙盒里走**真后端**
             *    (`sandboxInterceptor.ts:1262` 白名单),所以**余额不足的服务商在教程第八步
             *    是走不通的** —— 这不在本单范围,只记不改。
             */
            if (path === '/api/wallet' || path.endsWith('/api/wallet')) return json({ success: true, data: {
                paid_points: 100000, commission_points: 0, bonus_points: 0,
                frozen_points: 0, total_recharged: 100000,
                /* 🔴 `customer_credit` 少一个键就整份响应作废(`WalletContext.tsx:428` 直接抛),
                      而抛出去之后余额停在 0 —— 屏幕上写的是「只够 0 篇」,
                      读起来像"这个服务商没钱",不像"我的桩少了一个键"。 */
                customer_credit: {
                    tool_credit_points: 100000, publish_credit_points: 100000,
                    bonus_credit_points: 0, total_purchased_points: 100000,
                    total_consumed_points: 0,
                },
            } });
            if (path.includes('/wallet/pricing')) return json({ success: true, data: [
                /* 🔴 `is_active` 不能省:`getTrustedCost`(PricingContext:293)要求
                      `is_active && Number.isFinite(cost_points)`,少一个就回 null,
                      而 null 与"价目没回来"完全同形 —— 第一版就少了它,按钮照样灰。 */
                { feature_code: 'article_gen', cost_points: 390, is_active: true },
                { feature_code: 'article_rewrite', cost_points: 390, is_active: true },
                { feature_code: 'topic_gen', cost_points: 80, is_active: true },
            ] });
            /* [WO_235] `--debug` 时把**没被专门喂过**的 api 打出来:
               排"为什么这一格不动"时,第一件要知道的就是它到底请求了什么路径 ——
               而兜底响应长得跟成功一模一样,不打出来就只能猜。 */
            return json({ success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [] });
        });
        await page.goto(`http://127.0.0.1:${PORT}/index.html`);
        /* [WO_235] 挂载前先把后端状态摆好 —— 见 PRE_MOUNT 的注释。
           🔴 失败要出声:静默失败会让下一格的"0 张卡"背上不属于它的锅。 */
        let preFailed = '';
        for (const a of (PRE_MOUNT[stage] || [])) {
            const got = await page.evaluate(async (x) => {
                try {
                    const r = await globalThis.__authFetch(x.url, {
                        method: x.method || 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: x.body ? JSON.stringify(x.body) : undefined,
                    });
                    return r && r.ok === true;
                } catch (e) { return false; }
            }, a).catch(() => false);
            if (!got) { preFailed = `${a.url}(${a.why})`; break; }
        }
        if (preFailed) {
            skip(`G2[${stage}]`, `挂载前置调不通:${preFailed} —— 是我的桩没把后端摆到位,不是引导坏了`);
            await page.close();
            continue;
        }
        await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), surface);
        await page.waitForTimeout(1800);
        /*
         * [#194] 前置动作:先把页面推到该阶段引导**真的会出现**的那个状态。
         * 🔴 每一步都自证点到了 —— 点不到就当未评估处理并说清是哪一步,
         *    否则「我没点上」会被写成「引导不见了」。
         */
        let prepFailed = '';
        for (const step of [...(SURFACE_PREPARE[surface] || []), ...(PREPARE[stage] || [])]) {
            /*
             * [WO_235] `api`:有些阶段缺的不是"点一下",是**后端状态还没到那一步**
             * (补发闭环要先走 optimize-generate)。用产品自己的 authFetch 推 ——
             * 🔴 它返回的是沙盒合成的响应,不是我编的:走的是
             *    `tryFetchSandboxMockLazy` 那条路,与用户点出来的那一次同源。
             * 🔴 同样要自证:调不通就报未评估并说清是哪一条,不许当成"引导不见了"。
             */
            /*
             * [WO_235] `event`:产品自己留的刷新入口(WalletContext:668 的 Layer 4
             * `window.dispatchEvent(new CustomEvent('wallet:refresh'))`)。
             * 🔴 为什么需要它:余额那一次请求在**鉴权落定之前**就发了,
             *    WalletContext 会按 `activeUserIdRef !== requestedUserId` **静默丢弃**
             *    (`WalletContext.tsx:379-381`)—— 于是余额恒为 0,而屏幕上只写
             *    「只够 0 篇」,看起来像"这个代理没钱",不像"我的桩把那次响应丢了"。
             *    用产品自己的刷新事件重来一次,比我去改它的内部状态诚实。
             */
            if (step.event) {
                await page.evaluate((e) => window.dispatchEvent(new CustomEvent(e)), step.event).catch(() => { });
                await page.waitForTimeout(step.waitMs || 900);
                continue;
            }
            if (step.api) {
                const got = await page.evaluate(async (a) => {
                    try {
                        const r = await globalThis.__authFetch(a.url, {
                            method: a.method || 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: a.body ? JSON.stringify(a.body) : undefined,
                        });
                        return r && r.ok === true;
                    } catch (e) { return false; }
                }, step.api).catch(() => false);
                if (!got && step.optional) continue;
                if (!got) { prepFailed = `${step.api.url}(${step.why})`; break; }
                await page.waitForTimeout(step.waitMs || 900);
                continue;
            }
            /* [#194] `bySelector`:有 testid 就按 testid 点,比按文字稳
               (文字会随文案改,而文案本来就是这一单在动的东西)。 */
            if (step.bySelector) {
                const hit2 = await page.evaluate((sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    el.click();
                    return true;
                }, step.bySelector).catch(() => false);
                if (!hit2 && step.optional) continue;
                if (!hit2) { prepFailed = `${step.bySelector}(${step.why})`; break; }
                await page.waitForTimeout(step.waitMs || 900);
                continue;
            }
            /* 🔴 不能只找 `<button>`:Token 那一格是一张 `<Card onClick>`,
               第一版只查按钮 ⇒ 报「前置动作点不到」,而那是**我的探针窄**,不是页面没有。
               改成:找到带这段文字的元素,再取它**最近的可点祖先**。 */
            const hit = await page.evaluate(([txts, notWithin]) => {
                const CLICKABLE = 'button,[role="button"],a,.cursor-pointer';
                const nodes = [...document.querySelectorAll('button,[role="button"],a,div,p,span')];
                for (const n of nodes) {
                    if (!txts.some((t) => (n.textContent || '').includes(t))) continue;
                    if (notWithin && n.closest(notWithin)) continue;
                    const target = n.matches(CLICKABLE) ? n : n.closest(CLICKABLE);
                    if (!target) continue;
                    if (target.disabled) continue;
                    const r = target.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    target.click();
                    return true;
                }
                return false;
            }, [[].concat(step.byText), step.notWithin || '']).catch(() => false);
            if (!hit && step.optional) continue;      // 可选步骤(比如切换器本来就展开着)
            if (!hit) { prepFailed = `${[].concat(step.byText).join('/')}(${step.why})`; break; }
            /* [WO_235] byText 这一支原来写死 900ms,于是给它配的 waitMs 被静默忽略 ——
               而"等得不够"和"这一步没做成"在下一格读数上同形。 */
            await page.waitForTimeout(step.waitMs || 900);
        }
        if (prepFailed) {
            skip(`G2[${stage}]`, `前置动作点不到:${prepFailed} —— 是我的桩没把页面推到位,不是引导坏了`);
            await page.close();
            continue;
        }
        /* [#194] `--debug=<stage>`:把这一阶段跑完前置动作之后屏幕上的文字打出来 ——
           排"为什么 0 张卡"时不用另写脚本,也免得靠猜。 */
        if (process.argv.includes(`--debug=${stage}`)) {
            const txt = await page.evaluate(() => (document.getElementById('root') || document.body).innerText)
                .catch(() => '');
            /* [WO_235] 700 字在写作大厅会在页签("已完成 (N)")之前就截断 ——
               而那几个数正是排"为什么 0 张卡"时唯一要看的东西。 */
            console.log(`  [debug ${stage}] ${txt.replace(/\s+/g, ' ').slice(0, 2600)}`);
        }
        /*
         * 🔴 [#194] **跑完前置动作要把阶段重新按回去。**
         *    前置动作本身会推进阶段:点「开始创作」进工作台那一下,产品会
         *    `setTutorialStage('step3-explain-modes')`。于是我设的
         *    `step3-upload-kb` 被覆盖掉,屏幕上出现的是**别的阶段**的卡 ——
         *    归因那一层如实报了「看见的是 explain_modes」,但真正的原因是
         *    **我的前置动作把阶段推走了**,不是那一阶段的引导不见了。
         *    ⇒ 用产品自己的机制按回去(写 storage + 发它自己的事件)。
         */
        if (RESTAGE_AFTER_PREP.includes(stage)) {
            await page.evaluate((st) => {
                localStorage.setItem('omnirank_sandbox_tutorial_stage', st);
                window.dispatchEvent(new Event('sandbox-tutorial-stage:change'));
            }, stage).catch(() => { });
            await page.waitForTimeout(900);
        }
        /* [#194] 非 coach mark 的那一类:换探针,断言那段文字真的可见。 */
        const dlg = DIALOG_PROBE[stage];
        if (dlg) {
            const seen = await page.evaluate((needle) => {
                const hit = [...document.querySelectorAll('div,p,h2,span')]
                    .filter((el) => (el.textContent || '').includes(needle))
                    .filter((el) => {
                        const r = el.getBoundingClientRect();
                        const cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0
                            && cs.display !== 'none' && cs.visibility !== 'hidden'
                            && Number(cs.opacity) > 0.05;
                    });
                return hit.length;
            }, dlg.needle).catch(() => 0);
            let lockOk = true;
            let lockNote = '';
            if (seen > 0 && dlg.lockCheck) {
                /* 点一下弹窗**外面**(背景),再看它是不是还在 —— 锁住才算这一阶段的引导成立 */
                await page.mouse.click(8, 8).catch(() => { });
                await page.waitForTimeout(600);
                const still = await page.evaluate((needle) => [...document.querySelectorAll('div,h3')]
                    .some((el) => (el.textContent || '').includes(needle)
                        && el.getBoundingClientRect().height > 0), dlg.needle).catch(() => false);
                lockOk = still;
                lockNote = still ? ' · 点背景关不掉(锁成立)' : ' 🔴 点背景就关掉了 —— 锁没生效';
            }
            if (seen > 0 && lockOk) {
                covered += 1;
                ok(`G2[${stage}] 引导**看得见**(${surface} · 弹窗探针,非 coach mark)`,
                    `${dlg.why} · 命中 ${seen} 处「${dlg.needle}」${lockNote}`);
            } else if (DIALOG_BASELINE.includes(stage)) {
                bad(`G2[${stage}] 🔴 这个阶段的弹窗引导**本来看得见**,现在不见了`,
                    `可见命中 ${seen} 处「${dlg.needle}」${lockNote}(${dlg.why})`);
            } else {
                skip(`G2[${stage}]`, `弹窗探针没看到「${dlg.needle}」(${dlg.why})`);
            }
            await page.close();
            continue;
        }
        /*
         * 🔴 「看得见」不是「在 DOM 里」:量 rect 非零 + 不被 hidden。
         *    只查存在的话,`display:none` 的引导也算数,而用户什么都看不到。
         */
        const vis = await page.evaluate(() => {
            const cards = [...document.querySelectorAll('[data-coach-card]')];
            return cards.map((el) => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return {
                    fid: el.getAttribute('data-feature-id') || '',
                    w: Math.round(r.width), h: Math.round(r.height),
                    hidden: cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0,
                    text: (el.textContent || '').trim().slice(0, 40),
                };
            });
        }).catch(() => []);
        let visible = vis.filter((v) => v.w > 0 && v.h > 0 && !v.hidden);
        /*
         * @@R@@ **归因**:只认这个阶段自己的那张卡。认不出归属的阶段
         *    (源码里找不到 featureId 与它共现)一律报未评估,不拿别人的卡充数。
         */
        const wantIds = STAGE_FEATURES[stage];
        if (wantIds && wantIds.size) {
            const mine = visible.filter((v) => wantIds.has(v.fid));
            if (visible.length > 0 && mine.length === 0) {
                /* 🔴 冻结表里的阶段**不许**从这条路溜成"未评估":
                   那等于给假绿开了一个新出口。 */
                const say = VISIBLE_BASELINE.includes(stage) ? bad : skip;
                say(`G2[${stage}]`, `屏幕上有 ${visible.length} 张卡,但没有一张是这个阶段的`
                    + `(期望 featureId ∈ {${[...wantIds].join(',')}},实际 `
                    + `{${[...new Set(visible.map((v) => v.fid || '?'))].join(',')}})`
                    + ' —— 看见的是别的引导,不算这一格过');
                await page.close();
                continue;
            }
            visible = mine;
        }
        if (visible.length > 0) {
            covered += 1;
            ok(`G2[${stage}] 引导卡**看得见**(${surface})`,
                `${visible.length} 张 · ${visible[0].text}`);
        } else {
            /*
             * 🔴 没看见 ≠ 一定是缺陷:这个阶段的引导可能还需要页面里有数据
             *    (例如"6 篇文章写完了"才轮到 show-articles)。
             *    我的桩是空数据,所以这里报**未评估**并说清缺什么,不报红。
             *    报红会把"我没喂够数据"说成"引导坏了",下一个人会去改没坏的代码。
             */
            if (VISIBLE_BASELINE.includes(stage)) {
                bad(`G2[${stage}] 🔴 这个阶段**本来看得见**引导卡,现在不见了`,
                    `在 ${surface} 上 DOM 里 ${vis.length} 张卡、可见 0 —— 冻结表里的阶段回退即失败`);
            } else {
                skip(`G2[${stage}]`, `在 ${surface} 上没渲染出引导卡 —— 多半是本桩没有该阶段要的页面数据`
                    + `(DOM 里 ${vis.length} 张卡,可见 0)`);
            }
        }
        await page.close();
    }
    check(VISIBLE_BASELINE.every((st) => produced.includes(st)),
        'G2c 冻结表里的阶段都还在分母里(阶段被删掉时这条会红,提醒同步表)');
    check(covered >= 1,
        'G2b 分母自证:至少有一个阶段**真的**渲染出了可见引导卡 —— 一个都没有的话,'
        + '说明探测器坏了(全阴性先疑采样器死),而不是全仓都没引导',
        `${covered} 个阶段可见`);
} catch (e) {
    console.log('FAIL 判据自身异常:' + String((e && e.stack) || e));
    failed += 1;
} finally {
    await browser.close();
    server.close();
}

/* ══ WO_235:未评估必须被声明 ══════════════════════════════════════ */
{
    const undeclared = [...skippedStages].filter((k) => !PENDING_DECLARED[k]);
    check(undeclared.length === 0,
        'G4 🔴 每一项未评估都在 `PENDING_DECLARED` 里**声明过** —— '
        + '没声明的未评估会静默躺在桶里,而「消费点是死的」与「桩缺数据」读数同形(#234 就是这么藏的)',
        undeclared.join(' / ') || `${skippedStages.size} 项全部已声明`);

    /*
     * 🔴 [2026-09-18 我自己注毒照出来的] 光有「每项未评估都已声明」还不够:
     *    **声明了、却已经不再被 skip** 的条目会留在表里 —— 那说明这一项已经烧掉了,
     *    而条目没删、G4c 那个数也就没降。「烧一条又加一条」时条数不变,棘轮放行。
     *    ⇒ 反过来也要钉:表里每一条**必须仍然确实是未评估的**。
     *    这与 WO_238 丁修的陈旧豁免是**同一个形状** —— 同一个人、隔一天、又漏一次,
     *    所以这次把它写在原位,不只写在消息里。
     */
    const staleDecl = Object.keys(PENDING_DECLARED).filter((k) => !skippedStages.has(k));
    check(staleDecl.length === 0,
        'G4d 🔴 表里每一条都**仍然确实未评估** —— 已经烧掉的项必须同笔删条目、降 G4c 那个数;'
        + '留着的话「烧一条又加一条」条数不变,棘轮就放行了',
        staleDecl.join(' / ') || '零条陈旧');

    const badDecl = Object.entries(PENDING_DECLARED)
        .filter(([, v]) => !v.kind || !v.reason || !v.owner || !v.since || v.reason.length < 20)
        .map(([k]) => k);
    check(badDecl.length === 0,
        'G4b 每条声明都带 kind / reason / owner / since —— 没有理由的声明等于把桶又变回静默的',
        badDecl.join(',') || `${Object.keys(PENDING_DECLARED).length} 条都带了`);

    /*
     * 🔴 [2026-09-18 复审点名 · 我自己同一天写过更强的形式却没用在这里]
     *    第一版是 `length <= 10`,而注释写着「只许变小不许变大」——
     *    **两句不是一回事**:烧掉一条(10→9)再加一条(9→10)会**静默通过**,
     *    那个数**永远不必真的降下来**。意图活在注释里,代码只管「别超过」。
     *
     *    改成**恰好**:烧一条和加一条**都**必须在同一笔里改这个数字 ⇒
     *    数字进 diff,人就看得见方向。这就是我在 WO_238 E12c 用的棘轮形式
     *    (`EXEMPT.length === 1`)—— 同一天、同类的格,那边用了强形式,这边没用。
     *    差别不是疏忽,是我在这一格给自己留了余量,而余量的代价正落在这张表的目的上。
     */
    /* 🔴 [WO_235 2026-09-18] 10 → 9:`step3-start-writing` 已烧掉(补了「先点批量生成标题」
          这一步,卡当场可见,同笔进了 VISIBLE_BASELINE)。这个数只许因**烧掉**而变小。 */
    check(Object.keys(PENDING_DECLARED).length === 9,
        'G4c 🔴 声明表**恰好 9 条**(建表 10 条,已烧掉 1 条)—— '
        + '烧掉一条要把这个数**降下来**,新增一条要抬上去,两个方向都必须在同一笔里改;'
        + '写成上界(<=)的话,烧完再加回来就静默通过了,数字永远不必真降',
        `${Object.keys(PENDING_DECLARED).length} 条 · 其中 needs-stage-prep `
        + `${Object.values(PENDING_DECLARED).filter((v) => v.kind === 'needs-stage-prep').length} 条(这几条是要烧掉的)`);
}

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过` + (pending ? ` · 另有 ${pending} 项未评估` : '')); process.exit(1); }
if (pending > 0) {
    console.log(`未评估 ${pending} 项(见上面 .. 行)—— **不是通过**,退出码 3`);
    process.exit(3);
}
console.log('全部通过:每个阶段都有看得见的引导卡');
process.exit(0);
