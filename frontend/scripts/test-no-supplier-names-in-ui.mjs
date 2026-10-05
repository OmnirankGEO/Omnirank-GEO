/**
 * 防线 · 业务流程文案禁出现「媒介盒子」
 *
 * 铁律:不给用户看供应商名。我们向谁采购媒介是内部事,代理只该看到「发布通道」这类中性口径。
 * 2026-07-27 生产事故:沙盒教程引导写着「敏感词被媒介盒子拦下了」,把媒介供应商名直接露给服务商。
 *
 * 🔴 范围是刻意收窄的。第一版扫全部 src + 全部供应商名,喷出 28 条命中,其中绝大多数是:
 *   - `pages/Settings/**` 管理员配 API key 的界面(本来就该写 DashScope / Metaso / TikHub);
 *   - `pages/Employees` 的模型选择器(社媒工作台设置页、顾问团页两处随开源 E3 删);
 *   - 尾随注释、字段名(metaso_api_key)。
 * 一个满屏误报的防线最后一定会被关掉,所以这里只守【真正会被代理看到的业务流程文案】,
 * 只禁【我们转售的媒介供应商名】。要扩范围请连同 allowlist 一起想清楚再改。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(fileURLToPath(new URL('../src', import.meta.url)));

/** 转售型供应商名 · 出现在业务流程文案里即违规 */
const FORBIDDEN = ['媒介盒子'];

/** 配置/内部后台界面 · 允许出现供应商名 */
const ALLOW_PREFIXES = [
    ['pages', 'Admin'].join(sep),
    ['pages', 'Settings'].join(sep),
    ['pages', 'Employees'].join(sep),
    ['pages', 'Home', 'AdminDashboard.tsx'].join(sep),
    ['components', 'dashboard', 'FlowFunnelCard.tsx'].join(sep),
];

function walk(dir, out = []) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) walk(p, out);
        else if (/\.(tsx?|jsx?)$/.test(name)) out.push(p);
    }
    return out;
}

/** 去掉整行注释与尾随注释后再判定(注释不渲染,不算泄漏) */
function strippedOfComments(line) {
    const s = line.trimStart();
    if (s.startsWith('//') || s.startsWith('*') || s.startsWith('/*')) return '';
    const idx = line.indexOf('//');
    return idx >= 0 ? line.slice(0, idx) : line;
}

const violations = [];
for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (ALLOW_PREFIXES.some((a) => rel.startsWith(a))) continue;
    readFileSync(file, 'utf8').split(/\r?\n/).forEach((line, i) => {
        const code = strippedOfComments(line);
        for (const word of FORBIDDEN) {
            if (code.includes(word)) {
                violations.push(`${rel}:${i + 1}  「${word}」  ${code.trim().slice(0, 90)}`);
            }
        }
    });
}

if (violations.length) {
    console.error('\n❌ 业务流程文案出现供应商名(违反「不给用户看供应商名」铁律):\n');
    violations.forEach((v) => console.error('   ' + v));
    console.error('\n   对用户请用中性口径,例如「发布通道」。\n');
    process.exit(1);
}

console.log('✅ 业务流程文案无供应商名');
