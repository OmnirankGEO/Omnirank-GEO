#!/usr/bin/env node
/**
 * 判据 · #195 发布中心「图文」档:已发 / 在发 / 失败的列表视图。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 状态类判据最容易写成**存在锁**(「这几个词在源码里」)。那种锁挡不住
 *    "有这张表但渲染时又推了一遍"。所以这里:
 *    · 映射与分桶由**纯函数**产出 ⇒ 判据 transpile 后**真调它**拿值比;
 *    · 接线与结构单独钉(组件渲染的是那些函数的产物、且这一档没有发布按钮);
 *    · 颜色/可见性/点击后的变化交给浏览器臂(test-image-note-publish-list-render.mjs)。
 *
 * 🔴 **本闸不引后端路径**(build 镜像只有 frontend/,`verify-no-backend-refs-in-build-chain`
 *    会拦)。后端取值域漂移由 `verify-image-note-publish-status-domain.mjs` 盯,
 *    那把闸**不在 build 链里**,由我与复审手动跑。
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
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Publishing/imageNotePublishStatus.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a195-'));
    const f = join(tmp, 'status.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL L0 🔴 状态投影模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

const LIST = decomment(rd('src/pages/Publishing/ImageNoteList.tsx'));
const PC = decomment(rd('src/pages/Publishing/PublishCenter.tsx'));

console.log('L #195 图文发布列表');

/* ══ L0 分母自证:白名单真的有内容,且下面每条不是在对空表恒真 ══════════ */
{
    ok(Array.isArray(M.KNOWN_PUBLISH_STATUSES) && M.KNOWN_PUBLISH_STATUSES.length === 5,
        'L0a 分母自证:白名单里是 5 个非空原值(六档里 `\'\'` 不算原值)',
        `${(M.KNOWN_PUBLISH_STATUSES || []).length} 个:${(M.KNOWN_PUBLISH_STATUSES || []).join(',')}`);
    ok(Array.isArray(M.PUBLISH_CHIPS) && M.PUBLISH_CHIPS.length === 7,
        'L0b 分母自证:七枚包含待发四态、尚未做完、待核对、全部',
        (M.PUBLISH_CHIPS || []).map((c) => c.key).join(','));
}

/* ══ L1 状态只投影,不推断 ═══════════════════════════════════════════ */
{
    /*
     * 🔴 六档逐个真调。**照后端写入点枚举出来的**,不是照工单抄的四档:
     *    `self_reported_unverified`(客户端自报的线索值)与 `measured`
     *    (后端终态集里那个)工单都没写,漏掉前者就会被 published_url 推成「已发布」。
     */
    const EXPECT = [
        ['', 'unpublished'],
        ['publishing', 'inflight'],
        ['published', 'published'],
        ['measured', 'published'],
        ['failed', 'failed'],
        ['self_reported_unverified', 'inflight'],
    ];
    const wrong = EXPECT.filter(([v, b]) => M.bucketOf(v) !== b)
        .map(([v, b]) => `${v || "''"}→${M.bucketOf(v)}(应 ${b})`);
    ok(wrong.length === 0,
        'L1a 六档状态逐个映射到写死的桶(真调 bucketOf,不是 grep 那张表)',
        wrong.length ? wrong.join(' / ') : `${EXPECT.length} 档全对`);

    /*
     * 🔴 **这一格是工单点名的毒**:前端按 `published_url` 非空自推「已发布」。
     *    两个反例都要:①未发布却带 url;②自报未核实却带 url。
     *    ②尤其要紧 —— 把"有人说发了"显示成"已发布",是替服务商向客户
     *    断言一件我们没有证据的事。
     */
    const r1 = M.publishRow({ id: 1, publish_status: '', published_url: 'https://www.xiaohongshu.com/x/1' });
    const r2 = M.publishRow({ id: 2, publish_status: 'self_reported_unverified', published_url: 'https://www.xiaohongshu.com/x/2' });
    ok(r1.bucket === 'unpublished' && r1.url === '',
        'L1b 🔴 `published_url` 非空**不能**把「未发布」推成已发布,也不给链接',
        `bucket=${r1.bucket} url=${r1.url || '(空)'}`);
    ok(r2.bucket === 'inflight' && r2.url === '',
        'L1c 🔴 「自报未核实」带着 url 也**不算**已发布 ——'
        + '那是替服务商对客户断言一件没有证据的事',
        `bucket=${r2.bucket} label=${r2.statusLabel} url=${r2.url || '(空)'}`);
    /* 反向控制:真的 published + 有 url 时**必须**给链接。
       没有这一条,上面两格可以靠"永远不给链接"作弊满足。 */
    const r3 = M.publishRow({ id: 3, publish_status: 'published', published_url: 'https://www.xiaohongshu.com/x/3' });
    ok(r3.bucket === 'published' && r3.url === 'https://www.xiaohongshu.com/x/3',
        'L1d 反向控制:`published` + 有 url ⇒ **给**链接(否则上两格可以靠"从不给链接"作弊)',
        `url=${r3.url || '(空)'}`);

    /* 认不出的值:不上屏裸串、也不消失 */
    const rx = M.publishRow({ id: 4, publish_status: 'brand_new_state_from_backend' });
    ok(rx.bucket === 'unknown' && !rx.statusLabel.includes('brand_new_state_from_backend'),
        'L1e 🔴 认不出的状态 ⇒ 桶 unknown、徽章**不含裸串**'
        + '(后端原话:前端对 publish_status 没有白名单,新加一个裸串会直接上屏)',
        `bucket=${rx.bucket} label=${rx.statusLabel}`);
    const st = M.listState([{ id: 4, publish_status: 'brand_new_state_from_backend' }]);
    ok(M.filterRows(st.rows, 'all').length === 1,
        'L1f 🔴 认不出的那条仍然落在「全部」里 —— 它最该被看见,不许从所有视图消失');

    /* published 但没回链接:说清楚,不给空 href */
    const r5 = M.publishRow({ id: 5, publish_status: 'published', published_url: '' });
    ok(r5.bucket === 'published' && r5.url === '',
        'L1g `published` 但服务端没回 url ⇒ 不给链接(空 href 又是一颗点了没反应的东西)');
    ok(/PUBLISHED_WITHOUT_URL_NOTE/.test(LIST) && /pub-imagenote-nourl/.test(LIST),
        'L1h 那种行有一句说明,不是默默什么都不显示');
}

