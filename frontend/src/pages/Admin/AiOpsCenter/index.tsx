// AI 运维控制塔 · 全屏暗色驾驶舱 · /admin/ai-ops[/tasks/:taskId | /reports | /reports/:date]
// 布局按设计图:左侧独立导航 + 顶栏 + 6 健康卡 + 三列驾驶舱 + 右侧固定 AI 命令台。
// 数据全部来自真实 /api/admin/ai-ops/*;无真实接口的板块显示「待接入」,绝不伪造。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, CheckCircle2, FileText, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/context/AuthContext'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import {
  generateReport, getOverview, listReports, listTasks, setKillSwitch,
} from './api'
import type { AiOpsOverview, AiOpsReportListItem, AiOpsTask } from './types'
import { AiCommandConsole } from './components/AiCommandConsole'
import { AiOpsNav, NAV_ITEMS, type SectionKey } from './components/AiOpsNav'
import { ApprovalQueue } from './components/ApprovalQueue'
import { CodexTasksPanel } from './components/CodexTasksPanel'
import { ComingSoon } from './components/ComingSoon'
import { DailyReportPanel } from './components/DailyReportPanel'
import { FeedbackInbox } from './components/FeedbackInbox'
import { FinanceRiskPanel } from './components/FinanceRiskPanel'
import { HealthSummary } from './components/HealthSummary'
import { MonitoringPanel } from './components/MonitoringPanel'
import { ProductionStatusPanel } from './components/ProductionStatusPanel'
import { ReportViewer } from './components/ReportViewer'
import { TaskDetailDrawer } from './components/TaskDetailDrawer'
import { TopBar } from './components/TopBar'
import { EmptyHint, Panel, timeAgo } from './components/shared'

