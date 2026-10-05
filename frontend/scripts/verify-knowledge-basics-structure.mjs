#!/usr/bin/env node
/**
 * #220 结构臂 —— **进 build 链的那一半**。
 *
 * 🔴 由来(2026-09-16):我把整个 #220 行为门(起 playwright chromium)挂进了
 *    `npm run build` 的 && 链。本机装了 chromium 所以一直 rc=0,
 *    而那条链真正跑的地方是 Dockerfile 的 frontend-builder 阶段 —— 那儿没有浏览器,
 *    门按三态退 3(「没跑成」),整条链失败,0913g 烤镜像中止。
 *    本仓既有约定(原例 verify-publish-self-axis 已随 WO_273 插件自助发布退役删除;
 *    同一约定现见 test-self-publish-retired / test-self-publish-retired-render):**结构臂进链、行为臂另挂 npm 项**。
 *
 * 🔴 摘门出链是**减覆盖**。这个文件就是把减掉的那部分补回去:
 *    结构面 6 格回到链里,行为面 38 格留在 `npm run verify:knowledge-basics`。
 *    判据本体在 `lib/a220-structure-criteria.mjs`,两边共用**同一份**,不抄。
 *
 * 三态:0 全绿 · 1 有判据红 · 3 门自己没跑成(源文件读不到)。
 * 🔴 3 和 1 必须分开:「我没跑」被读成「我跑了没事」是这套门最贵的一种假绿。
 */
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
    readStructureSources, runStructureCriteria, STRUCTURE_CRITERIA_COUNT, STRUCTURE_SOURCES,
} from './lib/a220-structure-criteria.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

let failures = 0;
let ran = 0;
const ok = (cond, name, detail) => {
    ran += 1;
    if (!cond) failures += 1;
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${detail !== undefined ? ` — ${detail}` : ''}`);
};

console.log('#220 结构臂(不起浏览器 —— 这一臂要能在构建机上跑)');
console.log(`  源:${STRUCTURE_SOURCES.join(' · ')}`);

let sources;
try {
    sources = readStructureSources(ROOT);
} catch (err) {
    console.log(`\n3 门没跑成:结构面源文件读不到 —— ${String((err && err.message) || err).slice(0, 300)}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

runStructureCriteria(ok, sources);

/* 🔴 分母自证:零条 FAIL 既可能是全绿,也可能是压根没跑。断言跑满条数。 */
if (ran !== STRUCTURE_CRITERIA_COUNT) {
    console.log(`\n3 门没跑成:只跑了 ${ran} 格,应为 ${STRUCTURE_CRITERIA_COUNT} 格 —— 有人删了格或模块没加载全`);
    process.exit(3);
}

console.log(`\n跑满 ${ran} 条结构判据`);
if (failures > 0) {
    console.log(`FAIL ${failures} 条红`);
    process.exit(1);
}
console.log('全部通过');
process.exit(0);
