#!/usr/bin/env node
/**
 * 「团队与席位」枚举 → 人话映射表的**完备性锁**。
 *
 * [工单 2026-08-17 §5-2]
 *   「映射完备性锁:枚举源(TS 类型/后端枚举)与映射表机械 diff,漏一项红
 *     (判据打枚举定义全集,不打手挑样例 —— 笛卡尔积那课)」
 *
 * 为什么必须机械取全集:B 类 15 条英文露出里,`statusLabel` 那条(B9)恰恰是
 * 「作者手挑了 5 个状态、DB CHECK 里有 5 个**另外的**状态」造成的。手挑必漏。
 * 所以这里的全集一律从**定义处**取:
 *   - services/organization_contract.py 的 LIMIT_KINDS / APPROVAL_ACTIONS /
 *     BILLABLE_FEATURE_CAPABILITIES(Python 侧 SSOT);
 *   - scripts/migration_organization_*.sql 的 `CHECK (col IN (...))`(DB 侧 SSOT);
 *   - services/organization_*.py 里全部 `_audit(action=/entity_type=)` 站点。
 *
 * 判据成对(工单 §7「每个必须命中配一个必须不命中」):
 *   `node verify-organization-enum-labels.mjs`            → 必须全绿
 *   `node verify-organization-enum-labels.mjs --selftest` → **必须能报红**
 *     (往每一条轴里注入一个人造缺口,逐条确认这把锁真的会红;
 *      任何一条轴注入后仍然绿 = 那条轴是恒真的假锁)
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..', '..');
const read = (relative) => readFileSync(resolve(repo, relative), 'utf8');

const CONTRACT = read('services/organization_contract.py');
const MIGRATION = read('scripts/migration_organization_internal_seats_2026_07_20.sql');
const USERNAME_MIGRATION = read('scripts/migration_organization_username_invite_2026_07_25.sql');
const LABELS = read('frontend/src/lib/organizationLabels.ts');
const CENTER = read('frontend/src/pages/Organization/OrganizationCenter.tsx');

const SERVICE_FILES = [
  'services/organization_service.py',
  'services/organization_approvals.py',
  'services/organization_artifacts.py',
  'services/organization_billing.py',
  'services/organization_limits.py',
  'services/organization_onboarding.py',
  'services/organization_payer_policy.py',
  'services/organization_plans.py',
  'services/organization_worker.py',
  'services/organization_membership_lifecycle.py',
];
const SERVICE_SOURCE = SERVICE_FILES.map(read).join('\n');

/* ── 取全集:Python 侧 ─────────────────────────────────────────────────── */

function pyTuple(name) {
  const match = CONTRACT.match(new RegExp(`^${name}\\s*=\\s*\\(([\\s\\S]*?)\\n\\)`, 'm'));
  if (!match) throw new Error(`取不到 Python 元组 ${name}`);
  return [...match[1].matchAll(/"([^"]+)"/g)].map(m => m[1]);
}

function pyDictKeys(name) {
  const match = CONTRACT.match(new RegExp(`^${name}\\s*=\\s*\\{([\\s\\S]*?)\\n\\}`, 'm'));
  if (!match) throw new Error(`取不到 Python 字典 ${name}`);
  return [...match[1].matchAll(/^\s*"([^"]+)":/gm)].map(m => m[1]);
}

function pyFrozenset(name) {
  const match = CONTRACT.match(new RegExp(`^${name}\\s*=\\s*frozenset\\(([\\s\\S]*?)\\n\\)`, 'm'));
  if (!match) throw new Error(`取不到 Python frozenset ${name}`);
  return [...match[1].matchAll(/"([^"]+)"/g)].map(m => m[1]);
}

/* ── 取全集:SQL CHECK 约束 ─────────────────────────────────────────────── */

function tableBody(sql, table) {
  const start = sql.indexOf(`CREATE TABLE IF NOT EXISTS ${table} (`);
  if (start < 0) throw new Error(`取不到建表语句 ${table}`);
  let depth = 0;
  for (let i = sql.indexOf('(', start); i < sql.length; i += 1) {
    if (sql[i] === '(') depth += 1;
    else if (sql[i] === ')') {
      depth -= 1;
      if (depth === 0) return sql.slice(start, i + 1);
    }
  }
  throw new Error(`建表语句 ${table} 括号不闭合`);
}

/** 取 `CHECK (<column> IN ('a','b',...))` 里的全部取值。 */
function checkValues(sql, table, column) {
  const body = tableBody(sql, table);
  const match = body.match(new RegExp(`CHECK\\s*\\(\\s*${column}\\s+IN\\s*\\(([^)]*)\\)`, 'i'));
  if (!match) throw new Error(`取不到 ${table}.${column} 的 CHECK 约束`);
  return [...match[1].matchAll(/'([^']+)'/g)].map(m => m[1]);
}

