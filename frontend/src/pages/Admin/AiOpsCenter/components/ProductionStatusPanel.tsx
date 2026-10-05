// 生产状态:运行状态说明(人话 · 防"页面能打开=AI已在自动运维"误解)+ 策略开关明细
// 数据全部来自 overview.policy / chat_llm(真实 DB flag);Runner 行改读 overview.runner 真实心跳(P1-B)。
// 生产 active 容器/HEAD/scheduler 明细由 admin-only 聚合接口读取(Batch G · 待 Runner 就绪),
// 前端不直连未鉴权的 /api/scheduler/status。

import { CheckCircle2, XCircle } from 'lucide-react'
import type { AiOpsOverview, AiOpsRunnerStatus } from '../types'
import { Panel } from './shared'

// 服务端 age_seconds → 人话相对时间。不用 new Date(last_seen_at):那是无时区 TIMESTAMP,
// 浏览器按本地时区解析会错位(DB UTC vs 用户 UTC+8 差 8 小时);age 由 DB 同一时钟算出,恒准。
function formatAge(sec: number): string {
  if (sec < 60) return `${sec} 秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}

// 本机 Codex 六态:null=未接入 / 离线 / 在线·已冻结(Kill Switch) / 在线·未授权执行 /
// 在线·正在处理 / 在线·可领取。与 worker/local_codex_runner 行为对齐:
// Kill Switch 开启时不领取任务,即便 env+总开关都开也绝不显示"可领取"。
function runnerRow(
  runner: AiOpsRunnerStatus | null | undefined,
  aiOpsOn: boolean,
  kill: boolean,
  runningCount: number,
): { value: string; tone: 'ok' | 'off' | 'warn'; hint: string } {
  if (!runner) {
    return { value: '未接入', tone: 'off', hint: '本机 Codex 尚未启动过 · 暂无心跳' }
  }
  if (!runner.online) {
    return { value: '离线', tone: 'warn', hint: `最后心跳 ${formatAge(runner.age_seconds)} · 任务会排队等它开机` }
  }
  // Kill Switch 优先:在线但被冻结,不领取任务
  if (kill) {
    return { value: '在线 · 已冻结', tone: 'warn', hint: 'Kill Switch 已开启 · 不领取任务' }
  }
  const codex = runner.codex_available ? 'Codex 可用' : 'Codex 不可用'
  if (runner.env_enabled && aiOpsOn) {
    if (runningCount > 0) {
      return { value: '在线 · 正在处理', tone: 'ok', hint: `${runner.worker_id} · ${runningCount} 个任务处理中` }
    }
    return { value: '在线 · 可领取', tone: 'ok', hint: `${runner.worker_id} · ${codex}` }
  }
  // 在线但未授权执行:本机 enabled 关或服务器总开关关
  return {
    value: '在线 · 未授权执行',
    tone: 'ok',
    hint: runner.env_enabled ? `${runner.worker_id} · 总开关关闭` : `${runner.worker_id} · 本机未授权(enabled=false)`,
  }
}

function Flag({ label, on }: { label: string; on: boolean }) {
  return (
    <div className="flex items-center justify-between rounded-md border bg-background px-3 py-1.5 text-xs">
      <span className="text-muted-foreground">{label}</span>
      {on ? (
        <span className="inline-flex items-center gap-1 text-emerald-400"><CheckCircle2 className="size-3.5" />开启</span>
      ) : (
        <span className="inline-flex items-center gap-1 text-muted-foreground"><XCircle className="size-3.5" />关闭</span>
      )}
    </div>
  )
}

const POLICY_LABELS: Record<string, string> = {
  'ai_ops.enabled': 'AI 运维总开关',
  'codex.diagnose.enabled': 'Codex 诊断',
  'codex.fix.enabled': 'Codex 修复',
  'ssh_runner.enabled': 'SSH Runner',
  'ai_ops.kill_switch': 'Kill Switch',
  'ai_ops.chat_llm.enabled': '命令台 LLM 解析',
  'ai_ops.glm_triage.enabled': 'GLM 一线分诊',
  'auto_deploy.enabled': '自动部署(未实现·占位)',
}

// 运行状态一行:状态词 + 人话解释
function StatusRow({ label, value, tone, hint }: {
  label: string
  value: string
  tone: 'ok' | 'off' | 'warn'
  hint?: string
}) {
  const color = tone === 'ok' ? 'text-emerald-400' : tone === 'warn' ? 'text-amber-400' : 'text-muted-foreground'
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className={`text-right ${color}`}>{value}{hint ? <span className="text-muted-foreground"> · {hint}</span> : null}</span>
    </div>
  )
}

export function ProductionStatusPanel({ overview }: { overview: AiOpsOverview | null }) {
  const policy = overview?.policy || {}
  const on = (k: string) => Boolean((policy[k] as { enabled?: boolean })?.enabled)
  const kill = on('ai_ops.kill_switch')
  const tc = overview?.task_counts
  const runningCount = tc?.running ?? 0
  const runner = runnerRow(overview?.runner, on('ai_ops.enabled'), kill, runningCount)
  const glmOn = on('ai_ops.glm_triage.enabled')
  // 队列口径:等待 Codex=排队任务 · 等待复审=待审批任务 · 等待部署=已批准未执行的审批
  const waitingCodex = tc?.queued ?? 0
  const waitingReview = tc?.waiting_approval ?? 0
  const waitingDeploy = overview?.approval_counts?.approved ?? 0

  return (
    <Panel title="生产状态">
      {/* 运行状态说明:让老板一眼看懂"现在 AI 到底在不在自动干活" */}
      <div className="mb-3 space-y-1.5 rounded-md border border-border bg-muted/20 p-3">
        <div className="mb-1 text-xs font-medium text-foreground">运行状态说明</div>
        <StatusRow label="Web 控制塔" value="可访问" tone="ok" hint="你正在看的这个页面" />
        <StatusRow
          label="自动日报"
          value={kill ? '已冻结' : on('ai_ops.enabled') ? '开启' : '关闭'}
          tone={kill ? 'warn' : on('ai_ops.enabled') ? 'ok' : 'off'}
          hint={on('ai_ops.enabled') ? '每天 08:10 生成' : '手动生成仍可用'}
        />
        <StatusRow
          label="GLM 一线"
          value={kill ? '已冻结' : glmOn ? '开启' : '关闭'}
          tone={kill ? 'warn' : glmOn ? 'ok' : 'off'}
          hint="只分诊/回复,不改代码不部署"
        />
        <StatusRow label="本机 Codex" value={runner.value} tone={runner.tone} hint={runner.hint} />
        <StatusRow
          label="队列"
          value={`等 Codex ${waitingCodex} · 等复审 ${waitingReview} · 等部署 ${waitingDeploy}`}
          tone={waitingCodex + waitingReview + waitingDeploy > 0 ? 'warn' : 'off'}
        />
        <StatusRow
          label="Codex 诊断"
          value={kill ? '已冻结' : on('ai_ops.enabled') && on('codex.diagnose.enabled') ? '开启' : '关闭'}
          tone={kill ? 'warn' : on('ai_ops.enabled') && on('codex.diagnose.enabled') ? 'ok' : 'off'}
          hint="需总开关+Runner 就绪才真跑"
        />
        <StatusRow
          label="Codex 修复"
          value={kill ? '已冻结' : on('codex.fix.enabled') ? '开启' : '关闭'}
          tone={kill ? 'warn' : on('codex.fix.enabled') ? 'ok' : 'off'}
          hint="改动合并永远需人工审批"
        />
        <StatusRow
          label="SSH Runner"
          value={kill ? '已冻结' : on('ssh_runner.enabled') ? '开启' : '关闭'}
          tone={kill ? 'warn' : on('ssh_runner.enabled') ? 'ok' : 'off'}
          hint="只读 allowlist·审批后才执行"
        />
        <p className="pt-1 text-[11px] leading-4 text-muted-foreground">
          页面能打开 ≠ AI 在自动运维:执行链默认全关,逐次授权开启。
          本机 Codex 关闭时任务会排队,开机后继续处理,不丢、不失败。
        </p>
      </div>

      {/* 策略开关明细(真实 DB flag) */}
      <div className="space-y-2">
        {Object.keys(POLICY_LABELS).map((k) => (
          <Flag key={k} label={POLICY_LABELS[k]} on={on(k)} />
        ))}
        <p className="pt-1 text-[11px] leading-4 text-muted-foreground">
          生产容器 / active HEAD / 调度明细由 admin-only 聚合接口读取(Runner 就绪后接入),
          不直连未鉴权调度端点。
        </p>
      </div>
    </Panel>
  )
}
