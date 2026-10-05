#!/usr/bin/env node
/**
 * 判据 · WO_251 §4 —— 监测客户行显示**品牌现值**,不是下单那一刻的快照。
 *
 * 客户改了名 / 当初分类选错时,报价快照不会跟着变
 * (Owner 截图:同一个客户,报价上是旧名 +「餐饮食品」,品牌现值是酒店)。
 *
 * 契约原件:`db/monitoring_db.py::get_paid_clients` docstring(sha `31f905276`)。
 * 后端已做完「现值 → 快照」回落,都没有时返 `null`;**前端只做显示默认值**。
 *
 * 🔴 本门最要紧的一格是 D3:**「字段缺失」与「字段为 null」不是一回事**。
 *    契约说这两列「对所有消费方一律附带」—— 对真实端点成立,
 *    但演示态 `services/demo_access.py::_demo_clients` **自己拼行、根本没有这两列**。
 *    压成一种处理,演示态会从「显示旧名」变成「显示 品牌 #id」。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成。
 */
import { readFileSync, readdirSync } from 'node:fs';
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

let mod;
try {
    const ts = require_('typescript');
    const src = readFileSync(join(ROOT, 'src/lib/clientDisplay.ts'), 'utf8');
    const js = ts.transpileModule(src, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    mod = await import(`data:text/javascript;base64,${Buffer.from(js, 'utf8').toString('base64')}`);
} catch (err) { cannotRun('被测模块加载不了', err); }

const { clientDisplayName, clientDisplayIndustry } = mod;
ok(typeof clientDisplayName === 'function' && typeof clientDisplayIndustry === 'function',
    'D0 分母自证:两个函数真的加载进来了',
    `${typeof clientDisplayName} / ${typeof clientDisplayIndustry}`);

console.log('\n=== WO_251 §4 · 监测客户行显示现值 ===\n');

/* ── 一、现值优先(本单要治的那一条)──────────────────────────────────── */
const STALE = { brand_id: 19, brand_name: '旧名有限公司', industry: '餐饮食品' };
const ROW_CURRENT = { ...STALE, brand_current_name: 'QA 现名酒店', brand_current_industry: '文旅娱乐' };
ok(clientDisplayName(ROW_CURRENT) === 'QA 现名酒店',
    'D1 🔴 有现值时显示**现值**,不是快照 —— 客户改了名,监测页不该还挂着下单那天的名字',
    clientDisplayName(ROW_CURRENT));
ok(clientDisplayIndustry(ROW_CURRENT) === '文旅娱乐',
    'D1b 🔴 行业同理 —— 当初分类选错(报价写「餐饮食品」)时,监测页要显示纠正后的现值',
    clientDisplayIndustry(ROW_CURRENT));
ok(clientDisplayName(ROW_CURRENT) !== STALE.brand_name
    && clientDisplayIndustry(ROW_CURRENT) !== STALE.industry,
    'D1c 反向对照:快照值**没有**被显示出来 —— 只断言"等于现值"时,'
    + '若两者碰巧相同这一格没有分辨力;夹具里故意让它们不同',
    `快照「${STALE.brand_name}/${STALE.industry}」未出现`);

/* ── 二、两级都空 ⇒ 显示默认值(契约逐字)──────────────────────────────── */
for (const [why, v] of [['null', null], ['空串', ''], ['仅空白', '   ']]) {
    const row = { brand_id: 19, brand_current_name: v, brand_current_industry: v };
    ok(clientDisplayName(row) === '品牌 #19',
        `D2[${why}] 现值为${why} ⇒ 名字显示 \`品牌 #<brand_id>\``, clientDisplayName(row));
    ok(clientDisplayIndustry(row) === '未填写',
        `D2b[${why}] 现值为${why} ⇒ 行业显示 \`未填写\``, clientDisplayIndustry(row));
}
ok(clientDisplayName({ brand_id: null, brand_current_name: null }) === '未命名客户',
    'D2c `brand_id` 也为空 ⇒ `未命名客户`(契约点名的第二层默认值)',
    clientDisplayName({ brand_id: null, brand_current_name: null }));

/* ── 三、缺字段 ≠ null(演示态那条链)────────────────────────────────── */
/*
 * 🔴 契约说两列「对所有消费方一律附带」—— 对真实端点成立,
 *    但 `services/demo_access.py::_demo_clients` 自己拼行,**没有这两列**。
 *    ⇒ 缺失 = 这条链不提供现值 ⇒ **回落快照**(保持原行为);
 *      null = 后端明说两级都没有 ⇒ 用显示默认值。
 *    压成一种,演示态会从「显示旧名」变成「显示 品牌 #19」——
 *    那是把一处没人要求改的行为顺手改掉。
 */
ok(clientDisplayName(STALE) === '旧名有限公司',
    'D3 🔴 **字段缺失**(演示态)⇒ 回落快照,保持原行为 —— '
    + '与「字段为 null」压成一种的话,演示态会从"显示旧名"变成"显示 品牌 #19"',
    clientDisplayName(STALE));
ok(clientDisplayIndustry(STALE) === '餐饮食品',
    'D3b 行业同理:缺字段时回落快照', clientDisplayIndustry(STALE));
ok(clientDisplayName({ brand_id: 19, brand_name: '旧名有限公司', brand_current_name: null }) === '品牌 #19',
    'D3c 🔴 **同一行、同样有快照**,但现值显式为 `null` ⇒ **不回落**,用默认值 —— '
    + 'D3 与这一格只差 `undefined` / `null`,两格必须同时在,否则分不出是哪条规则在起作用',
    clientDisplayName({ brand_id: 19, brand_name: '旧名有限公司', brand_current_name: null }));

/*
 * 🔴 **这一格是「键判」与「值判」的唯一分水岭**,补它的原因值得写下来:
 *    我把实现从 `row.x === undefined` 改成 `hasOwnProperty(row,'x')` 之后,
 *    **20 格判据一格都没红** —— 对 JSON 负载两种写法结果相同,
 *    于是那次改动**没有任何判据看得见**(与 WO_250 的「最长优先」同形)。
 *    差别只出现在**键在、值是 `undefined`** 这一种:
 *      · 值判 ⇒ 当成"这条链不提供" ⇒ 回落快照;
 *      · 键判 ⇒ 键在就是提供了 ⇒ 按"没有值"走显示默认值。
 *    契约(C `9c6878450` 三态)定义的是**键在不在**,所以键判才对。
 *    这种行不是 JSON 能产生的,但**任何一层 `{...row, brand_current_name: x}` 的映射
 *    都能产生**,而那正是最容易被顺手加进来的东西。
 */
{
    const keyPresentUndefined = { brand_id: 19, brand_name: '旧名有限公司', brand_current_name: undefined };
    ok(clientDisplayName(keyPresentUndefined) === '品牌 #19',
        'D3d 🔴 **键在、值是 `undefined`** ⇒ 按「提供了但没有值」走显示默认值,**不回落快照** —— '
        + '这一格是键判与值判的唯一分水岭;改成 `=== undefined` 判的话它会红',
        clientDisplayName(keyPresentUndefined));
    ok(/hasOwnProperty/.test(readFileSync(join(ROOT, 'src/lib/clientDisplay.ts'), 'utf8')),
        'D3e 实现确实按**键**分支(`hasOwnProperty`),不是按值 —— '
        + '只有 D3d 一格时,有人把实现换回值判、而恰好没有那种行经过,仍会全绿',
        '命中');
}

/*
 * 🔴 **两条演示链会把「值为 None 的键」整个丢掉**(C 2026-09-20 订正:
 *    `_demo_clients` 与 `_monitoring_snapshot` 都是
 *    `{k: v for ... if value is not None}`,我原先只点了后者)。
 *    ⇒ 一行里**连快照键也可能整个不在**,这不是假想形状,是那两条链的常态。
 *    行为要对,而且要有格子看着 —— 我先量过行为是对的,但**没有判据覆盖它**,
 *    那就等于没做过。
 */
ok(clientDisplayName({ quote_id: 1, brand_id: 19 }) === '品牌 #19',
    'D8 🔴 现值键与快照键**都不在**(演示链丢 None 键的常态)⇒ 名字落到 `品牌 #<id>`',
    clientDisplayName({ quote_id: 1, brand_id: 19 }));
ok(clientDisplayIndustry({ quote_id: 1, brand_id: 19 }) === '未填写',
    'D8b 同一行的行业落到 `未填写`', clientDisplayIndustry({ quote_id: 1, brand_id: 19 }));
ok(clientDisplayName({}) === '未命名客户',
    'D8c 一个键都没有的行 ⇒ `未命名客户`(不抛错、不显示 undefined)—— '
    + '演示链丢完 None 之后真的可能只剩几个键',
    clientDisplayName({}));

/* ── 四、一处口径(锁)──────────────────────────────────────────────── */
const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (
    e.isDirectory() ? walk(join(dir, e.name))
        : (/\.(ts|tsx)$/.test(e.name) ? [join(dir, e.name)] : [])));
