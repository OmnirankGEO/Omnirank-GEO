// 营销军师 · 方案审批详情(三栏:方案概览 / 内容预览与编辑 / 审批检查清单)
// 参考图 03。自取 fetchCase(caseId) → { case, checks }。文案编辑仅本地预览(v1 不持久化)。
// 主题感知:仅用语义 token(bg-card / text-foreground / border-border / text-brand …),无内联样式、无硬编码 hex。
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ArrowLeft, CalendarClock, CheckCircle2, History, ImageIcon,
  RotateCcw, ShieldAlert, Target, Users, Wallet, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import type { MarketingCase, ChecksResult } from '../types'
import { approveCase, fetchCase, rejectCase, requestChanges } from '../api'
import {
  CaseStatusBadge, EmptyState, ErrorState, FadeIn, LoadingState,
  Panel, PowerAndYuan, RiskBadge,
} from './ui'

// 触达渠道 → 中文标签(只渲染方案里真实存在的渠道)
const CHANNEL_LABEL: Record<string, string> = {
  station: '站内信文案',
  wecom: '企业微信文案',
  sms: '短信文案',
  email: '邮件文案',
}

function fmtDate(s?: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString('zh-CN')
}

function daysBetween(from: string, to?: string | null): number | null {
  if (!to) return null
  const d = Math.round((new Date(to).getTime() - new Date(from).getTime()) / 86400000)
  return Number.isFinite(d) ? Math.max(1, d) : null
}

// 左栏信息卡(标签 + 图标 + 内容)
function InfoCard({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        <span>{label}</span>
      </div>
      <div className="text-sm text-foreground">{children}</div>
    </div>
  )
}

