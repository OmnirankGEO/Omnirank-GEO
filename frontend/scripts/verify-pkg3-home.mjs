#!/usr/bin/env node
/**
 * 判据 · 包三 §G「首页看板 / 今日工作台」收口。三态退出码:0 / 1 / 3。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const rd = (p) => strip(readFileSync(join(ROOT, p), 'utf8'));
const HOME = 'src/pages/Home/GeoHome.tsx';
const SRC = rd(HOME);
const WIDGET = rd('src/components/workbench/TodayPriorityWidget.tsx');

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['E0', 'F0'];
const PENDING_SKIPS = ['C-brandcount'];   // 依赖 C 修 dashboard_api 的口径
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 今天只给 3 件事 ────────────────────────────────────────────
/**
 * 🔴 5 张同类卡片不是"三件事",是一张列表的前 5 行 —— 她仍然要自己挑。
 *    给 3 张、其余留在「全部」后面,才叫"一眼看到"。
 */
ok(/<TodayPriorityWidget[^>]*maxSecondary=\{2\}/.test(SRC),
    'A1 🔴 首页 1 primary + 2 secondary = 3 件事');
ok(/maxSecondary = 4/.test(WIDGET),
    'A2 🔴 改的是**调用点的参数**,组件默认值仍是 4 —— '
    + 'M3 工作台那边按 5 张排版,改默认会顺手改掉别人 territory 的布局');
ok(/共 \{total\} 客户|共 \{total\}/.test(WIDGET),
    'A3 其余的仍有出口(「共 N 客户」),不是把它们藏掉');

// ── B 🔴 快捷入口的落点必须是真实路由 ───────────────────────────────
/**
 * 🔴 与「下一步」条同一条纪律:给一颗点了 404 的按钮比不给更糟。
 *    分母**机械枚举**页面里所有 `link: '/xxx'`,逐个回 App.tsx 核。
 */
const app = readFileSync(join(ROOT, 'src/App.tsx'), 'utf8');
const links = [...new Set((SRC.match(/link: '\/[a-z0-9\-/]*'/g) || [])
    .map((x) => x.slice(8, -1)))];
ok(links.length >= 5, `B0 正样本臂:抽到 ${links.length} 个快捷入口(抽 0 会让下一条恒真)`);
const deadLinks = links.filter((to) => {
    const seg = to.replace(/^\//, '').split('/')[0];
    return !new RegExp(`path="${seg}(/|")`).test(app);
});
ok(deadLinks.length === 0, `B1 🔴 每个快捷入口在 App.tsx 里都是真实路由(实得死链 ${JSON.stringify(deadLinks)})`);
ok(!/path="zzq-必然不存在"/.test(app), 'B2 反向对照:编造的路由必须查不到');

// ── C 🔴 数字同源:首页「管理品牌」与「我的客户」列表口径必须一致 ─────
/**
 * 🔴 [#116 落地后重写] 这条锁的**前提被一次正确的重构推翻过一次,而它没有变红,变绿了。**
 *
 * 原写法比的是两个字面锚:`dashboard_api` 有没有 `is_test = FALSE`,
 * 与 `brand_api` 有没有 `own_scope_test_clause = ""`。#116 把后者抽成了共用函数
 * `services/brand_test_visibility.own_scope_test_clause()` ⇒ 那个字面量消失,
 * 我的第二个输入永远为假 ⇒ 整条判据**恒真**。
 * 注毒实测:把排除 is_test 那行加回 dashboard ⇒ **判据仍绿**。
 * 「未评估 → 真检查」的转换看起来成功了,而它转成的是一条空判据 ——
 * 退出码从 3 变 0 这个信号为真,却没有意义。
 *
 * 新写法照 C 在 `resolve_keywords` 上立的形状:**唯一实现 + 两个消费方**。
 *   ① 规则只有一处定义(`brand_test_visibility.own_scope_test_clause`);
 *   ② 两个消费方都调它(不是各自手写一份 SQL 片段);
 *   ③ 两个消费方的 own-scope 路径里**不许再出现手写的 is_test 排除**。
 * 少了 ③,有人在调用旁边再加一行 AND,①② 照绿。
 */
let dash = null; let brandApi = null; let svc = null;
try {
    dash = readFileSync(join(REPO, 'api/dashboard_api.py'), 'utf8');
    brandApi = readFileSync(join(REPO, 'api/brand_api.py'), 'utf8');
    svc = readFileSync(join(REPO, 'services/brand_test_visibility.py'), 'utf8');
} catch { /* 下面按未评估处理 */ }
if (!dash || !brandApi || !svc) {
    notEvaluated += 1; skipped.push('C-brandcount');
    console.log('  ⚠️ C0 **未评估(依赖方未交)**:读不到 #116 的三个文件之一 —— 共用规则尚未落地。');
} else {
    ok(/def own_scope_test_clause\(\)/.test(svc),
        'C0 正样本臂:唯一实现存在(读空会让下面几条恒真)');
    for (const [name, src] of [['dashboard_api', dash], ['brand_api', brandApi]]) {
        ok(/from services\.brand_test_visibility import[\s\S]{0,120}own_scope_test_clause/.test(src),
            `C1[${name}] 🔴 从唯一实现 import 那条规则`);
    }
    const dIdx = dash.indexOf('brand_count = cur.fetchone()');
    const dSql = dIdx > 0 ? dash.slice(Math.max(0, dIdx - 900), dIdx) : '';
    // 🔴 python 侧也要剥注释:#116 的说明里就写着 is_test,
    //    不剥的话 C4 会命中**解释它的那段话**而不是代码(今天第 N 次)。
    const dSqlCode = dSql.replace(/^\s*#.*$/gm, '');
    ok(dSql.length > 200, 'C2 正样本臂:切到了 brand_count 的 SQL');
    ok(/_own_scope_test_clause\(\)/.test(dSql),
        'C3 🔴 首页那条 SQL **调**共用规则(不是自己拼)');
    ok(!/is_test/.test(dSqlCode),
        `C4 🔴 首页那条 SQL 里**没有手写的 is_test** —— 有人在调用旁边再加一行 AND,`
        + 'C1/C3 照绿而两个数又对不上了');
}

// ── E 🔴 红臂 ───────────────────────────────────────────────────────
const BASE = '2f8aae1b7';
try {
    const base = execFileSync('git', ['show', `${BASE}:frontend/${HOME}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    ok(!/maxSecondary=\{2\}/.test(base), 'E1 红臂:改动前首页没有限到 3 件');
    ok(/TodayPriorityWidget/.test(base), 'E2 配对臂:基线里本来就有的锚确实在');
} catch (e) {
    notEvaluated += 1; skipped.push('E0');
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('maxSecondary') || js.includes('今日跟进'), 'F1 今日模块进了产物');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t) && !PENDING_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
const pend = skipped.filter((t) => PENDING_SKIPS.includes(t));
if (pend.length) console.log(`  ⚠️ SKIP-GUARD 依赖方未交:${JSON.stringify(pend)} —— C 修完自动转真检查,现在别读成通过。`);
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
