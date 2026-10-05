#!/usr/bin/env node
/**
 * 判据 · WO_256 —— 真实客户数据不许进公开前端包。
 *
 * ## 这一单要治的事
 *
 * `nginx.conf:82-84` 把 `/assets/*` 当公开静态资源,`ProtectedRoute` 只挡**路由渲染**、
 * 不挡 **chunk 下载** ⇒ 匿名 `curl` 就能拿到 `dist/assets/*.js`。
 * 而沙盒演示数据里曾经原样落着一份**真实生产诊断报告**(87KB),
 * 经 `mockData.ts` 的 `?raw` 编进公开 bundle。
 *
 * ## 三格,前两格是源头,第三格是终点
 *
 * · G1 源文件:合成标记在 + 工商主体形状命中 ⊆ 白名单
 * · G2 `EmployeeHall.tsx` 的 placeholder 不含工商主体形状
 * · G3 **构建产物** `dist/assets/*.js` 形状命中 ⊆ 白名单 —— **这一格才是终点判据**
 *   (源文件干净不等于产物干净:任何一处新的 `?raw`、任何一份新夹具都能绕过前两格)
 *
 * ## 🔴 自陈盲区(先说,免得「绿」被读成「没有 PII」)
 *
 * 本门认的是**工商主体形状** `[一-龥]{2,12}(有限公司|股份有限公司|集团)`。
 * 它**看不见**:
 *   · 自然人姓名(那份真实报告里有法定代表人姓名)
 *   · 注册地址、注册资本、成立日期这类登记信息
 *   · **不带**「有限公司」的公司**简称** —— WO_256 事实④ 正是栽在这里:
 *     `EmployeeHall.tsx` 三处 placeholder 用的是短名,形状锁天生看不见,
 *     所以 G2 钉的是「那三处 placeholder 的字面内容」,不是形状。
 * ⇒ **本门全绿只代表「这一类形状没漏」**,不代表「没有真实客户数据」。
 *   新增演示数据时,责任在「只放合成内容」,不在「让这道门绿」。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成。
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

let failures = 0;
let ran = 0;
const ok = (cond, name, detail) => {
    ran += 1;
    if (!cond) failures += 1;
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${detail !== undefined ? ` — ${detail}` : ''}`);
};
function cannotRun(what) {
    console.log(`\n3 门没跑成:${what}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

/**
 * 合成工商主体白名单。
 *
 * 🔴 **只含合成名。真实名字不许写进来** —— 这份清单会进开源仓,
 *    把真名写进锁等于换个地方泄露一次。
 *
 * 🔴 [WO_264 · 2026-09-22] 名单**从文件读**,不再内联。
 *    WO_264 的产物扫描门跑在**生产机**上(bash + grep,那台没有 node),
 *    要用同一份白名单。抄一份过去就是两份各自演化 —— 本周刚踩过同形的:
 *    交付单正文订正了、生成的对表没跟着改,差点让人按旧表退役一把好门。
 *    ⇒ 一份清单,两个消费方都读它;配套三格锁见下方 W264 段。
 */
const ALLOW_FILE = 'scripts/synthetic-allow.txt';
const SYNTHETIC_ALLOW = new Set(
    (() => {
        const p = join(ROOT, ALLOW_FILE);
        if (!existsSync(p)) cannotRun(`读不到 ${ALLOW_FILE}(白名单已抽成文件,见 WO_264)`);
        return readFileSync(p, 'utf8')
            .split(/\r?\n/)
            .map((s) => s.trim())
            .filter((s) => s && !s.startsWith('#'));
    })(),
);

/** 工商主体形状。用 JS 正则按**码位**匹配 —— `grep -oE '[一-龥]…'` 在 UTF-8 下按字节切,会截出半截名字。 */
const SHAPE = /[一-龥]{2,12}(?:有限公司|股份有限公司|集团)/g;
const MARKER = '[SYNTHETIC-DEMO]';