export function ApprovalDetail({ caseId, onBack, onChanged }: {
  caseId: number
  onBack: () => void
  onChanged: () => void
}) {
  const [data, setData] = useState<{ case: MarketingCase; checks: ChecksResult } | null>(null)
  const [copies, setCopies] = useState<Record<string, string>>({})
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 独立确认实例(命名 askConfirm,防遮蔽全局 confirm)
  const [confirmDialog, askConfirm] = useConfirmDialog()

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetchCase(caseId)
      setData(res)
      setCopies({ ...res.case.touch_copy_jsonb })
    } catch (err) {
      setError(String((err as Error).message))
    } finally {
      setLoading(false)
    }
  }, [caseId])

  useEffect(() => { void load() }, [load])

  const backBtn = (
    <Button variant="ghost" size="sm" onClick={onBack} className="gap-1 text-muted-foreground">
      <ArrowLeft className="size-4" /> 返回
    </Button>
  )

  if (loading) {
    return <div className="p-4">{confirmDialog}<div className="mb-3">{backBtn}</div><LoadingState /></div>
  }
  if (error) {
    return <div className="p-4">{confirmDialog}<div className="mb-3">{backBtn}</div><ErrorState text={error} onRetry={() => void load()} /></div>
  }
  if (!data) {
    return (
      <div className="p-4">
        {confirmDialog}
        <div className="mb-3">{backBtn}</div>
        <EmptyState title="方案不存在或已被移除" hint="返回列表选择其它待审批方案。" />
      </div>
    )
  }

  const c = data.case
  const checks = data.checks
  const validDays = daysBetween(c.created_at, c.expires_at)
  const channels = Object.keys(copies).filter((k) => k in c.touch_copy_jsonb)
  // 仅 pending / 待修改 状态可执行审批动作(已执行/已驳回等只读)
  const actionable = c.status === 'pending' || c.status === 'changes_requested'

  const runAction = async (
    fn: (id: number, note: string) => Promise<{ ok: boolean }>,
    okMsg: string,
    failMsg: string,
  ) => {
    setBusy(true)
    try {
      await fn(caseId, note)
      toast.success(okMsg)
      onChanged()
      onBack()
    } catch (err) {
      // approve 检查未过会返回 422,detail 透传到 message
      toast.error(failMsg, { description: String((err as Error).message) })
    } finally {
      setBusy(false)
    }
  }

  const doApprove = async () => {
    const ok = await askConfirm({
      title: '批准执行该方案?',
      description: '批准后系统将按方案对目标人群发起触达,执行后不可直接撤回。请确认所有检查项已逐一核对。',
      confirmLabel: '批准执行',
    })
    if (!ok) return
    await runAction(approveCase, '已批准 · 方案进入执行', '批准失败')
  }

  const restore = (ch: string) =>
    setCopies((prev) => ({ ...prev, [ch]: c.touch_copy_jsonb[ch] ?? '' }))

  return (
    <div className="p-4">
      {confirmDialog}
      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)_360px]">

        {/* ============ 左栏:方案概览 ============ */}
        <FadeIn className="space-y-3">
          <Panel
            title="方案概览"
            action={backBtn}
          >
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm font-semibold tabular-nums text-foreground">{c.case_no}</span>
              <CaseStatusBadge status={c.status} />
            </div>

            <div className="space-y-2.5">
              <InfoCard icon={<Users className="size-3.5" />} label="目标人群">
                <div>{c.audience_jsonb.definition || '未指定人群'}</div>
                <div className="mt-1 text-xs text-muted-foreground tabular-nums">
                  约 {c.audience_jsonb.count.toLocaleString()} 人
                  {c.audience_jsonb.segment ? ` · ${c.audience_jsonb.segment}` : ''}
                </div>
              </InfoCard>

              <InfoCard icon={<Target className="size-3.5" />} label="预计触达 · 影响面">
                <div className="tabular-nums">{c.audience_jsonb.count.toLocaleString()} 人可触达</div>
                {c.expected_impact && (
                  <div className="mt-1 text-xs text-muted-foreground">{c.expected_impact}</div>
                )}
              </InfoCard>

              <InfoCard icon={<Wallet className="size-3.5" />} label="预算上限">
                <PowerAndYuan points={c.budget_cost_jsonb.points} />
                {c.budget_cost_jsonb.note && (
                  <div className="mt-1 text-xs text-muted-foreground">{c.budget_cost_jsonb.note}</div>
                )}
              </InfoCard>

              <InfoCard icon={<ShieldAlert className="size-3.5" />} label="风险等级 · 涉及权益">
                <RiskBadge risk={c.risk_level} />
                <div className="mt-1.5 text-xs text-muted-foreground">
                  {c.budget_cost_jsonb.points > 0
                    ? `涉及算力发放(上限见预算),观察窗口 ${c.observation_window_days} 天`
                    : `仅触达通知,不涉及算力发放,观察窗口 ${c.observation_window_days} 天`}
                </div>
                {c.skill_packs_jsonb.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {c.skill_packs_jsonb.map((sp) => (
                      <span key={sp} className="rounded border border-border bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                        {sp}
                      </span>
                    ))}
                  </div>
                )}
              </InfoCard>

              <InfoCard icon={<CalendarClock className="size-3.5" />} label="方案有效期">
                <div className="text-xs text-muted-foreground tabular-nums">
                  {fmtDate(c.created_at)} → {fmtDate(c.expires_at)}
                </div>
                <div className="mt-1 tabular-nums">
                  {validDays != null ? `共 ${validDays} 天` : '未设截止日'}
                </div>
              </InfoCard>

              <InfoCard icon={<History className="size-3.5" />} label="历史方案参考">
                <div className="text-xs text-muted-foreground">
                  暂无同人群历史方案。积累若干轮后,这里会展示过往触达与相关转化,供你对照决策。
                </div>
              </InfoCard>
            </div>
          </Panel>
        </FadeIn>

        {/* ============ 中栏:内容预览与编辑 ============ */}
        <FadeIn delay={0.05} className="space-y-3">
          <Panel title="方案内容预览与编辑">
            {/* 海报占位 */}
            <div className="mb-4 flex aspect-[16/9] w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/40 text-muted-foreground">
              <ImageIcon className="size-8 opacity-60" />
              <span className="text-xs">海报预览(方案生成配图后展示)</span>
            </div>

            {channels.length === 0 ? (
              <EmptyState title="暂无触达文案" hint="该方案未包含任何渠道文案,无需在此编辑。" />
            ) : (
              <div className="space-y-4">
                {channels.map((ch) => {
                  const val = copies[ch] ?? ''
                  const original = c.touch_copy_jsonb[ch] ?? ''
                  return (
                    <div key={ch}>
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        <label className="text-sm font-medium text-foreground">
                          {CHANNEL_LABEL[ch] ?? ch}
                        </label>
                        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                          <span className="tabular-nums">{val.length} 字</span>
                          <button
                            type="button"
                            disabled={val === original}
                            onClick={() => restore(ch)}
                            className={cn(
                              'inline-flex items-center gap-1 hover:text-foreground',
                              val === original && 'cursor-default opacity-40 hover:text-muted-foreground',
                            )}
                          >
                            <RotateCcw className="size-3" /> 恢复默认
                          </button>
                        </div>
                      </div>
                      <textarea
                        value={val}
                        onChange={(e) => setCopies((prev) => ({ ...prev, [ch]: e.target.value }))}
                        rows={4}
                        className="w-full resize-y rounded-md border border-border bg-background p-2.5 text-sm text-foreground outline-none focus:ring-1 focus:ring-brand"
                      />
                    </div>
                  )
                })}
                <p className="text-[11px] leading-4 text-muted-foreground">
                  文案编辑仅为本地预览,当前版本不会保存改动;正式内容以方案原文为准。
                </p>
              </div>
            )}

            <p className="mt-4 rounded-md border border-border bg-muted/40 px-3 py-2 text-[11px] leading-4 text-muted-foreground">
              所有文案内容需合规,不得包含虚假承诺或诱导性信息。
            </p>
          </Panel>
        </FadeIn>

        {/* ============ 右栏:审批检查清单 ============ */}
        <FadeIn delay={0.1} className="space-y-3">
          <Panel title="审批检查清单">
            <p className="mb-3 text-xs text-muted-foreground">请逐项确认,确保方案合规可执行。</p>

            {checks.checks.length === 0 ? (
              <EmptyState title="暂无检查项" hint="该方案未生成合规检查项,请谨慎人工核对后再决定。" />
            ) : (
              <ul className="space-y-2.5">
                {checks.checks.map((item) => (
                  <li key={item.key} className="flex gap-2">
                    {item.passed
                      ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-brand" />
                      : <XCircle className="mt-0.5 size-4 shrink-0 text-rose-500" />}
                    <div className="min-w-0">
                      <div className="text-sm font-semibold text-foreground">{item.label}</div>
                      {item.detail && (
                        <div className="text-xs text-muted-foreground">{item.detail}</div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}

            <div className="mt-4">
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-sm font-medium text-foreground">审批备注</label>
                <span className="text-[11px] text-muted-foreground tabular-nums">{note.length}/200</span>
              </div>
              <textarea
                value={note}
                maxLength={200}
                onChange={(e) => setNote(e.target.value)}
                rows={3}
                placeholder="填写审批意见(要求修改 / 驳回时建议说明原因)"
                className="w-full resize-y rounded-md border border-border bg-background p-2.5 text-sm text-foreground outline-none focus:ring-1 focus:ring-brand"
              />
            </div>

            {!actionable && (
              <p className="mt-3 text-[11px] leading-4 text-muted-foreground">
                该方案当前状态不可再审批(仅「需人工审批 / 待修改」可操作)。
              </p>
            )}

            <div className="mt-4 space-y-2">
              <div>
                <Button
                  className="w-full"
                  disabled={!checks.all_passed || busy || !actionable}
                  onClick={() => void doApprove()}
                >
                  批准执行
                </Button>
                <p className="mt-1 text-center text-[11px] text-muted-foreground">
                  {checks.all_passed ? '执行后不可直接撤回' : '存在未通过的检查项,暂不可批准'}
                </p>
              </div>

              <Button
                variant="outline"
                className="w-full border-amber-500/40 text-amber-600 hover:bg-amber-500/10 hover:text-amber-600"
                disabled={busy || !actionable}
                onClick={() => void runAction(requestChanges, '已要求修改', '操作失败')}
              >
                要求修改
              </Button>

              <Button
                variant="destructive"
                className="w-full"
                disabled={busy || !actionable}
                onClick={() => void runAction(rejectCase, '已驳回该方案', '驳回失败')}
              >
                驳回
              </Button>
            </div>
          </Panel>
        </FadeIn>
      </div>
    </div>
  )
}
