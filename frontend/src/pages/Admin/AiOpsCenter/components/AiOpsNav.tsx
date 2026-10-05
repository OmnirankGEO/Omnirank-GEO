// AI 运维控制塔 · 左侧独立导航
// live=有真实接口的板块;soon=无真实后端 → 内容区显示「待接入」占位(不伪造数据)。

import type { LucideIcon } from 'lucide-react'
import {
  BellRing, BookOpen, Bot, ClipboardCheck, Coins, FileText, GitBranch,
  LayoutDashboard, LogOut, PanelLeftClose, PanelLeftOpen, Rocket, ScrollText,
  Server, Settings, ShieldAlert, Terminal, MessageSquareWarning,
} from 'lucide-react'

export type SectionKey =
  | 'overview' | 'feedback' | 'tasks' | 'production' | 'changes'
  | 'approvals' | 'ssh_runner' | 'deploy' | 'monitoring' | 'logs'
  | 'cost' | 'finance' | 'kb' | 'reports' | 'settings'

type NavItem = { key: SectionKey; label: string; icon: LucideIcon; live: boolean }

export const NAV_ITEMS: NavItem[] = [
  { key: 'overview', label: '总览看板', icon: LayoutDashboard, live: true },
  { key: 'feedback', label: '问题反馈', icon: MessageSquareWarning, live: true },
  { key: 'tasks', label: 'Codex 任务', icon: Bot, live: true },
  { key: 'production', label: '生产状态', icon: Server, live: true },
  { key: 'changes', label: '变更管理', icon: GitBranch, live: false },
  { key: 'approvals', label: '审批中心', icon: ClipboardCheck, live: true },
  { key: 'ssh_runner', label: 'SSH Runner', icon: Terminal, live: true },
  { key: 'deploy', label: '部署管理', icon: Rocket, live: false },
  { key: 'monitoring', label: '监控告警', icon: BellRing, live: true },
  { key: 'logs', label: '日志中心', icon: ScrollText, live: false },
  { key: 'cost', label: '成本中心', icon: Coins, live: false },
  { key: 'finance', label: '资金安全', icon: ShieldAlert, live: true },
  { key: 'kb', label: '知识库', icon: BookOpen, live: false },
  { key: 'reports', label: '报告中心', icon: FileText, live: true },
  { key: 'settings', label: '系统设置', icon: Settings, live: true },
]

export function AiOpsNav({ active, onSelect, counts, collapsed, onToggleCollapse, onExit }: {
  active: SectionKey
  onSelect: (key: SectionKey) => void
  counts: Partial<Record<SectionKey, number>>
  collapsed: boolean
  onToggleCollapse: () => void
  onExit: () => void
}) {
  return (
    <nav className={`hidden shrink-0 flex-col border-r border-border bg-[#0e121b] lg:flex ${collapsed ? 'w-14' : 'w-56'}`}>
      {/* 品牌 */}
      <div className={`flex h-14 items-center gap-2 border-b border-border px-3 ${collapsed ? 'justify-center' : ''}`}>
        <span className="grid size-7 shrink-0 place-items-center rounded-md bg-primary/90 text-primary-foreground">
          <Bot className="size-4" />
        </span>
        {!collapsed && <span className="truncate text-sm font-semibold text-foreground">AI 运维控制塔</span>}
      </div>

      {/* 导航项 */}
      <div className="min-h-0 flex-1 overflow-y-auto py-2">
        {NAV_ITEMS.map((it) => {
          const Icon = it.icon
          const isActive = active === it.key
          const badge = counts[it.key]
          return (
            <button
              key={it.key}
              type="button"
              title={collapsed ? it.label : undefined}
              onClick={() => onSelect(it.key)}
              className={[
                'group relative flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors',
                collapsed ? 'justify-center' : '',
                isActive
                  ? 'bg-primary/10 font-medium text-foreground'
                  : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
              ].join(' ')}
            >
              {isActive && <span className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-primary" />}
              <Icon className="size-4 shrink-0" />
              {!collapsed && <span className="min-w-0 flex-1 truncate">{it.label}</span>}
              {!collapsed && !it.live && (
                <span className="shrink-0 rounded border border-border px-1 text-[10px] text-muted-foreground">待接入</span>
              )}
              {!collapsed && it.live && typeof badge === 'number' && badge > 0 && (
                <span className="shrink-0 rounded-full bg-amber-500/20 px-1.5 text-[10px] font-medium text-amber-400">{badge}</span>
              )}
            </button>
          )
        })}
      </div>

      {/* 底部:折叠 + 返回 */}
      <div className="border-t border-border p-2">
        <button
          type="button" onClick={onToggleCollapse}
          className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted-foreground hover:bg-muted/60 hover:text-foreground ${collapsed ? 'justify-center' : ''}`}
        >
          {collapsed ? <PanelLeftOpen className="size-4" /> : <><PanelLeftClose className="size-4" />收起菜单</>}
        </button>
        <button
          type="button" onClick={onExit} title="返回主后台"
          className={`mt-1 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-muted-foreground hover:bg-muted/60 hover:text-foreground ${collapsed ? 'justify-center' : ''}`}
        >
          <LogOut className="size-4" />{!collapsed && '返回主后台'}
        </button>
      </div>
    </nav>
  )
}
