/**
 * xiaobang-unified 的 webServer 前后卫(R3-P4 建立 · R3-P5 重做身份判据)。
 *
 * 🔴 为什么要有这个文件:
 *
 * 两个实证摆在面前 ——
 *   · Codex 侧:`ERR_CONNECTION_REFUSED`,事后 4186 上还挂着一个**残留 vite**;
 *   · Review 侧:`member-mobile` 抽屉收起动画拦截超时,单跑 3.3s 全绿。
 *
 * 两条都是**基础设施红**,可它们在报告里长得和产品红一模一样(同一个红叉、
 * 同一个退出码)。本仓为"基础设施红被当成产品红"付过费,反过来
 * "产品红被当成 flaky 忽略"更贵。
 *
 * ## R3-P5:两条旧判据作废
 *
 * **一、「开跑后才出现的监听者 = 我们的」作废。**
 * R3-P4 用时序当身份:teardown 杀掉"开跑后才出现在端口上"的进程。
 * 这个判据在并发下就是错的 —— 别人的服务恰好在我们开跑后启动并抢到端口,
 * 我们就会**杀掉别人的进程**。时序不是身份。
 * 现在改成**真身份**:`pid + 创建时间 + 命令行 hash` 三元组,
 * 在 `globalSetup`(webServer 已起)那一刻采集并持久化;teardown **只杀三元组
 * 逐字段相等的**。端口上出现身份不匹配的进程 → 只报基础设施红,**禁杀**。
 * (pid 会被复用,所以单靠 pid 不够;创建时间把复用的 pid 区分开。)
 *
 * **二、探测器"取不到就当没有"作废。**
 * R3-P4 里 `listenersOn` 出任何错都 `return []`,于是"探测器坏了"和
 * "端口上没人"在返回值上完全同形 —— 这正是本仓反复付费的那种假绿。
 * 而且 R3-P4 交付书还写了句错话:「探测失败时残留不会被清,但下一轮的前置检查
 * 会拦下」—— **不成立**:探测器坏了的话,下一轮的前置检查同样什么都探不到,
 * 会直接放行。所以探测器现在 **fail-closed**:分不清就抛基础设施红。
 */
import { execFileSync, execSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export const PORT = 4186

const STATE_DIR = join(tmpdir(), 'xbvnext-pw')
const STATE_FILE = join(STATE_DIR, `port-${PORT}.json`)
const GUARD_DONE_ENV = 'XBVNEXT_PORT_GUARD_DONE'

/** 测试用:强制探测器进入"失效"分支,用来证明 fail-closed 真的会红。 */
export const PROBE_BREAK_ENV = 'XBVNEXT_FORCE_PROBE_FAILURE'

export class InfraError extends Error {
  constructor(message: string) {
    super(`[基础设施 · 非产品红] ${message}`)
    this.name = 'InfraError'
  }
}

export type ProbeResult =
  | { ok: true; pids: number[] }
  | { ok: false; reason: string }

export interface ProcessIdentity {
  pid: number
  /** 进程创建时间(ms)。pid 会被复用,创建时间把复用的区分开。 */
  createdAt: number
  /** 命令行的 sha256 前 16 位。命令行本身也留着,报错时给人看。 */
  cmdlineHash: string
  cmdline: string
}

// ──────────────────────────────────────────────────────────────
// 探测器:fail-closed
// ──────────────────────────────────────────────────────────────

/**
 * 谁在监听 `port`。
 *
 * 🔴 返回值把两件事**分开**:
 *   · `{ok: true, pids: []}`  = 探测正常,确实没人监听;
 *   · `{ok: false, reason}`   = 探测器本身失效(命令缺失 / 无权限 / 输出解析不了)。
 * 调用方必须对第二种报基础设施红,不许当"没人"。
 */
export function listenersOn(port: number): ProbeResult {
  if (process.env[PROBE_BREAK_ENV] === '1') {
    return { ok: false, reason: `探测器被 ${PROBE_BREAK_ENV}=1 强制置为失效(反向对照用)` }
  }
  return process.platform === 'win32' ? probeWindows(port) : probePosix(port)
}

function probeWindows(port: number): ProbeResult {
  let out: string
  try {
    // 🔴 不再用 `netstat | findstr`:findstr 无匹配也退 1,和 netstat 自己失败同形。
    //    这里只跑 netstat,过滤放到 JS 里做,退出码就只反映 netstat 本身。
    out = execFileSync('netstat', ['-ano', '-p', 'TCP'], {
      encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20_000,
    })
  } catch (exc) {
    return { ok: false, reason: `netstat 执行失败:${(exc as Error).message}` }
  }
  // 输出形状自检:一台机器不可能一条 TCP 记录都没有。解析不出行 = 输出格式变了。
  const rows = out.split(/\r?\n/).filter((line) => /^\s*TCP\s+\S+:\d+\s+\S+:\d+\s+\w+/i.test(line))
  if (!rows.length) {
    return { ok: false, reason: 'netstat 输出里一条 TCP 记录都解析不出 —— 格式或权限有问题' }
  }
  const pids = new Set<number>()
  for (const row of rows) {
    const cols = row.trim().split(/\s+/)
    const local = cols[1]
    const state = cols[3]
    const pidText = cols[4]
    if (!/^LISTENING$/i.test(state || '')) continue
    if (!new RegExp(`:${port}$`).test(local || '')) continue
    const pid = Number(pidText)
    if (!Number.isInteger(pid) || pid <= 0) {
      return { ok: false, reason: `netstat 行解析不出 PID:${row}` }
    }
    pids.add(pid)
  }
  return { ok: true, pids: [...pids] }
}

function probePosix(port: number): ProbeResult {
  try {
    const out = execFileSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t'], {
      encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20_000,
    })
    return { ok: true, pids: [...new Set(out.split(/\s+/).map(Number).filter((p) => p > 0))] }
  } catch (exc) {
    // lsof 的约定:退出码 1 = 查到 0 条(这是"正常且零匹配");其余都是它自己有问题。
    const status = (exc as { status?: number }).status
    if (status === 1) return { ok: true, pids: [] }
    return { ok: false, reason: `lsof 执行失败(status=${String(status)}):${(exc as Error).message}` }
  }
}

