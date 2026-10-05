#!/usr/bin/env node
/**
 * 判据 · WO_253(P0)—— ①保存按落库事实说话 ②服务期提示出声 + 出口。
 *
 * 来源:Owner 三图。
 * ① 原缺陷:`await authApi.put(...)` **把响应丢掉**,然后无条件 `toast.success('已保存')`
 *    ⇒ 提交了联系方式而后端没落库时,用户看到的是**绿色的「已保存」**。
 *    🔴 「请求成功」与「字段落库」是两件事 —— 200 只说明这次调用被受理了。
 * ② 原缺陷:服务期信息走 catch ⇒ 链接明明生成了,用户只看到红色报错,
 *    既不知道怎么续费、也拿不到链接。🔴 「有话要说」≠「这次失败了」。
 *
 * 后端契约在 WO_252(C 做中):`persisted[]` / `service_notice{state,paid_at,service_end_date,renew_url}`。
 * 🔴 **本门按契约字段名先行**(Review 2026-09-20 授权),C 的 sha 到了对接;
 *    形状解析各收一处,字段名若有出入只改那一处。
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

const load = async (rel) => {
    const ts = require_('typescript');
    const js = ts.transpileModule(readFileSync(join(ROOT, rel), 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    return import(`data:text/javascript;base64,${Buffer.from(js, 'utf8').toString('base64')}`);
};

let sf; let sn;
try {
    sf = await load('src/lib/saveFeedback.ts');
    sn = await load('src/lib/serviceNotice.ts');
} catch (err) { cannotRun('被测模块加载不了', err); }

const { saveFeedback } = sf;
const { parseServiceNotice, noticeView } = sn;
ok(typeof saveFeedback === 'function' && typeof parseServiceNotice === 'function' && typeof noticeView === 'function',
    'V0 分母自证:三个函数都真的加载进来了',
    `${typeof saveFeedback} / ${typeof parseServiceNotice} / ${typeof noticeView}`);

console.log('\n=== WO_253 · 保存反馈 + 服务期提示 ===\n');

/* ── 一、保存按落库事实说话 ──────────────────────────────────────────── */
const PAYLOAD = { contact_phone: '13800000000', contact_wechat: 'qa_wx', name: 'QA 客户' };

const miss = saveFeedback(PAYLOAD, { persisted: ['name'] });
ok(miss.kind === 'missing',
    'S1 🔴 提交了联系字段而 `persisted` 里没有 ⇒ **红**,不许绿 —— '
    + '这正是 Owner 那张图:后端没落库,屏幕上是绿色的「已保存」',
    `${miss.kind} · ${miss.message}`);
ok(miss.missing.join(',') === 'contact_phone,contact_wechat' && /电话/.test(miss.message) && /微信/.test(miss.message),
    'S1b **点名**说没保存哪几个 —— 只说"保存失败"会让人以为整单都没存',
    miss.message);

const good = saveFeedback(PAYLOAD, { persisted: ['contact_phone', 'contact_wechat', 'name'] });
ok(good.kind === 'ok' && good.shouldReload === true,
    'S2 都落库了 ⇒ 绿,**并重新拉详情回显落库值**(不拿本地表单值假装)',
    `${good.kind} · reload=${good.shouldReload}`);

/*
 * 🔴 三态,与 WO_251 §4 同一课:`persisted` **键不存在** = 这个后端还不报告明细
 *    (WO_252 未上线)⇒ 保持原行为(绿)。压成一种的话,本笔一上线
 *    **每一次保存都会变成红字**,而后端其实好好的。
 */
const unreported = saveFeedback(PAYLOAD, { ok: true });
ok(unreported.kind === 'unreported',
    'S3 🔴 `persisted` **键不存在**(后端还没上线这一段)⇒ 保持原行为,不红',
    unreported.kind);
const emptyArr = saveFeedback(PAYLOAD, { persisted: [] });
ok(emptyArr.kind === 'missing',
    'S3b 🔴 `persisted: []` **键在、但是空** ⇒ 后端明说"一个都没落" ⇒ **红** —— '
    + 'S3 与这一格只差「键在不在」,两格必须同时在;'
    + '用 `response.persisted || []` 判会把两者压成一种,而**最该红的那一种反而永远绿**',
    emptyArr.kind);

const noContact = saveFeedback({ name: 'QA 客户' }, { persisted: ['name'] });
ok(noContact.kind === 'ok',
    'S4 没提交联系字段 ⇒ 不因为 `persisted` 里没有它们而红(空值不算"提交过")',
    noContact.kind);
const blankContact = saveFeedback({ contact_phone: '   ' }, { persisted: [] });
ok(blankContact.kind === 'ok',
    'S4b 只填了空白的联系字段 ⇒ 同样不算提交过 —— 与后端 `NULLIF(TRIM(...))` 同口径',
    blankContact.kind);

