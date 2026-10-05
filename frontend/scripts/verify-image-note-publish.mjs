#!/usr/bin/env node
/**
 * 判据 · #186 图文发布面板(极简两步)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 本单最要紧的一条是 **N1:提交体每项恰好七个键**。
 *    内容(标题/正文/图)**不在请求体里** —— 它来自冻结版本
 *    (`post_revision_id` + `prepared_artifact_id` + `manifest_hash`)。
 *    往 body 里塞 title 就等于开了一条"绕过冻结版本改内容"的路:
 *    预览看到的是 A,发出去的是 B,而两边都不会报错。
 *
 * 🔴 契约取自**代码**不是转述:`api/geo_image_note_api.py:285-312`。
 *    模型上还有第八个 `expected_price_points`,但全后端**零读取**
 *    (2026-09-13 实测:只有它自己的声明与一条 pytest 夹具命中),
 *    它头顶那句「服务端据此逐项比对」是假注释 —— 真正的逐项校验走指纹。
 *    所以本单**不发**它;N1 的"七"也因此成立。
 *
 * 真 DOM 那几条在 `test-image-note-publish-render.mjs`(chromium,不进 build 链)。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

/*
 * 🔴 [#188 · Review 09-13] 发布 UI 从**发布中心的批量面板**搬到
 *    **制作台作品卡上的行内发布**(设计 §2 区 E)。`ImageNotePublishPanel.tsx` 已删除。
 *    本段大部分判据钉的是**契约与失败态**(七键、价格不落 0、终态、错误码、禁用理由),
 *    这些一条没变,只是住址变了 —— 换路径不换意图。
 *    形状真变了的那几条(批量行选择 / 全阻塞解释行)另行重锚到区 E,见 N2 段。
 */
