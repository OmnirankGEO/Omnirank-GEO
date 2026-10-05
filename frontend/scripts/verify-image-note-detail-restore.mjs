#!/usr/bin/env node
/**
 * 判据 · #203 接回 08-02 的图文三栏详情页 + 发布收口(结构臂)。
 *
 * 两态退出码:**0 全过 / 1 有失败**。
 * 🔴 本闸进 build 链,而链是 `&&` 串的 —— 三态里的 rc=3 会让后面几十把闸根本不跑。
 *    真的没到的上游一律打印说明后返回 0(#199 a2 已经栽过一次)。
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const ts = createRequire(import.meta.url)('typescript');
const flowJs = ts.transpileModule(rd('src/pages/Writing/imageNoteFlow.ts'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
}).outputText;
const Flow = await import(`data:text/javascript;base64,${Buffer.from(flowJs).toString('base64')}`);
const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

function walk(rel, out) {
    for (const name of readdirSync(join(ROOT, rel))) {
        const p = `${rel}/${name}`;
        if (statSync(join(ROOT, p)).isDirectory()) walk(p, out);
        else if (/\.tsx?$/.test(name)) out.push(p);
    }
    return out;
}

const APP = decomment(rd('src/App.tsx'));
const ROUTE = rd('src/pages/Writing/ImageNoteDetailRoute.tsx');
const DETAIL = rd('src/pages/Writing/DouyinPostDetail.tsx');
const LIST = decomment(rd('src/pages/Publishing/ImageNoteList.tsx'));

console.log('D #203 图文详情页接回 + 发布收口');

/* ══ D1 路由挂的是 08-02 那张页,不是 08-17 的壳子 ═══════════════════ */
{
    ok(/path="writing\/image-note\/:postId"[\s\S]{0,160}<ImageNoteDetailRoute\s*\/>/.test(APP),
        'D1 🔴 `/writing/image-note/:postId` 的 element 指向新容器 `ImageNoteDetailRoute`');
    ok(/from '\.\/DouyinPostDetail'/.test(ROUTE) && /<DouyinPostDetail/.test(ROUTE),
        'D1b 容器渲染的是 `DouyinPostDetail`(08-02 那张三栏页)本体');
    /*
     * 🔴 配对臂:光证"指向新容器"不够 —— 新容器里面渲染别的东西也能满足 D1。
     *    这一格证被测对象**确实是那张页**:它读的是真回包字段名。
     *    (08-17 的壳子读的是 `bodyText` / `cards[].image_url`,库里根本没有。)
     */
    ok(/body_text/.test(DETAIL) && /preview_urls/.test(DETAIL) && /oss_keys/.test(DETAIL),
        'D1c 🔴 这张页说的是**后端真回包的方言**(`body_text` / `preview_urls` / `oss_keys`)'
        + ' —— 旧壳子读的 `bodyText` / `cards[].image_url` 库里没有,所以真数据上必然空');
}