// ──────────────────────────────────────────────────────────────
// 进程身份
// ──────────────────────────────────────────────────────────────

function hashCmdline(cmdline: string): string {
  return createHash('sha256').update(cmdline).digest('hex').slice(0, 16)
}

/** 取进程身份三元组。取不到 → `null`(调用方按基础设施红处理,不许当"没这个进程")。 */
export function identityOf(pid: number): ProcessIdentity | null {
  try {
    if (process.platform === 'win32') {
      const json = execFileSync('powershell', [
        '-NoProfile', '-NonInteractive', '-Command',
        `Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" | ` +
        'Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress',
      ], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 30_000 }).trim()
      if (!json) return null
      const row = JSON.parse(json) as { CreationDate?: string; CommandLine?: string }
      const ms = /\/Date\((\d+)/.exec(row.CreationDate || '')
      const cmdline = row.CommandLine || ''
      if (!ms || !cmdline) return null
      return { pid, createdAt: Number(ms[1]), cmdlineHash: hashCmdline(cmdline), cmdline }
    }
    const out = execSync(`ps -o lstart=,args= -p ${pid}`, {
      encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20_000,
    }).trim()
    if (!out) return null
    // lstart 是固定 24 字符的时间串,其后是完整命令行。
    const started = out.slice(0, 24)
    const cmdline = out.slice(24).trim()
    const createdAt = Date.parse(started)
    if (!cmdline || Number.isNaN(createdAt)) return null
    return { pid, createdAt, cmdlineHash: hashCmdline(cmdline), cmdline }
  } catch {
    return null
  }
}

export function sameIdentity(a: ProcessIdentity, b: ProcessIdentity): boolean {
  return a.pid === b.pid && a.createdAt === b.createdAt && a.cmdlineHash === b.cmdlineHash
}

/** 这条命令行看着像不像本轮 webServer(只用于**采集时**确认端口上的是我们自己)。 */
export function looksLikeOurServer(cmdline: string): boolean {
  const normalized = cmdline.replace(/\\/g, '/').toLowerCase()
  return normalized.includes('vite') && normalized.includes(`--port ${PORT}`)
}

// ──────────────────────────────────────────────────────────────
// 三个钩子
// ──────────────────────────────────────────────────────────────

function readState(): ProcessIdentity[] {
  try {
    if (!existsSync(STATE_FILE)) return []
    const parsed = JSON.parse(readFileSync(STATE_FILE, 'utf-8'))
    return Array.isArray(parsed.ours) ? parsed.ours : []
  } catch {
    return []
  }
}

function writeState(ours: ProcessIdentity[]): void {
  mkdirSync(STATE_DIR, { recursive: true })
  writeFileSync(STATE_FILE, JSON.stringify({ ours }), 'utf-8')
}

/**
 * 开跑前:端口必须是空的。
 *
 * 🔴 **必须在 config 模块求值时调用,不能放 globalSetup** ——
 *    Playwright 先起 webServer 再跑 globalSetup,放那儿会把我们自己刚起的 vite
 *    当占用者。另外 config 会在**每个 worker 进程**里被重新 import,
 *    所以要一次性闸(env 标志,子进程继承 env)。
 *    这两条都不是推理出来的,是真跑各踩一次踩出来的。
 */