const PANEL = 'src/pages/Writing/ImageNotePublishInline.tsx';
const API = 'src/lib/imageNoteApi.ts';
const MOD = 'src/pages/Publishing/imageNotePublishScope.ts';
const PC = 'src/pages/Publishing/PublishCenter.tsx';
const market = decomment(rd('src/pages/Publishing/ShortVideoAccountMarket.tsx'));
const marketHelpers = rd('src/pages/Publishing/shortVideoMarket.ts');
const marketJs = require_('typescript').transpileModule(marketHelpers, {
    compilerOptions: { module: require_('typescript').ModuleKind.ESNext, target: require_('typescript').ScriptTarget.ES2022 },
}).outputText;
const K = await import(`data:text/javascript;base64,${Buffer.from(marketJs).toString('base64')}`);

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd(MOD), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a186-'));
    const f = join(tmp, 'scope.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL M0 🔴 判定模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}
/*
 * 🔴 [#188] `toSelectableRows` 从这张清单里**删掉**了 —— 它是批量面板的行模型,
 *    面板退役后零调用,已从模块里删除。相应的谓词由区 E 的
 *    `zoneEState / canPublishCard` 承担(见下面 N2 段的搬家说明)。
 * 🔴 另加一条**反臂**:它必须真的不在了。留一个没人调用的导出,
 *    判据就能继续对着它报绿,而屏幕上那件事归另一份实现管。
 */
ok(['eligibleAccounts', 'buildBatchItems', 'isCommandTerminal',
    'totalPointsFromPreview'].every((k) => typeof M[k] === 'function'),
    'M1 四个判定函数都导出了');
ok(typeof M.toSelectableRows === 'undefined',
    'M1b 🔴 已退役的 toSelectableRows 真的删了(留着 = 判据可以对着一个没人调用的函数报绿)');

/*
 * 🔴 [#204 a2] 这里原来 transpile `imageNoteStudioApi.ts` 去真调 `zoneEState`/`canPublishCard`。
 *    那个模块随制作台退役已删(删屏后它的生产 import 只剩 0 处)。
 *
 *    「没有版本就发不出去」这个谓词,到今天为止**搬过三次家**:
 *      ① `toSelectableRows`(#186 批量面板)—— 面板退役后零调用;
 *      ② `zoneEState / canPublishCard`(#188 制作台区 E)—— 本单退役;
 *      ③ 现在:`imageNotePublishStatus.canPublishRow`(发布中心)。
 *    每一次都是**宿主死了**,而判据留在原地继续绿 —— 那正是「毒够不着」的形状:
 *    毒它,纯函数臂红、浏览器臂纹丝不动,因为被毒的那份不在被测路径上。
 *
 *    ⇒ 这一次不把它抄到这里来:活的那份由 `verify-image-note-publish-list.mjs`
 *      的 L4 / L5 / L7 段承担。同一个谓词两处判据,必有一处没人验。
 */
const panel = decomment(rd(PANEL));
const api = decomment(rd(API));
const pc = decomment(rd(PC));

// Production prepares a bigint JSON number, not an invented opaque artifact string.
const ART = { prepared_artifact_id: 101, manifest_hash: 'mh-1', state: 'ready' };
const CHOSEN = [{ postId: 7, revisionId: 3, mediaId: 88, itemRequestId: 'req-1' }];
const PREVIEW_ITEMS = [{ item_request_id: 'req-1', geo_post_id: 7, publish_price_fingerprint: 'fp-1', final_price_points: 130 }];

// ══ N1 提交体形状:恰好七个键 ═══════════════════════════════════════
console.log('\nN1 提交体七个键');
{
    const items = M.buildBatchItems({ chosen: CHOSEN, artifacts: { 7: ART }, previewItems: PREVIEW_ITEMS });
    ok(items.length === 1, 'N1a 拼得出一项(分母非空)', JSON.stringify(items));
    ok(items[0]?.prepared_artifact_id === '101', 'N1a2 真回包数字素材编号归一为提交字符串');
    const keys = Object.keys(items[0] || {}).sort();
    const want = [...M.BATCH_ITEM_KEYS].sort();
    ok(keys.length === 7, 'N1b 🔴 每项**恰好七个键**', `${keys.length} 个:${keys.join(',')}`);
    ok(JSON.stringify(keys) === JSON.stringify(want),
        'N1c 🔴 键名逐个对上契约(api/geo_image_note_api.py:285-312)', keys.join(','));
    for (const forbidden of ['title', 'body', 'images', 'image_urls', 'content']) {
        ok(!(forbidden in (items[0] || {})),
            `N1d 🔴 请求体里没有 ${forbidden} —— 内容来自冻结版本,`
            + '塞进 body 就等于开了一条绕过冻结改内容的路');
    }
    ok(!('expected_price_points' in (items[0] || {})),
        'N1e 不发那个**零读取**的第八键(它头顶的注释是假的,真校验走指纹)');
    // 反臂:任一必需字段缺失就整项不发(宁可少发一条,也不发必被拒的项)
    const noFp = M.buildBatchItems({ chosen: CHOSEN, artifacts: { 7: ART }, previewItems: [] });
    ok(noFp.length === 0, 'N1f 🔴 preview 没给指纹 ⇒ **不发这一项**(不自己造指纹)');
    const noArt = M.buildBatchItems({ chosen: CHOSEN, artifacts: {}, previewItems: PREVIEW_ITEMS });
    ok(noArt.length === 0, 'N1g 素材没就绪 ⇒ 不发');
    ok(/expected_price_fingerprint: string/.test(api) && !/expected_price_points/.test(
        api.slice(api.indexOf('export function submitPublish'), api.indexOf('export function submitPublish') + 900)),
        'N1h 接线:submitPublish 的签名就是七键(签名与拼装两处不许分家)');
}

// ══ N2 无版本不可勾 ═══════════════════════════════════════════════
console.log('\nN2 没有版本就不许勾');
{
    /**
     * 🔴 N2a–N2e **搬家(#188)**:原来验的是 `toSelectableRows`(批量面板的行模型)。
     *    面板退役后它零调用,而"没有版本就发不出去"这件事**改由**
     *    `imageNoteStudioApi.zoneEState / canPublishCard` 决定。
     *    继续验那份死的 = 判据全绿而屏幕上归另一份管(注毒 V3 当场照出来了:
     *    毒它,纯函数臂红、浏览器臂纹丝不动 —— 「毒够不着」不是「锁没牙」)。
     *    死的那份已删,这几条钉到活的那份上。
     */
    /*
     * 🔴 [#204 a2 · 杀] N2a–N2e3(8 条)原先真调 `zoneEState / canPublishCard`。
     *    那个模块随制作台退役已删。活的那份谓词是发布中心的 `canPublishRow`,
     *    由 `verify-image-note-publish-list.mjs` 的 L4 / L5 / L7 段承担。
     *    不在两处各写一遍:同一个谓词两处判据,必有一处没人验。
     */
    /**
     * 🔴 N2f **重锚(#188)**:原来是批量面板里"没版本的行,勾选框置灰"。
     *    区 E 的做法**更强**:没版本的卡**根本不给发布按钮**(Review 09-13 裁定:
     *    不给按钮、也不做灰行 —— 一列灰按钮只会让人反复点)。
     *    所以锚从"置灰"改成"压根不渲染",意图(发不出去的东西不许摆成可点)没变,门槛还高了。
     */
    /*
     * 🔴 [#204 a2 · 迁] N2f(无版本的卡压根不渲染发布键)与
     *    N2g0 / N2g(一条都不能勾时必须有区域级说明,且守卫就是那个条件本身)
     *    —— 命题都活着,宿主换成**发布中心**:那才是用户现在发东西的地方。
     *    新家:`verify-image-note-publish-list.mjs` 的 L7a–L7f
     *    (新增纯函数 `blockedAllNotice` + `pub-imagenote-blocked-note`)。
     *
     *    🔴 迁的时候发现那句说明原文写着「…系统补齐后就能在**发布中心**发出去」——
     *      它住在制作台、指向发布中心。制作台一删,用户到了发布中心
     *      看到的是一排没有「发布」的行 + 零解释。说明要跟着**用户**走。
     */
}

// ══ N3 账号只列能发图文且可用的 ═══════════════════════════════════
console.log('\nN3 账号过滤');
{
    const accts = M.eligibleAccounts([
        { id: 1, media_name: '能发', can_tuwen: 1, blacklist: 0, is_active: true },
        { id: 2, media_name: '不能发图文', can_tuwen: 0, blacklist: 0, is_active: true },
        { id: 3, media_name: '拉黑', can_tuwen: 1, blacklist: 1, is_active: true },
        { id: 4, media_name: '停用', can_tuwen: 1, blacklist: 0, is_active: false },
    ]);
    ok(accts.length === 1 && accts[0].mediaId === 1,
        'N3a 🔴 只留 can_tuwen=1 且未拉黑且在用的', JSON.stringify(accts.map(a => a.mediaId)));
    /**
     * 🔴 N3b 是**边界锁**,防的是我自己把这里做成"第二个资格判官":
     *    资格 SSOT 是 `services/geo_douyin/account_eligibility.py`
     *    (抖音平台 + can_tuwen + active + blacklist + **真实频控**)。
     *    前端只做得到前三条(目录里有这三个字段),**当日额度看不到也不许猜** ——
     *    它由 publish-preview 逐项回的 eligibility 说了算。
     */
    const mod = decomment(rd(MOD));
    for (const forbidden of ['today_remaining', 'daily_limit', 'today_used', 'quota']) {
        ok(!new RegExp(forbidden).test(mod),
            `N3b 🔴 前端不碰 ${forbidden} —— 当日额度归服务端,前端算就是造第二个判官`);
    }
    /*
     * 🔴 [#192] N3c 重锚。原来禁的是"面板里出现 can_tuwen 这个词" ——
     *    #192 之后面板**必须**出现它:作为**查询参数**让服务端过滤
     *    (不带的话就是在两万条目录的第一页里找,永远找不到)。
     *    它真正要挡的是"面板自己再写一遍过滤逻辑",也就是**比较/分支**,
     *    不是"提到这个字段名"。所以改钉比较式。
     */
    ok(!/can_tuwen\s*(===|!==|==|!=)/.test(panel) && !/\.can_tuwen/.test(panel),
        'N3c 🔴 面板**不自己实现**过滤(没有对 can_tuwen 的比较/取值)——'
        + '过滤主力在服务端,前端那道二次防御只在 eligibleAccounts 一处');
    ok(/<ShortVideoAccountMarket/.test(panel) && /imageNote: true/.test(panel)
        && /<ShortVideoAccountMarket/.test(rd('src/pages/Publishing/ShortVideoPanel.tsx')),
        'N3d 图文与视频共用同一完整账号市场，图文固定能力范围；动态资格仍只归preview');

    /*
     * 🔴 [#192 a4] N3 换锚:过滤的**主力从前端搬到服务端**。
     *    现场(0913a)撞到的缺陷:面板拉的是媒介盒子**供应商目录**(生产 21,850 条),
     *    第一页 50 条 `can_tuwen` 全 0 ⇒ 前端怎么滤都滤不出东西,
     *    面板永远说「还没有能发图文的账号」,发布键永远灰着。
     *    ⇒ 请求必须带 `can_tuwen=1`(C 的 c1 落这个参数)。
     *    前端那道过滤**保留**,降级为二次防御(N3a–N3d 原样不动)。
     */
    const query = new URLSearchParams(K.marketQuery({ ...K.emptyMarketFilters(), page: 3, search: '酒店' }, { platform: '抖音', imageNote: true }));
    ok(query.get('can_tuwen') === '1' && /marketQuery\(filters, scope\)/.test(market),
        'N3e 🔴 账号请求**带 can_tuwen=1** —— 不带的话就是在两万条目录的第一页里'
        + '找能发图文的号,永远找不到(这正是现场那一格)');
    ok(query.get('limit') === '20' && query.get('page') === '3' && query.get('platform') === '抖音' && query.get('search') === '酒店',
        'N3f 账号请求带分页参数(目录两万条,不翻页只能看见头 50 个)');
    ok(/search/.test(market) && /inp-account-search/.test(market),
        'N3g 🔴 有搜索框 —— 没有它,用户只能在服务端给的那一页里碰运气');
    {
        /* 新字段:服务端原值直显,且 pricePoints 用 null 不用 0 */
        const withFields = M.eligibleAccounts([{
            id: 5, media_name: '甲媒体', can_tuwen: 1, blacklist: 0, is_active: true,
            platform: '抖音', price_points: 120, fans_num_text: '12.3w',
        }]);
        ok(withFields.length === 1 && withFields[0].platform === '抖音'
            && withFields[0].pricePoints === 120 && withFields[0].fans === '12.3w',
            'N3h 选账号看得见 平台 / 售价算力 / 粉丝(服务端原值)', JSON.stringify(withFields));
        const noPrice = M.eligibleAccounts([
            { id: 6, media_name: '乙', can_tuwen: 1, blacklist: 0, is_active: true }]);
        ok(noPrice[0].pricePoints === null,
            'N3i 🔴 服务端没给价 ⇒ `null` 而不是 0 —— 0 会被读成"免费"');
    }
}

// ══ N4 前端零价格算术 ═════════════════════════════════════════════
console.log('\nN4 零价格算术');
{
    ok(M.totalPointsFromPreview({ total_price_points: 260 }) === 260, 'N4a 合计原样取服务端的数');
    ok(M.totalPointsFromPreview({}) === null,
        'N4b 🔴 取不到 ⇒ **null 而不是 0**(显示 0 等于告诉用户免费)');
    /**
     * 谓词打的是「price/points 类变量参与算术」,不是"那一行不见了" ——
     * 后者删掉一处乘号就绿,而真正的算式可能在上一层(#150 的教训)。
     */
    const ARITH = /(points|Price|price)[A-Za-z_]*\s*[*+\-/]\s*[A-Za-z_(]|[A-Za-z_)]\s*[*+\-/]\s*(points|Price|price)[A-Za-z_]*/;
    for (const [name, src] of [['面板', panel], ['数据层', api], ['判定层', decomment(rd(MOD))]]) {
        const hit = (src.match(new RegExp(ARITH, 'g')) || []);
        ok(hit.length === 0, `N4c ${name}:零价格算术`, hit.slice(0, 2).join(' | ') || '0 处');
    }
    ok(ARITH.test('const t = firstPrice + over * extraCardPrice;'),
        'N4d 正样本臂:旧页那种算式会被这个谓词识别(否则 N4c 是空断言)');
    ok(/expected_total_price_points: total/.test(panel),
        'N4e 提交时报的总价就是 preview 给的那个数(不重算、不摊平)');
}

// ══ N5 轮询到终态停 ═══════════════════════════════════════════════
console.log('\nN5 轮询终止');
{
    for (const s of ['succeeded', 'failed', 'partial', 'cancelled']) {
        ok(M.isCommandTerminal(s) === true, `N5a 终态 ${s} ⇒ 停`);
    }
    for (const s of ['accepted', 'running', 'queued', '']) {
        ok(M.isCommandTerminal(s) === false, `N5b 非终态 ${s || '(空)'} ⇒ 继续`);
    }
    ok(M.isCommandTerminal('WEIRD_NEW_STATE') === false,
        'N5c 🔴 认不出的状态**当作没结束**(宁可多轮一次,也不谎称结束)');
    ok(/isCommandTerminal\(command\?\.command_status\)/.test(panel),
        'N5d 接线:轮询的停止条件走这个函数');
    ok(/failure_reason/.test(panel),
        'N5e 失败行显示服务端原话(不自造文案 —— 自造会在后端改口径时静默变谎)');
    ok(/PUBLISH_ITEM_COPY\[String\(it\.state\)\]|PUBLISH_ITEM_COPY\[it\.state\]/.test(panel)
        && !/it\.state === 'failed' \?/.test(panel),
        'N5f 🔴 [Review 09-13 ①] 终态由**服务端 state** 翻译,面板不自己推断');
    // 🔴 又是存在锁:第一版只查 testid 在不在,而毒把**码本身**从渲染里拿掉时
    //    testid 还在 —— 照样绿。要钉的是"码真的被渲染出来"。
    ok(/data-testid="inp-submit-error-code"/.test(panel)
        && /\{submitErr\.contract\.code\}/.test(panel),
        'N5g 🔴 [Review 09-13 ②] 服务端拒绝时把**错误码**也显示出来 ——'
        + '吞掉码只剩一句笼统的"操作未完成",定位信息就没了(d5 之后塞 title 会 400 带码)');
}

// ══ N6 三处入口 ═══════════════════════════════════════════════════
console.log('\nN6 入口');
{
    /**
     * 🔴 N6a **重锚(#188 · Review 09-13)**:发布从"跳发布中心"改成**行内展开**,
     *    跨页链撤了 ⇒ "URL 三参同带"这个锚失去了指称对象。
     *    但它挡的事没变:成品落到**别的服务商的客户**名下(生产实证 svideo 3 条里 2 条)。
     *    风险搬进了提交体 —— 报价/提交都带 `brand_id`,且它来自 props。锚跟着搬。
     */
    {
        const inline = rd('src/pages/Writing/ImageNotePublishInline.tsx');
        /*
         * 🔴 [#204 a2 · 杀] N6a / N6a3 钉的是**制作台**给的那条跨页链
         *    (存在 + 三参同带)。制作台已删 ⇒ 指称对象没了。
         *    它挡的那件事(成品落到别的服务商的客户名下)没变,现在由
         *    a1 的 **D3r2** 承担:详情页那颗「去发布投放」**真的**带上 brand_id
         *    —— 而且 D3r2 量的是浏览器真的去了哪,死代码满足不了它。
         */
        /*
         * 🔴 [#204 a2 · 复原] N6a2 是被我**误删**的:切除 N6a / N6a3 时区间把中间
         *    这一条一起吃了,而它讲的是**行内面板**的提交体,跟制作台没关系。
         *    是注毒 V9 把它照出来的 —— 毒下去了却全绿,查下去发现"锁没了"。
         *    (「毒仍绿」的第五解:锁被我自己删了。)
         */
        ok(/brand_id: brandId/.test(inline),
            'N6a2 🔴 行内发布的报价请求带 brand_id(风险搬家后的新住址;'
            + '面板换了挂载点,这一格要守的东西一个字没变)');
    }
    /**
     * 🔴 N6b **重锚(#188)**:批量面板已删,发布搬到制作台行内。
     *    这一格原来钉"tab 挂着新面板";现在钉的是三件事:
     *      · `'imagenote'` 这一档**仍在状态机里** —— #150 撤它时只删了按钮与面板,
     *        `?media_type=imagenote` 的深链照样切过去然后**什么都不渲染**(判据 G5 抓到过);
     *        #186 发出去的链就是这个形状,可能已经在别人收藏夹里;
     *      · 它渲染的是**一句指路**(搬哪儿去了 + 送过去),不是空白;
     *      · 它**不是**第二条发布路(面板真的不在了)。
     */
    /*
     * 🔴 **N6b 再重锚(#195)**:这一档从「一句指路」长成了**只看不发的列表**,
     *    指路那一行连同它的两个 testid 搬进了 `ImageNoteList.tsx`。
     *    意图一次没变(**不许是空白 / 不许是第二条发布路 / 必须能走出去**),
     *    所以锚跨两个文件:本页挂列表组件,组件里带指路与出口。
     *    🔴 只改本页的锚会让这一格在"指路被整段删掉"时仍然绿 ——
     *    那正是 #150 那次的形状(删了面板,深链渲染一片空白)。
     */
    const inl = decomment(rd('src/pages/Publishing/ImageNoteList.tsx'));
    ok(/imageNoteMode && \(/.test(pc) && /publicationEntry\(searchParams\)/.test(pc) && pc.includes('<ImageNoteList')
        && !/ImageNotePublishPanel/.test(pc),
        'N6b 🔴 深链这一档还在,挂的是那张列表(不是空白 —— #150 那次就是删了面板、深链渲染一片空白)');
    ok(/publish-imagenote-moved/.test(inl) && /publish-imagenote-goto-studio/.test(inl),
        'N6b2 🔴 列表顶上仍带**一句指路 + 出口** —— 人到了这里要知道"做新的一条"去哪');
    /*
     * 🔴 N6b3 **翻面(#203 §1.3)**。#195 时这一格钉的是"列表只看不发",
     *    因为那会儿发布在制作台,列表再能发就是第二条路。
     *    Owner/Review 把发布**收口到本档**之后,唯一那一处就在这里 ——
     *    继续钉"这里不许发"会**反过来挡住正确的改法**。
     *    ⇒ 命题不变(同一个付费动作只许一处实现),换成:本档有面板、制作台没有。
     */
    ok(/ImageNotePublishInline/.test(inl) && /pub-imagenote-publish-toggle/.test(inl),
        'N6b3 🔴 发布面板**挂在本档**(#203 收口后这里是唯一发布点)');
    /*
     * 🔴 [#204 a2 · 杀] N6b4 是**乙类**:「制作台里没有面板」。
     *    制作台删掉之后它**永远为真** —— 不会红,也不再挡任何事。
     *    反向对照本身不能就这么没了(N6b3 只证"这里有"),域升成全仓:
     *    `verify-image-note-detail-restore.mjs` 的 **Z3**(`<ImageNotePublishInline` 全仓恰好一处)。
     */
    ok(/canPublishRow\(/.test(inl),
        'N6b5 🔴 "这一行能不能发"走**同一处谓词**(`canPublishRow`),'
        + '不在 JSX 里另写一遍条件 —— 另写一遍就会有一天按钮的条件和判据的条件不是同一句话');
    ok(/publish-svideo-imagenote/.test(pc) && /publish-svideo-upload/.test(pc) && !/publish-tab-imagenote/.test(pc), 'N6c 短视频双子入口可定位，无独立图文顶层tab');
    const svp = decomment(rd('src/pages/Publishing/ShortVideoPanel.tsx'));
    ok(!/选择图文/.test(svp) && !/发图文笔记/.test(svp),
        'N6d 🔴 短视频面板那条**必 400 的老口**仍然关着 ——'
        + '恢复新链不等于把老链也放回来(#179 G12 回归)');
    /**
     * 🔴 N6e **重锚(#188)**:原来钉"面板不收 brandId prop,自己读 ClientContext" ——
     *    那是当年防"第二个当前客户来源"的写法。行内组件长在制作台里面,
     *    制作台**已经**从 ClientContext 取了 brandId;行内组件再读一次 context
     *    才是造第二个来源(两处各读一次,切客户时可能短暂不一致)。
     *    ⇒ 意图不变(**全页只有一个**"当前客户"),锚翻面:
     *      行内组件**只**收 prop、**不许**自己读 context;context 只在制作台读一次。
     */
    /* 🔴 [#204 a2 · 迁] 宿主从制作台换成**发布中心的图文档**:
       面板现在长在 `ImageNoteList` 里,由它从 ClientContext 取一次 brandId 传下去。
       意图一个字没变:**全页只有一个**"当前客户"来源;行内组件只收 prop、不自己读 context
       (两处各读一次,切客户时会短暂不一致)。 */
    ok(/brandId, postId, revisionId, keyword/.test(panel)
        && !/useClientContext/.test(panel)
        && /brandId=\{brandId\}/.test(inl),
        'N6e 🔴 全页只有一个"当前客户"来源:发布中心图文档读一次并传下去,行内组件只收 prop');
}

// ══ N7 极简自证:可点控件计数 ═══════════════════════════════════════
console.log('\nN7 发布主动作单一（账号筛选复用完整市场）');
{
    /**
     * 🔴 Owner 原则:「不要弄非必要的按钮,选项尽量简单」。
     *    这一格把它变成**机械可查**的:面板里可点的东西只许是这几类,
     *    多一个就要在交付物里写存在理由(写不出就删)。
     */
    const allow = [
        'inp-select-all',     // 全选
        'inp-pick-',          // 每行勾选
        'inp-account-',       // 每行账号下拉
        'inp-publish',        // 发布(唯一主动作)
        'inp-reload',         // 读取失败时的重试
        'inp-result-link',    // 成功后的"查看"
    ];
    const found = [...panel.matchAll(/data-testid="(inp-[a-z0-9-]+)"/g)].map(m => m[1]);
    const clickable = found.filter(t => /select-all|pick-|account-|publish|reload|result-link/.test(t));
    const extra = clickable.filter(t => !allow.some(a => t.startsWith(a.replace(/-$/, ''))));
    ok(extra.length === 0,
        'N7a 🔴 可点控件只有清单内那几类(多一个就要写存在理由)', extra.join(',') || '无多余');
    // 一屏只有一个主动作
    const primaries = (panel.match(/<Button\s(?![^>]*variant=)/g) || []).length;
    ok(primaries === 1,
        'N7b 🔴 **主动作只有一个**(其余按钮一律 variant=outline/ghost 弱化)',
        `${primaries} 个实心 Button`);
    ok(/data-testid="inp-disabled-reason"/.test(panel),
        'N7c 禁用理由画在按钮**外面**(写在按钮里会跟着透明度一起变淡 —— #179 根因①)');
    ok(!/自定义标题|编辑正文|重新准备|重投/.test(panel),
        'N7d 🔴 撤掉的东西没偷偷回来:自定义标题/正文、手动"重新准备"、失败重投');
}

// Owner 09-20 asks for the full market. Run real React rendering/behavior, not an obsolete card-count gate.
await import('./test-short-video-market.mjs');
console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('全部通过');
    process.exitCode = 0;
}
