#!/usr/bin/env node
/**
 * 判据 · WO_250 —— 报价行业自动分类把「酒店」归成「餐饮食品」。
 *
 * 客户反馈:酒店品牌写文章搜竞品,搜出一堆餐饮店/寿司店。
 * 根因:原 `normalizeIndustry` 是「表顺序 × 首个命中的子串」,
 * 而「餐饮食品」含单字 `'酒'` 且排在「文旅娱乐」(`'酒店'`)之前。
 *
 * 🔴 本门的核心不是「新的对不对」,是 **「变了哪些,变的是不是只有我打算变的那些」**。
 *    单字改多字必然**丢掉一些原来能命中的输入** —— 不把差集列出来,
 *    就是拿一个假阳性换一批看不见的假阴性。
 *    ⇒ I7 把新旧差分**冻成声明表**:多一条少一条都红。
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

/* ── 真调被测模块(不是读它的源码文本)────────────────────────────────── */
let mod;
try {
    const ts = require_('typescript');
    const src = readFileSync(join(ROOT, 'src/lib/industries.ts'), 'utf8');
    const js = ts.transpileModule(src, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    mod = await import(`data:text/javascript;base64,${Buffer.from(js, 'utf8').toString('base64')}`);
} catch (err) { cannotRun('被测模块加载不了', err); }

const { INDUSTRY_CATEGORIES, normalizeIndustry, classifyWith } = mod;
ok(typeof normalizeIndustry === 'function' && Array.isArray(INDUSTRY_CATEGORIES),
    'I0 分母自证:被测模块真的加载进来了 —— 加载失败时下面每一格都会"全绿"',
    `${INDUSTRY_CATEGORIES?.length} 类`);

console.log('\n=== WO_250 · 行业自动分类 ===\n');

/* ── 一、客户那一条,以及它的反向对照 ────────────────────────────────── */
const HOTEL = '酒店住宿 / 商务出行 / 中端连锁酒店';
ok(normalizeIndustry(HOTEL) !== '餐饮食品',
    'I1 🔴 客户那一条:「酒店住宿 / 商务出行 / 中端连锁酒店」**不是餐饮食品** —— '
    + '归错会让写文章的竞品检索搜出一堆餐饮店',
    `实得「${normalizeIndustry(HOTEL)}」`);
ok(normalizeIndustry(HOTEL) === '文旅娱乐',
    'I1b 它归到**文旅娱乐** —— WO_251 对表已结案(2026-09-20):后端 `distill_trade` 对 '
    + '`酒店`/`民宿`/`住宿` 原样透传、非空,不另立「住宿酒店」类。这一格钉的是**定论**,不是暂定',
    `实得「${normalizeIndustry(HOTEL)}」`);

for (const [raw, want] of [
    ['第九岭私厨', '餐饮食品'],
    ['滇粤食坊', '餐饮食品'],
    ['XX酒业', '餐饮食品'],
    ['潮汕牛肉火锅', '餐饮食品'],
    ['某某茶饮连锁', '餐饮食品'],
]) {
    ok(normalizeIndustry(raw) === want,
        `I2[${raw}] 反向对照:真餐饮**仍然**是餐饮食品 —— 修「酒店」不许误伤这一类`,
        `实得「${normalizeIndustry(raw)}」`);
}

/* ── 二、类修法:单字与截胡 ──────────────────────────────────────────── */
const singles = INDUSTRY_CATEGORIES
    .flatMap((c) => c.keywords.filter((k) => k.length === 1).map((k) => `${c.label}:${k}`));
ok(singles.length === 0,
    'I3 🔴 表里**没有单字关键词** —— 最长优先只解决关键词之间的碰撞,'
    + '解决不了单字命中**任意自由文本**(`车` 会把「自行车道路施工」判成汽车出行)。'
    + '原表有 6 个:铝/酒/茶/奶/车/鞋',
    singles.join(' ') || '零个');

const hijacks = [];
for (let i = 0; i < INDUSTRY_CATEGORIES.length; i += 1) {
    for (let j = 0; j < INDUSTRY_CATEGORIES.length; j += 1) {
        if (i === j) continue;
        for (const a of INDUSTRY_CATEGORIES[i].keywords) {
            for (const b of INDUSTRY_CATEGORIES[j].keywords) {
                /* 只有**更短**的词才可能截胡更长的;等长看表顺序,不算结构缺陷 */
                if (a.length < b.length && b.includes(a)
                    && normalizeIndustry(b) !== INDUSTRY_CATEGORIES[j].label) {
                    hijacks.push(`'${a}'(${INDUSTRY_CATEGORIES[i].label}) 截 '${b}'(${INDUSTRY_CATEGORIES[j].label})`);
                }
            }
        }
    }
}
ok(hijacks.length === 0,
    'I4 🔴 **没有跨类截胡**:任何关键词 B,喂给分类器都必须落回 B 自己的类 —— '
    + '这是「酒 截 酒店」那个缺陷的**类**,改前实测恰好 1 处',
    hijacks.join(' | ') || '零处');

/*
 * 🔴 **直接验「最长优先」这条规则本身**,而不是验它在当前表上的效果。
 *    注毒实测:把 `.sort()` 撤掉,其余 15 格**一格都不红** —— 因为单字去掉后
 *    现表已无跨类碰撞,表顺序与最长优先结果相同。**这条规则今天是冗余的**,
 *    它防的是将来新增的词。冗余不等于可以不验:没有判据看得见的修法,
 *    下一个人重构时会顺手删掉,而那一刻所有格子仍然全绿。
 *    ⇒ 喂一张**故意造出碰撞**的合成表:短词排在前面,长词排在后面。
 */
const COLLIDE_TABLE = [
    { label: 'A类', keywords: ['甲'] },          // 短词、排前面
    { label: 'B类', keywords: ['甲乙丙'] },      // 长词、排后面,且包含短词
];
ok(classifyWith(COLLIDE_TABLE, '某某甲乙丙公司') === 'B类',
    'I8 🔴 **最长关键词优先**:短词排在前面也不许截胡更长的词 —— '
    + "这就是「'酒' 截 '酒店'」的那条规则;撤掉排序这一格必红",
    `实得「${classifyWith(COLLIDE_TABLE, '某某甲乙丙公司')}」(表顺序优先会得「A类」)`);
ok(classifyWith(COLLIDE_TABLE, '某某甲公司') === 'A类',
    'I8b 反向对照:只含短词时**仍然**命中短词那一类 —— '
    + '否则"最长优先"可能是"长词独吞",那是另一个错',
    `实得「${classifyWith(COLLIDE_TABLE, '某某甲公司')}」`);
ok(classifyWith([], 'xx', '其他') === '其他' && classifyWith([], 'xx') === 'xx',
    'I8c 兜底两种取值各配一格 —— 只喂一个值的话,把兜底写反也照样绿',
    '两种都对');

/* ── 三、一处定义(锁)────────────────────────────────────────────────── */
const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (
    e.isDirectory() ? walk(join(dir, e.name))
        : (/\.(ts|tsx)$/.test(e.name) ? [join(dir, e.name)] : [])));
