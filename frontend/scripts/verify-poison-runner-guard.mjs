#!/usr/bin/env node
/**
 * 闸:注毒 runner 必须带**语法尺子**(下毒后先验语法)—— 且这把尺子自己要有两条臂。
 *
 * ## 为什么要有这一道
 *
 * 几乎所有 runner 都由「门红了没有」推出「毒被咬住了没有」。
 * 一发**把文件写成语法错**的毒同样让门红 ⇒ 报 CAUGHT,而门**什么都没测到**。
 * 🔴 「毒没下成」与「锁咬住了」在退出码上**完全同形**。
 *
 * ## 分母按后果定,不按字符串定(本仓 scope-the-denominator-by-consequence)
 *
 * 一开始有两个错的分母:
 * · Review 说「其余 5 个」—— 那是 `rc === 1` 这个**字符串**的出现次数;
 * · 我说「25 个」—— 那是**我自己的注释风格**的出现次数。
 * 真分母 = **凡是把毒写进源文件、再由门的读数判 CAUGHT 的 runner**,
 * 机械枚举 `scripts/mutation_runner_*.mjs` = 41 个,**一个都不许漏在清单外**。
 *
 * ## 花名册锁(G1)
 *
 * 「已接」+「待接」必须**恰好等于**盘上那 41 个文件 ——
 * 否则从清单里删一行就能让分母悄悄缩水,而这一格照样绿
 * (本仓 a-scanner-with-no-exclusions-is-still-limited-by-its-input)。
 *
 * 退出码三态:0 全绿 · 1 有格子红 · 3 闸自己没跑成。
 */
