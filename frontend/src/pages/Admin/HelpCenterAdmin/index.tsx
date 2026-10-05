// [2026-05-18 帮助中心 Phase 3 v2] 管理员主页 /admin/help-center
// 两 tab: 反馈收件箱 + FAQ 内容(分组卡片版)
// 编辑器拆成独立路由 /admin/help-center/faq/:id · 不再 Sheet 抽屉
// Tab 状态用 URL ?tab= 持久化 · 从编辑页 navigate(-1) 回来还在原 tab

import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AlertTriangle, LifeBuoy, Loader2, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { getFeedbackCounts, getReindexStatus, rebuildXiaobangKB } from './api'
import type { ReindexStatus } from './api'
import { FAQItemsTab } from './FAQItemsTab'
import { FAQFeedbackTab } from './FAQFeedbackTab'

type TabValue = 'items' | 'feedback'

export default function HelpCenterAdmin() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const activeTab: TabValue = tabParam === 'items' ? 'items' : 'feedback'
  const [pendingCount, setPendingCount] = useState(0)
  const [rebuilding, setRebuilding] = useState(false)
  const [kbStatus, setKbStatus] = useState<ReindexStatus | null>(null)

  useEffect(() => {
    getFeedbackCounts('bug')
      .then((c) => setPendingCount(c.pending ?? 0))
      .catch(() => {
        /* 失败不打扰 */
      })
  }, [])

  // [WO-A ② · 2026-08-20] 索引更新状态位。
  // 为什么要轮询:FAQ 改完之后的重建是后台跑的,接口返 200 只代表「FAQ 存下了」,
  // 不代表小榜下一句会照着新答案念。失败以前只落一条没人查的日志。
  const refreshKbStatus = useCallback(() => {
    getReindexStatus()
      .then(setKbStatus)
      .catch(() => setKbStatus(null))
  }, [])

  useEffect(() => {
    refreshKbStatus()
    const timer = window.setInterval(refreshKbStatus, 20000)
    return () => window.clearInterval(timer)
  }, [refreshKbStatus])

  const setTab = (next: TabValue) => {
    const sp = new URLSearchParams(searchParams)
    if (next === 'feedback') sp.delete('tab')
    else sp.set('tab', next)
    setSearchParams(sp, { replace: true })
  }

  // 2026-05-25 小榜知识库手动重建 · 改完 docs-data.ts / preset 后兜底刷一次
  // FAQ CRUD 已经自动 reindex,这个按钮主要给 docs / preset 改了之后用
  const handleRebuildKB = async () => {
    if (rebuilding) return
    setRebuilding(true)
    try {
      const result = await rebuildXiaobangKB('all')
      toast.success(
        `小榜知识库已重建 · 文档 ${result.doc ?? 0} + FAQ ${result.faq ?? 0} + 预设 ${result.preset ?? 0}`,
      )
    } catch (e) {
      toast.error('重建失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setRebuilding(false)
      refreshKbStatus()
    }
  }

  // 只在「有话要说」时出现:一切正常时不占地方(空状态不提示)。
  const kbBadge = (() => {
    if (!kbStatus) return null
    if (kbStatus.state === 'running') {
      return (
        <Badge variant="secondary" className="gap-1.5" title="小榜知识库正在更新,稍等几秒再问它">
          <Loader2 className="size-3 animate-spin" />
          索引更新中
        </Badge>
      )
    }
    if (kbStatus.state === 'failed') {
      const first = kbStatus.failures[0]
      return (
        <Badge
          variant="destructive"
          className="gap-1.5"
          title={first ? `${first.title} · ${first.detail}` : '索引更新失败'}
        >
          <AlertTriangle className="size-3" />
          索引更新失败
        </Badge>
      )
    }
    if (kbStatus.state === 'unknown') {
      return (
        <Badge variant="outline" className="gap-1.5" title="读不到索引状态(告警表不可用)">
          索引状态未知
        </Badge>
      )
    }
    return null
  })()

  return (
    <div className="min-h-full bg-background">
      <div className="mx-auto w-full max-w-6xl px-4 py-6 md:px-8 md:py-8">
        {/* 标题 + 重建按钮 */}
        <div className="mb-6 flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <LifeBuoy className="size-5" />
            </span>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight text-foreground md:text-3xl">
                帮助中心管理
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">
                收用户问题反馈 + 管 FAQ 内容
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {kbBadge}
            <Button
              variant="outline"
              size="sm"
              onClick={handleRebuildKB}
              disabled={rebuilding}
              title="改完 FAQ / 帮助文档 / 预设答案后手动刷一次小榜的知识库 · FAQ CRUD 已经自动刷,这个按钮主要给改 docs-data.ts 后用"
            >
              <RefreshCw className={rebuilding ? 'size-3.5 animate-spin' : 'size-3.5'} />
              {rebuilding ? '重建中...' : '重建小榜知识库'}
            </Button>
          </div>
        </div>

        <Tabs value={activeTab} onValueChange={(v) => setTab(v as TabValue)}>
          <TabsList>
            <TabsTrigger value="feedback" className="gap-1.5">
              用户反馈
              {pendingCount > 0 && (
                <Badge variant="destructive" className="h-5 min-w-[20px] px-1.5 text-[10px]">
                  {pendingCount}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value="items">FAQ 内容</TabsTrigger>
          </TabsList>

          <TabsContent value="feedback" className="mt-6">
            <FAQFeedbackTab onCountsLoaded={setPendingCount} />
          </TabsContent>

          <TabsContent value="items" className="mt-6">
            <FAQItemsTab />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