let defs = [];
try {
    defs = walk(join(ROOT, 'src')).filter((f) => {
        const t = readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');
        return /(const|let|var)\s+INDUSTRY_CATEGORIES\s*[:=]/.test(t);
    });
} catch (err) { cannotRun('扫不了 src/', err); }
ok(defs.length === 1,
    'I5 🔴 全仓 `INDUSTRY_CATEGORIES` **定义恰好 1 处** —— 原来有两处,而且两份**并不相同**'
    + '(OQF 多一个「珠宝饰品」、金融服务写 `复核` 而非 `审计`、服装纺织三词不同、兜底也不同):'
    + '「重复的同一张表」本身就是未验断言',
    defs.map((f) => f.replace(ROOT, '').replace(/\\/g, '/')).join(' | '));

/* ── 四、新旧差分:改了哪些,必须逐条声明 ─────────────────────────────── */
/*
 * 🔴 下面这份是**改动前那两张表的冻结快照**,只给本门做差分用。
 *    它**不是第二个 SSOT**:产品代码不 import 它,I5 那一格盯着"定义只有一处"。
 *    冻结的东西不会跟着产品演化 —— 那正是它当基线的价值。
 */
const OLD_LIB = [
    ['制造业', ['制造', '生产', '加工', '工厂', '设备', '机械', '机器', '五金', '钢材', '钢铁', '铝', '模具', '焊接', '仪器', '仪表', '电子元器件', '自动化', '显示', '工业']],
    ['建筑建材', ['建筑', '建材', '装修', '装饰', '幕墙', '门窗', '地板', '瓷砖', '水泥', '混凝土', '防水', '保温', '家居建材', '全屋定制', '家居']],
    ['信息技术', ['软件', '信息技术', '互联网', 'it', '科技', '数字化', '系统集成', '云计算', '大数据', 'ai', '人工智能', 'saas', 'geo', 'seo', '网络']],
    ['教育培训', ['教育', '培训', '学校', '课程', '职业教育', '考试', '学历', '早教', '幼儿']],
    ['医疗健康', ['医疗', '医药', '健康', '医院', '诊所', '药品', '保健', '康复', '养老', '护理', '口腔', '牙科', '美容', '整形']],
    ['金融服务', ['金融', '银行', '保险', '证券', '基金', '投资', '理财', '贷款', '信用', '支付', '财税', '会计', '审计']],
    ['电商零售', ['电商', '零售', '批发', '经销', '贸易', '商贸', '日用品', '百货', '超市', '跨境']],
    ['餐饮食品', ['餐饮', '食品', '饮料', '酒', '茶', '烘焙', '奶', '农产品', '生鲜', '外卖']],
    ['物流运输', ['物流', '运输', '快递', '货运', '仓储', '供应链', '配送', '搬家', '航运']],
    ['文旅娱乐', ['旅游', '酒店', '民宿', '景区', '文化', '传媒', '影视', '游戏', '娱乐', '演出']],
    ['农林牧渔', ['农业', '农林', '畜牧', '渔业', '种植', '园林', '绿化', '花卉', '苗木', '修枝', '绑枝']],
    ['房产物业', ['房产', '房地产', '物业', '中介', '租赁', '公寓', '写字楼', '园区']],
    ['法律咨询', ['法律', '律师', '知识产权', '专利', '商标', '咨询', '管理咨询', '企业服务', '代理']],
    ['汽车出行', ['汽车', '车', '出行', '租车', '驾校', '维修', '保养', '充电', '新能源']],
    ['能源环保', ['能源', '电力', '光伏', '太阳能', '环保', '节能', '水处理', '垃圾', '回收']],
    ['服装纺织', ['服装', '纺织', '面料', '鞋', '箱包', '饰品', '珠宝', '定制']],
    ['广告营销', ['广告', '营销', '品牌', '策划', '设计', '公关', '推广', '新媒体', '直播', '社媒']],
];
const oldNormalize = (raw) => {
    if (!raw) return '其他';
    const t = String(raw).toLowerCase();
    for (const [label, kws] of OLD_LIB) for (const k of kws) if (t.includes(k)) return label;
    return raw;
};