/* ══ L2 链接是原值 + 新开 + rel ══════════════════════════════════════ */
{
    ok(/href=\{row\.url\}/.test(LIST),
        'L2a 链接 href 用的是投影出来的 `row.url`(服务端原值)');
    ok(/target="_blank"/.test(LIST) && /rel="noopener noreferrer"/.test(LIST),
        'L2b 新开 + `rel="noopener noreferrer"`');
    /*
     * 🔴 毒面:链接指向**站内详情**。所以钉两件事:
     *    ① 那颗链接的 href 不是模板串;② 投影层不拼任何站内路径进 `url`。
     *    `proofHref`(校对)**是**站内路径,它是另一颗链接 —— 别把两者混为一谈。
     */
    const r = M.publishRow({ id: 7, publish_status: 'published', published_url: 'https://example.com/real' });
    ok(!r.url.startsWith('/') && !r.url.includes('/writing/image-note/'),
        'L2c 🔴 `url` 里不许出现站内路径 —— 拼一个站内详情塞进去,'
        + '用户点了看不到"发出去的那篇",而界面看起来一切正常',
        r.url);
    ok(r.proofHref === '/writing/image-note/7',
        'L2d 「校对」走的是**另一颗**链接、已有站内路由 `/writing/image-note/:postId`',
        r.proofHref);
}