let files = [];
try { files = walk(join(ROOT, 'src')); } catch (err) { cannotRun('扫不了 src/', err); }
const strip = (t) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');

const defFiles = files.filter((f) => /export function clientDisplay(Name|Industry)/.test(strip(readFileSync(f, 'utf8'))));
ok(defFiles.length === 1,
    'D4 🔴 显示口径**只有一处定义** —— 契约逐字:「收在一个共用 helper 里,'
    + '两个消费点不各写一份」;写两处迟早有一处跟不上,而两边各自看都"对"',
    defFiles.map((f) => f.replace(ROOT, '').replace(/\\/g, '/')).join(' | '));

/* 🔴 反面:消费点不许再自己写回落。监测页里不该再出现直接渲染快照字段的写法。 */
const mon = strip(readFileSync(join(ROOT, 'src/pages/Monitoring/index.tsx'), 'utf8'));
const rawUse = [...mon.matchAll(/\{selectedClientInfo\.(brand_name|industry)\}/g)].map((m) => m[0]);
ok(rawUse.length === 0,
    'D5 🔴 监测页不再**直接渲染快照字段** —— 留一处就等于口径又变回两份',
    rawUse.join(' | ') || '零处');
ok(/clientDisplayName\(selectedClientInfo\)/.test(mon) && /clientDisplayIndustry\(selectedClientInfo\)/.test(mon),
    'D5b 分母自证:两个显示点**确实**走了 helper(只证"没有旧写法"不够,'
    + '整块被删掉也满足那一条)',
    '两处都命中');

