#!/usr/bin/env node
/**
 * 小榜运营助手渲染层 · 变异 runner
 *
 * 三关:①改到字节 ②门禁转红且是点名那条 ③还原逐字节一致。
 * 🔴 二进制读写(Buffer),不走字符串 —— 本仓 2026-08-08 踩过 patch 脚本把文件写成 CRLF
 *    导致后续锚点全部失配,那种"存活"是锚点问题不是锁弱,会浪费一整轮分诊。
 * 🔴 中断安全:finally + SIGINT/SIGTERM + 落盘备份 + 下一轮残留检测拒跑。
 *    最坏失败模式不是"变异存活",是变异代码留在树里被当实现提交。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, rmSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve, basename } from 'node:path'
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const GATE = resolve(ROOT, 'scripts/test-gap-assistant-card.mjs')
const BACKUP = resolve(ROOT, '.mutation_backup_gap_assistant')

const HOOK = resolve(ROOT, 'src/hooks/useXiaobangChat.ts')
const LIST = resolve(ROOT, 'src/components/xiaobang/XiaobangMessageList.tsx')
const CARD = resolve(ROOT, 'src/components/xiaobang/XiaobangGapCard.tsx')

const inFlight = new Map()

function keepBackup(file, buf) {
  mkdirSync(BACKUP, { recursive: true })
  writeFileSync(resolve(BACKUP, basename(file)), buf)
}

function restoreAll(reason = '') {
  for (const [file, buf] of [...inFlight.entries()]) {
    try { writeFileSync(file, buf) } catch (e) { console.error(`!! 还原失败 ${file}: ${e.message}`) }
    inFlight.delete(file)
    try { rmSync(resolve(BACKUP, basename(file)), { force: true }) } catch { /* ignore */ }
  }
  try { if (existsSync(BACKUP) && readdirSync(BACKUP).length === 0) rmSync(BACKUP, { recursive: true }) } catch { /* ignore */ }
  if (reason) console.error(`[已还原:${reason}]`)
}

function abortIfStale() {
  if (existsSync(BACKUP) && readdirSync(BACKUP).length > 0) {
    console.error(
      `🔴 上一轮变异未正常收尾,${basename(BACKUP)}/ 里还留着:${readdirSync(BACKUP).join(', ')}\n` +
      '   工作树可能带着变异代码。先 git diff 自查并手工恢复,确认干净后删掉该目录再跑。\n' +
      '   (刻意不自动还原:上一轮状态不明,静默覆盖你的改动比拒跑更糟。)',
    )
    return true
  }
  return false
}

process.on('SIGINT', () => { restoreAll('SIGINT'); process.exit(130) })
process.on('SIGTERM', () => { restoreAll('SIGTERM'); process.exit(143) })
process.on('exit', () => restoreAll())