export function assertPortFreeBeforeServerStarts(): void {
  if (process.env[GUARD_DONE_ENV] === '1') return
  // `--list` 只列用例、不起 webServer,没有端口可查(分母锁会用到这个模式)。
  if (process.argv.includes('--list')) return
  process.env[GUARD_DONE_ENV] = '1'

  const probe = listenersOn(PORT)
  if (!probe.ok) {
    throw new InfraError(
      `端口探测器失效,无法判断 ${PORT} 是否空闲:${probe.reason}\n` +
      '  探测不出来就不能开跑 —— "探测器坏了"和"端口没人"必须区分,\n' +
      '  否则残留会静默污染本轮,红出来还会算到产品头上。',
    )
  }
  if (probe.pids.length) {
    const detail = probe.pids
      .map((pid) => `${pid}(${identityOf(pid)?.cmdline || '命令行取不到'})`)
      .join('; ')
    throw new InfraError(
      `端口 ${PORT} 开跑前就被占用:${detail}\n` +
      `  处理:taskkill /PID ${probe.pids.join(' /PID ')} /T /F(或 kill -9),然后重跑。`,
    )
  }
  writeState([])
}

/**
 * webServer 已起之后:**采集本轮子进程身份**并落盘。
 *
 * 时序判据(「开跑后才出现的就是我们的」)在 R3-P5 作废 —— 那会误杀并发启动的
 * 别人的服务。这里把身份钉死:pid + 创建时间 + 命令行 hash。
 */
export default function globalSetup(): void {
  const probe = listenersOn(PORT)
  if (!probe.ok) {
    throw new InfraError(`webServer 起来后探测端口失败:${probe.reason}`)
  }
  if (!probe.pids.length) {
    throw new InfraError(
      `webServer 应该已经在 ${PORT} 上监听,却一个监听者都没探到 —— ` +
      '要么 vite 起在别处,要么探测口径不对。',
    )
  }
  const ours: ProcessIdentity[] = []
  const foreign: string[] = []
  for (const pid of probe.pids) {
    const identity = identityOf(pid)
    if (!identity) {
      throw new InfraError(
        `拿不到 PID ${pid} 的身份(创建时间/命令行)。` +
        '身份取不到就不能给它盖"本轮子进程"的章 —— 后面 teardown 会据此杀进程。',
      )
    }
    if (looksLikeOurServer(identity.cmdline)) ours.push(identity)
    else foreign.push(`${pid} → ${identity.cmdline}`)
  }
  if (foreign.length) {
    throw new InfraError(
      `端口 ${PORT} 上有**不属于本轮**的进程:\n    ${foreign.join('\n    ')}\n` +
      '  本轮不碰它(禁杀),但也不能在它身上跑测试。',
    )
  }
  writeState(ours)
}

/**
 * 跑完:**只杀身份匹配者**。
 *
 * · 身份匹配 → 杀;杀不掉 → 抛(非零退出),不许当没事;
 * · 身份不匹配 → **禁杀**,只报基础设施红;
 * · 探测器失效 → 抛;
 * · 杀完仍被本轮身份占着 → 抛。
 */
export function globalTeardown(): void {
  const ours = readState()
  const probe = listenersOn(PORT)
  if (!probe.ok) {
    throw new InfraError(
      `跑完清理时端口探测器失效:${probe.reason}\n` +
      '  这时候既不能确认残留、也不能确认干净,所以按红处理 —— ' +
      '不能指望"下一轮会拦下",探测器坏了下一轮同样探不到。',
    )
  }

  const failedKills: string[] = []
  const foreign: string[] = []
  for (const pid of probe.pids) {
    const now = identityOf(pid)
    if (!now) {
      foreign.push(`${pid}(身份取不到 —— 按不匹配处理,禁杀)`)
      continue
    }
    if (!ours.some((own) => sameIdentity(own, now))) {
      foreign.push(`${pid} → ${now.cmdline}`)
      continue
    }
    // eslint-disable-next-line no-console
    console.log(`[基础设施] 清理本轮 webServer 残留 PID ${pid}(端口 ${PORT})`)
    if (!killTree(pid)) failedKills.push(`${pid} → ${now.cmdline}`)
  }

  const after = listenersOn(PORT)
  if (!after.ok) {
    throw new InfraError(`清理后复核探测失败:${after.reason}`)
  }
  const stillOurs = after.pids.filter((pid) => {
    const now = identityOf(pid)
    return now ? ours.some((own) => sameIdentity(own, now)) : false
  })

  try { rmSync(STATE_FILE, { force: true }) } catch { /* noop */ }

  const problems: string[] = []
  if (failedKills.length) problems.push(`杀本轮子进程失败:${failedKills.join('; ')}`)
  if (stillOurs.length) problems.push(`跑完端口 ${PORT} 仍被本轮身份占着:${stillOurs.join(', ')}`)
  if (foreign.length) {
    problems.push(
      `端口 ${PORT} 上有身份不匹配的进程(**已禁杀**,不是我们的东西):\n    ${foreign.join('\n    ')}`,
    )
  }
  if (problems.length) throw new InfraError(problems.join('\n  '))
}

function killTree(pid: number): boolean {
  try {
    if (process.platform === 'win32') {
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', timeout: 20_000 })
    } else {
      process.kill(pid, 'SIGKILL')
    }
    return true
  } catch {
    // 已经自己退了也算成功 —— 以"端口上还在不在"为准,下面会复核。
    const recheck = listenersOn(PORT)
    return recheck.ok && !recheck.pids.includes(pid)
  }
}