import { readdirSync, readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

/** 已接尺子的 —— 这一组**必须**三样齐:import / 对照臂 / 牙证。 */
const ADOPTED = [
    'mutation_runner_180b.mjs', 'mutation_runner_190.mjs', 'mutation_runner_191.mjs',
    'mutation_runner_196.mjs', 'mutation_runner_199.mjs', 'mutation_runner_200.mjs',
    'mutation_runner_203.mjs', 'mutation_runner_204.mjs', 'mutation_runner_210.mjs',
    'mutation_runner_218.mjs', 'mutation_runner_220a.mjs', 'mutation_runner_222.mjs',
    'mutation_runner_225.mjs', 'mutation_runner_225b.mjs', 'mutation_runner_243.mjs',
    'mutation_runner_250.mjs', 'mutation_runner_251.mjs', 'mutation_runner_253.mjs',
    'mutation_runner_defgeo_p0_route.mjs', 'mutation_runner_delist_entries.mjs', 'mutation_runner_diagnosis_launch_ui.mjs',
    'mutation_runner_directions.mjs', 'mutation_runner_gap_assistant_card.mjs', 'mutation_runner_image_note_publish.mjs',
    'mutation_runner_ios_touch_ux.mjs', 'mutation_runner_media_list_paging.mjs', 'mutation_runner_monitoring_poll.mjs',
    'mutation_runner_peer_compare.mjs', 'mutation_runner_provider_downgrade_ux.mjs', 'mutation_runner_publish_client_scope.mjs',
    'mutation_runner_publish_layout.mjs', 'mutation_runner_quote_order_date.mjs', 'mutation_runner_revoke_two_boxes.mjs',
    'mutation_runner_selection_all_excluded.mjs', 'mutation_runner_silent_reload_behavior.mjs', 'mutation_runner_silent_reload_outlets.mjs',
];

/**
 * 待接的存量 —— 🔴 这一组的读数「全咬住」都**带保留**:
 * 其中可能混着「其实没下成」的发数,只是没人分得出来。
 * 每接一个就从这里删一行,并把 CEILING 调小。**只许变小**。
 */
const PENDING = [
    'mutation_runner_195.mjs', 'mutation_runner_defgeo_legacy_required.mjs', 'mutation_runner_defgeo_pkgh_ux.mjs',
    'mutation_runner_defgeo_report_retry.mjs', 'mutation_runner_gap_plan.mjs', 'mutation_runner_quote_pricing_privacy.mjs',
];

/**
 * 🔴 [2026-09-20 补] 冻结名单从 37 改到 38:补进 `mutation_runner_243.mjs`。
 *
 * **这不是把棘轮放宽,是快照本来就漏了** —— 证据不在这段话里,在两个 sha:
 * · 243 由 `b8aefdb2b`(2026-09-18 23:54)创建;
 * · 花名册那一笔是 `587e3cab3`(2026-09-19 21:41),**晚于它**;
 * · 但 b8aefdb2b **不是** 587e3cab3 的祖先 ⇒ 写花名册那棵树里根本没有这个文件,
 *   `readdirSync` 枚举不到它。
 *
 * 形状 = 本仓 `a-scanner-with-no-exclusions-is-still-limited-by-its-input`:
 * **限制不在排除项里,在输入里**。我按后果定了域、也机械枚举了,
 * 但枚举的输入是「我这棵树」,而分母的真实边界是「本班所有平行支的并集」。
 *
 * 🔴 **留给下一个人的坑**:「补漏掉的存量」与「把棘轮放宽」在 diff 上完全同形,
 *    这道闸**分不出来**。所以往冻结表里加名字,规矩是:
 *    必须附上「创建提交 sha + 它不在花名册那笔的祖先里」这两条可复核的事实;
 *    附不出来的,就是放宽,要 Review 批。
 *
 * 棘轮 = **冻结名单**,不是一个数。
 *
 * 第一版写成 `const CEILING = 37`,而「把上限改大」正是那道闸自己给的合法出口 ——
 * 注毒实测:把 37 改成 99,**一格都不红**。一个和它守的东西放在同一处、
 * 谁都能改大的数,不是棘轮。
 *
 * ⇒ 改成冻结 2026-09-20 当天那 37 个名字:PENDING 只能是它的**子集**。
 *   新写的 runner 不在这张冻结表里 ⇒ **没法停进待接区**,只能直接带尺子。
 */
const PENDING_FROZEN_2026_09_20 = [
    'mutation_runner_180b.mjs', 'mutation_runner_190.mjs', 'mutation_runner_191.mjs',
    'mutation_runner_195.mjs', 'mutation_runner_196.mjs', 'mutation_runner_199.mjs',
    'mutation_runner_200.mjs', 'mutation_runner_203.mjs', 'mutation_runner_204.mjs',
    'mutation_runner_210.mjs', 'mutation_runner_220a.mjs', 'mutation_runner_222.mjs',
    'mutation_runner_225.mjs', 'mutation_runner_225b.mjs', 'mutation_runner_defgeo_legacy_required.mjs',
    'mutation_runner_defgeo_p0_route.mjs', 'mutation_runner_defgeo_pkgh_ux.mjs', 'mutation_runner_defgeo_report_retry.mjs',
    'mutation_runner_delist_entries.mjs', 'mutation_runner_diagnosis_launch_ui.mjs', 'mutation_runner_directions.mjs',
    'mutation_runner_gap_assistant_card.mjs', 'mutation_runner_gap_plan.mjs', 'mutation_runner_image_note_publish.mjs',
    'mutation_runner_ios_touch_ux.mjs', 'mutation_runner_media_list_paging.mjs', 'mutation_runner_monitoring_poll.mjs',
    'mutation_runner_peer_compare.mjs', 'mutation_runner_provider_downgrade_ux.mjs', 'mutation_runner_publish_client_scope.mjs',
    'mutation_runner_publish_layout.mjs', 'mutation_runner_quote_order_date.mjs', 'mutation_runner_quote_pricing_privacy.mjs',
    'mutation_runner_revoke_two_boxes.mjs', 'mutation_runner_selection_all_excluded.mjs', 'mutation_runner_silent_reload_behavior.mjs',
    'mutation_runner_silent_reload_outlets.mjs',
    'mutation_runner_243.mjs',
];

let fail = 0;
let n = 0;
const ok = (id, cond, msg) => {
    n += 1;
    if (cond) { console.log(`  PASS ${id}`); } else { console.log(`  FAIL ${id}  ${msg}`); fail += 1; }
};

/* ── G1 花名册锁 ─────────────────────────────────────────────────────── */
console.log('=== G1 花名册(清单必须恰好等于盘上那些文件)===');
let onDisk;
try {
    onDisk = readdirSync(join(ROOT, 'scripts'))
        .filter((f) => /^mutation_runner_.*\.mjs$/.test(f)).sort();
} catch (e) {
    console.log(`  🔴 闸没跑成:读不到 scripts/ —— ${e.message}`);
    process.exit(3);
}
const listed = [...ADOPTED, ...PENDING].sort();
const missing = onDisk.filter((f) => !listed.includes(f));
const ghosts = listed.filter((f) => !onDisk.includes(f));
console.log(`  盘上 ${onDisk.length} 个 · 清单 ${listed.length} 个(已接 ${ADOPTED.length} · 待接 ${PENDING.length})`);
ok('G1a', missing.length === 0, `盘上有、清单没有(分母漏了):${missing.join(', ')}`);
ok('G1b', ghosts.length === 0, `清单有、盘上没有(清单烂了):${ghosts.join(', ')}`);
ok('G1c', new Set(listed).size === listed.length, '清单里有重复项');
const parked = PENDING.filter((f) => !PENDING_FROZEN_2026_09_20.includes(f));
ok('G1d', parked.length === 0, `这些不在 2026-09-20 冻结表里,却被停进待接区:${parked.join(', ')}`);
ok('G1f', PENDING.length <= PENDING_FROZEN_2026_09_20.length,
    `待接 ${PENDING.length} > 冻结 ${PENDING_FROZEN_2026_09_20.length} —— 只许降不许升`);
ok('G1e', ADOPTED.every((f) => !PENDING.includes(f)), '同一个文件同时在两张表里');

/* ── G2 已接的三样齐 ─────────────────────────────────────────────────── */
console.log('=== G2 已接尺子的 runner:import / 对照臂 / 牙证,三样齐 ===');
for (const f of ADOPTED) {
    let src;
    try { src = readFileSync(join(ROOT, 'scripts', f), 'utf8'); } catch (e) {
        console.log(`  🔴 闸没跑成:读不到 ${f} —— ${e.message}`);
        process.exit(3);
    }
    ok(`G2-import[${f}]`, src.includes("from './lib/poison-syntax-guard.mjs'"), '没 import 那把共用尺子');
    /*
     * 🔴 第一版写成 `/if\s*\(\s*!\s*(parses|syntaxOk\()/` —— 钉的是「用 if 判」这个**形状**。
     *    199 用的是 `touched.filter((rel) => !syntaxOk(abs(rel)))`(一发毒可能改多份文件),
     *    同样正确,却判不过。**锁又一次瞄了我猜的写法**
     *    (a-lock-aimed-at-a-guessed-future-shape-is-blind)。
     *    改成钉「结果被**取反**用掉了」—— 那才是有意义的部分,与 if / filter / 三元无关。
     * ⚠️ 仍是文本锚:它看得见「取反了」,看不见「取反之后那个值有没有控制一个后果」。
     *    那一层由各 runner 自己的牙证(下面 G2-tooth)在**行为上**兜。
     */
    ok(`G2-syntaxOk[${f}]`, /!\s*syntaxOk\s*\(|!\s*parses\b|landPoisons?\s*\(/.test(src),
        '下毒后没判语法(判据看不见「没下成」)');
    /* 🔴 对照臂:尺子恒 false 时必须打成 rc=3,而不是输出一屏「每一发都没下成」。 */
    ok(`G2-control[${f}]`, src.includes('assertRulerWorks('), '缺对照臂 —— 恒 false 的尺子会把每一发都报成「没下成」');
    /* 🔴 牙证:一发故意写坏语法的毒。缺了它,这道前置平时永远返回 true,没东西证明它在工作。 */
    /*
     * 🔴 用 `includes('expectSyntaxFail')` 会被 `expectSyntaxFailDISABLED` 满足 ——
     *    注毒实测它**存活**了。锚要钉到「这个键真的是 true」。
     * 牙证有两种落法,都认:
     *   · 表里一发 `expectSyntaxFail: true` 的毒(runner 自己的循环处理);
     *   · 调一次共用的 `proveGuardHasTeeth(...)`(存量 runner 接线用,
     *     它抛异常而不是返布尔,漏看不了)。
     */
    ok(`G2-tooth[${f}]`,
        /^\s*expectSyntaxFail:\s*true\s*,?\s*$/m.test(src) || /proveGuardHasTeeth\s*\(/.test(src),
        '缺牙证 —— 没有任何东西证明这道前置会红(改名/改值都算缺)');
}

/* ── G3 尺子本身的行为(钉行为,不钉写法)───────────────────────────── */
console.log('=== G3 尺子本身:好文件必须绿、坏文件必须红 ===');
let dir;
let mod;
try {
    /* 🔴 临时目录建在系统 tmp,不建在 node_modules/.cache ——
          那是构建产物,干净树上没有,会让闸崩成 rc=1(读作"判据红了")。 */
    dir = mkdtempSync(join(tmpdir(), 'poison-ruler-'));
    mod = await import(new URL('./lib/poison-syntax-guard.mjs', import.meta.url).href);
} catch (e) {
    console.log(`  🔴 闸没跑成(bootstrap):${e.message}`);
    process.exit(3);
}
try {
    const cases = [
        ['G3a-ts-good', 'good.ts', 'export const a: number = 1;\n', true],
        ['G3b-ts-bad', 'bad.ts', 'export const a: number = ;\n', false],
        ['G3c-tsx-good', 'good.tsx', 'export const A = () => <div className="x">hi</div>;\n', true],
        ['G3d-tsx-bad', 'bad.tsx', 'export const A = () => <div className="x">hi</div;\n', false],
    ];
    for (const [id, name, body, want] of cases) {
        const p = join(dir, name);
        writeFileSync(p, body, 'utf8');
        const got = mod.syntaxOk(p);
        ok(id, got === want, `syntaxOk 回 ${got},应当是 ${want}`);
    }
    /* 对照臂对**好文件**不许抛;对坏文件必须抛。 */
    const good = join(dir, 'good.ts');
    let threwOnGood = false;
    try { mod.assertRulerWorks(good); } catch { threwOnGood = true; }
    ok('G3e', !threwOnGood, '对照臂把好文件也判成尺子坏了');
    let threwOnBad = false;
    try { mod.assertRulerWorks(join(dir, 'bad.ts')); } catch { threwOnBad = true; }
    ok('G3f', threwOnBad, '对照臂对坏文件不吭声 —— 它自己没牙');
} finally {
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* 清不掉不影响判据 */ }
}

console.log(`\n${fail === 0 ? 'ALL PASS' : `${fail} FAILED`} · ${n - fail}/${n}`);
process.exit(fail === 0 ? 0 : 1);
