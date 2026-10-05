#!/usr/bin/env node
/**
 * 判据 · w5_8「侧栏标签接术语 SSOT」。三态退出码:0 / 1 / 3。
 *
 * 🔴 为什么要另立一条,而不是让 `tests/test_v35_followup.py::test_w5_8_sidebar_rename` 说了算:
 *    那把锁查的是**字面串在不在 AppSidebar.tsx 里**,而本任务要求的正是
 *    「标签改从 SSOT 取」—— 一旦取自 SSOT,字面量就**不在**那个文件里了。
 *    **满足其中一个必然破坏另一个。** 实测:基线 2/8 红;我接完 SSOT 变 1/8,更红。
 *    而它还有一条 `if found == 0: skip` —— 也就是说「压根没做」与「做对了(走 SSOT)」
 *    都会落进 skip,只有「做一半(留字面量)」才红。**三态塌成两态,且惩罚正确做法。**
 *    锁在 `tests/`(窗口 B 的仪器域),本窗口不动;重锚建议已报 Review。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const SIDEBAR = strip(readFileSync(join(ROOT, 'src/components/layout/AppSidebar.tsx'), 'utf8'));
const TERM_RAW = readFileSync(join(ROOT, 'src/lib/v35Terminology.ts'), 'utf8');
const TERM = strip(TERM_RAW);

let bad = 0; const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

/**
 * 批 3 八条的**现行名**(两条旧名含禁词已换,见 D 段):
 *   额度包管理 → 算力定价中心(那一页已被合并进去,PricingCenter.tsx:5)
 *   资金与额度对账 → 资金与算力对账
 *   代理打款审核 → 服务商打款审核(Review 09-06 补裁禁「代理」)
 */
const EIGHT = {
    agent_inventory: '我的库存',
    agent_pricing: '销售定价',
    agent_settlement: '收益结算',
    agent_promotion: '获客推广',
    agent_agreement: '合作协议',
    admin_settlements: '服务商打款审核',
    admin_pricing_center: '算力定价中心',
    admin_fund_audit: '资金与算力对账',
};
const SIX = EIGHT;   // 名字留着不改,免得下面一堆引用一起动
/** 🔴 禁词:资金链 SSOT「统一算力」+ Review 2026-09-06 补裁「对外称服务商」。 */
const BANNED = ['额度', '积分', '工具额度', '代理'];

// ── A 🔴 SSOT 里有这张表,且值逐字正确 ───────────────────────────────
const m = await import(new URL('../src/lib/v35Terminology.ts', import.meta.url).href).catch(() => null);
ok(!!m, 'A0 正样本臂:能 import 术语模块');
if (m) {
    ok(typeof m.sidebarLabel === 'function', 'A1 导出了 sidebarLabel()');
    for (const [k, v] of Object.entries(SIX)) {
        ok(m.sidebarLabel(k) === v, `A2[${k}] 🔴 → 「${v}」(实得「${m.sidebarLabel(k)}」)`);
    }
    ok(m.sidebarLabel('zzq_没这个键') === 'zzq_没这个键',
        'A3 🔴 取不到时**报出 key**,不回落到一个像模像样的中文 —— '
        + '回落会让漏项永远看不见(屏幕上很正常,而那一条根本没进 SSOT)');
}

// ── B 🔴 侧栏真的从 SSOT 取 ─────────────────────────────────────────
ok(/import \{ sidebarLabel \} from '@\/lib\/v35Terminology';/.test(SIDEBAR),
    'B1 🔴 AppSidebar 引用术语 SSOT(原来是 0 处引用,全靠硬编码)');
for (const k of Object.keys(SIX)) {
    ok(SIDEBAR.includes("label: sidebarLabel('" + k + "')"), `B2[${k}] 该条走 SSOT`);
}

// ── C 🔴 侧栏里不许再有这六条的硬编码(新旧名都不许)───────────────────
/**
 * 🔴 只查旧名不够:有人把**新名**直接写进 AppSidebar 也能骗过 test_w5_8,
 *    而那正是"看起来做完了、SSOT 仍然没人用"的状态。新旧都禁。
 */
const OLD = ['算力库存', '客户售价', '提现结算', '推广获客'];
const inlined = [...OLD, ...Object.values(SIX)].filter((n) => new RegExp(`label: '${n}'`).test(SIDEBAR));
ok(inlined.length === 0, `C1 🔴 侧栏零硬编码标签(新旧名都算,实得 ${JSON.stringify(inlined)})`);
ok(/label: '/.test(SIDEBAR), 'C2 正样本臂:侧栏里确实还有别的 `label: \'…\'`(否则 C1 恒真)');

// ── D 🔴 两条期望名与「全站统一算力」冲突 ⇒ 不收(锚过期)──────────────
/**
 * 🔴 `test_w5_8` 还期望 `额度包管理` / `资金与额度对账`,而
 *    `CLAUDE.md:1475` 与 `AppSidebar.tsx` 自己的头注释都写着
 *    「全站文案统一『算力』(禁『积分/额度/工具额度』)」。
 *    批 3(2026-05-26)早于那条统一口径 ⇒ 这两条是**锚过期**,不是缺陷。
 *    侧栏现名「资金与算力对账」才是现行正确名。**收了它们等于把昨天刚清掉的禁词请回来。**
 */
ok(!/额度/.test(SIDEBAR), 'D1 🔴 侧栏零「额度」(统一算力口径)');
// 🔴 D3 第一版写成「侧栏里有『资金与算力对账』这串字」—— 那正是我批评 test_w5_8 的毛病:
//    接了 SSOT 之后字面量本来就不在侧栏了。改钉 SSOT 那一侧 + 路由确实走 SSOT。
ok(SIDEBAR.includes("label: sidebarLabel('admin_fund_audit')"),
    'D3 🔴 对账那条走 SSOT(而不是为了迁就过期的锚把禁词写回侧栏)');
/**
 * 🔴 D4/D6 必须读**模块里的** SIDEBAR_LABELS,不能读判据里那份 `EIGHT` 拷贝。
 *    第一版读的是拷贝 ⇒ 把 SSOT 改成含禁词的值,D4 照绿(只有 A2 红)——
 *    **期望值取自近侧的判据,永远抓不到远侧真的漂了**。注毒才发现。
 */
const LIVE = (m && m.SIDEBAR_LABELS) ? m.SIDEBAR_LABELS : {};
const banHits = Object.entries(LIVE)
    .flatMap(([k, v]) => BANNED.filter((b) => String(v).includes(b)).map((b) => [k, v, b]));
ok(banHits.length === 0, `D4 🔴 八条现行名里零禁词(实得 ${JSON.stringify(banHits)})`);
ok(BANNED.some((b) => '代理打款审核'.includes(b)),
    'D5 反向对照:禁词表对批 3 那个旧名确实命中(否则 D4 的"零"只是词表坏了)');
ok(Object.keys(LIVE).length === 8, `D6 🔴 **SSOT 里**恰 8 条(实得 ${Object.keys(LIVE).length})`);

if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
console.log('\n✅ 全部通过');
process.exit(0);