const read = (rel) => {
    const p = join(ROOT, rel);
    if (!existsSync(p)) cannotRun(`读不到 ${rel}`);
    return readFileSync(p, 'utf8');
};
/** 形状命中(去重),以及其中不在白名单的那些。 */
const scan = (text) => {
    const hits = [...text.matchAll(SHAPE)].map((m) => m[0]);
    const uniq = [...new Set(hits)];
    return { count: hits.length, uniq, bad: uniq.filter((x) => !SYNTHETIC_ALLOW.has(x)) };
};

console.log('=== WO_256 · 真实客户数据不进公开包 ===\n');

/* ── W264 白名单单一来源:两格锁 ─────────────────────────────────────
   🔴 白名单被抽成 `scripts/synthetic-allow.txt`,给 WO_264 那道跑在**生产机**上的
      产物扫描门共用(那台没有 node,只能 bash + grep 读同一份文件)。
      两份清单各自演化是本周刚踩过的病 —— 所以这两格钉住「只有一份」。 */
{
    ok(SYNTHETIC_ALLOW.size >= 4,
        `W264-1 白名单文件 ${ALLOW_FILE} 至少 4 条`,
        `实得 ${SYNTHETIC_ALLOW.size} 条`);

    /* 每条都必须命中形状正则 —— 否则「⊆ 白名单」这句话对它不成立:
       一个不成形状的条目永远不会被 scan() 命中,放在这里只是装饰。 */
    const notShaped = [...SYNTHETIC_ALLOW].filter((n) => {
        SHAPE.lastIndex = 0;
        const m = n.match(new RegExp(`^${SHAPE.source}$`));
        return !m;
    });
    ok(notShaped.length === 0,
        'W264-2 白名单每条都命中形状正则(不成形状的条目等于装饰)',
        notShaped.length === 0 ? `${SYNTHETIC_ALLOW.size} 条全部成形状`
            : `🔴 ${notShaped.length} 条不成形状(不抄值,只报个数)`);

    /* 🔴 结构锁:本文件里不得再出现**工商主体形状的字面量**。
       没有这一格,有人「顺手」把清单加回来、文件继续存在,
       两份就又开始各自演化 —— 而所有判据照常绿。

       🔴 按**内容**判,不按变量名判。第一版写的是
          `/const\s+SYNTHETIC_ALLOW\s*=\s*new\s+Set/`,我毒它时随手把注入的常量
          命名成 `SYNTHETIC_ALLOW_OLD` —— **锁没响**。改个名就绕过去了。
          那不是毒写错了,是锁钉在了一个**谁都能改的标识符**上。
          现在钉的是「文件里有没有公司名形状的串」,和它叫什么无关。 */
    const selfSrc = readFileSync(join(ROOT, 'scripts/verify-no-real-client-data.mjs'), 'utf8');
    /* 只看**字符串字面量**里的形状,避开本文件正则源码与注释里的说明文字。 */
    const litShapes = [...selfSrc.matchAll(/['"`]([^'"`\n]{2,40})['"`]/g)]
        .map((m) => m[1])
        .filter((v) => new RegExp(`^${SHAPE.source}$`).test(v));
    ok(litShapes.length === 0,
        'W264-3 🔴 本文件里不得出现工商主体形状的字面量(单一来源结构锁 · 按内容判不按变量名判)',
        litShapes.length === 0 ? '名单只从文件读,源码里零形状字面量'
            : `🔴 源码里有 ${litShapes.length} 个形状字面量 —— 白名单又变成两份(不抄值,只报个数)`);
}

/* ── G1 源文件:合成标记 + 主体形状 ⊆ 白名单 ───────────────────────── */
const SOURCES = ['src/sandbox/data/demoReport.md', 'src/sandbox/mockData.ts'];
for (const rel of SOURCES) {
    const text = read(rel);
    ok(text.includes(MARKER), `W256-1a[${rel}] 带合成标记 \`${MARKER}\``,
        text.includes(MARKER) ? '在' : '🔴 没有 —— 这份演示数据没有声明自己是合成的');
    const r = scan(text);
    ok(r.bad.length === 0, `W256-1b[${rel}] 工商主体形状命中 ⊆ 合成白名单`,
        r.bad.length === 0
            ? `命中 ${r.count} 处 / 去重 ${r.uniq.length} 个,全在白名单`
            : `🔴 白名单外 ${r.bad.length} 个(不抄值,只报个数与去重数:命中 ${r.count} / 去重 ${r.uniq.length})`);
}
/* 🔴 分母自证:白名单本身得真的被用上。若源文件一个形状都没有,
      上面那两格会**空过** —— 那和"全在白名单"读数同形。 */
{
    const r = scan(read(SOURCES[0]));
    ok(r.uniq.length >= 2,
        'W256-1c 分母自证:演示报告里确实有 ≥2 个主体形状可查(否则上一格是空过)',
        `去重 ${r.uniq.length} 个`);
}

/* ── G2 EmployeeHall 的 placeholder 不含公司名 ────────────────────── */
{
    const rel = 'src/pages/Employees/EmployeeHall.tsx';
    const text = read(rel);
    const r = scan(text);
    ok(r.bad.length === 0, `W256-2a[${rel}] 没有白名单外的工商主体形状`,
        r.bad.length === 0 ? `形状命中 ${r.count} 处` : `🔴 白名单外 ${r.bad.length} 个`);
    /*
     * 🔴 形状锁在这里**天生不够**:WO_256 事实④ —— 这三处用的是**不带「有限公司」的短名**,
     *    形状正则一个都看不见。所以这一格钉的是 placeholder 的**字面内容**:
     *    示例只许用通用占位,不许出现具体公司。
     */
    const ph = [...text.matchAll(/帮我(?:搜索|为)"([^"]{1,20})"/g)].map((m) => m[1]);
    ok(ph.length >= 3, 'W256-2b 分母自证:确实找到了那几处 placeholder 示例', `${ph.length} 处`);
    const GENERIC = new Set(['某某科技', '某某公司', '某某品牌']);
    const bad = [...new Set(ph)].filter((x) => !GENERIC.has(x));
    ok(bad.length === 0, 'W256-2c placeholder 里的示例名是通用占位(不是某个真实客户)',
        bad.length === 0 ? `去重 ${new Set(ph).size} 个,全是通用占位` : `🔴 非通用占位 ${bad.length} 个`);
}

/* ── G3 构建产物(终点判据)─────────────────────────────────────── */
{
    const distDir = join(ROOT, 'dist', 'assets');
    if (!existsSync(distDir)) {
        /*
         * 🔴 没有产物时**不当通过**:本门在 build 链里排在 `vite build` 之后,
         *    产物理应存在。当成"不适用"放过去,等于把终点判据变成可选项。
         */
        cannotRun('dist/assets 不存在 —— 本门必须在 vite build 之后跑,现在没有产物可扫');
    }
    const files = readdirSync(distDir).filter((f) => f.endsWith('.js'));
    ok(files.length >= 5, 'W256-3a 分母自证:产物里确实有一堆 js chunk 可扫',
        `${files.length} 个 .js`);
    let total = 0;
    const offenders = [];
    for (const f of files) {
        const r = scan(readFileSync(join(distDir, f), 'utf8'));
        total += r.count;
        if (r.bad.length > 0) offenders.push(`${f}(白名单外 ${r.bad.length} 个)`);
    }
    ok(offenders.length === 0,
        'W256-3b 🔴 **构建产物** dist/assets/*.js 的工商主体形状命中 ⊆ 合成白名单(终点判据)',
        offenders.length === 0
            ? `${files.length} 个 chunk · 形状命中合计 ${total} 处,全在白名单`
            : `🔴 ${offenders.join(' · ')}`);
    ok(total >= 2,
        'W256-3c 分母自证:产物里确实扫到了形状命中(否则上一格是空过)',
        `合计 ${total} 处`);
}

console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