/* 语料:两张表的全部关键词 + 现实组合 + 工单点名的那几条 */
const CORPUS = [...new Set([
    ...OLD_LIB.flatMap(([, ks]) => ks),
    ...INDUSTRY_CATEGORIES.flatMap((c) => c.keywords),
    HOTEL, '第九岭私厨', '滇粤食坊', 'XX酒业', '潮汕牛肉火锅', '某某茶饮连锁',
    '酒店住宿', '连锁酒店', '精品民宿', '自行车道路施工', '奶瓶制造厂',
    '珠宝定制', '女装定制', '铝合金门窗', '茶叶批发', '婴幼儿奶粉',
    '二手车交易', '汽车维修保养', '鞋业制造', '白酒经销',
])];
ok(CORPUS.length >= 200,
    'I6 分母自证:语料够大(两表全部关键词 + 现实组合)—— 语料缩了,差分就看不见东西',
    `${CORPUS.length} 条`);

const changed = CORPUS
    .map((x) => [x, oldNormalize(x), normalizeIndustry(x)])
    .filter(([, a, b]) => a !== b)
    .map(([x, a, b]) => `${x}: ${a} → ${b}`);

/*
 * 🔴 **冻结表**:新旧分类发生变化的输入,逐条在这里声明。
 *    写成 `===` 而不是 `<=`:多一条(我改坏了别的)红,少一条(我的修法失效了)也红。
 *    每一条后面的理由不是装饰 —— 没有理由的变化就是没人看过的变化。
 */