/* ── 二、服务期提示:出声,但不猜 ─────────────────────────────────────── */
const expired = noticeView(parseServiceNotice({
    state: 'expired', service_end_date: '2026-08-01', renew_url: 'https://qa.invalid/renew',
}));
ok(expired?.tone === 'warning' && /2026-08-01/.test(expired.text) && expired.renewUrl === 'https://qa.invalid/renew',
    'N1 🔴 `expired` ⇒ 黄提示 + **带到期日** + 续费出口 —— '
    + '不带日期时客户无法判断是昨天还是半年前',
    JSON.stringify(expired));
ok(noticeView(parseServiceNotice({ state: 'never_paid' }))?.tone === 'info',
    'N2 `never_paid` ⇒ 提示「尚未付款,链接已生成」',
    JSON.stringify(noticeView(parseServiceNotice({ state: 'never_paid' }))));
ok(noticeView(parseServiceNotice({ state: 'active' })) === null,
    'N3 `active` ⇒ **不提示**', String(noticeView(parseServiceNotice({ state: 'active' }))));
for (const [why, raw] of [['未知 state', { state: 'weird' }], ['不是对象', 'expired'], ['缺省', undefined]]) {
    ok(parseServiceNotice(raw) === null,
        `N4[${why}] 认不出来一律不提示 —— **绝不猜一个状态**,猜错会对客户说一句不成立的服务期结论`,
        String(parseServiceNotice(raw)));
}
const noDate = noticeView(parseServiceNotice({ state: 'expired' }));
ok(noDate?.tone === 'warning' && !/undefined|null/.test(noDate.text),
    'N5 `expired` 但没给日期 ⇒ 照样提示,但**不编一个日期**(也不把 undefined 显示出来)',
    noDate?.text);

/* ── 三、接线(源码)──────────────────────────────────────────────── */
const page = readFileSync(join(ROOT, 'src/pages/Brand/BrandDetailPage.tsx'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');

/*
 * 🔴 锚必须**钉住"丢掉响应"这件事**,不是钉那串字符:
 *    `const putRes = await authApi.put(endpoint, payload);` 里**也含**那串字符,
 *    第一版没锚行首 ⇒ 改对了却判红。⇒ 只认「整行以 await 开头」的那种写法。
 * 🔴 顺带:第一版的读数写死成「命中零处」—— 无论真假都那么印。
 *    **标签要打印事实,不要打印意图**;写死的读数在红的时候尤其误导。
 */
const discarded = [...page.matchAll(/^\s*await authApi\.put\(endpoint, payload\);/gm)].length;
ok(discarded === 0,
    'W1 🔴 保存不再**丢掉响应** —— 丢掉响应就只能靠"没抛错"说话',
    `丢弃式写法 ${discarded} 处`);
ok(/const fb = saveFeedback\(/.test(page) && /if \(fb\.kind === 'missing'\) toast\.error\(/.test(page),
    'W2 保存处走 `saveFeedback`,并按它分流红/绿', '命中');
ok(/if \(fb\.shouldReload\) setDetailNonce\(/.test(page),
    'W2b 落库后**触发重拉详情** —— 回显落库值,不拿本地表单值假装', '命中');
ok(/setServiceNotice\(noticeView\(parseServiceNotice\(/.test(page),
    'W3 `handleGenerateLink` **读**服务期信息(不再只在 catch 里出现)', '命中');

/*
 * 🔴 出口与链接**必须解耦**:提示块不许长在 catch 里,也不许跟链接共用一个条件。
 *    原缺陷正是把两者绑在一起 —— 提示走了 catch,链接跟着没了。
 */
const catchBlock = (page.match(/catch \(error: any\) \{[\s\S]{0,400}?\n {4}\} finally/) || [''])[0];
ok(!/service_notice|serviceNotice/.test(catchBlock),
    'W4 🔴 服务期信息**不在 catch 里** —— 「有话要说」≠「这次失败了」;'
    + '塞进 catch 的代价是把出口一起吞掉',
    catchBlock ? 'catch 块里没有它' : '🔴 没定位到 catch 块(这一格作废,需重写锚)');
ok(/data-testid="service-notice"/.test(page) && /data-testid="service-notice-renew"/.test(page),
    'W5 提示与续费出口各有 testid —— 给真实点击测试留钩子;'
    + '钩子丢了,那条测试会报"找不到元素"而不是"没提示",两种读数同形',
    '两个都在');

console.log('');
console.log('🔴 本门**没有**证到的:屏幕上真的显示了那条提示 —— 它证的是口径对、接线在。');
console.log('   真实点击测试由复审排,钩子 service-notice / service-notice-renew 已留。');
console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