const MUTATIONS = [
  { id: 'F1', file: HOOK, expect: 'hook 的 meta 组装带上 gap_assistant',
    task: '把 gap_assistant 从 meta 组装里删掉(复现本次修的原 bug)',
    old: '                gap_assistant: normalizeGapAssistant(data.gap_assistant),\n', new: '' },
  { id: 'F2', file: HOOK, expect: '反向对照:meta 组装块确实是从 SSE data 取值的,不是写死',
    task: 'gap_assistant 写死成常量(不再从 SSE 取)',
    old: 'gap_assistant: normalizeGapAssistant(data.gap_assistant),',
    new: 'gap_assistant: undefined,' },
  { id: 'F3', file: HOOK, expect: 'headline 为空的载荷会被判成"没有建议"',
    task: '空 headline 也返回对象(渲染出空卡片)',
    old: '  if (!headline) return undefined', new: '  if (false) return undefined' },
  { id: 'F4', file: LIST, expect: 'XiaobangMessageList 渲染 XiaobangGapCard',
    task: '导入了但不渲染(死导入 —— 正是修之前的状态)',
    old: '            <XiaobangGapCard\n              assistant={message.meta.gap_assistant}\n              onNavigate={onLinkNavigate}\n            />',
    new: '            <span />' },
  { id: 'F5', file: LIST, expect: '渲染以 meta.gap_assistant 存在为条件',
    task: '无条件渲染(没有建议时也出一张空卡)',
    old: '{message.meta?.gap_assistant && (', new: '{true && (' },
  { id: 'F6', file: CARD, expect: '🔴 卡片源码里不得出现任何路由字面量',
    task: '前端自己拼路径(不再用服务端签发的 target_route)',
    /* [2026-09-20 重锚] navigate 后来多了第二个参数(state) ⇒ 旧锚命中 0。
       只钉第一个实参,连同后面的逗号一起换掉,行为等价于「不再用服务端签发的 route」。 */
    old: 'navigate(action.target_route as string,', new: "navigate('/pricing', {}," },
  { id: 'F7', file: CARD, expect: '只渲染 enabled 且带 target_route 的动作',
    task: '不判 enabled(服务端说不可用的动作也渲染成按钮)',
    /* [2026-09-20 重锚] 过滤条件已拆成多行且更强 ⇒ 旧的单行锚命中 0。
       钉「判 enabled」那一行本身:删掉它 = 服务端说不可用的动作也渲染。 */
    old: '    && a.enabled === true\n', new: '' },
  { id: 'F8', file: CARD, expect: '反向对照:过滤是 filter 不是恒真',
    task: '原样返回入参(等于没过滤)',
    /* [2026-09-20 重锚] 过滤器已改成多行 ⇒ 旧的整行锚命中 0。
       钉 `return (actions || []).filter((a) =>` 这一句开头,
       换成直接返回入参 = 等于没过滤(后面的多行条件成了死代码,语法仍合法)。 */
    old: '  return (actions || []).filter((a) =>\n'
      + '    !!a\n'
      + '    && a.enabled === true\n'
      + '    && !!a.registry_version\n'
      + "    && typeof a.target_route === 'string'\n"
      + "    && a.target_route.startsWith('/')\n"
      + "    && !a.target_route.startsWith('//'),\n"
      + '  ).slice(0, 2)',
    new: '  return actions' },
  { id: 'F9', file: CARD, expect: '降级态有渲染且措辞不像故障',
    task: '降级态不渲染(取不到建议时用户以为页面坏了)',
    old: '{assistant.degraded && (', new: '{false && (' },
  { id: 'F10', file: CARD, expect: '禁硬编码色值(必须走主题 token)',
    task: '硬编码品牌色(绕开主题 token,暗色模式会瞎)',
    /* [2026-09-20 重锚] 类名串已变(现为 `'bg-brand text-white hover:opacity-90'`)
       ⇒ 旧锚命中 0。钉 `bg-brand` 这个 token 本身。 */
    old: "'bg-brand text-white hover:opacity-90'",
    new: "'bg-[#6CBE1E] text-white hover:opacity-90'" },
]

function runGate() {
  try {
    execFileSync(process.execPath, [GATE], { cwd: ROOT, stdio: 'pipe', encoding: 'utf8' })
    return { ok: true, out: '' }
  } catch (e) {
    return { ok: false, out: (e.stdout || '') + (e.stderr || '') }
  }
}

function main() {
  if (abortIfStale()) return 2
  const base = runGate()
  if (!base.ok) {
    console.error('🔴 基线就不是绿的,变异结果没有意义:\n' + base.out.slice(-2000))
    return 2
  }
  console.log('基线绿\n')

  const killed = []
  const survived = []
  /* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(HOOK)

for (const m of MUTATIONS) {
    const original = readFileSync(m.file)
    const text = original.toString('utf8')
    const hits = text.split(m.old).length - 1
    if (hits !== 1) {
      survived.push([m, `锚点命中 ${hits} 次(需恰好 1 次)—— 锚点失配,不是锁弱`])
      continue
    }
    try {
      inFlight.set(m.file, original)
      keepBackup(m.file, original)
      assertRulerWorks(m.file)
      writeFileSync(m.file, Buffer.from(text.replace(m.old, m.new), 'utf8'))
      if (!syntaxOk(m.file)) {
        console.log(`  ${NOT_LANDED_SYNTAX}`)
        restoreAll()
        failures++
        continue
      }
      if (readFileSync(m.file).equals(original)) throw new Error('变异没有改到字节')
      const r = runGate()
      if (r.ok) survived.push([m, '门禁仍然全绿'])
      else if (!r.out.includes(m.expect)) survived.push([m, `转红了但不是点名那条(${m.expect})`])
      else killed.push(m)
    } finally {
      restoreAll()
    }
  }

  console.log('\n' + '='.repeat(58))
  for (const m of killed) console.log(`OK  ${m.id} 被杀 · ${m.task}`)
  for (const [m, why] of survived) console.log(`XX  ${m.id} 存活 · ${m.task} · ${why}`)
  console.log('='.repeat(58))
  console.log(`${killed.length}/${MUTATIONS.length} 条变异被杀死`)
  return survived.length === 0 ? 0 : 1
}

process.exitCode = main()