const DECLARED_CHANGES = new Set([
    /* ① 本单要治的那一条:酒店不再是餐饮 */
    '酒店: 餐饮食品 → 文旅娱乐',
    '酒店住宿 / 商务出行 / 中端连锁酒店: 餐饮食品 → 文旅娱乐',
    '酒店住宿: 餐饮食品 → 文旅娱乐',
    '连锁酒店: 餐饮食品 → 文旅娱乐',

    /* ② 单字展开的**代价**:这六个词单独出现时不再命中,落回原文。
          这是刻意的 —— 单字会命中任意自由文本(见 ⑤ 的「自行车道路施工」)。 */
    '铝: 制造业 → 铝',
    '酒: 餐饮食品 → 酒',
    '茶: 餐饮食品 → 茶',
    '奶: 餐饮食品 → 奶',
    '车: 汽车出行 → 车',
    '鞋: 服装纺织 → 鞋',
    '定制: 服装纺织 → 定制',

    /* ③ 合表:`珠宝饰品` 原来只在 OnlineQuoteFlow 那份表里 ——
          合进公共表后,lib 的调用点(MyClientsPage 等)第一次能认出这些词。
          🔴 这是**行为变化不是纯搬运**:珠宝/饰品 从服装纺织改判到更细的珠宝饰品。 */
    '珠宝: 服装纺织 → 珠宝饰品',
    '饰品: 服装纺织 → 珠宝饰品',
    '珠宝定制: 服装纺织 → 珠宝饰品',
    '首饰: 首饰 → 珠宝饰品',
    '翡翠: 翡翠 → 珠宝饰品',
    '玉石: 玉石 → 珠宝饰品',
    '玉器: 玉器 → 珠宝饰品',
    '宝石: 宝石 → 珠宝饰品',
    '钻石: 钻石 → 珠宝饰品',
    '黄金: 黄金 → 珠宝饰品',
    '珍珠: 珍珠 → 珠宝饰品',
    '银饰: 银饰 → 珠宝饰品',
    '水贝: 水贝 → 珠宝饰品',
    '服饰: 服饰 → 服装纺织',

    /* ④ 为补回单字丢掉的匹配而新增的词 —— 它们原来**一个都不命中**。
          🔴 其中「私厨 / 食坊」值得单说:工单把它们写成「**仍**是餐饮食品」的反向对照,
             而实测**改之前它们根本不是** —— 老表里没有这两个词,落回原文。
             照工单字面写判据的话,会写出一个"验证了从未成立的事"的格子。 */
    '私厨: 私厨 → 餐饮食品',
    '食坊: 食坊 → 餐饮食品',
    '第九岭私厨: 第九岭私厨 → 餐饮食品',
    '滇粤食坊: 滇粤食坊 → 餐饮食品',
    '小吃: 小吃 → 餐饮食品',
    '火锅: 火锅 → 餐饮食品',
    '料理: 料理 → 餐饮食品',
    '寿司: 寿司 → 餐饮食品',
    '潮汕牛肉火锅: 潮汕牛肉火锅 → 餐饮食品',
    '乳制品: 乳制品 → 餐饮食品',
    '住宿: 住宿 → 文旅娱乐',
    '客栈: 客栈 → 文旅娱乐',
    '女装: 女装 → 服装纺织',
    '男装: 男装 → 服装纺织',
    '童装: 童装 → 服装纺织',
    '内衣: 内衣 → 服装纺织',

    /* ⑤ 单字治理**该有的**后果:这一条正是「为什么不能留单字」的活证据。 */
    '自行车道路施工: 汽车出行 → 自行车道路施工',
]);

const unexpected = changed.filter((c) => !DECLARED_CHANGES.has(c));
const missing = [...DECLARED_CHANGES].filter((c) => !changed.includes(c));
ok(unexpected.length === 0,
    'I7 🔴 新旧差分里**没有未声明的变化** —— 单字改多字必然丢掉一些原来能命中的输入,'
    + '不列出来就是拿一个假阳性换一批看不见的假阴性',
    unexpected.join(' | ') || `${changed.length} 条变化全部已声明`);
ok(missing.length === 0,
    'I7b 🔴 声明表里**没有已经不成立的条目** —— 声明了却没发生,说明我的修法没落到实处'
    + '(或者表与现实脱钩了,那是本仓反复栽的形状)',
    missing.join(' | ') || '零条陈旧');

/* 差分机制自证:拿一个**已知会变**的输入证明这套差分看得见变化 */
ok(changed.includes('酒店: 餐饮食品 → 文旅娱乐'),
    'I7c 差分机制自证:那条**已知会变**的输入确实出现在变化集里 —— '
    + '变化集恒空的话 I7 会永远绿',
    '命中');

console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