function ReportsList() {
  const [items, setItems] = useState<AiOpsReportListItem[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    listReports().then(setItems).catch(() => {}).finally(() => setLoading(false))
  }, [])
  return (
    <Panel title="运维日报">
      {loading ? <EmptyHint text="加载中" /> : items.length === 0 ? (
        <EmptyHint text="还没有日报" />
      ) : (
        <ul className="divide-y divide-border">
          {items.map((r) => (
            <li key={r.id}>
              <Link to={`/admin/ai-ops/reports/${r.report_date}`}
                className="flex items-center gap-3 py-2.5 hover:bg-muted/40">
                <FileText className="size-4 text-muted-foreground" />
                <span className="text-sm font-medium text-foreground">{r.report_date}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{r.summary}</span>
                <span className="shrink-0 text-[11px] text-muted-foreground">{timeAgo(r.updated_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

// 可翻转的执行开关:开启=升权,弹确认(写清后果);关闭=降权,直接执行(与顶栏 Kill Switch 同一原则)。
// 每次翻转都走 PATCH /policies(白名单校验 + updated_by 留痕 + 服务端 warning 日志)。
type TogglableFlag = { key: string; label: string; desc: string; confirmOn: string; danger?: boolean }

const TOGGLABLE_FLAGS: TogglableFlag[] = [
  {
    key: 'ai_ops.enabled',
    label: 'AI 运维总开关',
    desc: '一切自动执行的前提:Runner 领任务 + 每日 08:10 自动日报',
    confirmOn: '开启后:本机 Codex Runner 在线且已授权时会开始自动领取诊断任务;每日 08:10 自动生成运维日报。合并 / 部署 / 资金等 L3/L4 动作仍然全部人工审批,急停可随时用顶栏 Kill Switch。',
    danger: true,
  },
  {
    key: 'codex.diagnose.enabled',
    label: 'Codex 诊断',
    desc: 'L0 只读诊断:查代码/日志出报告,不改任何东西',
    confirmOn: '开启后:Codex 可领取只读诊断任务(L0,不改代码、不碰生产)。',
  },
  {
    key: 'codex.fix.enabled',
    label: 'Codex 修复',
    desc: '允许产出修复补丁;合并永远需要人工审批,绝不自动 merge',
    confirmOn: '开启后:Codex 可领取修复类任务,在隔离 worktree 写代码并产出 diff。任何 diff 都要过风险扫描 + 人工审批才可能合并,绝不自动 merge、绝不自动部署。',
    danger: true,
  },
  {
    key: 'ai_ops.glm_triage.enabled',
    label: 'GLM 一线分诊',
    desc: '用户 bug 反馈先过 GLM:非 bug 直接生成回复关单,真 bug 附分诊交 Codex',
    confirmOn: '开启后:feedback 来源的新任务会自动调用 GLM-5.2 分诊(服务器需已配 GLM_API_KEY,未配则自动跳过)。GLM 只分诊 / 回复,不改代码不部署;分诊失败时任务原样排队。',
  },
  {
    key: 'ai_ops.auto_create_from_feedback',
    label: '反馈自动立案',
    desc: '用户提交 bug 反馈后自动创建诊断任务(同一反馈去重)',
    confirmOn: '开启后:每条新的 bug 类用户反馈自动创建一条诊断任务进入队列(去重,不会重复立案)。任务是否被执行仍由总开关与 Codex 诊断开关决定。',
  },
  {
    key: 'ai_ops.patrol.enabled',
    label: '主动巡逻',
    desc: '每 5 分钟巡查心跳/失败/积压/日报,异常拉响告警、恢复自动消警',
    confirmOn: '开启后:系统每 5 分钟自动巡逻一轮(只读检查 + 写告警,不碰业务数据)。失败任务积累到阈值且总开关开着时,会自动立一条只读诊断案;急停时不立案。关闭后仍可在「监控告警」手动巡逻。',
  },
]

// 只读:执行条件不存在,给按钮也没意义(诚实原则,不给不可用的开关)
const READONLY_FLAGS: { key: string; label: string; reason: string }[] = [
  { key: 'ssh_runner.enabled', label: 'SSH Runner', reason: '专用受限 Runner 未就绪,开了也没有执行体' },
  { key: 'auto_deploy.enabled', label: '自动部署', reason: '仅占位无执行代码;部署由 Deploy-CTO 人工执行' },
]

function SettingsSection({ overview, onChanged }: { overview: AiOpsOverview | null; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  // 独立 confirm 实例(命名 askConfirm 防遮蔽全局 confirm)
  const [settingsDialog, askConfirm] = useConfirmDialog()
  const policy = overview?.policy || {}
  const flagOn = (k: string) => Boolean((policy[k] as { enabled?: boolean })?.enabled)
  const chatLlmOn = Boolean(overview?.chat_llm?.enabled)
  const chatLlmModel = overview?.chat_llm?.model || '—'

  const patchFlag = async (key: string, enabled: boolean, label: string) => {
    setBusy(true)
    try {
      const { patchPolicy } = await import('./api')
      await patchPolicy(key, { enabled })
      toast.success(`${label} 已${enabled ? '开启' : '关闭'}`)
      onChanged()
    } catch (err) {
      toast.error('操作失败', { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const toggleFlag = async (f: TogglableFlag) => {
    const next = !flagOn(f.key)
    if (next) {
      const ok = await askConfirm({
        title: `开启「${f.label}」?`,
        description: f.confirmOn,
        confirmLabel: '开启',
        danger: f.danger,
        // portal 到 body(亮色),必须带 text-foreground 强制重算,否则标题/取消按钮继承亮色前景近乎不可读
        contentClassName: 'dark border-border bg-[#0e121b] text-foreground',
      })
      if (!ok) return
    }
    await patchFlag(f.key, next, f.label)
  }

  const toggleChatLlm = () => patchFlag('ai_ops.chat_llm.enabled', !chatLlmOn,
    chatLlmOn ? '命令台 LLM(回到规则匹配)' : '命令台 LLM 意图解析')

  return (
    <div className="max-w-2xl space-y-3">
      {settingsDialog}
      <Panel title="执行开关">
        <div className="space-y-2">
          {TOGGLABLE_FLAGS.map((f) => {
            const on = flagOn(f.key)
            return (
              <div key={f.key} className="flex items-center justify-between gap-3 rounded-md border border-border bg-background px-3 py-2 text-sm">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 text-foreground">
                    {on ? <CheckCircle2 className="size-3.5 text-emerald-400" /> : <XCircle className="size-3.5 text-muted-foreground" />}
                    {f.label}
                  </div>
                  <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{f.desc}</div>
                </div>
                <Button size="sm" variant={on ? 'destructive' : 'default'} className="shrink-0"
                  onClick={() => void toggleFlag(f)} disabled={busy || !overview}>
                  {on ? '关闭' : '开启'}
                </Button>
              </div>
            )
          })}
        </div>
        <p className="mt-3 text-[11px] leading-4 text-muted-foreground">
          开启属升权操作,会弹确认并写清后果;关闭随时直接生效(任务不丢,只是停止领取)。
          每次翻转服务端都记录操作人。急停请用顶栏 Kill Switch。
        </p>
      </Panel>

      <Panel title="命令台 AI 解析">
        <div className="flex items-center justify-between rounded-md border border-border bg-background px-3 py-2 text-sm">
          <div className="min-w-0">
            <div className="text-foreground">LLM 意图解析</div>
            <div className="text-[11px] text-muted-foreground">
              模型 {chatLlmModel} · 复用平台密钥(密钥只在服务器,前端不配置)
            </div>
          </div>
          <Button size="sm" variant={chatLlmOn ? 'destructive' : 'default'} onClick={() => void toggleChatLlm()} disabled={busy || !overview}>
            {chatLlmOn ? '关闭' : '开启'}
          </Button>
        </div>
        <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
          开启后命令台每条消息先过一次 LLM 意图分类(更聪明地识别问数/修复/日报),
          LLM 只分类不改写你的原话;超时或失败自动回到规则匹配,命令台不会因此不可用。
          此开关只影响意图识别,不影响任务执行权限(执行仍受总开关与审批管控)。
        </p>
      </Panel>

      <Panel title="未具备执行条件 · 只读">
        <div className="space-y-2">
          {READONLY_FLAGS.map((f) => {
            const on = flagOn(f.key)
            return (
              <div key={f.key} className="flex items-center justify-between gap-3 rounded-md border border-border bg-background px-3 py-2 text-sm">
                <div className="min-w-0">
                  <div className="text-muted-foreground">{f.label}</div>
                  <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground/70">{f.reason}</div>
                </div>
                {on ? (
                  <span className="inline-flex shrink-0 items-center gap-1 text-emerald-400"><CheckCircle2 className="size-4" />开启</span>
                ) : (
                  <span className="inline-flex shrink-0 items-center gap-1 text-muted-foreground"><XCircle className="size-4" />关闭</span>
                )}
              </div>
            )
          })}
        </div>
      </Panel>
    </div>
  )
}

function SshRunnerSection({ overview, onOpenTask, refreshKey }: {
  overview: AiOpsOverview | null
  onOpenTask: (id: number) => void
  refreshKey: number
}) {
  const on = Boolean((overview?.policy?.['ssh_runner.enabled'] as { enabled?: boolean })?.enabled)
  return (
    <div className="space-y-3">
      <Panel title="SSH Runner · 受控生产执行器">
        <div className="flex items-center justify-between rounded-md border border-border bg-background px-3 py-2 text-sm">
          <span className="text-muted-foreground">ssh_runner.enabled</span>
          {on ? <span className="text-emerald-400">开启</span> : <span className="text-muted-foreground">关闭(默认)</span>}
        </div>
        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          生产 SSH 动作(重启 / 部署 / 回滚等)一律 <b className="text-foreground">L3/L4</b>,
          在下方审批队列人工放行后,才由独立 Runner 领取执行 · <b className="text-foreground">通过 ≠ 立即执行</b> ·
          实时执行明细待专用 Runner VM 就绪后接入。
        </p>
      </Panel>
      <ApprovalQueue onOpenTask={onOpenTask} refreshKey={refreshKey} />
    </div>
  )
}

export default function AiOpsCenter() {
  const navigate = useNavigate()
  const location = useLocation()
  const params = useParams<{ taskId?: string; date?: string }>()
  const { user } = useAuth()
  const adminName = user?.display_name || user?.username || 'admin'

  const [overview, setOverview] = useState<AiOpsOverview | null>(null)
  const [tasks, setTasks] = useState<AiOpsTask[]>([])
  const [generating, setGenerating] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  // 手机/窄屏(<1024)默认折叠左导航,避免占死 224px(PC 优先 + 移动 fallback)
  const [collapsed, setCollapsed] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1024)
  const [consoleOpen, setConsoleOpen] = useState(false)
  const [confirmDialog, confirm] = useConfirmDialog()
  const [activeSection, setActiveSection] = useState<SectionKey>(
    location.pathname.includes('/ai-ops/reports') ? 'reports' : 'overview',
  )
  const ovTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const taskTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadOverview = useCallback(async () => {
    try { setOverview(await getOverview()) } catch { /* transient */ }
  }, [])
  const loadTasks = useCallback(async () => {
    try { setTasks(await listTasks({ limit: 60 })) } catch { /* transient */ }
  }, [])

  useEffect(() => {
    void loadOverview(); void loadTasks()
    ovTimer.current = setInterval(() => { void loadOverview() }, 15000)
    taskTimer.current = setInterval(() => { void loadTasks() }, 10000)
    return () => {
      if (ovTimer.current) clearInterval(ovTimer.current)
      if (taskTimer.current) clearInterval(taskTimer.current)
    }
  }, [loadOverview, loadTasks])

  // URL ↔ 板块 双向同步(浏览器前进/后退、深链接进 /reports 时导航态不漂移)
  useEffect(() => {
    if (location.pathname.includes('/ai-ops/reports')) {
      setActiveSection('reports')
    } else {
      setActiveSection((s) => (s === 'reports' ? 'overview' : s))
    }
  }, [location.pathname])

  // ≥xl 右侧命令台常驻,窄屏 Sheet 自动关闭(防 resize 后残留全屏遮罩挡点击)
  useEffect(() => {
    if (!consoleOpen) return
    const mq = window.matchMedia('(min-width: 1280px)')
    if (mq.matches) { setConsoleOpen(false); return }
    const onChange = (e: MediaQueryListEvent) => { if (e.matches) setConsoleOpen(false) }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [consoleOpen])

  const openTask = (taskId: number) => navigate(`/admin/ai-ops/tasks/${taskId}`)
  const closeTask = () => navigate('/admin/ai-ops')

  const refreshAll = () => {
    void loadOverview(); void loadTasks(); setRefreshKey((k) => k + 1)
  }

  const toggleKill = async () => {
    const next = !overview?.health.kill_switch
    // 解除急停 = 恢复执行通道,高风险,必须二次确认(开启急停是安全动作,直接执行)
    if (!next) {
      const ok = await confirm({
        title: '解除 Kill Switch?',
        description: '解除后 Worker 可能恢复领取任务、SSH Runner 可能恢复执行已审批动作。确认解除?',
        confirmLabel: '确认解除',
        cancelLabel: '取消',
        danger: true,
        contentClassName: 'dark border-border bg-[#0e121b] text-foreground',
      })
      if (!ok) return
    }
    try {
      await setKillSwitch(next)
      toast[next ? 'error' : 'success'](next ? 'Kill Switch 已开启 · 执行全部冻结' : 'Kill Switch 已解除')
      await loadOverview()
    } catch (err) {
      toast.error('操作失败', { description: String((err as Error).message) })
    }
  }

  const onGenerate = async () => {
    setGenerating(true)
    try {
      await generateReport()
      toast.success('已触发生成日报')
      setTimeout(() => { void loadOverview() }, 800)
    } catch (err) {
      toast.error('生成失败', { description: String((err as Error).message) })
    } finally {
      setGenerating(false)
    }
  }

  const selectSection = (key: SectionKey) => {
    setActiveSection(key)
    // URL 规范化:报告中心有独立路径,其余板块都在基础路径(避免停留在 /reports 或 /tasks/:id 造成刷新后跳板块)
    const target = key === 'reports' ? '/admin/ai-ops/reports' : '/admin/ai-ops'
    if (location.pathname !== target) navigate(target)
  }

  const health = overview?.health
  const killOn = Boolean(health?.kill_switch)
  const attention = (health?.pending_approvals ?? 0) + (health?.open_bug_feedback ?? 0)
  const navCounts = useMemo<Partial<Record<SectionKey, number>>>(() => ({
    feedback: health?.open_bug_feedback,
    approvals: health?.pending_approvals,
    tasks: health?.tasks_running,
    monitoring: health?.alerts_firing,
  }), [health?.open_bug_feedback, health?.pending_approvals, health?.tasks_running, health?.alerts_firing])

  const renderMain = () => {
    // 日报详情:深链接优先,占据内容区
    if (params.date) {
      return (
        <div className="space-y-3">
          <button type="button" onClick={() => { setActiveSection('reports'); navigate('/admin/ai-ops/reports') }}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-3.5" />返回日报列表
          </button>
          <ReportViewer reportDate={params.date} />
        </div>
      )
    }
    switch (activeSection) {
      case 'overview':
        return (
          <div className="space-y-3">
            <HealthSummary overview={overview} />
            <div className="grid gap-3 xl:grid-cols-3">
              <div className="space-y-3">
                <FeedbackInbox key={refreshKey} onOpenTask={openTask} />
                <ApprovalQueue onOpenTask={openTask} refreshKey={refreshKey} />
              </div>
              <div className="space-y-3">
                <CodexTasksPanel tasks={tasks} onOpen={openTask} />
                <DailyReportPanel latestReport={overview?.latest_report ?? null} onGenerate={() => void onGenerate()} generating={generating} />
              </div>
              <div className="space-y-3">
                <ProductionStatusPanel overview={overview} />
                <FinanceRiskPanel />
              </div>
            </div>
          </div>
        )
      case 'feedback':
        return <div className="max-w-2xl"><FeedbackInbox key={refreshKey} onOpenTask={openTask} max={20} /></div>
      case 'tasks':
        return <div className="max-w-3xl"><CodexTasksPanel tasks={tasks} onOpen={openTask} /></div>
      case 'approvals':
        return <div className="max-w-2xl"><ApprovalQueue onOpenTask={openTask} refreshKey={refreshKey} /></div>
      case 'production':
        return <div className="max-w-2xl"><ProductionStatusPanel overview={overview} /></div>
      case 'ssh_runner':
        return <div className="max-w-2xl"><SshRunnerSection overview={overview} onOpenTask={openTask} refreshKey={refreshKey} /></div>
      case 'finance':
        return <div className="max-w-2xl"><FinanceRiskPanel /></div>
      case 'reports':
        return <div className="max-w-3xl"><ReportsList key={refreshKey} /></div>
      case 'settings':
        return <SettingsSection overview={overview} onChanged={() => void loadOverview()} />
      case 'changes':
        return <ComingSoon title="变更管理" desc="配置/流控变更编排暂无对应真实接口。当前变更仍走审批中心 L3/L4 人工放行。" />
      case 'deploy':
        return <ComingSoon title="部署管理" desc="蓝绿部署/切流由 Deploy-CTO 在生产 SSH 执行,控制塔尚未接入部署编排接口。" />
      case 'monitoring':
        return <MonitoringPanel overview={overview} onOpenTask={openTask} refreshKey={refreshKey} />
      case 'logs':
        return <ComingSoon title="日志中心" desc="集中日志检索接口待接入。任务级日志见每个任务详情的事件流与产物。" />
      case 'cost':
        return <ComingSoon title="成本中心" desc="LLM/算力成本核算接口待接入,不在此展示估算数字以免误导。" />
      case 'kb':
        return <ComingSoon title="知识库" desc="运维知识库检索待接入。" />
      default:
        return null
    }
  }

  return (
    <div className="dark flex h-screen overflow-hidden bg-[#0b0e14] text-foreground">
      <AiOpsNav
        active={activeSection}
        onSelect={selectSection}
        counts={navCounts}
        collapsed={collapsed}
        onToggleCollapse={() => setCollapsed((c) => !c)}
        onExit={() => navigate('/')}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onRefresh={refreshAll} killOn={killOn} onToggleKill={() => void toggleKill()} attention={attention} onOpenConsole={() => setConsoleOpen(true)} />

        <div className="border-b border-border bg-[#0e121b] px-3 py-2 lg:hidden">
          <label htmlFor="aiops-section" className="sr-only">选择运维板块</label>
          <select
            id="aiops-section"
            value={activeSection}
            onChange={(event) => selectSection(event.target.value as SectionKey)}
            className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground"
          >
            {NAV_ITEMS.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}{item.live ? '' : '（待接入）'}
              </option>
            ))}
          </select>
        </div>

        {killOn && (
          <div className="border-b border-rose-500/40 bg-rose-500/10 px-4 py-2 text-xs text-rose-300">
            Kill Switch 已开启:Worker 不再领取新任务,SSH Runner 不执行任何已审批动作。
          </div>
        )}
        {/* 总开关未启用 = 「命令台/反馈建的任务为什么一直排队不动」的根因,必须全局明示 */}
        {!killOn && overview != null && !overview.health.ai_ops_enabled && (
          <div className="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
            AI 运维总开关未启用(默认保守关):新建的诊断/修复任务会排队保留、不会自动执行,每日定时日报也暂停;启用后 AI 才开始处理。手动日报和问数不受影响。可在「系统设置」开启。
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          <main className="min-w-0 flex-1 overflow-y-auto p-4">
            {renderMain()}
          </main>
          <aside className="hidden w-[340px] shrink-0 border-l border-border xl:block">
            <AiCommandConsole onOpenTask={openTask} recentTasks={tasks} adminName={adminName} />
          </aside>
        </div>
      </div>

      {/* NaN 守卫:/tasks/abc 之类非数字 id 不开抽屉(否则永远「加载中」+ 轮询报错) */}
      <TaskDetailDrawer
        taskId={params.taskId && Number.isFinite(Number(params.taskId)) ? Number(params.taskId) : null}
        onClose={closeTask}
        aiOpsEnabled={overview ? overview.health.ai_ops_enabled : null}
      />

      {/* 窄屏(<xl · 右侧固定命令台被隐藏)入口:右侧 Sheet 打开同一个命令台。
          注意不要给 SheetContent 加 xl:hidden —— overlay 是兄弟节点不会跟着隐藏,
          resize 到 xl 会留下全屏遮罩挡点击;xl 自动关由上面的 matchMedia effect 负责 */}
      <Sheet open={consoleOpen} onOpenChange={setConsoleOpen}>
        <SheetContent side="right" aria-label="AI 命令台"
          className="dark w-[92vw] max-w-[360px] border-border bg-[#0e121b] p-0">
          <AiCommandConsole
            onOpenTask={(id) => { setConsoleOpen(false); openTask(id) }}
            recentTasks={tasks}
            adminName={adminName}
          />
        </SheetContent>
      </Sheet>

      {confirmDialog}
    </div>
  )
}
