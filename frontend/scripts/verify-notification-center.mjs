/**
 * verify-notification-center.mjs — 通知中心构建门禁(工单 2026-07-29 T3)
 *
 * 仓库没有前端单测框架(无 vitest/jest),verify-*.mjs 是既有的前端断言载体,
 * 本脚本沿用同一形态并挂进 `npm run build`。任一违规即 exit 1。
 *
 * 锁住的是**最容易悄悄退化回去**的那几条:
 *   ① 详情弹窗存在,且铃铛与历史页两个入口都挂了它(去掉弹窗 → 转红)
 *   ② 弹窗正文用 whitespace-pre-wrap 且**不许**出现 line-clamp(正文仍截断 → 转红)
 *   ③ 列表项点击走"开弹窗",不是直接 navigate 走人(回到"点了看不到正文" → 转红)
 *   ④ 已读走后端 feed/read 端点,不是只改本地 state(已读不持久化 → 转红)
 *   ⑤ 历史页真有 offset 翻页(退回"只取最近 N 条" → 转红)
 *   ⑥ 通知不许有删除入口(工单 §3.3.4:通知是记录,尤其扣费类要可追溯)
 *   ⑦ 扣费类渲染路径不许出现任何资金写操作端点
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const root = path.resolve(process.cwd(), 'src')
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
const failures = []
const fail = (msg) => failures.push(msg)

/** 只看代码,不看注释 —— 否则解释根因的注释会把门禁自己误伤掉。 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => {
      const t = line.trim()
      return !t.startsWith('//') && !t.startsWith('*')
    })
    .join('\n')
}

const DIALOG = 'components/layout/NotificationDetailDialog.tsx'
const BELL = 'components/layout/NotificationBell.tsx'
const PAGE = 'pages/Notifications/NotificationCenter.tsx'
const LIB = 'lib/notificationFeed.ts'

for (const rel of [DIALOG, BELL, PAGE, LIB]) {
  if (!fs.existsSync(path.join(root, rel))) fail(`缺文件: ${rel}`)
}
if (failures.length) {
  failures.forEach((m) => console.error(`❌ ${m}`))
  process.exit(1)
}

const dialog = stripComments(read(DIALOG))
const bell = stripComments(read(BELL))
const page = stripComments(read(PAGE))

// ① 弹窗存在且两个入口都挂了
if (!/export function NotificationDetailDialog/.test(dialog)) {
  fail('NotificationDetailDialog 没有导出 —— 详情弹窗被拆了')
}
for (const [name, src] of [['NotificationBell', bell], ['通知历史页', page]]) {
  if (!/<NotificationDetailDialog\b/.test(src)) {
    fail(`${name} 没有挂载 <NotificationDetailDialog> —— 点击又变成没有详情`)
  }
}

// ② 正文完整:弹窗正文必须 whitespace-pre-wrap,且弹窗内不许 line-clamp
if (!/whitespace-pre-wrap/.test(dialog)) {
  fail('详情弹窗正文没有 whitespace-pre-wrap —— 换行/长文会被压平')
}
if (/line-clamp/.test(dialog)) {
  fail('详情弹窗里出现了 line-clamp —— 正文又被截断了(工单锁 1)')
}
if (!/\{item\.content\}/.test(dialog)) {
  fail('详情弹窗没有渲染 item.content 原文')
}

// ③ 列表项点击 = 开弹窗(不是直接 navigate)
if (!/onClick=\{\(\) => handleNotificationClick\(n\)\}/.test(bell)) {
  fail('铃铛列表项没有走 handleNotificationClick —— 点击行为被改回去了')
}
if (!/setDetail\(n\)/.test(bell)) {
  fail('handleNotificationClick 不再打开详情弹窗(工单锁 1)')
}
if (!/onClick=\{\(\) => openDetail\(n\)\}/.test(page)) {
  fail('历史页列表项点击没有走 openDetail —— 详情弹窗打不开')
}

// ④ 已读持久化:必须打后端 feed/read 端点
for (const [name, src] of [['NotificationBell', bell], ['通知历史页', page]]) {
  if (!/\/api\/user\/notifications\/feed\/read/.test(src)) {
    fail(`${name} 没有调用 /api/user/notifications/feed/read —— 已读只留在本地 state,刷新即回退`)
  }
}

// ⑤ 历史页真有 offset 翻页
if (!/fetchPage\(items\.length\)/.test(page)) {
  fail('历史页没有按 offset 翻页 —— 退回"只取最近 N 条"')
}
if (!/loadMore/.test(page)) {
  fail('历史页没有"加载更多" —— 取不到超出首屏的旧通知(工单锁 5)')
}

// ⑥ 不许有删除入口
for (const [name, src] of [['NotificationBell', bell], ['通知历史页', page], ['详情弹窗', dialog]]) {
  if (/method:\s*['"]DELETE['"]/.test(src) || /notifications\/[^'"`]*\/delete/.test(src)) {
    fail(`${name} 出现了删除通知的调用 —— 工单 §3.3.4 明令不做删除`)
  }
}

// ⑦ 扣费类渲染路径零资金写操作
const FUND_WRITE_ENDPOINTS = [
  '/api/wallet/consume', '/api/wallet/refund', '/api/wallet/freeze', '/api/wallet/release',
  '/api/billing/', '/api/points/consume', '/api/points/refund',
]
for (const [name, src] of [['NotificationBell', bell], ['通知历史页', page], ['详情弹窗', dialog]]) {
  for (const endpoint of FUND_WRITE_ENDPOINTS) {
    if (src.includes(endpoint)) {
      fail(`${name} 引用了资金写端点 ${endpoint} —— 扣费通知只做展示与跳转(工单锁 7)`)
    }
  }
}

if (failures.length) {
  console.error('通知中心构建门禁未通过:')
  failures.forEach((m) => console.error(`  ❌ ${m}`))
  process.exit(1)
}
console.log('✅ 通知中心构建门禁通过(弹窗/完整正文/已读持久化/翻页/无删除/零资金写)')
