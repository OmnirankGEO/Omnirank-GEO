#!/usr/bin/env node
/**
 * 判据 · #199 「禁猜」存量清理第一批(发布中心媒体表 3 处 + 监测顶栏 2 处)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 判据来源:质量标准第 11 条(`RV_FRONTEND_QUALITY_BAR_2026-09-12.md`)。
 * 正面样板(照它改,别另发明):体检页「高级选项 · 有内容」/ 监测「服务期未设 去设服务期 →」/
 * 发布中心「请先在左侧选择文章」。
 *
 * 🔴 文案类判据最容易写成**存在锁**(「这句话在源码里」)。那种锁挡不住
 *    `{false && ...}`、挡不住"写了但没接到控件上"。所以这里:
 *    · 文案由**纯函数**产出 ⇒ 判据 transpile 后**真调它**拿字符串比;
 *    · 接线与"不可见"这类只有渲染后才成立的,交给浏览器臂
 *      (`test-no-guess-batch1-render.mjs`)。
 */
import { readFileSync, existsSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
let pending = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};
const skip = (label, why) => { console.log(`  ..   未评估 ${label} — ${why}`); pending += 1; };
const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Publishing/mediaTableLabels.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a199-'));
    const f = join(tmp, 'labels.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL N0 🔴 文案模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

const PC = decomment(rd('src/pages/Publishing/PublishCenter.tsx'));
const ROW = decomment(rd('src/components/density/InfoBadgeRow.tsx'));
const HINT = decomment(rd('src/components/pricing/PricingUnavailableHint.tsx'));
const CTX = decomment(rd('src/context/PricingContext.tsx'));
const PARTS = decomment(rd('src/components/workbench/parts.tsx'));
const BRIDGE = decomment(rd('src/components/workbench/DecisionBarBridge.tsx'));

console.log('N #199 禁猜清理第一批');

/* ══ N1 权重列:量纲 + 列头自己的 HelpHint ═══════════════════════════ */
{
    /*
     * 🔴 区间数字**不许猜**。199-d1 的只读取证到了才填;没到就必须是 null。
     *    判据两边都钉 —— 有取证文件时常量 == 文件里的 max;没有取证文件时常量必须 null。
     *    「先填一个看起来合理的数」正是这张卡要清的那种东西。
     */
    /*
     * 🔴 [a2 订正] 取证文件必须在**仓内**。
     *    第一版是 `join(ROOT, '..', '..', <一个 .md 文件名>)` —— 读的是仓**外**的东西。
     *    后果是一填上数字,任何没有那份文件的机器(CI / 别人的 worktree / 干净 clone)
     *    构建立刻红:判据的参照物跑到了代码管不着的地方,它证的就只是
     *    "我这台机器上有什么"。⇒ 证据跟代码一起进仓,路径由常量给,形状固定成 JSON。
     */
    const evidence = join(ROOT, M.WEIGHT_RANGE_EVIDENCE);
    ok(!M.WEIGHT_RANGE_EVIDENCE.includes('..') && M.WEIGHT_RANGE_EVIDENCE.startsWith('src/'),
        'N1a0 🔴 取证文件路径在**仓内**(不许 `../..` 跑到仓外)', M.WEIGHT_RANGE_EVIDENCE);
    if (existsSync(evidence)) {
        let ev = null;
        try { ev = JSON.parse(readFileSync(evidence, 'utf8')); } catch { ev = null; }
        const need = ['max_pc', 'max_m', 'source', 'sha256', 'queried_at'];
        const missingKeys = ev ? need.filter((k) => ev[k] === undefined || ev[k] === null) : need;
        if (!ev || missingKeys.length) {
            /* 🔴 不猜:形状不全就说形状不全,不去正则里捞一个数字当 max。 */
            ok(false, 'N1a 🔴 取证文件在但形状不全 —— 缺 '
                + (missingKeys.join('/') || 'JSON 解析失败'), M.WEIGHT_RANGE_EVIDENCE);
        } else if (!Number.isInteger(ev.max_pc)) {
            ok(false, 'N1a 🔴 取证文件的 max_pc 不是整数 —— 不拿它填常量', String(ev.max_pc));
        } else {
            ok(M.WEIGHT_RANGE_MAX === ev.max_pc,
                'N1a 🔴 区间常量 == 199-d1 取证文件里的 max_pc',
                `常量 ${M.WEIGHT_RANGE_MAX} / 取证 ${ev.max_pc}`);
        }
    } else {
        ok(M.WEIGHT_RANGE_MAX === null,
            'N1a 🔴 取证文件还没到 ⇒ 区间常量必须是 **null**(不许先填一个看着合理的数)',
            `常量 = ${String(M.WEIGHT_RANGE_MAX)} · 等 ${M.WEIGHT_RANGE_EVIDENCE}`);
        /*
         * 🔴 这里**不报"未评估"**,只打印一行说明 —— 理由:
         *    N1a 已经把**当前该成立的事**评估过了(没证据 ⇒ 常量必须是 null)。
         *    真正没到的是**上游输入**(199-d1),不是"这一格没测"。
         *    把它记成未评估会让本闸 rc=3,而本闸在 build 链里 —— `&&` 串起来的链
         *    遇到非 0 就停,后面几十把闸**根本不会跑**。那是拿一个诚实的标记
         *    换掉了别人的覆盖面,代价不对。
         */
        console.log(`       (等 199-d1:${M.WEIGHT_RANGE_EVIDENCE} 到了之后,`
            + 'N1a 自动改成「常量 == 取证文件里的 max」)');
    }
    /* 列头文案:有区间写区间,没有只写名字 —— 两种都不许编 */
    const h = M.weightHeader('pc');
    ok(M.WEIGHT_RANGE_MAX === null ? h === '电脑权重' : h === `电脑权重 · 0–${M.WEIGHT_RANGE_MAX}`,
        'N1b 列头文案随常量走(没区间就只写名字)', h);
    const hint = M.weightHint();
    ok(hint.includes('0 = 渠道方没标') && hint.includes('越高'),
        'N1c 🔴 hint 说清**方向**与 **0 的含义** —— '
        + '改前看「7」的人不知道 0 是"最差"还是"没数据"', hint);
    /* 接线:两列各自挂 hint,不是借收录率那一格 */
    /*
     * 🔴 窗口要**卡到本列结束为止**。第一版用「往后 320 字符里有没有 HelpHint」,
     *    而两列是紧挨着的 —— 删掉电脑权重那一个,窗口照样够到**移动权重**那一个,
     *    于是毒 V2 下去了却不红。窗口太宽 = 锚被隔壁满足(同族:
     *    a-string-anchor-satisfied-by-an-unrelated-line)。
     *    改成:从 testid 取到**本列自己的 `</span>`** 为止。
     */
    const colHasHint = (testid) => {
        const i = PC.indexOf(`data-testid="${testid}"`);
        if (i < 0) return false;
        const end = PC.indexOf('</span>', i);
        if (end < 0) return false;
        return PC.slice(i, end).includes('<HelpHint');
    };
    ok(colHasHint('media-th-pc-weight') && colHasHint('media-th-m-weight'),
        'N1d 🔴 两个权重列头**各自**挂 HelpHint(改前唯一说明藏在收录率那一列)',
        `pc ${colHasHint('media-th-pc-weight')} / m ${colHasHint('media-th-m-weight')}`);
    ok(!/<b>电脑 \/ 移动权重<\/b>/.test(PC),
        'N1e 收录率那条 hint 里关于权重的那一句已删 —— 同一件事两处说,改一处就会打架');
}

/* ══ N2 图例 chip 带可见短标签 ═══════════════════════════════════════ */
{
    ok(/showLabel\?: boolean;/.test(ROW) && /showLabel = false/.test(ROW),
        'N2a `InfoBadgeRow` 有 `showLabel`,且**默认 false**(别处形状不变)');
    ok(/<InfoBadgeRow\s+showLabel/.test(PC),
        'N2b 🔴 媒体表图例**开**了可见短标签 —— 改前四个光秃秃的 chip,'
        + '名字与说明全在 hover 弹层里,而手机上根本没有 hover');
    ok(/showLabel && \(\s*<span[^>]*>\{item\.label\}<\/span>/.test(ROW.replace(/\s+/g, ' '))
        || /showLabel &&/.test(ROW),
        'N2c 标签渲染在 badge 旁(不是又塞进弹层)');
}

/* ══ N3 折叠按钮:列出生效排序 + 非默认筛选 ═══════════════════════════ */
{
    const allDefault = M.collapsedFilterSummary({
        sort: 'geo_authority_score_desc', activeFilters: [], total: 128, totalIsFiltered: true,
    });
    ok(allDefault.includes('排序 GEO 真权威') && allDefault.includes('筛选 不限'),
        'N3a 全默认时:**排序照样列出**(它不会因为"没改过"就不影响结果)', allDefault);
    const twoFilters = M.collapsedFilterSummary({
        sort: 'geo_authority_score_desc',
        activeFilters: [['价格', '0–6500'], ['地区', '广东']],
        total: 12, totalIsFiltered: true,
    });
    ok(twoFilters.includes('筛选 2 项') && twoFilters.includes('价格 0–6500')
        && twoFilters.includes('地区 广东'),
        'N3b 有筛选时逐项列出(只说"2 项"等于还要人去猜是哪两项)', twoFilters);
    const sortOnly = M.collapsedFilterSummary({
        sort: 'price_asc', activeFilters: [], total: 128, totalIsFiltered: true,
    });
    ok(sortOnly.includes('排序 价格从低到高'), 'N3c 只改排序时文案跟着变', sortOnly);
    /*
     * ══ N3d 🔴 [a2] 排序档位**只有一个源** ═══════════════════════════════
     *
     * Review 复跑抓到的接缝:芯片列表写死在 PublishCenter,说法表手写在 mediaTableLabels。
     * 两个源 ⇒ 芯片 9 个值、表里只有 6 个键,其中一个还拼错
     * (表里 `included_rate_desc` / 芯片真值 `inclusion_rate_desc`)。
     * 于是选「收录率↓ / 出稿时间↑ / GEO 引擎数↓ / 默认 ID」这四档,折叠行都写
     * 「排序 默认顺序」—— **具体但错**。而且没有一格会红:兜底句"认不出就说默认顺序"
     * 永远返回一个看着合理的答案,把错配整个盖住了。
     *
     * ⇒ 下面对**列表里的每一个值**逐个断言,并把分母打印出来 ——
     *    只测一个值的话,今天这个错配照样溜过去。
     */
    const SORTS = M.MEDIA_SORT_OPTIONS;
    const sortCount = Array.isArray(SORTS) ? SORTS.length : 0;
    ok(sortCount >= 9,
        `N3d0 正样本臂:排序档位表解析出 ${sortCount} 档`
        + ' —— 解析不出来的话,下面逐档的断言不携带信息');
    const fellBack = (SORTS || []).filter((o) => M.sortLabel(o.value) === M.SORT_FALLBACK_LABEL);
    ok(fellBack.length === 0,
        `N3d 🔴 列表里 ${sortCount} 档**每一档**都有自己的说法`,
        `落到兜底句的:${fellBack.map((o) => o.value).join(',') || '无'}`);
    /* 反臂:列表**外**的值才许走兜底 —— 否则"每档都有说法"可以靠把兜底句改成别的字混过去 */
    ok(M.sortLabel('zzq_不存在的排序值') === M.SORT_FALLBACK_LABEL,
        'N3d2 反臂:列表外的值**才**走兜底句(只有它许走)');
    ok(!M.collapsedFilterSummary({ sort: 'some_new_sort', activeFilters: [], total: 1, totalIsFiltered: true })
        .includes('some_new_sort'),
        'N3d3 🔴 认不出的排序值**不上屏裸串**');
    ok(!M.SORT_FALLBACK_LABEL.includes('默认'),
        'N3d4 🔴 兜底句**不声称任何顺序** —— 「默认顺序」是一句关于结果排列方式的断言,'
        + '拿它兜一个认不出的值就是拿断言替不知道', M.SORT_FALLBACK_LABEL);
    /* 页面必须**从常量渲染**芯片,且不许再留一份手写列表 */
    ok(PC.includes('MEDIA_SORT_OPTIONS.map('),
        'N3d5 🔴 排序芯片从 `MEDIA_SORT_OPTIONS` 渲染(不是页面里再写死一份)');
    ok(!PC.includes("label: '🔥 GEO 真权威↓'") && !PC.includes("value: 'inclusion_rate_desc'"),
        'N3d6 🔴 页面里那份手写芯片列表**已经没有了**(留着就还是两个源)');
    /* 🔴 缺省值必须在列表里 —— 否则一进页面折叠行就写「未识别的排序」 */
    const dflt = PC.includes("useState('geo_authority_score_desc')") ? 'geo_authority_score_desc' : '';
    ok(!!dflt && (SORTS || []).some((o) => o.value === dflt),
        'N3d7 🔴 页面缺省排序值在列表里(不在的话,一进页面折叠行就写「未识别的排序」)', dflt);
    /* 🔴 「N 家」是不是筛选后的数 —— 工单要求自核并打印 */
    const filtered = M.collapsedFilterSummary({ sort: 'price_asc', activeFilters: [], total: 12, totalIsFiltered: true });
    const whole = M.collapsedFilterSummary({ sort: 'price_asc', activeFilters: [], total: 12, totalIsFiltered: false });
    ok(filtered.includes('符合 12 家') && whole.includes('全量 12 家'),
        'N3e 两种说法都产得出来(判据能测到"说错"这件事)');
    ok(/totalIsFiltered: true/.test(PC),
        'N3f 🔴 本页用的是「符合 N 家」—— 自核:`loadMhzMedia` 把 search/area/'
        + 'resource_type/news_resource/portal_media/resource_type_name/points_min/points_max/'
        + 'geo_platform/special_industry **全部**放进了请求,`total` 取自同一个响应 ⇒ '
        + '它是**筛选后**的数;改前那句「全量 N 家」是错的说法');
    ok(!/高级搜索 · 全量/.test(PC), 'N3g 改前那句「高级搜索 · 全量 N 家」已不在');
}

/* ══ N4 价目读不到:原因可见 + 出口可点 + 退避窗说清 ═══════════════════ */
{
    /* 结构锁:全树不许再出现那四个字(分母 = 整个 src) */
    const hits = [];
    const walk = (dir) => {
        for (const name of require_('node:fs').readdirSync(dir)) {
            const p = join(dir, name);
            const st = require_('node:fs').statSync(p);
            if (st.isDirectory()) walk(p);
            else if (/\.(ts|tsx)$/.test(name)) {
                const t = readFileSync(p, 'utf8');
                /* 组件自己的注释里会引用这四个字(说明改前是什么样),那不算 */
                if (decomment(t).includes('价目不可用')) hits.push(p.slice(ROOT.length + 1));
            }
        }
    };
    walk(join(ROOT, 'src'));
    ok(hits.length === 0,
        'N4a 🔴 全树不再有「价目不可用」(分母 = 整个 src 的 .ts/.tsx)',
        hits.length ? hits.join(' / ') : '0 处');
    ok(/PRICING_UNAVAILABLE_LABEL/.test(decomment(rd('src/pages/Monitoring/index.tsx')))
        && /PRICING_UNAVAILABLE_LABEL/.test(decomment(rd('src/pages/Brand/BrandDetailPage.tsx'))),
        'N4b 三处按钮文案都走**同一个常量**(一处谓词一处实现)');
    const mon = decomment(rd('src/pages/Monitoring/index.tsx'));
    const brand = decomment(rd('src/pages/Brand/BrandDetailPage.tsx'));
    ok((mon.match(/<PricingUnavailableHint/g) || []).length === 1
        && (brand.match(/<PricingUnavailableHint/g) || []).length === 2,
        'N4c 三处都挂了提示组件(监测 1 处 + 品牌详情 2 处)',
        `${(mon.match(/<PricingUnavailableHint/g) || []).length} + ${(brand.match(/<PricingUnavailableHint/g) || []).length}`);
    ok(/retryNotBefore: number;/.test(CTX) && /retryNotBefore,/.test(CTX),
        'N4d `PricingContext` 暴露了 `retryNotBefore`');
    ok(/setRetryNotBefore/.test(CTX),
        'N4e 🔴 退避窗用 **state** 暴露(只写 ref 的话界面不会更新,'
        + '窗过了按钮还是灰的 —— 又一处"看起来能点其实不能")');
    ok(/秒后可重试/.test(HINT) && /disabled=\{waiting\}/.test(HINT),
        'N4f 🔴 退避窗内出口**禁用并说清还要等多久** —— '
        + '给一颗此刻什么都不做的「重试」,比不给更糟');
    ok(/data-testid="pricing-unavailable-reason"/.test(HINT),
        'N4g 原因画在按钮**外面**(title 里的原因手机上看不到)');
}

/* ══ N5 资料徽章:说清是什么分 + 缺项 + 出口 ══════════════════════════ */
{
    ok(/showLabel && <span className="mr-1 font-sans font-medium opacity-80">资料<\/span>/
        .test(PARTS.replace(/\s+/g, ' ')) || /资料<\/span>/.test(PARTS),
        'N5a 徽章带「资料」二字(说清这是什么分)');
    ok(/missing=\{snapshot\.completenessDetail\?\.missing\}/.test(BRIDGE)
        && /showLabel/.test(BRIDGE) && /brandId=\{snapshot\.id\}/.test(BRIDGE),
        'N5b 🔴 `DecisionBarBridge` 把 showLabel / missing / brandId **都传了** —— '
        + '改前两个都没传,徽章上只有「85 /100」');
    ok(/data-testid="completeness-missing"/.test(PARTS)
        && /data-testid="completeness-goto"/.test(PARTS),
        'N5c 点开能看缺哪几项,且有「去补资料」出口');
    /*
     * 🔴 [a2 订正] N5c 只查了 DOM 里**有没有** —— 而"有"不等于"点得到"。
     *    这枚徽章主挂点 `DecisionBarBridge` 那张 Card 带 `overflow-hidden`,
     *    绝对定位的弹层**不撑高父容器** ⇒ 在父容器下边界被裁,
     *    而「去补资料 →」在最下面,**必被切掉**:DOM 里有、判据绿、用户点不到。
     *    ⇒ 结构上钉 Portal;能不能点由行为臂 R5 用 elementFromPoint 量。
     */
    ok(PARTS.includes('createPortal(') && PARTS.includes('document.body,'),
        'N5c2 🔴 弹层 **Portal 到 body** —— 脱出 overflow-hidden 的父容器');
    ok(BRIDGE.includes('overflow-hidden'),
        'N5c3 配对臂:主挂点那张 Card 确实带 `overflow-hidden`'
        + '(它不带,N5c2 就只是好习惯而不是必需)');
    ok(!PARTS.includes('absolute left-0 top-full'),
        'N5c4 反臂:改前那个 `absolute left-0 top-full` 写法已经没有了');
    ok(/\/my-clients\/\$\{brandId\}/.test(PARTS),
        'N5d 出口指向 `/my-clients/:id`(真实存在的路由)');
    ok(/const canExpand = !!\(missing && missing\.length > 0 && brandId\)/.test(PARTS),
        'N5e 🔴 没有缺项或没有 brandId 时**不做成可点的** —— '
        + '点开一个空清单,就是一颗点了没反应的东西');
    /*
     * 🔴 199-c1 **不触发**:缺项不是新字段。
     *    `ClientWorkbenchSnapshot.completenessDetail` 就是 `/api/brands/{id}/completeness`
     *    的回包(`BrandCompleteness.missing: string[]`),和阻断 Dialog 用的是同一个来源。
     *    改前只是**没传给徽章**。判据钉住这件事,免得下一个人又去开后端卡。
     */
    const api = decomment(rd('src/services/m3/api.ts'));
    ok(/missing: string\[\];/.test(api),
        'N5f 🔴 缺项来自既有契约 `BrandCompleteness.missing` ⇒ **199-c1 不需要触发**');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
if (pending > 0) {
    console.log(`未评估 ${pending} 项(见上面 .. 行)—— **不是通过**,退出码 3`);
    process.exit(3);
}
console.log('全部通过');
process.exit(0);