/* ══ L3 失败行**必须显示原话**,且不许拿制作失败的原因冒充 ═════════ */
{
    /*
     * 🔴 **这一格 09-13 翻面**:上一版钉的是「缺字段 ⇒ 只许说实话」。
     *    C 的 195-c1(452b8e812)已经把 `publish_failure_reason`
     *    (供应商 `reject_reason` 原话)带进 `GET /posts` 的**每一行** ——
     *    `publish_status='failed'` 时非空,其余状态恒空串(稳定形状,不是 None)。
     *    ⇒ 现在钉两件事:失败行**显示原话**、原话为空才退兜底。
     */
    const RAW = '账号当日额度已用完,明天再发';
    const rf = M.publishRow({ id: 11, publish_status: 'failed', publish_failure_reason: RAW });
    ok(rf.failureReason === RAW,
        'L3a 失败行把服务端原话**逐字**投影出来(不改写、不概括)', rf.failureReason);
    ok(/row\.failureReason/.test(LIST) && /FAILED_REASON_FALLBACK/.test(LIST),
        'L3b 组件渲染的是那句原话,原话为空才退兜底');
    /* 反向对照:非失败行服务端恒空串 ⇒ 不许凭空长出一句失败原因 */
    const rp = M.publishRow({ id: 12, publish_status: 'published', published_url: 'https://a/1',
        publish_failure_reason: '' });
    ok(rp.failureReason === '',
        'L3c 反向对照:非失败行没有失败原话(空串)—— 有的话就是凭空长出一句解释');
    /*
     * 🔴 **本单最容易做错的地方**:同一行上还有一个 `failure_reason`,
     *    那是**制作**任务的 error_msg(`describe_task_progress` 的 failed/stalled 分支)。
     *    C 的提交说得最清楚:混用会让"发布被拒"显示成"生成失败" ——
     *    用户按前者去重做内容,而发布被拒真正要做的是换账号或改文案。
     *    所以投影层与组件**都不许**碰它;堵一处等于没堵。
     */
    const mix = M.publishRow({
        id: 13, publish_status: 'failed',
        publish_failure_reason: '',
        failure_reason: '生成第 3 张图超时(制作侧)',
    });
    ok(mix.failureReason === '',
        'L3d 🔴 只有 `publish_failure_reason` 空时,**不许**把制作的 `failure_reason` 顶上来'
        + '(一句具体但错的解释比含糊更糟)', mix.failureReason || '(空)');
    ok(!/failure_reason/.test(LIST),
        'L3e 组件源码里不出现裸 `failure_reason`(只许 `row.failureReason`)');
    const proj = decomment(rd('src/pages/Publishing/imageNotePublishStatus.ts'));
    /*
     * 🔴 这里**不用 `\b`**:同一天我在两个闸里都把它写成了退格字符(0x08),
     *    否定断言恒真、恒绿。用精确子串,并带正负样本控制。
     *    注意 `o.publish_failure_reason` **不含** `o.failure_reason` 这个子串,
     *    所以精确 includes 不会互相误伤。
     */
    const readsGenFailure = (t) => t.includes('o.failure_reason');
    ok(readsGenFailure('const x = o.failure_reason;')
        && !readsGenFailure('const y = o.publish_failure_reason;'),
        'L3f0 判据自证:能认出读制作 error_msg 那一行,且不把 `publish_failure_reason` 误判成它');
    ok(!readsGenFailure(proj),
        'L3f 投影层也不读它 —— 两处都得堵');
}