/** 取 `ALTER TABLE ... ADD CONSTRAINT ... CHECK (<column> IN (...))` 里的全部取值。 */
function alterCheckValues(sql, table, column) {
  const match = sql.match(new RegExp(
    `ALTER TABLE\\s+${table}[\\s\\S]{0,400}?CHECK\\s*\\(${column}\\s+IN\\s*\\(([^)]*)\\)`, 'i',
  ));
  if (!match) return [];
  return [...match[1].matchAll(/'([^']+)'/g)].map(m => m[1]);
}

/* ── 取全集:审计站点 ──────────────────────────────────────────────────── */

function auditLiterals(kind) {
  return [...SERVICE_SOURCE.matchAll(new RegExp(`${kind}="([a-z0-9_.]+)"`, 'g'))].map(m => m[1]);
}

/**
 * f-string 展开的审计动作。这三处是代码里唯一的 `action=f"..."`(已逐一核对),
 * 变量取值域来自同文件的显式白名单校验:
 *   - `member.{action}`        ← set_member_status 的 {suspend,resume,remove}
 *   - `approval.{decision}`    ← decide_approval 的 {approve,reject}
 *   - `automatic_plan.{status}`← set_plan_status 的 {active,paused,cancelled}
 * 另加 `assignment.replace`(`audit_action` 默认值)与 `invite.assignment.apply`
 * (同一函数的具名实参)。任何**新增**的 `action=f"..."` 站点会被下方的
 * FSTRING_SITES 计数守卫抓到 —— 数量变了就红,逼迫更新这张表。
 */
const FSTRING_EXPANSIONS = [
  'member.suspend', 'member.resume', 'member.remove',
  'approval.approve', 'approval.reject',
  'automatic_plan.active', 'automatic_plan.paused', 'automatic_plan.cancelled',
  'automatic_plan.completed',
  'assignment.replace', 'invite.assignment.apply',
];
const FSTRING_SITES = 3;

/* ── 取全集:TS 侧映射表 ───────────────────────────────────────────────── */

function tsMapKeys(name) {
  const match = LABELS.match(new RegExp(`export const ${name}: Record<string, string> = \\{([\\s\\S]*?)\\n\\};`));
  if (!match) throw new Error(`取不到 TS 映射表 ${name}`);
  return [...match[1].matchAll(/^\s*'?([A-Za-z0-9_.]+)'?:/gm)].map(m => m[1]);
}

function capabilityLabelKeys() {
  const match = CENTER.match(/const CAPABILITY_LABELS: Record<string, string> = \{([\s\S]*?)\n\};/);
  if (!match) throw new Error('取不到 CAPABILITY_LABELS');
  return [...match[1].matchAll(/^\s*'([a-z0-9_.]+)':/gm)].map(m => m[1]);
}

function capabilityGroupCodes() {
  const match = CENTER.match(/const CAPABILITY_GROUPS: Array<\{ title: string; codes: string\[\] \}> = \[([\s\S]*?)\n\];/);
  if (!match) throw new Error('取不到 CAPABILITY_GROUPS');
  return [...match[1].matchAll(/'([a-z0-9_.]+)'/g)].map(m => m[1]).filter(code => code.includes('.'));
}

/* ── 轴定义 ───────────────────────────────────────────────────────────── */

function axes(inject = null) {
  const bend = (name, values) => (inject === name ? values.filter((_, index) => index !== 0) : values);
  return [
    { name: 'LIMIT_KIND', expected: pyTuple('LIMIT_KINDS'), actual: bend('LIMIT_KIND', tsMapKeys('LIMIT_KIND_LABELS')) },
    { name: 'FEATURE_CODE', expected: pyDictKeys('BILLABLE_FEATURE_CAPABILITIES'), actual: bend('FEATURE_CODE', tsMapKeys('FEATURE_CODE_LABELS')) },
    {
      name: 'APPROVAL_ACTION',
      expected: [...new Set([...pyFrozenset('EXTERNAL_ACTIONS'), ...pyFrozenset('APPROVAL_ACTIONS')])],
      actual: bend('APPROVAL_ACTION', tsMapKeys('APPROVAL_ACTION_LABELS')),
    },
    { name: 'APPROVAL_STATUS', expected: checkValues(MIGRATION, 'organization_approval_requests', 'status'), actual: bend('APPROVAL_STATUS', tsMapKeys('APPROVAL_STATUS_LABELS')) },
    { name: 'PLAN_STATUS', expected: checkValues(MIGRATION, 'organization_automatic_plans', 'status'), actual: bend('PLAN_STATUS', tsMapKeys('PLAN_STATUS_LABELS')) },
    { name: 'LIMIT_STATUS', expected: checkValues(MIGRATION, 'organization_spend_limits', 'status'), actual: bend('LIMIT_STATUS', tsMapKeys('LIMIT_STATUS_LABELS')) },
    {
      name: 'MEMBER_STATUS',
      expected: [
        ...checkValues(MIGRATION, 'organization_memberships', 'status'),
        ...checkValues(MIGRATION, 'organization_invites', 'status'),
      ],
      actual: bend('MEMBER_STATUS', tsMapKeys('MEMBER_STATUS_LABELS')),
    },
    {
      name: 'INVITE_TARGET_KIND',
      expected: [
        ...checkValues(MIGRATION, 'organization_invites', 'target_kind'),
        ...alterCheckValues(USERNAME_MIGRATION, 'organization_invites', 'target_kind'),
      ],
      actual: bend('INVITE_TARGET_KIND', tsMapKeys('INVITE_TARGET_KIND_LABELS')),
    },
    { name: 'ACTOR_KIND', expected: ['owner', 'member', 'system'], actual: bend('ACTOR_KIND', tsMapKeys('ACTOR_KIND_LABELS')) },
    { name: 'ENTITY_TYPE', expected: auditLiterals('entity_type'), actual: bend('ENTITY_TYPE', tsMapKeys('ENTITY_TYPE_LABELS')) },
    {
      name: 'AUDIT_ACTION',
      expected: [...auditLiterals('action'), ...FSTRING_EXPANSIONS],
      actual: bend('AUDIT_ACTION', tsMapKeys('AUDIT_ACTION_LABELS')),
    },
    { name: 'CAPABILITY_GROUPING', expected: capabilityLabelKeys(), actual: bend('CAPABILITY_GROUPING', capabilityGroupCodes()) },
  ];
}

