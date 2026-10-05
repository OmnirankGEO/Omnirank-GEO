#!/usr/bin/env node
/**
 * 判据 · #204 a1 图文选题流(结构 + 纯函数臂)。
 *
 * 工单铁律:**不发明新交互** —— 逐项对照写文章大厅。
 * 所以 A1 不是"有这些控件就行",而是**每一项都要指得出它在写文章大厅里的对应物**;
 * 对照表就写在 `ImageNoteTopicPanel` 的抬头里,这里逐项验它落地了。
 *
 * 两态退出码:**0 全过 / 1 有失败**(本闸进 build 链,rc=3 会让后面的闸不跑)。
 */
import { readFileSync, mkdirSync, writeFileSync, rmSync, mkdtempSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

/*
 * The current step flow must satisfy real DTO/navigation/state behavior, not only source anchors.
 * 🔴 [WO_258] 这两份原来在文件顶部**静态 import**(a9ab52b8e 挂进来;它们自己没有别的
 *    运行者 —— 不在 build 链、没有键 run 它们 —— 所以不能删)。它们在 import 阶段就断言,
 *    一红就抛 ⇒ 本闸在跑到自己任何一格之前整闸崩掉:runner 204 的 S4 / S6 / S8 下去,
 *    t 闸 rc=1 却**一格都没报**(A4f / A9b / A4 根本没跑到)。「毒打崩了闸」与「锁咬住了」
 *    在 rc 上同形。改成逐个动态 import:失败记成**有名字的一格**,本闸自己的格照常跑完。
 */
for (const [id, file, what] of [
    ['A0a', './test-image-note-flow.mjs', '纯函数契约(DTO / 导航 / 状态)'],
    ['A0b', './test-image-note-steps-render.mjs', '三步条静态渲染'],
]) {
    let err = null;
    try { await import(file); } catch (e) { err = e; }
    ok(err === null, `${id} ${file.slice(2)} 全过 —— ${what}`,
        err ? String((err && err.message) || err).split('\n')[0].slice(0, 160) : '');
}

/* ── 真调那只零 import 模块 ─────────────────────────────────────── */
const require_ = createRequire(import.meta.url);
let T = null;
const cacheRoot = join(ROOT, 'node_modules', '.cache');
mkdirSync(cacheRoot, { recursive: true });
const tmp = mkdtempSync(join(cacheRoot, 'a204-'));
process.on('exit', () => { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 尽力 */ } });
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/imageNoteTopics.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const f = join(tmp, 'imageNoteTopics.mjs');
    writeFileSync(f, js, 'utf8');
    T = await import(`file://${f.split('\\').join('/')}`);
} catch (e) {
    console.log('FAIL 判据不可用(不当绿灯):取不到领域层模块 —— '
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}

/* [WO_282] 下单载荷那只模块也真调:它按**名字**抛「写给人看」的错(不能值导入 userError),A13e 要跨模块对上 */
let P = null;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/imageNoteProduction.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const f = join(tmp, 'imageNoteProduction.mjs');
    writeFileSync(f, js, 'utf8');
    P = await import(`file://${f.split('\\').join('/')}`);
} catch (e) {
    console.log('FAIL 判据不可用(不当绿灯):取不到下单载荷模块 —— '
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}

const PANEL_RAW = rd('src/pages/Writing/ImageNoteTopicPanel.tsx');
const PANEL = decomment(PANEL_RAW);
const DETAIL = decomment(rd('src/pages/Writing/DouyinPostDetail.tsx'));
const ROUTE = decomment(rd('src/pages/Writing/ImageNoteDetailRoute.tsx'));
const APP = decomment(rd('src/App.tsx'));
const HALL = decomment(rd('src/pages/Writing/WritingHall.tsx'));

console.log('T #204 a1 图文选题流');

/* ══ A1 逐项对照写文章大厅 ═══════════════════════════════════════════ */
{
    /*
     * 🔴 每一项两句话:**这里有**,而且**那边也有同一件事**。
     *    只证"这里有"的话,我可以发明任何交互都全绿 —— 而工单的命题是
     *    「用习惯写文章的用户可以完全不用学习」。
     */
    const PAIRS = [
        ['生成按钮带价', /data-testid="topics-distill"/, /立即生成标题/],
        ['行内改标题', /data-testid="topic-title-edit"/, /const startEdit = \(topic: Topic\)/],
        ['勾选', /data-testid="topic-check"/, /selectedTopics/],
        ['开始按钮带价', /data-testid="topics-start"/, /开始写作/],
        /* 🔴 对照锚取在**代码**里,不取注释:错注释不会红,拿它当证据等于拿一句话当证据。
           这里用的是写文章大厅那张状态标签表(`writing: { label: "写作中" }` 等)。 */
        ['三态', /data-testid="topics-chip"/, /writing: \{ label: "写作中"/],
    ];
    for (const [name, here, there] of PAIRS) {
        ok(here.test(PANEL), `A1 这里有:${name}`);
        ok(there.test(HALL), `A1 对照:写文章大厅也有同一件事(${name})`
            + ' —— 只证"这里有"的话,发明任何交互都全绿');
    }
    ok(/每行风格带样图|STYLE_SAMPLES\[r\.styleKey\]/.test(PANEL),
        'A1 这里有:每行风格**带可见样图**(不是只给风格名)');
    ok(/data-testid="topic-style-sample"/.test(PANEL) || /data-testid="topic-style-none"/.test(PANEL),
        'A1b 样图 / 「推荐」占位都能被行为臂定位');
}

/* ══ A2 进度只来自服务端 ═════════════════════════════════════════════ */
{
    /* 纯函数臂:服务端给多少就是多少 */
    const p = T.distillProgress({ state: 'running', stage: 'gathering', stage_label: '正在找素材', percent: 37, active: true });
    ok(p.percent === 37 && p.line === '正在找素材' && p.keepPolling === true,
        'A2 🔴 进度条与那句话**直接取服务端**的 percent / stage_label', `${p.percent}% · ${p.line}`);
    const done = T.distillProgress({ state: 'succeeded', percent: 100, active: false });
    ok(done.keepPolling === false, 'A2b 终态停轮询');
    const gone = T.distillProgress({ state: 'failed', percent: 0, active: false, message: '这次的任务找不到了' });
    ok(gone.keepPolling === false && gone.line.includes('找不到'),
        'A2c 任务查不到时服务端给的是**终态**(给 queued 前端会永远转圈)—— 照它给的来', gone.line);
    /*
     * 🔴 结构反臂:组件里**不许**有前端自己涨百分比的写法。
     *    假进度条让"卡住了"和"正在跑"长得一模一样,而用户正是靠这条线判断要不要等下去。
     */
    ok(!/percent[^\n]{0,40}\+\+|setPercent\(|percent \+ \d/.test(PANEL),
        'A2d 🔴 反臂:组件里没有前端自己涨的假进度');
    ok(/data-percent=\{progress\.percent\}/.test(PANEL),
        'A2e 进度数上屏时带得出来(行为臂按它量)');
}

/* ══ A3 改标题后**重新读回** ═════════════════════════════════════════ */
{
    ok(/method: 'PATCH'/.test(PANEL) && /\/api\/geo-douyin\/topics\/\$\{editingId\}/.test(PANEL),
        'A3 改标题走 PATCH /topics/{id}(c1 契约)');
    /*
     * 🔴 钉的是**重新读回**,不是"发了 PATCH"。只改本地 state 时屏幕上看起来成功了,
     *    刷新一下打回原形 —— 而用户已经按改后的标题去制作了。
     */
    const saveStart = PANEL.indexOf('const saveTitle = async ()');
    const saveEnd = saveStart >= 0 ? PANEL.indexOf('\n    };', saveStart) : -1;
    const SAVE = saveStart >= 0 && saveEnd > saveStart ? PANEL.slice(saveStart, saveEnd) : '';
    ok(SAVE.length > 0 && SAVE.length < 1400,
        'A3a0 取窗自证:切出来的是 `saveTitle` 函数体', `${SAVE.length} 字符`);
    ok(/await load\(brandId\)/.test(SAVE),
        'A3 🔴 存完**重新读回**(不是只改本地 state)');
    ok(!/setRows\(/.test(SAVE),
        'A3b 🔴 反臂:`saveTitle` 里没有直接改本地行的写法');
}

/* ══ A4 开始制作:逐项带 topic_id,价来自 quote ═══════════════════════ */
{
    ok(/productionBatch\(brandId, selectedPending, settings, quote, requestRef.current.id\)/.test(PANEL)
        && /topic_id: row.id/.test(rd('src/pages/Writing/imageNoteProduction.ts')),
        'A4 🔴 请求体**逐项带 `topic_id`** —— 「按标题制作」就是这一个字段(c1 §4)');
    ok(/production-quote/.test(PANEL) && /d\?\.total_points/.test(PANEL),
        'A4b 价来自 `production-quote` 的 `total_points`');
    /*
     * 🔴 蒸馏价的键名钉死:后端 `/pricing` 回包里叫 **`topic_distill`**
     *    (`api/geo_douyin_api.py`:`"topic_distill": distill`)。
     *    第一版我按印象写成 `distill` / `distill_topics`,两个都不对 ⇒
     *    价永远读不到、按钮永远禁用,而界面上只显示「价目读不到」,
     *    看起来像服务端的问题。**键名错了不会报错,只会安静地永远取不到。**
     */
    ok(/d\?\.topic_distill\?\.cost_points/.test(PANEL),
        'A4b2 🔴 蒸馏价读的是 `topic_distill.cost_points`(后端回包里的真键名)');
    ok(!/d\?\.distill\?\.|distill_topics\?\./.test(PANEL),
        'A4b3 反臂:那两个猜错的键名已经不在了');
    /*
     * 🔴 A10:风格名只有**一个源** —— 后端 `/api/geo-douyin/styles`
     *    (`services/geo_douyin/card_templates.py` 的 `STYLE_PRESETS`)。
     *    前端再写一份 key→名字 的表,后端改名/加款时两边只会有一边跟上,
     *    而且**不报错**,只会有一处显示旧名字。
     */
    ok(/styles \|\| \[\]\)\.find\(\(o\) => o\.key === key\)\?\.label/.test(PANEL),
        'A10 🔴 行上的风格名查的是**后端给的那份 styles**');
    ok(!/STYLE_LABELS|const\s+\w*StyleLabel\w*\s*:\s*Record</.test(PANEL),
        'A10b 反臂:面板里没有自己另写的 key→名字 表');
    /* 两个调用点都得把那份传下去 —— 少传的那一处不报错,只会少半句话。 */
    ok(/styles=\{styles\}/.test(DETAIL) && /styles=\{styles\}/.test(ROUTE),
        'A10c 🔴 详情页与「这个客户还没作品」那一支**都**把 styles 传给了面板');
    /* 🔴 反臂:本文件一个价格算术都没有 */
    const arith = PANEL.match(/(points|Points)\s*[*+\-\/]\s*\w|\w\s*[*]\s*(points|Points)/g) || [];
    ok(arith.length === 0, 'A4c 🔴 反臂:本文件**一个价格算术都没有**(前端不算价)',
        arith.join(',') || '零处');
    /* 纯函数臂:价拿不到时说清楚,不显示一个看着合理的数,也不让点 */
    ok(T.startLabel(3, null).includes('价目读不到') && !/\d+ 算力/.test(T.startLabel(3, null)),
        'A4d 🔴 价拿不到 ⇒ **明说读不到**,不编一个数', T.startLabel(3, null));
    ok(T.startLabel(3, 390) === '开始制作 (3) · 390 算力',
        'A4e 价拿到了 ⇒ 数量与价都在按钮上', T.startLabel(3, 390));
    ok(T.canStart({ selectedCount: 3, points: null }) === false
        && T.canStart({ selectedCount: 0, points: 390 }) === false
        && T.canStart({ selectedCount: 3, points: 390 }) === true,
        'A4f 价没到 / 没勾选 ⇒ 点不了(给一颗按下去必然失败的按钮,比不给更糟)');
    ok(T.distillLabel(null).includes('价目读不到') && T.distillLabel(130) === '生成选题 · 130 算力',
        'A4g 「生成选题」同一条规矩', T.distillLabel(130));
}

/* ══ A5 新面板里没有旧的「按数量做」输入 ═════════════════════════════ */
{
    /*
     * 🔴 工单 A5 原文是「全树无『这次做几条』旧输入残留」。
     *    制作台整页退役在 **204-a2**(Review 09-15 准拆),所以**全树**那一半要等 a2。
     *    a1 能钉、也必须钉的是:**新面板里没有把它抄过来**。
     *    这里把全树计数**打印出来**,给 a2 留一个前后对比的分母 —— 不打印的话,
     *    a2 说"清干净了"就只是一句话。
     */
    ok(!/这次做几条|这次做多少条/.test(PANEL_RAW),
        'A5 🔴 新左栏里没有旧的「按数量做」输入(它被选题列表取代了)');
    const files = [];
    (function walk(rel) {
        for (const name of readdirSync(join(ROOT, rel))) {
            const p = `${rel}/${name}`;
            if (statSync(join(ROOT, p)).isDirectory()) walk(p);
            else if (/\.tsx?$/.test(name)) files.push(p);
        }
    })('src');
    const hits = files.filter((f) => /这次做几条/.test(rd(f)));
    console.log(`       (a2 的前置分母:全树「这次做几条」现有 ${hits.length} 处 —— `
        + `${hits.join(', ') || '无'};a2 退役后应为 0)`);
    ok(files.length > 500, `A5b 分母自证:扫到 ${files.length} 个 .ts/.tsx`);
}

/* ══ A6 / A7 选中某一行时中栏与右栏 ══════════════════════════════════ */
{
    /* 纯函数臂:四种状态各自该长什么样 */
    const mk = (status, postId = null, failure = '') => T.topicRow({
        id: 7, title: 't', status, post_id: postId, failure_reason: failure,
    });
    const pend = T.selectionView(mk('pending'));
    ok(pend.preview === 'style_sample' && pend.canEdit === false && pend.reason.length > 0,
        'A6 🔴 选中「待做」⇒ 中栏出风格样图、右栏禁用**且有一句原因**', pend.reason);
    const making = T.selectionView(mk('making'));
    ok(making.canEdit === false && making.reason.includes('正在制作'),
        'A6b 「制作中」也禁用,原因说的是"正在制作"不是"不能改"', making.reason);
    const doneV = T.selectionView(mk('done', 88));
    ok(doneV.preview === 'artifact' && doneV.canEdit === true && doneV.reason === '',
        'A7 🔴 选中「已完成」⇒ 中栏出真图、右栏可编辑(没有原因可说就不说)');
    const failedV = T.selectionView(mk('failed', null, '第 2 张图读取为空'));
    ok(failedV.canEdit === false && failedV.reason.includes('第 2 张图'),
        'A6c 「没做成」把服务端**原话**摆出来(不是一句"操作未完成")', failedV.reason);
    /* 🔴 每一格都必须有话 —— 不写原因的禁用就是第 11 条要清的那种猜 */
    const noReason = ['pending', 'making', 'failed']
        .filter((s) => T.selectionView(mk(s)).reason.trim().length === 0);
    ok(noReason.length === 0, 'A6d 🔴 三种不可编辑态**每一种都有一句原因**',
        noReason.join(',') || '三种都有');

    /* 接线臂:界面真按它渲染 */
    ok(/const topicView = selectionView\(pickedTopic\)/.test(DETAIL),
        'A6e 详情页按 `selectionView` 渲染(不在 JSX 里再写一遍条件)');
    ok(/data-testid="preview-not-made"/.test(DETAIL) && /data-testid="preview-style-sample"/.test(DETAIL),
        'A6f 中栏「还没做 + 风格样图」那一块能被行为臂定位');
    ok(/data-testid="content-locked-reason"/.test(DETAIL)
        && /data-editable=\{!pickedTopic \|\| topicView\.canEdit \? 'true' : 'false'\}/.test(DETAIL),
        'A6g 右栏禁用态带得出原因与可编辑标记');
}

/* ══ A8 无 id 路由 = 当前客户 ════════════════════════════════════════ */
{
    ok(/path="writing\/image-note"/.test(APP),
        'A8 `/writing/image-note`(无 id)这条路由在');
    ok(/data-testid="image-note-no-work-note"/.test(ROUTE) && /还没有作品，可以先从上面选题制作/.test(ROUTE),
        'A8b 这个客户一条作品都没有时**明说**,不渲染一个空详情页让人以为打不开');
    ok(/posts\.map\(p => <button/.test(ROUTE) && !/navigate\([^\n]*posts\[0\]/.test(ROUTE),
        'A8c 无 id 保留选题和作品入口，不再自动跳走；旧作品仍能点击打开');
}

/* ══ A9 三态分档:archived 不许混进来 ════════════════════════════════ */
{
    const rows = ['pending', 'pending', 'making', 'done', 'failed', 'archived']
        .map((s, i) => T.topicRow({ id: i + 1, title: `t${i}`, status: s }));
    const b = T.topicBuckets(rows);
    ok(b.pending.length === 2 && b.making.length === 1 && b.done.length === 1 && b.failed.length === 1,
        'A9 三态分档逐档对', JSON.stringify(b.counts));
    ok(b.counts.pending === 2 && !Object.values(b.counts).includes(6),
        'A9b0 `archived` 不进任何一档、也不进计数');
    /*
     * 🔴 A9b 换靶(注毒 S6 顺出来的)。原来那条钉的是「`archived` 不进计数」,
     *    而 `topicBuckets` 的 `pick()` 是精确匹配 —— 就算不挡 `archived`,
     *    它也进不了四档。**那道过滤是冗余的,所以那条判据两种情况下都真、
     *    永远不会红**(「毒仍绿」的第四解:目标冗余)。
     *    真正有牙的口子在 `topicRow` 的兜底:认不出的状态落成 `pending`,
     *    就是落进那档**能勾选、会被算进「开始制作 (N) · X 算力」**的桶。
     */
    const weird = T.topicRow({ id: 9, title: 'x', status: 'brand_new_status_from_backend' });
    ok(weird.status !== 'pending' && weird.selectable === false && weird.editable === false,
        'A9b 🔴 认不出的状态**不落进「待做」**、不能勾、不能改 —— '
        + '「待做」是要扣钱的那一档', weird.status + ' · ' + weird.statusLabel);
    ok(T.topicBuckets(rows.concat([weird])).counts.pending === 2,
        'A9b2 🔴 它也**不进计数** —— 进了的话「开始制作 (N)」的 N 会比真的多',
        JSON.stringify(T.topicBuckets(rows.concat([weird])).counts));
    ok(T.topicRow({ id: 1, status: 'making' }).selectable === false
        && T.topicRow({ id: 1, status: 'done' }).editable === false,
        'A9c 🔴 只有「待做」能勾选 / 能改标题 —— '
        + 'c1 对 making/done 的 PATCH 与建单都是 409,给一颗必然 409 的按钮比不给更糟');
}

// ══ A11 🔴 [#204 a2 迁入] 读不到就说读不到 · 卡住要出声 · 不许把不知道说成事实 ══
/*
 * 这一段迁自 `verify-image-note-usable.mjs` 的 G9a/G9b/G9c 与 G10a/G10b/G10c。
 * 那把闸随制作台整把退役,但它守的命题没退役 —— 宿主从制作台换成了这块左栏。
 *
 * 🔴 迁的时候它当场抓到我自己 a1 写的四处(判据先于我发现):
 *    ① 取数失败被当成「这个客户还没有图文」——(有 40 条的客户会被劝去重新生成选题,
 *       那是为已经做过的事再花一次钱);
 *    ② 风格名没取到时说「系统推荐的卡面风格」—— 把"不知道"讲成一个具体的、错的事实;
 *    ③ plan 读不到 ⇒ 顶栏整句消失,分不清"没买"还是"没取到";
 *    ④ 轮询连着失败不出声 ⇒ 进度条停在原地,和卡死一样,而钱已经付了。
 */
console.log('A11 静默吞掉(G9/G10 迁入)');
{
    ok(/catch[\s\S]*setError\(/.test(ROUTE) && /data-testid="image-note-load-failed"/.test(ROUTE)
        && /!loading && !error && posts.length === 0/.test(ROUTE),
        'A11a 🔴 取数失败 **≠** 「这个客户还没有图文」 —— 两件事分开说');
    ok(!/catch \{\s*if \(!ac\.signal\.aborted\) setNoWork\(true\); \}/.test(ROUTE),
        'A11a2 反臂:失败不再落进「还没有图文」那一支');
    ok(/with_production=true/.test(PANEL) && /data-testid="topics-error"/.test(PANEL)
        && /!loading && !error && shown.length === 0/.test(PANEL) && /重新读取/.test(PANEL),
        'A11b 关键词/制作进度读不到单独报错并给重试，不把失败说成零配额/未做');
    ok(/pollMisses/.test(PANEL) && /data-testid="topics-distill-stalled"/.test(PANEL),
        'A11c 🔴 进度连着问不到要**出声** —— 不出声的话进度条停在原地,'
        + '和卡死长得一模一样,而用户刚为这次蒸馏付过钱');
    ok(/setPollMisses\(0\)/.test(PANEL),
        'A11c2 问到了就清零 —— 只加不减的话第一次抖动之后那句话再也下不去');
    ok(/r\.styleKey \? '这一条的卡面风格' : '系统推荐的卡面风格'/.test(PANEL),
        'A11d 🔴 风格名没取到时**不许**说「系统推荐」 —— 这一条是有风格的,'
        + '只是名字没拿到;说成系统推荐是把"不知道"讲成一个错的事实');
    ok(/\} finally \{/.test(PANEL),
        'A11e 建单走 try/finally —— busy 复位放在 try 尾部时,'
        + '中途抛出来按钮就永远卡在"进行中"');
}

/* ══ A12 制作中的心跳**静默**刷新(Owner 2026-09-22:「前端一直看着它闪」)═══ */
{
    /*
     * 根因:心跳和首读走同一条 `load`,每 4 秒 `setLoading(true)` 一次,
     * 列表整块被换成「正在读取内容和制作进度…」再换回来。
     * 修法:读取计划由纯函数 `topicReadPlan(mode)` 给,心跳走 `refresh`。
     * 三格:纯函数(计划本身)/ 结构(心跳真的走了 refresh、loading 真的按计划才置)
     *      / 行为在 test-image-note-detail-render.mjs T14(真浏览器 4.6 秒内占位符零次出现)。
     */
    const planR = T.topicReadPlan('refresh');
    const planA = T.topicReadPlan('read');
    ok(planR.showLoading === false && planR.silentErrors === true,
        'A12 🔴 心跳(refresh)计划:不碰 loading、失败静默', JSON.stringify(planR));
    ok(planA.showLoading === true && planA.silentErrors === false,
        'A12a 首读(read)计划:出占位、失败报错(反向对照:两条路不能长一样)', JSON.stringify(planA));
    ok(/window\.setInterval\(\(\) => \{ void load\(brandId, ac\.signal, 'refresh'\); \}, 4000\)/.test(PANEL),
        'A12b 🔴 4 秒心跳那一行真的走了 `refresh`(走 `read` 就是 Owner 看到的那个闪)');
    ok(/if \(plan\.showLoading\) setLoading\(true\)/.test(PANEL)
        && /if \(plan\.showLoading && current\(\)\) setLoading\(false\)/.test(PANEL),
        'A12c 🔴 `setLoading` 两处都按计划走 —— 无条件 `setLoading(true)` 就是根因本身');
    ok(!/^\s*setLoading\(true\);\s*$/m.test(PANEL),
        'A12d 反臂:面板里没有裸的 `setLoading(true)`');
    ok(/if \(plan\.silentErrors\) \{ setRefreshMisses\(\(n\) => n \+ 1\); return; \}/.test(PANEL)
        && /setRefreshMisses\(0\)/.test(PANEL),
        'A12e 心跳失败记 miss 不清行、读到就清零');
    ok(T.refreshNotice(2) === '' && /3 次/.test(T.refreshNotice(3))
        && /data-testid="topics-refresh-stalled"/.test(PANEL),
        'A12f 连着 3 次没读到要**出声**且列表不动(沉默是最坏的那一种)');
}

/* ══ A13 出错分支上屏的那一句(WO_282-F4)═════════════════════════════
 * 🔴 真调 `userFacingError`:原话(浏览器 / 解析器)只进 console,不上屏。
 *    改写前「开始制作」断网时屏幕上是英文 `Failed to fetch`;工作台路由先裸 `.json()` 再判 ok,
 *    HTML 错误页会原样变成 `Unexpected token '<'`。分母 = 我们自己抛的 / 三家浏览器断网原话 /
 *    解析器原话 / 其它 Error / 非 Error。
 */
{
    const warns = [];
    const origWarn = console.warn;
    console.warn = (...a) => { warns.push(a); };
    let r;
    try {
        r = {
            own: T.userFacingError(T.userError('选题没存上，请检查标题和关键词后重试。'), '兜底'),
            chrome: T.userFacingError(new TypeError('Failed to fetch'), '兜底'),
            firefox: T.userFacingError(new TypeError('NetworkError when attempting to fetch resource.'), '兜底'),
            safari: T.userFacingError(new TypeError('Load failed'), '兜底'),
            parser: T.userFacingError(new SyntaxError('Unexpected token \'<\', "<html>" is not valid JSON'), '兜底'),
            other: T.userFacingError(new Error('Request failed with status code 500'), '兜底'),
            notError: T.userFacingError('一段字符串', '兜底'),
            nothing: T.userFacingError(null, '兜底'),
        };
    } finally {
        console.warn = origWarn;
    }
    ok(r.own === '选题没存上，请检查标题和关键词后重试。', 'A13a 我们自己抛的人话原样上屏', r.own);
    ok([r.chrome, r.firefox, r.safari].every((s) => s === T.NETWORK_ERROR_TEXT) && /网络不通/.test(T.NETWORK_ERROR_TEXT),
        'A13b 🔴 断网(三家浏览器的原话)⇒ 统一人话', `${r.chrome} / ${r.firefox} / ${r.safari}`);
    ok([r.parser, r.other, r.notError, r.nothing].every((s) => s === '兜底'),
        'A13c 🔴 解析器原话 / 其它原话 ⇒ 调用方给的兜底,一个字都不上屏',
        JSON.stringify([r.parser, r.other, r.notError, r.nothing]));
    const logged = warns.map((a) => a.map((x) => String((x && x.message) || x)).join(' '));
    ok(warns.length === 7 && logged.some((s) => s.includes('Failed to fetch'))
        && logged.some((s) => s.includes('Unexpected token')),
        'A13d 不吞错:别人的原话都进了 console(我们自己的人话不记)', `${warns.length} 条`);
    /* 下单载荷校验失败:另一只模块按同一个名字抛,跨模块对上才算数 */
    let thrown = null;
    try {
        P.productionBatch(3, [], { cardCount: 4, styleKey: '', aspectRatio: '3:4', industry: '' },
            { total_points: 0, lines: [] }, 'r');
    } catch (e) { thrown = e; }
    const quiet = console.warn;
    console.warn = () => { };
    let shown = '';
    try { shown = thrown ? T.userFacingError(thrown, '兜底') : ''; } finally { console.warn = quiet; }
    ok(shown === '制作清单或报价已变化，请重新确认。',
        'A13e 下单载荷校验失败按我们的人话上屏(imageNoteProduction 抛错的名字与 USER_ERROR_NAME 对上)',
        thrown ? `${thrown.name} → ${shown}` : '没抛');
    const RAW = /\b(\w+) instanceof Error \? \1\.message\b/;
    ok(!RAW.test(PANEL) && !RAW.test(ROUTE) && /userFacingError\(/.test(PANEL) && /userFacingError\(/.test(ROUTE),
        'A13f 🔴 面板与工作台路由的出错分支都走这一层,没有一处原样展示 .message');
    ok(/const d = await res\.json\(\)\.catch\(\(\) => null\);/.test(ROUTE),
        'A13g 工作台路由不再先裸 .json() 再判 ok(HTML 错误页 ⇒ 当没拿到,走我们的人话)');
}

/* ══ A14 WO_283:降级上屏(F2)/ ETA(F6)/ 一张徽章表(F7)/ 详情页不原样上屏(F9)════════ */
{
    /* F2:load_fewshot 写得出的四种原因逐个有人话;认不出的也要说;没降级就不说 */
    const codes = ['corpus_unavailable', 'corpus_empty', 'no_same_industry_image_post',
        'partial_same_industry_image_post'];
    const notes = codes.map((c) => T.degradeNotice(c));
    ok(notes.every((n) => n && !/corpus|same_industry/.test(n)) && new Set(notes).size === 4,
        'A14a 🔴 四种降级原因各有一句人话(不露工程词、彼此不同)', notes.map((n) => n.slice(0, 18)).join(' / '));
    ok(T.degradeNotice('') === '' && T.degradeNotice(null) === '' && T.degradeNotice('brand_new_code') !== '',
        'A14b 没降级不说;认不出的原因也要说(不沉默)');
    const done = T.distillProgress({ state: 'succeeded', percent: 100, active: false,
        fewshot_degraded: 'no_same_industry_image_post' });
    const running = T.distillProgress({ state: 'running', stage: 'distilling', stage_label: '正在想选题',
        percent: 60, active: true, eta_seconds: 42, fewshot_degraded: 'corpus_empty' });
    ok(done.degraded === T.degradeNotice('no_same_industry_image_post') && running.degraded === '',
        'A14c 成功时带出降级那句话,还在跑时不说', done.degraded.slice(0, 20));
    /* F6:ETA 只在还在跑时给,且就是服务端的数 */
    const noEta = T.distillProgress({ state: 'running', percent: 20, active: true, eta_seconds: null });
    ok(running.eta === '约 42 秒' && noEta.eta === '' && done.eta === '',
        'A14d 🔴 ETA = 服务端给的数;估不出 / 终态不编', `${running.eta} · "${noEta.eta}" · "${done.eta}"`);
    ok(/data-testid="topics-distill-eta"/.test(PANEL) && /\{progress\.eta\}/.test(PANEL)
        && /data-testid="topics-distill-degraded"/.test(PANEL) && /\/latest-topics/.test(PANEL),
        'A14e 面板画 ETA 与降级那句话,进页面读 latest-topics 恢复');
    /* F7:状态的人话只有一张表 */
    ok(T.statusText('pending') === '未制作'
        && T.keywordRow({ confirmed_keyword_id: 1, production_status: 'pending', required_articles: 2 }).statusLabel
            === T.statusText('pending')
        && T.topicRow({ id: 1, status: 'pending' }).statusLabel === T.statusText('pending'),
        'A14f 🔴 选题行 / 关键词行 / chip 的「未制作」是同一句(原来 chip 叫未制作、徽章叫待做)');
    ok(["pending", "making", "done", "failed"].every((s) => PANEL.includes(`label: statusText('${s}')`))
        && !/'(未制作|制作中|已完成|没做成)'/.test(PANEL),
        'A14g chip 各档名字从徽章表取,面板里没有另写的一份');
    /* F9:详情页 5 处原样 .message 也走人话层 */
    const RAW = /\b(\w+) instanceof Error \? \1\.message\b/;
    ok(!RAW.test(DETAIL) && /userFacingError\(/.test(DETAIL) && /userError\(await readError\(/.test(DETAIL),
        'A14h 🔴 详情页出错分支不再原样展示 .message(后端给的人话仍原样上屏)');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过`);
    process.exit(1);
}
console.log('\n全部通过');
process.exit(0);