/* ── 四之二、给真实点击测试留的钩子 ─────────────────────────────────── */
/*
 * 🔴 本门**证不到**「屏幕上显示的是现值」—— 它证的是 helper 对、接线在。
 *    真实点击测试由复审排(Owner 09-18 令)。那条测试要能点名两个元素,
 *    所以这里把 testid 钉住:它们悄悄消失的话,点击测试会变成"找不到元素"
 *    而不是"显示错了",而那两种读数在报告里长得一样。
 */
for (const tid of ['monitor-client-name', 'monitor-client-industry']) {
    ok(mon.includes(`data-testid="${tid}"`),
        `D7[${tid}] 🔴 真实点击测试的钩子还在 —— 钩子丢了,那条测试会报"找不到元素",`
        + '与"显示错了"在报告里同形',
        '命中');
}

/* ── 五、沙盒是第二个后端 ────────────────────────────────────────────── */
const mock = readFileSync(join(ROOT, 'src/sandbox/mockData.ts'), 'utf8');
ok(/brand_current_name:/.test(mock) && /brand_current_industry:/.test(mock),
    'D6 🔴 教程沙盒的 clients 夹具也带这两列 —— 沙盒是第二个后端,'
    + '不补的话教程里永远只执行"字段缺失 ⇒ 回落快照"那一支',
    '两列都在');

console.log('');
console.log('🔴 说清本门**没有**证到的:沙盒夹具里现值与快照**取同值**(教程要前后一致),');
console.log('   所以它证的是那条路**跑到了**,不是它**分得清** —— 分辨力在 D1/D1c,不在沙盒。');
console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