function evaluate(inject = null) {
  const failures = [];
  for (const axis of axes(inject)) {
    const expected = [...new Set(axis.expected)].sort();
    const actual = [...new Set(axis.actual)].sort();
    if (!expected.length) {
      failures.push(`${axis.name}: 取到的枚举全集是**空的** —— 取数正则失效了,这条轴是假绿`);
      continue;
    }
    const missing = expected.filter(value => !actual.includes(value));
    if (missing.length) failures.push(`${axis.name}: 映射表缺 ${missing.length} 项 → ${missing.join(', ')}`);
  }
  // CAPABILITY_GROUPS 额外查重复(同一权限落进两个分组 = 界面出现两个勾选框)
  const grouped = capabilityGroupCodes();
  const duplicated = grouped.filter((code, index) => grouped.indexOf(code) !== index);
  if (duplicated.length && inject !== 'CAPABILITY_GROUPING') {
    failures.push(`CAPABILITY_GROUPING: 有权限被分到多个组 → ${[...new Set(duplicated)].join(', ')}`);
  }
  // f-string 审计站点数量守卫:新增一处就必须回来更新 FSTRING_EXPANSIONS
  const sites = [...SERVICE_SOURCE.matchAll(/action=f"/g)].length;
  if (sites !== FSTRING_SITES) {
    failures.push(`AUDIT_ACTION: \`action=f"\` 站点数从 ${FSTRING_SITES} 变成 ${sites} —— 请核对并更新 FSTRING_EXPANSIONS`);
  }
  return failures;
}

/* ── 主流程 ───────────────────────────────────────────────────────────── */

if (process.argv.includes('--selftest')) {
  // 反向对照:逐条轴注入人造缺口,确认每条轴都真的会红。
  // 「注入后仍然绿」= 那条轴取数取空了 / 比较写反了,是个恒真的假锁。
  const names = axes().map(axis => axis.name);
  const blind = [];
  for (const name of names) {
    if (!evaluate(name).some(line => line.startsWith(`${name}:`))) blind.push(name);
  }
  if (blind.length) {
    console.error('❌ 自证失败:以下轴注入缺口后仍然全绿(= 假锁)：' + blind.join(', '));
    process.exit(1);
  }
  if (evaluate().length) {
    console.error('❌ 自证失败:未注入时本来就是红的,反向对照没有意义。先修真问题。');
    process.exit(1);
  }
  console.log(`✅ 自证通过:${names.length} 条轴各自注入一个人造缺口后都能报红，未注入时全绿。`);
  process.exit(0);
}

if (process.argv.includes('--counts')) {
  // 分母自证:每条轴取到的枚举全集有多少项。任何一条是 0 都说明取数正则失效
  // (「全绿」会变成假绿)。evaluate() 里也有同一条空集守卫。
  for (const axis of axes()) {
    console.log(
      `${axis.name.padEnd(20)} 枚举源 ${String([...new Set(axis.expected)].length).padStart(3)} 项`
      + ` | 映射表 ${String([...new Set(axis.actual)].length).padStart(3)} 项`,
    );
  }
}

const failures = evaluate();
if (failures.length) {
  console.error('❌ 团队模块枚举映射不完备(漏一项就是一个英文出口)：');
  for (const line of failures) console.error('   · ' + line);
  console.error('\n   修法:在 frontend/src/lib/organizationLabels.ts 补上对应中文;');
  console.error('   禁止把 fallback 改成 `|| rawValue` —— 那正是本次要清除的英文出口。');
  process.exit(1);
}
console.log(`✅ 团队模块枚举映射完备（${axes().length} 条轴逐项机械 diff 通过）。`);
