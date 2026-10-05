// 资金红线:复用现有财务口径(待接入)。红线动作(退款/钱包/结算/提现)一律 L4 人工审批,无自动执行。

import { ArrowUpRight, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Panel } from './shared'

export function FinanceRiskPanel() {
  const links: { to: string; label: string }[] = [
    { to: '/admin/finance', label: '财务中心' },
    { to: '/admin/refunds', label: '充值退款工单' },
    { to: '/admin/withdrawals', label: '提现审核' },
    { to: '/admin/settlements', label: '服务商打款' },
  ]
  return (
    <Panel title="资金红线">
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 text-xs leading-5">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
          <span className="text-muted-foreground">
            退款 / 钱包调账 / 服务商结算 / 提现 全部为 <b className="text-foreground">L4 高危</b>,
            AI 只能诊断和生成方案,<b className="text-foreground">必须人工审批,无自动执行</b>。
          </span>
        </div>
        <p className="text-[11px] text-muted-foreground">
          资金异常明细复用现有财务中心口径(cents · COALESCE(paid_at,created_at) · reversed_at IS NULL),日报待接入。
        </p>
        <div className="grid grid-cols-2 gap-2">
          {links.map((l) => (
            <Link key={l.to} to={l.to}
              className="inline-flex items-center justify-between rounded-md border bg-background px-3 py-1.5 text-xs hover:bg-muted/50">
              {l.label}
              <ArrowUpRight className="size-3.5 text-muted-foreground" />
            </Link>
          ))}
        </div>
      </div>
    </Panel>
  )
}
