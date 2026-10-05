// 营销军师控制台 · 页面外壳(主题感知全屏 · 自带 MarketingNav chrome)
// 路由:/admin/marketing[/:section][ /cases/:caseId ] · admin-only(App.tsx requiredModule="users")
import { useCallback, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MotionConfig } from 'framer-motion'
import { cn } from '@/lib/utils'
import type { SectionKey } from './types'
import { MarketingNav } from './components/MarketingNav'
import { Overview } from './components/Overview'
import { Cases } from './components/Cases'
import { ApprovalDetail } from './components/ApprovalDetail'
import { Campaigns } from './components/Campaigns'
import { Levers } from './components/Levers'
import { Effect } from './components/Effect'
import { Settings } from './components/Settings'
import { MaterialStudio } from './components/MaterialStudio'

const VALID: SectionKey[] = ['today', 'cases', 'campaigns', 'levers', 'materials', 'effect', 'settings']

export default function MarketingAdvisor() {
  const navigate = useNavigate()
  const params = useParams<{ section?: string; caseId?: string }>()
  const section = (VALID.includes(params.section as SectionKey) ? params.section : 'today') as SectionKey
  const [collapsed, setCollapsed] = useState(false)
  const [openCaseId, setOpenCaseId] = useState<number | null>(
    params.caseId ? Number(params.caseId) : null,
  )
  // 强制刷新子区(审批后)
  const [refreshTick, setRefreshTick] = useState(0)

  const goSection = useCallback((s: SectionKey) => {
    setOpenCaseId(null)
    navigate(`/admin/marketing/${s}`)
  }, [navigate])

  const openCase = useCallback((id: number) => {
    setOpenCaseId(id)
    navigate('/admin/marketing/cases')
  }, [navigate])

  const content = useMemo(() => {
    if (openCaseId != null) {
      return (
        <ApprovalDetail
          caseId={openCaseId}
          onBack={() => setOpenCaseId(null)}
          onChanged={() => setRefreshTick((t) => t + 1)}
        />
      )
    }
    switch (section) {
      case 'today': return <Overview onOpenCase={openCase} onGotoMaterials={() => goSection('materials')} />
      case 'cases': return <Cases key={refreshTick} onOpenCase={openCase} />
      case 'campaigns': return <Campaigns />
      case 'levers': return <Levers />
      case 'materials': return <MaterialStudio mode="admin" />
      case 'effect': return <Effect />
      case 'settings': return <Settings />
      default: return <Overview onOpenCase={openCase} onGotoMaterials={() => goSection('materials')} />
    }
  }, [section, openCaseId, refreshTick, openCase, goSection])

  return (
    // [返工 R7] MotionConfig reducedMotion="user":全站营销动效真正尊重系统"减少动态效果"设置
    <MotionConfig reducedMotion="user">
      <div className="flex h-screen overflow-hidden bg-background text-foreground">
        <MarketingNav
          active={section}
          onChange={goSection}
          collapsed={collapsed}
          onToggle={() => setCollapsed((c) => !c)}
        />
        <main className={cn('flex-1 overflow-y-auto')}>
          <div className="mx-auto max-w-[1400px] p-4 sm:p-6">{content}</div>
        </main>
      </div>
    </MotionConfig>
  )
}
