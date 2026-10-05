#!/usr/bin/env node
/**
 * 判据 · #233 对客称谓(**后端对客展示层**这一臂)。
 *
 * 🔴 为什么单独一个文件、而且**不进 build 链**:
 *    链上第一道闸 `verify-no-backend-refs-in-build-chain.mjs` 禁止链内脚本伸手到 `frontend/` 之外,
 *    因为镜像的 frontend-builder 阶段**只有 frontend/**,后端一个字节都不在那儿 ——
 *    把这一臂塞进链里,构建机上会直接炸在「读不到文件」上。
 *    (同一类教训本轮已有一次:起浏览器的门挂进链,本机绿、烤镜像必退 3。
 *     **链里能跑的前提是那个阶段有这个东西**,不是我本地有。)
 *    ⇒ 前端那臂进链(`verify-outcome-wording-mention.mjs`),这一臂挂 npm 项,由复审与本地跑。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};
const BANNED = ['明确推荐', '条件推荐', '带条件的推荐'];

console.log('#233 对客称谓 · 后端对客展示层');

/*
 * ══ 后端对客层 ══
 * 🔴 [2026-09-17 复审点名] 这门抬头原来自称「钉的是**全站**」,而它只 walk 了 `frontend/src`
 *    —— ROOT 是 `frontend/`,后端一个字节都没扫到。「全站零命中」在读数上成立、在语义上不成立。
 *    这正是本轮我在 H1 上刚总结过的那件事(**钉的是名字不是命题**),
 *    只不过这次名字写在**门自己的抬头**里,而它自称的范围比实际大。
 *    ⇒ 扩到后端对客层,并把分母**分成两个数**:前端一个、后端一个,各自自证。
 *
 * 🔴 后端这一格**不能**写成「全后端零命中」—— 那样会红,而且红得没道理:
 *    · `services/geo_observation/entity_review.py:416` 的「明确推荐」在一个**喂给模型的 prompt**里
 *      (`级别:recommended=明确推荐并给正向理由…`)。改它是**改判定行为**,不是改文案,属包二。
 *    · `writing/client_presence_policy.py:412` 的「AI 摘不出明确推荐句」说的是**文章写没写出结论**,
 *      跟 `target_outcome` 判定不是同一件事,扫进来是假阳。
 *    ⇒ 只扫**对客展示层**,并把这两处例外写在这里 —— 每一条排除都是一处自陈盲区,
 *      写出来才有人能质疑它;悄悄 filter 掉的排除没人看得见。
 */
const BACKEND_PRESENTATION = [
    '../services/defensive_geo/presentation/metric_definitions.py',
    '../services/defensive_geo/presentation/copy_registry.py',
    '../services/public_report_presentation.py',
];
{
    const decommentPy = (s) => s.replace(/^\s*#.*$/gm, '');
    let scanned = 0;
    const beHits = [];
    for (const rel of BACKEND_PRESENTATION) {
        let src;
        try { src = readFileSync(join(ROOT, rel), 'utf8'); } catch { src = null; }
        if (src === null) { beHits.push(`${rel}:读不到`); continue; }
        scanned += 1;
        const body = decommentPy(src);
        for (const w of BANNED) if (body.includes(w)) beHits.push(`${rel.replace('../', '')}:${w}`);
    }
    ok(scanned === BACKEND_PRESENTATION.length,
        'M1 分母自证:后端对客层三个文件都读到了(读不到就不是"干净",是没扫)',
        `${scanned}/${BACKEND_PRESENTATION.length}`);
    ok(beHits.length === 0,
        'B1 🔴 后端**对客展示层**(指标显示名 / 公开报告说明文字 / 文案表)零命中 —— '
        + '显示名会上客户的报告,只改前端等于只改了一半',
        beHits.join(' / ') || '零处');

    /* 正臂:新说法确实落到了指标显示名上。只钉"旧词没了"的话,把显示名删空也是绿的。 */
    const MD = readFileSync(join(ROOT, '../services/defensive_geo/presentation/metric_definitions.py'), 'utf8');
    ok(MD.includes('"提及率(正面措辞)"') && MD.includes('"提及率(带条件措辞)"'),
        'B2 正臂:两个指标的新显示名都在');
    /* 🔴 反臂:**契约键一个字都不许动**。改显示名是对客口径,改键是另一回事(下游按键取数)。 */
    ok(MD.includes('"explicit_recommendation_rate"') && MD.includes('"conditional_recommendation_rate"'),
        'B3 反臂:契约键 `explicit_recommendation_rate` / `conditional_recommendation_rate` **原样不动** —— '
        + '改的是显示名,不是键');
}


console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