/* ══ D3 「去发布投放」落在图文 tab 且三参同带 ════════════════════════ */
{
    const D = decomment(DETAIL);
    /*
     * 🔴 窗口必须**卡到 `goPublish` 函数体内**。
     *    第一版直接在整份源码里找 `brand_id=${post.brand_id}` ——
     *    而本文件 464 行拉风格列表时写的是 `?brand_id=${post.brand_id}`,
     *    同一串被它满足了:毒 P3(把发布链里的 brand_id 删掉)**落地却全绿**。
     *    今天第四次「锚被隔壁满足」。取窗要取到那个谓词自己身上。
     */
    const gpStart = D.indexOf('const goPublish = async () => {');
    const gpEnd = gpStart >= 0 ? D.indexOf('\n    };', gpStart) : -1;
    const GP = gpStart >= 0 && gpEnd > gpStart ? D.slice(gpStart, gpEnd) : '';
    ok(GP.length > 0 && GP.length < 600,
        'D3a0 取窗自证:切出来的是 `goPublish` 函数体本身(切空 / 切太大都说明锚错了)',
        `${GP.length} 字符`);
    const target = new URL(Flow.imageNotePublishHref(71, 23), 'http://localhost');
    ok(target.pathname === '/publish' && target.searchParams.get('media_type') === 'svideo' && target.searchParams.get('content_type') === 'imagenote' && target.searchParams.get('geo_post_id') === '71',
        'D3 图文携当前作品进入中央短视频的图文子入口');
    /*
     * 🔴 [a2 订正] D3b 第一版钉的是「`goPublish` 函数体里**出现过**
     *    `brand_id=${post.brand_id}`」。Review 复跑把 `${b}` 从 navigate 的模板里拿掉、
     *    只留下 `const b = …` 那行死代码 ⇒ **33/33 全绿**。
     *    取窗自证只保证窗切对了;窗内的**死代码**照样满足字面锚。
     *    ⇒ 改成钉**被使用的值**:navigate 的那条模板里必须真的插了 `${b}`,
     *      而 `b` 的定义里必须真的含 `brand_id=`。两段都要,缺一段都能造出死代码。
     */
    ok(/navigate\(imageNotePublishHref\(post.id, post.brand_id\)\)/.test(GP),
        'D3b0 保存后携带权威客户与作品去中央发布');
    ok(!/<ImageNotePublishInline/.test(D), 'D3b 制作页不再展开第二个发布入口');
    ok(target.searchParams.get('brand_id') === '23', 'D3b2 真调导航函数保留权威客户编号');
    ok(/dirtyRef.current && !\(await save\(\)\)/.test(GP), 'D3b3 未保存稿必须先等待保存成功，失败不导航');
    ok(/data-testid="douyin-goto-publish"/.test(D), 'D3c 出口能被行为臂定位');
    /* 反臂:发布中心那边真的认这个参数,否则 D3 只是"我这边写对了" */
    const PC = decomment(rd('src/pages/Publishing/PublishCenter.tsx'));
    ok(/publicationEntry\(searchParams\)/.test(PC) && Flow.publicationEntry(new URLSearchParams('media_type=imagenote')).content === 'imagenote'
        && /imageNoteMode && \(/.test(PC), 'D3d 旧链接解析为图文子入口且中央挂真实列表');
}

/* ══ D8 灰着的出口必须有一句话(禁猜 · 第 11 条第 4 款)════════════ */
{
    const D = decomment(DETAIL);
    /*
     * 🔴 这条是**出截图时自己照出来的**:夹具把 `/status` 回成了错形状,
     *    于是主按钮灰着出现在图里,下面一个字都没有。夹具改对之后按钮亮了,
     *    但"闸没给理由 ⇒ 按钮灰着且沉默"这条路**在产品里真实存在**
     *    (`gateReason` 本来就可以为空)。⇒ 补一句兜底,并在这里钉住。
     */
    ok(/gateReason \|\| '[^']+'/.test(D),
        'D8 🔴 `!canPublish` 且服务端没给理由时,出口下面**仍有一句话** ——'
        + '一颗灰着的主按钮配一片空白,用户知道有事却不知道是什么事');
    ok(/\{jumpBlocked && jumpHint && \(/.test(D),
        'D8b 那句话真的渲染出来(算出来不挂上去等于没有)');
    /* 反臂:别把"没理由"写成编一个原因出来 */
    ok(!/gateReason \|\| '(外部发布通道暂未开放|图文制作即将开放)/.test(D),
        'D8c 🔴 兜底句**不冒充服务端原话** —— 编一个具体但可能错的原因,比含糊更糟');
}

/* ══ D9 出口不落空(#204 a2 按「位置」重判)════════════════════════ */
/*
 * 🔴 原 D9 / D9b 钉的是「返回目标带 `tab=douyin`」与「WritingWorkspace 认这个参数」。
 *    那是一对**位置** —— 图文 tab 本单撤了(Owner「唯一一屏」),两条的指称对象一起没了。
 *    命题没死,而且**变多了一条**:撤一个入口的时候,要同时保证
 *      ① 老链接不落空(它已经发出去过);
 *      ② 新入口一步可达(否则从 /writing 到不了图文,只剩深链);
 *      ③ 详情页那颗按钮有一个不会落空的去处(留着指向死 tab 就是死路)。
 *    ——「撤掉出口就要说」那条老纪律,在这里变成「撤掉入口就要留一条新的」。
 */
{
    const R = decomment(ROUTE);
    const WS = decomment(rd('src/pages/Writing/WritingWorkspace.tsx'));

    ok(/tabFromUrl === 'douyin'/.test(WS) && /navigate\('\/writing\/image-note', \{ replace: true \}\)/.test(WS),
        'D9 🔴 ① 老链 `?tab=douyin` **重定向**到图文那条路由 —— '
        + '不重定向的话它静默落在「写文章」上:页面正常、内容不是他要的,比 404 更难发现');
    ok(/data-testid="writing-goto-image-note"/.test(WS) && /to="\/writing\/image-note"/.test(WS),
        'D9b 🔴 ② 撤了 tab,**入口不能跟着撤**:`/writing` 上仍有一步可达图文的那一处');
    ok(/onBack=\{\(\) => navigate\('\/writing\/image-note'\)\}/.test(R),
        'D9c 🔴 ③ 详情页那颗按钮的去处**还活着**(原来指 `?tab=douyin`,那个 tab 没了)');
    ok(/data-testid="detail-back-writing"/.test(decomment(DETAIL))
        && !/返回列表/.test(decomment(DETAIL)),
        'D9d 按钮说的和去处一致:图文的"列表"就是这一屏的左栏,没有另一层可回,'
        + '所以不再写「返回列表」');
    ok(/geo_image_post/.test(WS) && /image-note\/\$\{pid\}/.test(WS),
        'D9e 🔴 小帮深链里 `geo_image_post` 那一支改成**跳新屏**(能拿到 post_id 就直开那条)'
        + ' —— 原来是 `setTab(...)`,tab 撤了之后那句话会变成一次静默空转:'
        + '不报错、不跳转,用户停在写文章页');
}

/* ══ D4 旧壳子全树无引用(分母 = 整个 src)═════════════════════════ */
{
    const files = walk('src', []);
    const hits = [];
    for (const f of files) {
        /* 🔴 去注释再扫:这一格问的是**代码还引不引用它**,不是"文档里提没提过"。
           不去的话,解释"为什么退役"的那段注释会把本格打红(今天第二次栽这个)。 */
        if (/ImageNoteProof/.test(decomment(rd(f)))) hits.push(f);
    }
    ok(files.length > 500, `D4a 分母自证:扫到 ${files.length} 个 .ts/.tsx(扫空的话下面恒绿)`);
    ok(hits.length === 0, 'D4 🔴 全树已无 `ImageNoteProof` 引用(壳子退役,不留死码)',
        hits.join(' ') || `${files.length} 个文件里 0 处`);
    ok(!existsSync(join(ROOT, 'src/pages/Writing/ImageNoteProof.tsx'))
        && !existsSync(join(ROOT, 'src/pages/Writing/ImageNoteProofRoute.tsx')),
        'D4b 两个文件**真的删了**(只去掉引用、文件还躺着,下一个人会把它当现役)');
}

/* ══ D5 发布收口:面板在发布中心,制作台只给去处 ═══════════════════ */
{
    /*
     * 🔴 [#204 a2] D5 / D5b 原文钉在**制作台**上,那块屏本单退役、文件已删。
     *
     *    D5(「制作台不再挂行内发布面板」)是**乙类**:否定式。
     *      文件一删它永远为真 —— 不会红,也不再挡任何事。
     *      命题没死(同一个付费动作只许一处实现),域升成**全仓**,
     *      由本文件的 Z3 承担:`<ImageNotePublishInline` 全仓恰好一处。
     *    D5b(「制作台给的是去处」)是**位置**:那条去处本身随屏消失,
     *      而"作品能通往发布中心"这件事由 a1 的 D3r2(详情页导航目标真的带上
     *      brand_id 三参)承担。
     *
     *    ⇒ 两条都不留在这里。留一个恒真的绿灯比没有更坏。
     */
    ok(/ImageNotePublishInline/.test(LIST) && /pub-imagenote-publish-toggle/.test(LIST),
        'D5c 🔴 配对臂:面板**搬到了**发布中心图文档 —— 只证"制作台没有",'
        + '把面板整个删掉也能全绿');
    ok(/canPublishRow\(/.test(LIST),
        'D5d 🔴 "这一行能不能发"走同一处谓词,不在 JSX 里另写一遍条件');
    /*
     * 🔴 工单要求:搬家后轮询判据的**读数不许变少**。
     *    这里机械数一遍那把闸的断言条数并打印 —— 不打印的话,
     *    "没变少"就只是一句我自己说的话。
     */
    const POLL = rd('scripts/verify-image-note-prepare-polling.mjs');
    const n = (POLL.match(/\n\s*ok\(/g) || []).length;
    ok(n >= 44, `D5e 🔴 轮询判据条数 ${n} 条(搬家前 44 条,不许变少)`, `${n} 条`);
    ok(!/ImageNoteStudio/.test(POLL),
        'D5f 轮询判据钉的是**面板文件本身**,不是它挂在谁身上 —— 所以搬家不会让它失效');
}

/* ══ D6 上一条/下一条:顺序由容器传,详情页不自己兜底 ═══════════════ */
{
    const D = decomment(DETAIL);
    ok(/onSiblings\?\.\(/.test(D) || /onSiblingsRef\.current\?\.\(/.test(D),
        'D6 容器拿得到顺序(详情页把 `siblings` 回传上去)');
    ok(/siblingIds=\{siblingIds\}/.test(decomment(ROUTE)) && /onSiblings=\{onSiblings\}/.test(decomment(ROUTE)),
        'D6b 🔴 容器**真的把顺序传回去**了 —— 只回传不接线,翻页仍然是死的');
    /*
     * 🔴 详情页**不许**自己兜底成 "siblingIds 为空就用我自己的 siblings"。
     *    兜了的话,"父层忘了接线"这件事永远测不出来 —— 毒 P6(不传 siblingIds)会恒绿。
     */
    ok(/const orderIds = siblingIds\.length \? siblingIds : \[postId\];/.test(D),
        'D6c 🔴 顺序**只认传进来的那一份**(自己兜底会让"父层忘了接线"永远测不出来)');
    ok(/data-testid="post-prev"/.test(D) && /data-testid="post-next"/.test(D),
        'D6d 上一条/下一条能被行为臂定位');
}

/* ══ D7 三栏结构锁 ═════════════════════════════════════════════════ */
{
    const D = decomment(DETAIL);
    for (const [tid, who] of [
        ['detail-col-settings', '左栏 = 设置'],
        ['detail-col-preview', '中栏 = 预览'],
        ['detail-col-content', '右栏 = 重要内容'],
    ]) {
        ok(new RegExp(`data-testid="${tid}"`).test(D), `D7 三栏各有稳定锚:${who}`);
    }
    ok(/data-testid="detail-three-cols"/.test(D), 'D7b 栅格容器自己也有锚(行为臂按它量并排)');
    /* 归属:各栏里装的必须是该装的东西 —— 只有 testid 不证明东西搬对了 */
    const colOf = (tid) => {
        const i = D.indexOf(`data-testid="${tid}"`);
        if (i < 0) return '';
        const rest = D.slice(i);
        const next = ['detail-col-settings', 'detail-col-preview', 'detail-col-content']
            .map((t) => rest.indexOf(`data-testid="${t}"`, 1))
            .filter((x) => x > 0);
        return rest.slice(0, next.length ? Math.min(...next) : rest.length);
    };
    const settings = colOf('detail-col-settings');
    const preview = colOf('detail-col-preview');
    const content = colOf('detail-col-content');
    ok(preview.includes('data-testid="phone-frame"') && preview.includes('thumb-'),
        'D7c 归属:手机预览与缩略条在**中栏**');
    ok(content.includes('data-testid="title-input"') && content.includes('data-testid="body-input"')
        && content.includes('data-testid="douyin-goto-publish"'),
        'D7d 归属:标题 / 正文 / 「去发布投放」在**右栏**');
    ok(settings.includes('city-') && !settings.includes('data-testid="phone-frame"'),
        'D7e 归属:作品列表在**左栏**,且左栏里没有预览');
    ok(!preview.includes('data-testid="douyin-goto-publish"'),
        'D7f 反臂:「去发布投放」已不在中栏(搬走了就不能两处都在)');
    /* 文案只有一份 state —— 预览直接读它 */
    ok((D.match(/const \[bodyText, setBodyText\]/g) || []).length === 1,
        'D7g 🔴 文案仍然**只有一份 state**(重排只是换位置,不是复制一份给预览)');
}

// ══ Z 🔴 [#204 a2] 乙类命题全仓化 ═══════════════════════════════════════
/*
 * 这一段迁自四把闸里的**否定式**断言(D5 / E1c / E1d / G11e / G11e3 / N6b4 / G2b2)。
 *
 * 🔴 它们原来钉的都是「**制作台里**不许有 X」—— 一个**位置**。
 *    制作台本单退役之后,那些断言会**永远为真**:不会红,也不再挡任何事。
 *    一格永远绿的假保护,比没有更坏(绿灯不会引出任何问题)。
 *    命题没退役,退役的是宿主 ⇒ 把域从"那一个文件"升成"**全仓**"。
 *
 * 🔴 顺带证明了这不是多虑:`站内发布暂未开放` 这句话在制作台里早就删干净了
 *    (E1c/G11e 一直绿),而 `ShortVideoPanel.tsx:602` 今天还挂着它,
 *    还指路去「制作 GEO 图文」——本单要删的那块屏。位置绿了,命题一直破着。
 */
console.log('\nZ 乙类命题全仓化(不许再有 / 只许一处)');
{
    const SRC = [];
    const walk = (d) => {
        for (const e of readdirSync(join(ROOT, d), { withFileTypes: true })) {
            const rel = d + '/' + e.name;
            if (e.isDirectory()) { if (e.name !== 'node_modules') walk(rel); }
            else if (/\.tsx?$/.test(e.name)) SRC.push(rel);
        }
    };
    walk('src');
    /* 🔴 分母自证:扫不到文件 = 扫描器坏了,而坏掉的扫描器"什么都没找到"
       和"确实没有"在报文上同形。少于这个数就报错退出。 */
    ok(SRC.length > 300, `Z0 分母自证:扫到 ${SRC.length} 个 .ts/.tsx`, String(SRC.length));

    /* 🔴 扫 **decomment 之后**的源码:这一段量的是"用户看得见的字",
       而注释里原样引用旧文案(比如解释"这句话为什么改掉")不该把闸打红。
       —— 第一版没 decomment,当场被我自己写的那条解释注释打红。 */
    const hits = (re) => SRC.filter((f) => re.test(decomment(rd(f)))).map((f) => f.replace('src/', ''));

    const lie = hits(/站内发布暂未开放/);
    ok(lie.length === 0,
        'Z1 🔴 **全仓**不许再有「站内发布暂未开放」—— 图文线已恢复,留着就是骗人。'
        + '(原 E1c/G11e 只钉制作台,于是 ShortVideoPanel 里那句活到了今天)',
        lie.join(', ') || '零处');

    /*
     * 🔴 Z2 换轴。第一版钉的是**字符串**「制作 GEO 图文」—— 结果它当场打红了
     *    我自己新加的那条**合法入口**的标签(指向新路由的那个 Link)。
     *    名字不是问题,**指向已退役的那个 tab** 才是问题。
     *    ⇒ 钉的东西改成 `?tab=douyin` 这个**去处**;做重定向的那一处要放行
     *      (它的存在正是为了让老链接不落空)。
     */
    const stale = SRC.filter((f) => {
        const src = decomment(rd(f));
        if (!/tab=douyin/.test(src)) return false;
        /* 重定向那一处:读到老参数 ⇒ 送去新路由。它是解药,不是病。 */
        return !/navigate\('\/writing\/image-note', \{ replace: true \}\)/.test(src);
    }).map((f) => f.replace('src/', ''));
    ok(stale.length === 0,
        'Z2 🔴 **全仓**不许再跳向已退役的 `?tab=douyin`(做重定向的那一处除外)—— '
        + '跳过去就是把人送到一个不存在的 tab,而页面看起来一切正常',
        stale.join(', ') || '零处');

    const inline = hits(/<ImageNotePublishInline/);
    ok(inline.length === 1 && inline.includes('pages/Publishing/ImageNoteList.tsx'),
        'Z3 中央图文列表是唯一发布宿主，不复制实现',
        inline.join(', ') || '零处');
    const duplicateHandlers = ['src/pages/Writing/DouyinPostDetail.tsx', 'src/pages/Publishing/ImageNoteList.tsx']
        .filter(f => /submitPublish\(|image-notes\/publish-batch/.test(decomment(rd(f))));
    ok(duplicateHandlers.length === 0, 'Z3b 两个宿主不另写付费提交处理器');

    const bareSelect = hits(/studio-style-select|<select[^>]*styleKey/);
    ok(bareSelect.length === 0,
        'Z4 🔴 **全仓**不许拿裸 select 选卡面风格(Owner 09-13 点名废弃:'
        + '「画幅是什么形状全靠猜」)—— 有形态的选项必须给得出样图',
        bareSelect.join(', ') || '零处');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过`);
    process.exit(1);
}
console.log('\n全部通过');
process.exit(0);