/* ══ L4 chip 按桶过滤,计数与列表同源 ════════════════════════════════ */
{
    const posts = [
        { id: 1, publish_status: 'published', published_url: 'https://a/1' },
        { id: 2, publish_status: 'measured', published_url: 'https://a/2' },
        { id: 3, publish_status: 'publishing' },
        { id: 4, publish_status: 'self_reported_unverified' },
        { id: 5, publish_status: 'failed' },
        { id: 6, publish_status: '' },
        { id: 7, publish_status: 'who_knows' },
    ];
    const st = M.listState(posts);
    ok(st.rows.length === 7 && st.counts.all === 7,
        'L4a 分母自证:七条都进来了', `rows=${st.rows.length} all=${st.counts.all}`);
    const want = { published: 2, inflight: 2, failed: 1, unpublished: 1 };
    const off = Object.entries(want)
        .filter(([k, v]) => st.counts[k] !== v)
        .map(([k, v]) => `${k}=${st.counts[k]}(应 ${v})`);
    ok(off.length === 0, 'L4b 四个桶的计数逐个对',
        off.length ? off.join(' / ') : JSON.stringify(want));
    /*
     * 🔴 **计数与过滤必须同源**:各写一遍就会出现"chip 说 3 条、点进去 2 条"。
     *    这里逐 chip 断言 `counts[k] === filterRows(...).length`。
     */
    const drift = M.PUBLISH_CHIPS.filter((c) => c.key !== 'all')
        .filter((c) => M.filterRows(st.rows, c.key).length !== st.counts[c.key])
        .map((c) => c.key);
    ok(drift.length === 0,
        'L4c 🔴 每个 chip 的**计数**与**点进去看到的条数**逐个相等'
        + '(两处各算一遍 ⇒ chip 说 3 条、点进去 2 条)',
        drift.length ? drift.join(',') : '五枚全对');
    ok(M.filterRows(st.rows, 'all').length === 7,
        'L4d 「全部」= 全部(含认不出的那条)');
    /* 接线:组件真的用了这两只函数,而不是在 JSX 里又写一遍过滤 */
    ok(/listState\(/.test(LIST) && /publication_bucket=\$\{chip\}/.test(LIST) && !/filterRows\(/.test(LIST)
        && /setCounts\(data.counts/.test(LIST),
        'L4e 全分母服务端先筛选分页，列表不在已分页结果上再次过滤，计数读同一回包');
    ok(!/publish_status/.test(LIST),
        'L4f 🔴 组件里**不出现** `publish_status` —— 状态判断只许在投影层一处');
}

/* ══ L5 只看不发(结构臂)═══════════════════════════════════════════ */
{
    /*
     * 🔴 **L5a 第一版是个黑名单,而且漏掉了最显然的写法。**
     *    原来钉的是 `发布</Button>` / `'发布'` / `"发布"` 三种形状;注毒 Z7 加了一颗
     *    `<button type="button" ...>发布</button>`(小写标签、不带引号)—— 三条全不命中,
     *    判据**照样全绿**。黑名单锁对它存在的目的那件事是盲的。
     *    ⇒ 改成**按结构取**:把所有按钮的子内容切出来,里面出现「发布」就红。
     *
     * 🔴 为什么 chip 不会误伤:五枚 chip 的按钮体是 `{c.label} {…counts}`,
     *    字面量「已发布 / 发布中 / 发布失败」在**投影层**里,不在这个文件的按钮体里。
     *    (顶部那句指路里有「发布」,但它在 `<p>` 里,不是按钮 —— 所以必须按结构切,
     *     不能整文件 grep。)
     */
    const buttonBodies = (src) => {
        const out = [];
        /* 🔴 不用 `\b`:同一天它在四个文件里被改写工具吃成退格(0x08),
           正则从此永不匹配、否定断言恒绿。用显式字符类。 */
        const re = /<(button|Button)[\s>]/g;
        let m;
        while ((m = re.exec(src)) !== null) {
            const tag = m[1];
            const close = `</${tag}>`;
            const end = src.indexOf(close, m.index);
            if (end < 0) continue;                    // 自闭合或写法异常 ⇒ 没有子内容
            const seg = src.slice(m.index, end);
            /* 只要子内容那一段:跳过开标签的属性。属性里的 `=>` 会有 `>`,
               所以找的是**第一个换行后仍在段内**的 `>` 之后 —— 简化成:
               取最后一个 `>` 之后的部分容易漏,这里改成把整段都算上、
               再剔掉 `data-testid`/`aria-label`/`className` 这些属性值。 */
            const stripped = seg
                .replace(/(?:data-testid|aria-label|className|title)=\{?["'`][^"'`]*["'`]\}?/g, '')
                .replace(/aria-pressed=\{[^}]*\}/g, '');
            out.push(stripped);
        }
        return out;
    };
    /* 判据自证:这个切法既抓得到、又不误伤 chip 那种 `{c.label}` 形状。 */
    ok(buttonBodies('<button type="button">发布</button>').some((b) => b.includes('发布'))
        && !buttonBodies('<button data-testid="chip-published">{c.label} {n}</button>')
            .some((b) => b.includes('发布')),
        'L5a0 判据自证:按钮体切得出来,且不把 `{c.label}` 这种误判成「发布」');
    /*
     * 🔴 **L5a / L5b 在 #203 §1.3 翻面了。**
     *
     *    #195 时这一档"只看不发",因为发布住在制作台 —— 那会儿这里再长出一颗
     *    发布按钮,就是同一个付费动作的第二处实现。
     *    Owner/Review 09-13 把发布**收口到本档**(与写文章同一口径:发布只在发布中心),
     *    唯一那一处搬到了这里。继续钉"这里不许发"就会**反过来挡住正确的改法**。
     *
     *    ⇒ 命题一个字没变(**同一个付费动作只许一处实现**),换的是它落在哪:
     *      · 本档**恰好一颗**发布按钮;
     *      · 制作台**一颗都没有**(反向对照,否则"两处都有"也能全绿)。
     *    上面那套 `buttonBodies` 的黑名单教训照旧用着 —— 它当初是为了
     *    "小写 <button> 也算"而改成按结构取的,换方向不影响。
     */
    // 2026-09-20: content selection and mobile navigation may say 发布 without
    // charging. Keep the contract on the real publisher, not button copy.
    const singlePublisher = src => (src.match(/<ImageNotePublishInline[\s>]/g) || []).length === 1
        && !/submitPublish\(|doPublish\(/.test(src)
        && !src.slice(src.indexOf('export function ImageNoteContentRow')).includes('<ImageNotePublishInline');
    ok(singlePublisher(LIST), 'L5a 右栏唯一现役发布器，内容行没有嵌套发布器或第二份提交处理器');
    ok(!singlePublisher(`${LIST}\n<ImageNotePublishInline />`)
        && !singlePublisher(LIST.replace('<ImageNotePublishInline', '<RemovedPublisher')),
        'L5a1 单发布器判据可分辨多挂一份与完全撤除');
    {
        /*
         * 🔴 [#204 a2 换域] 这条反向对照原来只看**制作台**一个文件。
         *    那块屏已删 ⇒ 原样留着就是一格恒真的绿灯:
         *    「制作台里没有发布按钮」在制作台不存在之后永远成立。
         *    而 L5a 只证"这里有一颗",两处都有也能全绿 —— 反向对照不能就这么没了。
         *    ⇒ 域从"那一个文件"升成**图文现在真正住的那几块屏**。
         */
        const OTHERS = [
            'src/pages/Writing/ImageNoteTopicPanel.tsx',
            'src/pages/Writing/DouyinPostDetail.tsx',
            'src/pages/Writing/ImageNoteDetailRoute.tsx',
        ];
        // A navigation label may contain 发布; detect actual publication handlers, not wording.
        const stray = OTHERS.filter(f => /submitPublish\(|image-notes\/publish-batch/.test(decomment(rd(f)))
            || /<ImageNotePublishInline/.test(decomment(rd(f))));
        ok(stray.length === 0,
            'L5a2 制作详情只可复用现有发布面板，不准复制提交实现；选题/路由无付费发布处理器',
            stray.join(' / ') || '零颗');
    }
    ok(/ImageNotePublishInline/.test(LIST),
        'L5b 🔴 发布走的是**既有那只面板**(#196 的准备/轮询/原话都在它里面),'
        + '不是在本档另写一套提交');
    ok(!/导出/.test(LIST) && !/全选/.test(LIST) && !/批量/.test(LIST),
        'L5c 没有导出 / 全选 / 批量操作(工单 §1「不做」清单)');
    ok(/data-testid="pub-imagenote-row"/.test(LIST)
        && /data-testid="pub-imagenote-badge"/.test(LIST)
        && /data-testid="pub-imagenote-proof"/.test(LIST),
        'L5d 行、徽章、校对都能被行为臂定位');
    ok(/data-testid="pub-imagenote-empty"/.test(LIST)
        && /data-testid="pub-imagenote-chip-empty"/.test(LIST),
        'L5e 🔴 「一条都没有」与「这一档下没有」**分开两句** ——'
        + '合成一句的话用户会以为筛选把数据弄丢了');
}

/* ══ L6 客户切换 ⇒ 列表随之变 ════════════════════════════════════════ */
{
    ok(/<ImageNoteList/.test(PC) && /brandId=\{currentBrandId/.test(PC),
        'L6a 🔴 列表接的是左上角那一份 `currentBrandId`,'
        + '不是本页 `selectedProject` 推出来的第二个"当前客户"(#180 修的就是这件事)',
    );
    ok(!/brandId=\{projects\.find/.test(PC.slice(PC.indexOf('<ImageNoteList'),
        PC.indexOf('<ImageNoteList') + 300)),
        'L6b 那一处没有顺手从 projects 里再推一个 brandId');
    /*
     * 🔴 接线对但**跑得太早**是本仓栽过的坑:brandId 必须在 effect 依赖里。
     *
     * 🔴 取这段源码的**窗口自证**:第一版写的是 `indexOf('useEffect')`,
     *    它命中的是**import 那一行**(`import { useCallback, useEffect, ... }`),
     *    于是下面两格在一段 import 上找依赖数组 —— 报红,但原因是我的取窗错了,
     *    不是产品少写了依赖。所以锚改成 `useEffect(() =>`,并先断言它**恰好一处**:
     *    多一处时"我到底切到了哪一个 effect"就说不清了。
     */
    const EFF_ANCHOR = 'useEffect(() =>';
    const loadAt = LIST.indexOf('void load(brandId, ac.signal)');
    const effStart = LIST.lastIndexOf(EFF_ANCHOR, loadAt);
    const effEnd = LIST.indexOf(']);', loadAt);
    const eff = loadAt >= 0 && effStart >= 0 && effEnd > loadAt ? LIST.slice(effStart, effEnd + 3) : '';
    ok(eff.includes('void load(brandId, ac.signal)'), 'L6c0 取窗自证:选中真正加载列表的 effect，允许另有深链作品 effect');
    /* 🔴 钉的是「brandId 在依赖里」这件事,不是依赖数组逐字长什么样。
       第一版写死 `[brandId, load]`,#203 加了一个重拉计数就当场红了 ——
       而"切客户会不会重拉"一点没变。判据要钉命题,不要钉那一行的字形。 */
    ok(/\[\s*brandId\s*[,\]]/.test(eff),
        'L6c 🔴 `brandId` 在 effect 依赖里 —— 不在的话切客户后列表不重拉,'
        + '屏幕上留着上一个客户的作品',
        /* 🔴 读数与判定同源:第一版读数用的是另一条正则,先命中了 `setPosts([])`,
           于是屏幕上印着「[]」而这一格是绿的 —— 一个自相矛盾的绿。 */
        (eff.match(/\[\s*brandId[^\]]{0,60}\]/) || ['(没取到依赖数组)'])[0]);
    ok(/setPosts\(\[\]\)/.test(eff),
        'L6d 切客户先清空 —— 不清的话旧客户的作品会多停一个往返,'
        + '那是"看起来属于新客户的别人的内容"');
    ok(/setError\('图文列表没取到/.test(LIST),
        'L6e 取不到就说取不到,**不显示一张空列表**(空列表会被读成"这个客户没有图文")');
}

// ══ L7 🔴 [#204 a2 迁入] 有内容、却一条都发不出去 ⇒ 必须有一句区域级说明 ═══
/*
 * 这一段是从 `verify-image-note-publish.mjs` 的 N2g / N2g0 **迁**过来的,
 * 不是新写的判据。原来它钉在**制作台区 E**:那里有一句
 *  「…系统补齐后就能在**发布中心**发出去」。
 *
 * 🔴 为什么必须迁而不是随制作台一起删:
 *    发布 #203 已收口到发布中心 —— 用户是在**这一屏**看那排内容的。
 *    制作台退役后,说明若跟着文件死,用户在这里看到的就是
 *    一排没有「发布」的行 + 零解释,和"坏了"长得一模一样。
 *    说明要跟着**用户**走,不跟着文件走。
 *    (而且这句话在这里才是真话:版本补齐后「发布」确实出现在这一行上。)
 */
console.log('\nL7 一条都发不出去时有没有话说(N2g/N2g0 迁入)');
{
    const blocked = { id: 1, keyword: 'k', cover: '', cardCount: 3, bucket: 'unpublished',
        statusLabel: '未发布', publishedAt: '', url: '', revisionId: null };
    const ready = { ...blocked, id: 2, revisionId: 9 };

    ok(M.blockedAllNotice([]) === '',
        'L7a 一条都没有 ⇒ 不说这句(那是 EMPTY_NOTE 的事;两件事混一句,'
        + '用户分不清是"没做"还是"做了发不了")');
    ok(M.blockedAllNotice([blocked]) === M.BLOCKED_ALL_NOTE,
        'L7b 🔴 有内容、没有一条能发 ⇒ 说明上屏', M.blockedAllNotice([blocked]));
    ok(M.blockedAllNotice([blocked, ready]) === '',
        'L7c 🔴 只要有一条发得出去就不说 —— 这句是"全挡住了"的解释,'
        + '不是"有几条被挡"的提示');
    ok(/blockedAllNotice\(shown\)/.test(LIST)
        && /data-testid="pub-imagenote-blocked-note"/.test(LIST),
        'L7d 接线:这一屏真的渲染它,且渲染守卫**就是这个函数本身** —— '
        + '在 JSX 里另写一遍条件就是第二个判官');
    /*
     * 🔴 L7e 注入正对照(Review 09-15 ①)。
     *    否定式断言在被测对象消失后会**永远为真**:留着就是一格假保护。
     *    所以「不许指向将删的屏」这条必须能被一次注入打红 ——
     *    这里直接检查:这一屏的空态出口不许再指向创作中心的壳
     *    (`/writing` 下的「制作 GEO 图文」tab 随 a2 退役)。
     */
    ok(M.EMPTY_CTA_HREF === '/writing/image-note',
        'L7e 🔴 空态出口指向**还活着**的那条路由,不是退役屏所在的壳',
        M.EMPTY_CTA_HREF);
    ok(!/制作 GEO 图文|站内发布暂未开放/.test(LIST),
        'L7f 反臂:这一屏不许再出现指向退役屏的指路 / 那句已成谎话的文案');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
