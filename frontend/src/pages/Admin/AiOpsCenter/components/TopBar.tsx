// 顶栏:环境(真实 host)· 实时时钟 · 刷新 · Kill Switch · 待办数(真实)· 管理员名(真实)
// 诚实原则:不伪造"生产/预发"环境标签(前端无从判定),用真实 window.location.host;
// 通知红点用真实的(待审批 + 未处理反馈)之和,不伪造 12。

import { useEffect, useState } from 'react'
import { Bell, HelpCircle, Power, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/context/AuthContext'

function useClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  const p = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`
}

export function TopBar({ onRefresh, killOn, onToggleKill, attention, onOpenConsole }: {
  onRefresh: () => void
  killOn: boolean
  onToggleKill: () => void
  attention: number
  onOpenConsole: () => void
}) {
  const clock = useClock()
  const { user } = useAuth()
  const adminName = user?.display_name || user?.username || 'admin'
  const host = typeof window !== 'undefined' ? window.location.host : ''

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-[#0e121b] px-4">
      <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground">
        <span className="size-1.5 rounded-full bg-emerald-400" />
        环境 · <span className="text-foreground">{host || '—'}</span>
      </div>
      <div className="hidden text-xs text-muted-foreground sm:block">
        时间 · <span className="tabular-nums text-foreground">{clock}</span>
      </div>

      <div className="ml-auto flex items-center gap-2">
        {/* 窄屏(<xl 右侧命令台被隐藏)入口:打开命令台抽屉 */}
        <Button variant="outline" size="sm" className="xl:hidden" onClick={onOpenConsole}>
          <Sparkles className="mr-1 size-3.5" />命令台
        </Button>
        <Button variant="outline" size="sm" onClick={onRefresh}>
          <RefreshCw className="mr-1 size-3.5" />刷新
        </Button>
        <Button variant={killOn ? 'destructive' : 'outline'} size="sm" onClick={onToggleKill}>
          {killOn ? <ShieldAlert className="mr-1 size-3.5" /> : <Power className="mr-1 size-3.5" />}
          {killOn ? '解除急停' : 'Kill Switch'}
        </Button>

        <button type="button" className="relative grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted/60 hover:text-foreground" title="待处理事项">
          <Bell className="size-4" />
          {attention > 0 && (
            <span className="absolute -right-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-rose-500 px-1 text-[10px] font-medium text-white">
              {attention > 99 ? '99+' : attention}
            </span>
          )}
        </button>
        <span className="grid size-8 place-items-center rounded-md text-muted-foreground" title="帮助">
          <HelpCircle className="size-4" />
        </span>

        <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1">
          <span className="grid size-6 place-items-center rounded-full bg-primary/90 text-[11px] font-semibold text-primary-foreground">
            {adminName.slice(0, 1).toUpperCase()}
          </span>
          <span className="max-w-24 truncate text-xs text-foreground">{adminName}</span>
        </div>
      </div>
    </header>
  )
}
