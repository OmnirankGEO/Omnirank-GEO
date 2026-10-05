#!/usr/bin/env node
/**
 * 判据 · #233 对客称谓:`recommended` / `conditionally_recommended` 两档不再叫「推荐」。
 *
 * 🔴 为什么改说法:`classify_outcome` 在确认提及之后**只看回答里任意位置有没有正向词**,
 *    既不绑定推荐对象、也不识别否定。后端实测三例都被判成 `recommended`:
 *      · 「甲公司资料不足,无法评价。推荐乙公司…」 ← 推荐的是乙
 *      · 「甲公司是一家专业从事软件开发的企业。」   ← 是描述
 *      · 「目前无法推荐甲公司,资料太少。」         ← 是否定
 *    拿它对客户说「AI 明确推荐了你」,是拿一个会把「无法推荐」读成「推荐」的判定去做正面承诺。
 *    本单**不重写语义判定**(那要标注集+影子计算,是包二),只把说法收回到判定真正测到的东西上。
 *
 * 🔴 这门只钉**前端 src 全量**(后端对客层在 `verify-outcome-wording-mention-backend.mjs`,
 *    它不能进 build 链:链内禁止伸手到 frontend/ 之外,而构建阶段也确实没有后端)。不是某一处:同一份「档位 → 文案」映射在前端有**三份拷贝**
 *    (primitives 的 VERDICT_STYLE / EvidenceMatrix 的 VERDICT_OPTIONS / manyEvidence 夹具里的三元),
 *    只改一两处的话,剩下那份会在另一个屏上继续说「明确推荐」,而两处各自看起来都对。
 *    ⇒ 锚按「整个 src 下零命中」写,不按「这三个文件里没有」写 ——
 *      后者在有人新加第四份拷贝时看不见。
 *
 * 两态退出:0 全过 / 1 有失败(本门进 build 链)。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

/** 递归收 src 下所有 ts/tsx。 */
function walk(dir, out = []) {
    for (const f of readdirSync(dir)) {
        const p = join(dir, f);
        if (statSync(p).isDirectory()) walk(p, out);
        else if (/\.tsx?$/.test(f)) out.push(p);
    }
    return out;
}
const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

console.log('#233 对客称谓:两档判定不再叫「推荐」');

const files = walk(join(ROOT, 'src'));
ok(files.length > 500, 'M0 分母自证:扫到了整棵 src(数太少说明遍历坏了,'
    + '而坏掉的遍历和"全站已清干净"在报文上同形)', `${files.length} 个文件`);

/* 🔴 去注释再判:解释「为什么不再这么叫」的注释里必然出现这些词,
      拿原文判会被自己的注释打红 —— 注释不是对客文案。 */
const BANNED = ['明确推荐', '条件推荐', '带条件的推荐'];
const hits = [];
for (const p of files) {
    const src = decomment(readFileSync(p, 'utf8'));
    for (const w of BANNED) {
        if (src.includes(w)) hits.push(`${p.slice(ROOT.length + 1).split('\\').join('/')}:${w}`);
    }
}
ok(hits.length === 0,
    'W1 🔴 **前端 src 全量**(去注释后)没有任何一处仍把这两档叫「推荐」 —— '
    + '同一份映射前端有三份拷贝,只改一两处会让另一个屏继续说「明确推荐」',
    hits.join(' / ') || '零处');

/* 正臂:新说法确实落到了两份标签表里 —— 只证「旧词没了」不能证「新词到位了」,
   把标签整个删掉也会让上面那格绿。 */
const PRIM = readFileSync(join(ROOT, 'src/features/publicReportPremium/components/ui/primitives.tsx'), 'utf8');
const EVID = readFileSync(join(ROOT, 'src/features/publicReportPremium/components/sections/EvidenceMatrix.tsx'), 'utf8');
for (const [name, src] of [['primitives', PRIM], ['EvidenceMatrix', EVID]]) {
    ok(src.includes('提及(正面措辞)') && src.includes('提及(带条件措辞)'),
        `W2-${name} 正臂:新说法两档都在(只钉"旧词没了"的话,把标签删光也是绿的)`,
        src.includes('提及(正面措辞)') ? '两档都在' : '🔴 缺');
}

/* 🔴 反臂:其余八档**一个字都不许动**。本单只收回前两档的说法,
      顺手把「没提到」「只是提到」也改了的话,就把「进了候选」和「只被提到」的区别抹平了。 */
const GEO = readFileSync(join(ROOT, 'src/features/geoObservation/copy.ts'), 'utf8');
for (const w of ['列入备选', '仅提到', '只给标准', '未提到']) {
    ok(PRIM.includes(w), `W3-${w} 反臂:其余档位文案**原样不动**(本单只收回前两档)`);
}
ok(/candidate_only:/.test(GEO), 'W3b 反臂:`candidate_only` 那条句子还在(没被顺手删掉)');

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
