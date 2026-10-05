import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  BadgeDollarSign,
  BarChart3,
  BookOpen,
  Check,
  ChevronDown,
  CircleAlert,
  Clock,
  Download,
  Flame,
  History,
  Image as ImageIcon,
  Info,
  LayoutGrid,
  Loader2,
  MessagesSquare,
  Pencil,
  QrCode,
  RefreshCw,
  Send,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  Users,
  WandSparkles,
  X,
  Zap,
} from 'lucide-react'
import { toast } from 'sonner'

import { authFetch } from '@/lib/api'
import { useAuth } from '@/context/AuthContext'
// [WO-B ① 2026-08-20 · 规格 §12.3]「GEO 图文页需补真实 intent 解析,
// 不能继续打开空白默认表单」。这一页在本包之前**一个 URL 参数都不读**
// (`grep -n useSearchParams` 零命中)—— 从小榜深链进来只能看到默认空表。
import { XiaobangPrefillRegion } from '@/components/xiaobang/XiaobangPrefillRegion'
import { useXiaobangPrefill } from '@/hooks/useXiaobangPrefill'
import DealStudio from './DealStudio'
import {
  CHANNELS,
  STATUS_TEXT,
  ChannelSection,
  WarningsBar,
  assetSlot,
  assetUrl,
  downloadText,
  editErrorMessage,
  errorMessage,
  flattenCopy,
  isShowcaseDealJob,
  parseCopy,
  payloadHash,
  readJson,
  readPendingRequest,
  readPendingRetry,
  requestId,
  type ChannelId,
  type JobState,
  type JobWarning,
  type PendingRequest,
  type PendingRetry,
  type Strategy,
  type Teacher,
} from './geoChannelShared'
import { usePricing } from '@/context/PricingContext'
// [#65] code 由分辨率决定 —— 规则单独成纯模块,判据才锁得住**行为**而不只是常量。
import { bundleFeatureCode } from '@/lib/marketingBundlePricing'
import { FeatureCostBadge } from '@/components/FeatureCostBadge'
import './GeoContentCenter.css'

type Brand = { id: number; name: string }
type ServiceBrand = { configured?: boolean; name?: string; has_logo?: boolean }
type ProductFactsInfo = { pack_id?: string; version?: string; fact_count?: number }
type QrReference = {
  reference_id: string
  payload_hash: string
  file_sha256: string
  payload_preview: string
  reference_token: string
  organization_id?: number | null
  uploaded_at: string
}

const QUICK_TASKS: Array<{ id: string; label: string; brief: string; icon: typeof Send; preselectChannels?: ChannelId[] }> = [
  { id: 'promote_geo', label: '推广 GEO 服务', icon: Send, brief: '向有客户资源的服务商老板推广我的 GEO 服务，邀请他先领取一次真实诊断' },
  { id: 'diagnosis_case', label: '用案例数据做内容', icon: BarChart3, brief: '用一份真实诊断案例向潜在客户说明 GEO 能看见什么，邀请他体验诊断' },
  { id: 'invite_trial', label: '做反馈沟通素材', icon: MessagesSquare, brief: '给正在犹豫的潜在客户做一套反馈沟通素材：私聊邀约话术和示例对话，邀请他先体验一次诊断', preselectChannels: ['private_chat'] },
]

const STRATEGY_CELLS: Array<{ key: keyof Strategy; label: string; icon: typeof Users }> = [
  { key: 'audience', label: '受众', icon: Users },
  { key: 'human_problem', label: '真实痛点', icon: CircleAlert },
  { key: 'core_angle', label: '认知变化', icon: TrendingUp },
  { key: 'evidence_statement', label: '可核验证据', icon: ShieldCheck },
  { key: 'action_resistance', label: '适用边界', icon: ShieldAlert },
  { key: 'single_value', label: '核心卖点', icon: Sparkles },
  { key: 'audience_status', label: '使用场景', icon: Clock },
  { key: 'single_action', label: '行动引导', icon: Zap },
]

const VISUAL_STYLES = [
  { id: 'business', label: '商务简洁' },
  { id: 'bright', label: '明亮活泼' },
  { id: 'tech_dark', label: '科技深色' },
  { id: 'festive', label: '喜庆红金' },
] as const
type VisualStyleId = typeof VISUAL_STYLES[number]['id']

const RESOLUTIONS = [
  { id: '1k', label: '标准 1K（默认）' },
  { id: '2k', label: '高清 2K' },
  { id: '4k', label: '超清 4K' },
] as const
type ResolutionId = typeof RESOLUTIONS[number]['id']


const MOMENTS_LAYOUT_OPTIONS = [
  { id: 'single', label: '单图（默认）' },
  { id: 'grid', label: '九宫格 · 9 张' },
] as const
type MomentsLayoutId = typeof MOMENTS_LAYOUT_OPTIONS[number]['id']

const DRAFT_KEY_PREFIX = 'geo_content_center_draft_v2'
const PENDING_REQUEST_KEY_PREFIX = 'geo_content_center_pending_request_v2'
const PENDING_RETRY_KEY_PREFIX = 'geo_content_center_pending_retry_v2'
const LEGACY_UNSCOPED_KEYS = [
  'geo_content_center_draft_v1',
  'geo_content_center_pending_request_v1',
  'geo_content_center_pending_retry_v1',
]

type OperationScope = 'bootstrap' | 'generation' | 'qr' | 'history' | 'teacher' | 'recovery' | 'asset' | 'edit'
type PackageIssueAction = 'select_published_diagnosis' | 'use_evergreen' | 'remove_diagnosis_case' | 'review_settings'
type PackageIssue = {
  code: string
  message: string
  reason: string
  repair_hint?: string
  rule_version?: string
  actions: Array<{ id: PackageIssueAction; label: string }>
}

const RECOVERY_404_GRACE_MS = 5 * 60_000
const RECOVERY_RETRY_MS = 1200

const PACKAGE_ISSUE_FALLBACKS: Record<string, PackageIssue> = {
  published_diagnosis_evidence_not_found: {
    code: 'PUBLISHED_DIAGNOSIS_EVIDENCE_NOT_FOUND',
    message: '这个客户暂时没有可用于推广的已发布诊断。',
    reason: '只有当前账号仍有权限、且已经发布的诊断，才能作为对外内容的真实证据。',
    repair_hint: 'AI 可以不引用诊断数字，改用常青口径生成这份内容',
    actions: [
      { id: 'select_published_diagnosis', label: '去选择已发布诊断证据' },
      { id: 'use_evergreen', label: '改为不依赖诊断证据的常青内容' },
    ],
  },
}

function packageIssue(data: unknown, fallback: string): PackageIssue {
  const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : null
  if (typeof detail === 'string' && PACKAGE_ISSUE_FALLBACKS[detail]) return PACKAGE_ISSUE_FALLBACKS[detail]
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const value = detail as { code?: unknown; message?: unknown; reason?: unknown; repair_hint?: unknown; rule_version?: unknown; actions?: unknown }
    const actions = Array.isArray(value.actions) ? value.actions.flatMap((item) => {
      if (!item || typeof item !== 'object') return []
      const action = item as { id?: unknown; label?: unknown }
      if (!['select_published_diagnosis', 'use_evergreen', 'remove_diagnosis_case', 'review_settings'].includes(String(action.id))) return []
      return [{ id: String(action.id) as PackageIssueAction, label: String(action.label || '') }]
    }) : []
    if (typeof value.message === 'string') {
      return {
        code: String(value.code || 'CONTENT_PACKAGE_REJECTED'),
        message: value.message,
        reason: typeof value.reason === 'string' ? value.reason : '请检查当前选择后再试。',
        repair_hint: typeof value.repair_hint === 'string' && value.repair_hint ? value.repair_hint : undefined,
        rule_version: typeof value.rule_version === 'string' && value.rule_version ? value.rule_version : undefined,
        actions,
      }
    }
  }
  return {
    code: 'CONTENT_PACKAGE_REJECTED',
    message: fallback,
    reason: '请求没有通过校验，请检查当前选择后再试。',
    actions: [{ id: 'review_settings', label: '检查高级设置' }],
  }
}

/* ---------- AI 理解摘要 · 单格 ---------- */
function StrategyCell(props: {
  label: string
  icon: typeof Users
  value: string
  disabled?: boolean
  onSave: (next: string) => void
}) {
  const { label, icon: Icon, value, disabled = false, onSave } = props
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  return (
    <div className={`gcc-strategy-cell ${editing ? 'is-editing' : ''}`} data-strategy-key={label}>
      <small><Icon size={13} aria-hidden />{label}</small>
      {editing ? (
        <>
          {/* N1：生成进行期（disabled=composerLocked）连已打开的编辑器也整体锁定，
              锁定前点开的「保存」不能在轮询中途把任务作废 */}
          <textarea value={draft} maxLength={240} autoFocus disabled={disabled} onChange={(event) => setDraft(event.target.value)} aria-label={`编辑${label}`} />
          <div className="gcc-cell-actions">
            <button className="gcc-cell-save" disabled={disabled} onClick={() => {
              const next = draft.trim()
              if (!next) { toast.error('这一格不能为空'); return }
              onSave(next)
              setEditing(false)
            }}>保存</button>
            <button disabled={disabled} onClick={() => setEditing(false)}>取消</button>
          </div>
        </>
      ) : (
        <>
          <p className={value ? '' : 'is-placeholder'}>{value || '填入一句话后，AI 会先理解并填到这里'}</p>
          {value && <button className="gcc-cell-edit" aria-label={`编辑${label}`} disabled={disabled} onClick={() => { setDraft(value); setEditing(true) }}><Pencil size={13} /></button>}
        </>
      )}
    </div>
  )
}

export default function GeoContentCenter() {
  const { user: authUser, authorizationScope } = useAuth()
  // 🔴 键必须是后端 `_FORM_PREFILL_FIELDS` 真会写的那几个。本页认 brand/quote/图文,
  //    不认 article(它不做文章)。
  const { prefill: xiaobangPrefill, error: xiaobangPrefillError } = useXiaobangPrefill({
    numbers: { brand_id: 'brand_id', quote_id: 'quote_id', geo_post_id: 'geo_post_id' },
  })
  const browserOwnerScope = authUser
    ? `u${authUser.id}:p${authUser.permission_version ?? 0}`
    : null
  const storageKeys = useMemo(() => browserOwnerScope ? ({
    draft: `${DRAFT_KEY_PREFIX}:${browserOwnerScope}`,
    pendingRequest: `${PENDING_REQUEST_KEY_PREFIX}:${browserOwnerScope}`,
    pendingRetry: `${PENDING_RETRY_KEY_PREFIX}:${browserOwnerScope}`,
  }) : null, [browserOwnerScope])
  const [mode, setMode] = useState<'normal' | 'deal'>('normal')
  const [brief, setBrief] = useState('')
  const [quickTask, setQuickTask] = useState('promote_geo')
  const [strategy, setStrategy] = useState<Strategy | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [teachers, setTeachers] = useState<Teacher[]>([])
  const [teacher, setTeacher] = useState<Teacher | null>(null)
  const [brands, setBrands] = useState<Brand[]>([])
  const [serviceBrand, setServiceBrand] = useState<ServiceBrand | null>(null)
  const [productFacts, setProductFacts] = useState<ProductFactsInfo | null>(null)
  const [channels, setChannels] = useState<ChannelId[]>(['professional_poster', 'moments', 'xiaohongshu'])
  const [trend, setTrend] = useState(false)
  const [evidenceType, setEvidenceType] = useState<'none' | 'latest_diagnosis'>('none')
  const [brandId, setBrandId] = useState<number | null>(null)
  const [contactMode, setContactMode] = useState<'none' | 'text' | 'qr'>('none')
  const [contactText, setContactText] = useState('')
  const [qrReference, setQrReference] = useState<QrReference | null>(null)
  const [qrError, setQrError] = useState('')
  const [visualStyle, setVisualStyle] = useState<VisualStyleId>('business')
  const [resolution, setResolution] = useState<ResolutionId>('1k')
  // [#65] 价目取自 feature_pricing SSOT;为空 = 该 code 还没配 ⇒ 不显示任何数字。
  const { getCost } = usePricing()
  const bundleCode = bundleFeatureCode(resolution)
  const bundleCost = getCost(bundleCode)
  const [momentsLayout, setMomentsLayout] = useState<MomentsLayoutId>('single')
  const [understanding, setUnderstanding] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [uploadingQr, setUploadingQr] = useState(false)
  const [job, setJob] = useState<JobState | null>(null)
  // 创建前提醒预览(2026-07-23 外部审查 P1-2):interpret 返回的策略软提醒;
  // 有提醒时生成按钮变成显式确认动作(「知道了，继续生成」),请求带
  // warnings_acknowledged=true,后端仅此时记「用户确认继续」审计。
  const [strategyWarnings, setStrategyWarnings] = useState<JobWarning[]>([])
  const [history, setHistory] = useState<JobState[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [assetObjectUrls, setAssetObjectUrls] = useState<Record<number, string>>({})
  const [pageError, setPageError] = useState('')
  const [requestIssue, setRequestIssue] = useState<PackageIssue | null>(null)
  const operationSeq = useRef(0)
  const operations = useRef(new Map<OperationScope, { generation: number; controller: AbortController }>())
  const pollTimer = useRef<number | null>(null)
  const assetObjectUrlsRef = useRef<Record<number, string>>({})
  const autoUnderstandKeyRef = useRef('')
  const editedCopyRef = useRef(new Map<number, string>())
  const composerRef = useRef<HTMLElement | null>(null)
  const evidenceSettingsRef = useRef<HTMLFieldSetElement | null>(null)
  const renderedAuthorizationScopeRef = useRef<string | null>(null)
  const renderedBrowserOwnerScopeRef = useRef<string | null>(null)

  const beginOperation = useCallback((scope: OperationScope) => {
    operations.current.get(scope)?.controller.abort()
    const generation = ++operationSeq.current
    const controller = new AbortController()
    operations.current.set(scope, { generation, controller })
    return { generation, controller }
  }, [])

  const isCurrent = useCallback((scope: OperationScope, generation: number) => {
    const current = operations.current.get(scope)
    return current?.generation === generation && !current.controller.signal.aborted
  }, [])

  const finishOperation = useCallback((scope: OperationScope, generation: number) => {
    if (operations.current.get(scope)?.generation === generation) operations.current.delete(scope)
  }, [])

  const cancelActive = useCallback(() => {
    operationSeq.current += 1
    operations.current.forEach(operation => operation.controller.abort())
    operations.current.clear()
    if (pollTimer.current != null) window.clearTimeout(pollTimer.current)
    pollTimer.current = null
  }, [])

  /* F11：PATCH 保存成功的文案写入 ref；轮询/恢复回包若仍带编辑前快照，
     setJob 时用 ref 合并回来，避免 UI 回退到旧文案。asset id 全局唯一，合并不串任务。 */
  const mergeEditedCopy = useCallback((next: JobState): JobState => {
    if (!editedCopyRef.current.size) return next
    return {
      ...next,
      assets: (next.assets || []).map((asset) => {
        const edited = editedCopyRef.current.get(asset.id)
        return edited != null ? { ...asset, content_text: edited } : asset
      }),
    }
  }, [])

  const clearRenderedScopeState = useCallback(() => {
    cancelActive()
    Object.values(assetObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    assetObjectUrlsRef.current = {}
    setAssetObjectUrls({})
    setMode('normal')
    setBrief('')
    setQuickTask('promote_geo')
    setStrategy(null)
    setTeachers([])
    setTeacher(null)
    setBrands([])
    setChannels(['professional_poster', 'moments', 'xiaohongshu'])
    setTrend(false)
    setEvidenceType('none')
    setBrandId(null)
    setContactMode('none')
    setContactText('')
    setQrReference(null)
    setQrError('')
    setVisualStyle('business')
    setResolution('1k')
    setMomentsLayout('single')
    setUnderstanding(false)
    setGenerating(false)
    setUploadingQr(false)
    setJob(null)
    setHistory([])
    setHistoryOpen(false)
    setPageError('')
    setRequestIssue(null)
    autoUnderstandKeyRef.current = ''
    editedCopyRef.current.clear()
  }, [cancelActive])

  useLayoutEffect(() => {
    const previousAuthorization = renderedAuthorizationScopeRef.current
    const previousOwner = renderedBrowserOwnerScopeRef.current
    const authorizationChanged = previousAuthorization !== null
      && previousAuthorization !== authorizationScope
    // During a refresh AuthContext intentionally renders user=null before /me
    // confirms the successor. Preserve the last stable owner through that gap;
    // only a newly confirmed non-null owner can prove an account/generation
    // change and invalidate the old browser anchors.
    const durableOwnerChanged = browserOwnerScope !== null
      && previousOwner !== null
      && previousOwner !== browserOwnerScope
    const scopeChanged = authorizationChanged || durableOwnerChanged
    if (scopeChanged) {
      if (durableOwnerChanged) {
        // 真实账号/权限代际变更：渲染数据全部作废（旧作用域锚点按 owner 隔离清理）。
        clearRenderedScopeState()
      } else {
        // 安全 token 刷新（owner 代际 u+permVer 未变）：只中止在途请求，保住
        // 当前渲染的任务、策略与晒成交步骤里的未保存输入，不把用户踢出 DealStudio。
        // 丢响应恢复 effect 依赖 authorizationScope，会自动重跑并接续跟踪已接受的任务。
        cancelActive()
        setUnderstanding(false)
        setUploadingQr(false)
      }
    }
    if (browserOwnerScope !== null) {
      // Keep only the newly confirmed durable owner generation. This is more
      // robust than relying on the transient user=null refresh gap and also
      // prevents dormant A-account anchors from remaining readable in B's tab.
      const allowed = new Set([
        `${DRAFT_KEY_PREFIX}:${browserOwnerScope}`,
        `${PENDING_REQUEST_KEY_PREFIX}:${browserOwnerScope}`,
        `${PENDING_RETRY_KEY_PREFIX}:${browserOwnerScope}`,
      ])
      const prefixes = [DRAFT_KEY_PREFIX, PENDING_REQUEST_KEY_PREFIX, PENDING_RETRY_KEY_PREFIX]
      for (let index = localStorage.length - 1; index >= 0; index -= 1) {
        const key = localStorage.key(index)
        if (key && prefixes.some((prefix) => key.startsWith(`${prefix}:`)) && !allowed.has(key)) {
          localStorage.removeItem(key)
        }
      }
    }
    renderedAuthorizationScopeRef.current = authorizationScope
    if (browserOwnerScope !== null) renderedBrowserOwnerScopeRef.current = browserOwnerScope
  }, [authorizationScope, browserOwnerScope, cancelActive, clearRenderedScopeState, storageKeys])

  const invalidateComposer = useCallback(() => {
    operationSeq.current += 1
    ;(['generation', 'qr', 'history', 'teacher', 'recovery', 'asset', 'edit'] as OperationScope[]).forEach((scope) => {
      operations.current.get(scope)?.controller.abort()
      operations.current.delete(scope)
    })
    if (pollTimer.current != null) window.clearTimeout(pollTimer.current)
    pollTimer.current = null
    setUnderstanding(false)
    setGenerating(false)
    setStrategy(null)
    setStrategyWarnings([])
    setJob(null)
    setRequestIssue(null)
    // F3：策略失效后允许自动理解重新填充九格（失败时 key 保留防循环，
    // 理解成功后也会重置 key，双重保证不再永久停留占位）。
    autoUnderstandKeyRef.current = ''
  }, [])

  useEffect(() => () => cancelActive(), [cancelActive])
  useEffect(() => () => {
    Object.values(assetObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    assetObjectUrlsRef.current = {}
  }, [])

  useEffect(() => {
    LEGACY_UNSCOPED_KEYS.forEach((key) => localStorage.removeItem(key))
  }, [])

  useEffect(() => {
    if (!storageKeys) return
    try {
      const draft = JSON.parse(localStorage.getItem(storageKeys.draft) || '{}') as Record<string, unknown>
      if (typeof draft.quickTask === 'string') setQuickTask(draft.quickTask)
      if (Array.isArray(draft.channels)) setChannels(draft.channels.filter((item): item is ChannelId => CHANNELS.some((channel) => channel.id === item)))
      if (draft.contactMode === 'none' || draft.contactMode === 'text' || draft.contactMode === 'qr') setContactMode(draft.contactMode)
      if (typeof draft.trend === 'boolean') setTrend(draft.trend)
      if (VISUAL_STYLES.some((item) => item.id === draft.visualStyle)) setVisualStyle(draft.visualStyle as VisualStyleId)
      if (RESOLUTIONS.some((item) => item.id === draft.resolution)) setResolution(draft.resolution as ResolutionId)
      if (MOMENTS_LAYOUT_OPTIONS.some((item) => item.id === draft.momentsLayout)) setMomentsLayout(draft.momentsLayout as MomentsLayoutId)
      // F9：安全 token 刷新会按 candidate-token 设计卸载整页重挂；
      // 记住所在视图，重挂后回到晒成交而不是被踢回普通模式。
      if (draft.mode === 'deal') setMode('deal')
    } catch { /* damaged local draft starts clean */ }
  }, [storageKeys])

  useEffect(() => {
    // Keep only bounded, non-sensitive UI preferences. Brief/contact/evidence
    // remain in memory and are never copied to long-lived browser storage.
    if (storageKeys) localStorage.setItem(storageKeys.draft, JSON.stringify({ mode, quickTask, channels, trend, contactMode, visualStyle, resolution, momentsLayout }))
  }, [mode, quickTask, channels, trend, contactMode, visualStyle, resolution, momentsLayout, storageKeys])

  const loadBootstrap = useCallback(async () => {
    const { generation, controller } = beginOperation('bootstrap')
    setPageError('')
    try {
      const [bootstrapResponse, brandsResponse, factsResponse] = await Promise.all([
        authFetch('/api/marketing/content-center/bootstrap', { signal: controller.signal }),
        authFetch('/api/my-clients?page=1&page_size=50', { signal: controller.signal }),
        authFetch('/api/marketing/product-facts', { signal: controller.signal }),
      ])
      const bootstrap = await readJson<{ teachers?: Teacher[]; default_teacher?: Teacher; service_brand?: ServiceBrand }>(bootstrapResponse)
      if (!isCurrent('bootstrap', generation)) return
      if (!bootstrapResponse.ok) throw new Error(errorMessage(bootstrap, '内容中心暂时不可用'))
      setTeachers(bootstrap.teachers || [])
      setTeacher(bootstrap.default_teacher || bootstrap.teachers?.[0] || null)
      setServiceBrand(bootstrap.service_brand || null)
      // 产品事实库状态行:读取失败静默降级为"暂时读取失败",不影响生成主流程
      if (factsResponse.ok) {
        const factsData = await readJson<{ pack?: ProductFactsInfo }>(factsResponse)
        if (!isCurrent('bootstrap', generation)) return
        setProductFacts(factsData.pack?.version ? factsData.pack : null)
      } else {
        setProductFacts(null)
      }
      if (brandsResponse.ok) {
        const raw = await readJson<{
          data?: { items?: unknown[] }
          items?: unknown[]
          brands?: unknown[]
          clients?: unknown[]
        }>(brandsResponse)
        if (!isCurrent('bootstrap', generation)) return
        const list = raw.clients || raw.data?.items || raw.items || raw.brands || []
        setBrands(list.flatMap((item) => {
          if (!item || typeof item !== 'object') return []
          const row = item as { id?: unknown; name?: unknown; brand_name?: unknown }
          const id = Number(row.id)
          if (!Number.isFinite(id) || id <= 0) return []
          return [{ id, name: String(row.name || row.brand_name || `客户 #${id}`) }]
        }))
      }
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('bootstrap', generation)) return
      setPageError(error instanceof Error ? error.message : '内容中心暂时不可用')
    } finally {
      finishOperation('bootstrap', generation)
    }
  }, [authorizationScope, beginOperation, finishOperation, isCurrent])

  const loadHistory = useCallback(async () => {
    const { generation, controller } = beginOperation('history')
    try {
      const response = await authFetch('/api/marketing/my-materials?limit=30', { signal: controller.signal })
      const data = await readJson<{ materials?: JobState[] }>(response)
      if (!isCurrent('history', generation)) return
      if (!response.ok) throw new Error(errorMessage(data, '历史版本加载失败'))
      setHistory((data.materials || []).filter((item) => Array.isArray(item.channels)))
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('history', generation)) return
      toast.error(error instanceof Error ? error.message : '历史版本加载失败')
    } finally {
      finishOperation('history', generation)
    }
  }, [authorizationScope, beginOperation, finishOperation, isCurrent])

  useEffect(() => {
    if (!browserOwnerScope) return
    void loadBootstrap()
    void loadHistory()
  }, [authorizationScope, browserOwnerScope, loadBootstrap, loadHistory])

  const imageAssetSignature = useMemo(() => (
    (job?.assets || []).filter((asset) => assetSlot(asset).includes(':image:'))
      .map((asset) => `${asset.id}:${assetUrl(asset)}`).join('|')
  ), [job])

  useEffect(() => {
    const images = (job?.assets || []).filter((asset) => assetSlot(asset).includes(':image:') && assetUrl(asset))
    if (!images.length) {
      Object.values(assetObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
      assetObjectUrlsRef.current = {}
      setAssetObjectUrls({})
      return
    }
    const { generation, controller } = beginOperation('asset')
    void (async () => {
      const created: Record<number, string> = {}
      try {
        await Promise.all(images.map(async (asset) => {
          const response = await authFetch(assetUrl(asset), { signal: controller.signal })
          if (!response.ok) throw new Error('asset_preview_failed')
          created[asset.id] = URL.createObjectURL(await response.blob())
        }))
        if (!isCurrent('asset', generation)) {
          Object.values(created).forEach((url) => URL.revokeObjectURL(url))
          return
        }
        Object.values(assetObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
        assetObjectUrlsRef.current = created
        setAssetObjectUrls(created)
      } catch (error) {
        Object.values(created).forEach((url) => URL.revokeObjectURL(url))
        if ((error as Error)?.name !== 'AbortError' && isCurrent('asset', generation)) {
          setPageError('成图预览授权已失效，请刷新权限后重试')
        }
      } finally {
        finishOperation('asset', generation)
      }
    })()
    return () => controller.abort()
  }, [beginOperation, finishOperation, imageAssetSignature, isCurrent, job])

  const understand = useCallback(async (): Promise<Strategy | null> => {
    const input = brief.trim()
    if (input.length < 4) {
      toast.error('先用一句话说明想向谁推广什么')
      return null
    }
    const { generation, controller } = beginOperation('generation')
    setUnderstanding(true)
    setPageError('')
    try {
      const response = await authFetch('/api/marketing/interpret', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          brief: input,
          quick_task: quickTask,
          teacher_id: teacher?.teacher_id,
          teacher_version: teacher?.version,
        }),
      })
      const data = await readJson<{ strategy?: Strategy; teacher?: Teacher; warnings?: JobWarning[]; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return null
      if (!response.ok || !data.strategy) throw new Error(errorMessage(data, 'AI 暂时没理解清楚，请重试'))
      setStrategy(data.strategy)
      setTeacher(data.teacher || teacher)
      setStrategyWarnings(Array.isArray(data.warnings) ? data.warnings : [])
      // F3：理解成功即重置去重 key，后续失效（invalidateComposer）可重新自动理解；
      // 失败路径不重置，保留 key 防止自动重试循环。
      autoUnderstandKeyRef.current = ''
      return data.strategy
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('generation', generation)) return null
      const message = error instanceof Error ? error.message : 'AI 理解失败'
      setPageError(message)
      toast.error(message)
      return null
    } finally {
      if (isCurrent('generation', generation)) setUnderstanding(false)
      finishOperation('generation', generation)
    }
  }, [beginOperation, brief, finishOperation, isCurrent, quickTask, teacher])

  // 生成前让九要素可见：一句话写定后自动完成 AI 理解（去抖 700ms）。
  // 每次输入已 invalidate 旧策略；autoUnderstandKeyRef 防止失败后循环重试。
  useEffect(() => {
    if (!storageKeys || mode !== 'normal') return
    if (strategy || understanding || generating) return
    const input = brief.trim()
    if (input.length < 4) return
    const key = `${quickTask}:${input}`
    if (autoUnderstandKeyRef.current === key) return
    const timer = window.setTimeout(() => {
      if (autoUnderstandKeyRef.current === key) return
      autoUnderstandKeyRef.current = key
      void understand()
    }, 700)
    return () => window.clearTimeout(timer)
  }, [storageKeys, mode, strategy, understanding, generating, brief, quickTask, understand])

  const pollJob = useCallback(async (jobId: number, generation: number) => {
    if (!isCurrent('generation', generation)) return
    try {
      const response = await authFetch(`/api/marketing/jobs/${jobId}`, { signal: operations.current.get('generation')?.controller.signal })
      const data = await readJson<{ job?: JobState; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return
      if (!response.ok || !data.job) throw new Error(errorMessage(data, '任务状态读取失败'))
      setJob(mergeEditedCopy(data.job))
      if (data.job.status === 'generating' || data.job.status === 'pending') {
        pollTimer.current = window.setTimeout(() => void pollJob(jobId, generation), 1800)
      } else {
        setGenerating(false)
        const pending = readPendingRequest(storageKeys?.pendingRequest || null)
        if (pending?.jobId === jobId && storageKeys) localStorage.removeItem(storageKeys.pendingRequest)
        try {
          const retry = JSON.parse(storageKeys ? localStorage.getItem(storageKeys.pendingRetry) || 'null' : 'null') as { childJobId?: number } | null
          if (retry?.childJobId === jobId && storageKeys) localStorage.removeItem(storageKeys.pendingRetry)
        } catch { if (storageKeys) localStorage.removeItem(storageKeys.pendingRetry) }
        finishOperation('generation', generation)
        void loadHistory()
        if (data.job.status === 'succeeded') toast.success('推广包已生成')
        if (data.job.status === 'partial_success') toast.warning('部分成品已就绪，失败项可单独重做')
      }
    } catch (error) {
      if (!isCurrent('generation', generation)) return
      setGenerating(false)
      setPageError(error instanceof Error ? error.message : '任务状态读取失败')
      finishOperation('generation', generation)
    }
  }, [finishOperation, isCurrent, loadHistory, mergeEditedCopy, storageKeys])

  useEffect(() => {
    if (!storageKeys) return
    const pending = readPendingRequest(storageKeys.pendingRequest)
    const pendingRetry = readPendingRetry(storageKeys.pendingRetry)
    if (!pending && !pendingRetry) return
    const recovery = pending
      ? { requestId: pending.requestId, jobId: pending.jobId, storageKey: storageKeys.pendingRequest, createdAt: pending.createdAt }
      : { requestId: pendingRetry!.requestId, jobId: pendingRetry!.childJobId, storageKey: storageKeys.pendingRetry, createdAt: pendingRetry!.createdAt }
    // The grace period begins with the first observed 404, not when the POST
    // was started. A slow accepted request may legitimately be older than the
    // grace window before its first recovery lookup reaches the API.
    let recoveryNotFoundSince = pending?.notFoundSince ?? pendingRetry?.notFoundSince
    const { generation, controller } = beginOperation('generation')
    setGenerating(true)
    const recoverOnce = async (): Promise<void> => {
      try {
        const endpoint = recovery.jobId
          ? `/api/marketing/jobs/${recovery.jobId}`
          : `/api/marketing/content-packages/by-request/${encodeURIComponent(recovery.requestId)}`
        const response = await authFetch(endpoint, { signal: controller.signal })
        const data = await readJson<{ job?: JobState; detail?: unknown }>(response)
        if (!isCurrent('generation', generation)) return
        if (response.status === 404) {
          const now = Date.now()
          recoveryNotFoundSince = recoveryNotFoundSince || now
          if (now - recoveryNotFoundSince <= RECOVERY_404_GRACE_MS) {
            if (pending) localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({ ...pending, notFoundSince: recoveryNotFoundSince }))
            else if (pendingRetry) localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({ ...pendingRetry, notFoundSince: recoveryNotFoundSince }))
            pollTimer.current = window.setTimeout(() => void recoverOnce(), RECOVERY_RETRY_MS)
            return
          }
          localStorage.removeItem(recovery.storageKey)
          setGenerating(false)
          setPageError('未确认请求在宽限期后仍不存在，已允许创建新请求')
          finishOperation('generation', generation)
          return
        }
        if (!response.ok || !data.job) throw new Error(errorMessage(data, '未确认任务恢复失败'))
        if (pending) {
          localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({ ...pending, jobId: data.job.job_id }))
        } else if (pendingRetry) {
          localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({ ...pendingRetry, childJobId: data.job.job_id }))
        }
        setJob(mergeEditedCopy(data.job))
        if (data.job.status === 'generating' || data.job.status === 'pending') {
          await pollJob(data.job.job_id, generation)
        } else {
          localStorage.removeItem(recovery.storageKey)
          setGenerating(false)
          finishOperation('generation', generation)
        }
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError' && isCurrent('generation', generation)) {
          setGenerating(false)
          setPageError(error instanceof Error ? error.message : '未确认任务恢复失败')
          finishOperation('generation', generation)
        }
      }
    }
    void recoverOnce()
    return () => controller.abort()
  }, [authorizationScope, beginOperation, finishOperation, isCurrent, mergeEditedCopy, pollJob, storageKeys])

  const generate = useCallback(async () => {
    if (!storageKeys) return
    let confirmed = strategy
    if (!confirmed) confirmed = await understand()
    if (!confirmed) return
    if (!channels.length) {
      toast.error('至少选择一个渠道成品')
      return
    }
    if (evidenceType !== 'none' && !brandId) {
      setSettingsOpen(true)
      toast.error('选择要引用证据的客户；默认不会自动选第一个')
      return
    }
    if (contactMode === 'text' && !contactText.trim()) {
      setSettingsOpen(true)
      toast.error('请输入要展示的文字联系方式')
      return
    }
    if (contactMode === 'qr' && !qrReference) {
      setSettingsOpen(true)
      toast.error('请先上传并通过扫码校验的二维码')
      return
    }
    const { generation, controller } = beginOperation('generation')
    // F1：生成进行期锁定 composer（输入/渠道/快捷入口/高级设置触发器全部 disabled），
    // 并收起已打开的高级设置面板，任务落地前不能再改设置，任务在 UI 始终可见。
    // N2：setGenerating(true) 移到下方锚点写入之后——payloadHash 等待窗口内若被
    // safe-refresh 的 cancelActive 抢占，generating 从未置 true，恢复 effect
    // 无锚点退出时 composer 不会被永久锁死。
    setSettingsOpen(false)
    setPageError('')
    setRequestIssue(null)
    try {
      // 显式确认(2026-07-23 P1-2):用户当前能看见提醒(预览或上一次结果)时,
      // 按钮文案已是「知道了，继续生成」,点击即真实确认动作,请求带
      // warnings_acknowledged=true;无提醒时按钮是普通生成,不带确认语义。
      const warningsAcknowledged = strategyWarnings.length > 0 || (job?.warnings?.length ?? 0) > 0
      const requestPayload = {
        brief: brief.trim(),
        quick_task: quickTask,
        strategy: confirmed,
        teacher_id: teacher?.teacher_id,
        teacher_version: teacher?.version,
        channels,
        brand_id: evidenceType === 'none' ? null : brandId,
        evidence: { source_type: evidenceType, diagnosis_id: null },
        contact: { mode: contactMode, text: contactMode === 'text' ? contactText.trim() : '', qr_reference: contactMode === 'qr' ? qrReference : null },
        associate_recent_trend: trend,
        resolution,
        moments_layout: momentsLayout,
        visual_style: visualStyle,
        warnings_acknowledged: warningsAcknowledged,
      }
      const signature = await payloadHash(requestPayload)
      if (!isCurrent('generation', generation)) return
      const durablePending = readPendingRequest(storageKeys.pendingRequest)
      const durableRequestId = durablePending?.payloadHash === signature ? durablePending.requestId : requestId()
      localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({ requestId: durableRequestId, payloadHash: signature, createdAt: durablePending?.requestId === durableRequestId ? durablePending.createdAt : Date.now() }))
      // N2：锚点落盘后才上锁、同时清旧结果视图（hash 窗口内不闪空态）；
      // 此后被抢占也有锚点可恢复，generating 不会泄漏
      setJob(null)
      setGenerating(true)
      const response = await authFetch('/api/marketing/content-packages', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          ...requestPayload,
          request_id: durableRequestId,
        }),
      })
      const data = await readJson<{ job_id?: number; status?: string; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return
      if (!response.ok || !data.job_id) {
        const issue = packageIssue(data, '推广包创建失败，请检查当前设置')
        // A completed 422 proves the request was rejected before job creation;
        // retaining its recovery anchor would make refresh poll a phantom job
        // for the full lost-response grace window.
        if (response.status === 422) localStorage.removeItem(storageKeys.pendingRequest)
        setRequestIssue(issue)
        setGenerating(false)
        toast.error(issue.message)
        finishOperation('generation', generation)
        return
      }
      localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({ requestId: durableRequestId, payloadHash: signature, jobId: data.job_id, createdAt: durablePending?.requestId === durableRequestId ? durablePending.createdAt : Date.now() }))
      await pollJob(data.job_id, generation)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('generation', generation)) return
      const message = error instanceof Error ? error.message : '推广包创建失败'
      setPageError(message)
      setGenerating(false)
      toast.error(message)
      finishOperation('generation', generation)
    }
  }, [beginOperation, brandId, brief, channels, contactMode, contactText, evidenceType, finishOperation, isCurrent, job, momentsLayout, pollJob, qrReference, quickTask, resolution, storageKeys, strategy, strategyWarnings, teacher, trend, understand, visualStyle])

  const uploadQr = useCallback(async (file?: File) => {
    if (!file) return
    invalidateComposer()
    const { generation, controller } = beginOperation('qr')
    setUploadingQr(true)
    setQrReference(null)
    setQrError('')
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await authFetch('/api/marketing/qr-reference', { method: 'POST', body: form, signal: controller.signal })
      const data = await readJson<{ qr_reference?: QrReference; detail?: unknown }>(response)
      if (!isCurrent('qr', generation)) return
      if (!response.ok || !data.qr_reference) throw new Error(errorMessage(data, '二维码无法扫码'))
      setQrReference(data.qr_reference)
      toast.success('二维码已通过扫码校验')
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('qr', generation)) return
      const message = error instanceof Error ? error.message : '二维码无法扫码'
      setQrError(message)
      toast.error(message)
    } finally {
      if (isCurrent('qr', generation)) setUploadingQr(false)
      finishOperation('qr', generation)
    }
  }, [beginOperation, finishOperation, invalidateComposer, isCurrent])

  const setDefaultTeacher = useCallback(async () => {
    if (!teacher) return
    const selected = { teacher_id: teacher.teacher_id, version: teacher.version }
    const { generation, controller } = beginOperation('teacher')
    try {
      const response = await authFetch('/api/marketing/teacher-preference', {
        method: 'PUT',
        signal: controller.signal,
        body: JSON.stringify({ ...selected, reason: 'GEO 内容中心设置' }),
      })
      const data = await readJson<{ detail?: unknown; teacher?: Teacher }>(response)
      if (!isCurrent('teacher', generation) || teacher.teacher_id !== selected.teacher_id || teacher.version !== selected.version) return
      if (!response.ok) toast.error(errorMessage(data, '默认导师保存失败'))
      else if (data.teacher?.teacher_id === selected.teacher_id && data.teacher.version === selected.version) toast.success('已设为服务商默认导师')
    } catch (error) {
      if ((error as Error)?.name !== 'AbortError' && isCurrent('teacher', generation)) toast.error('默认导师保存失败')
    } finally {
      finishOperation('teacher', generation)
    }
  }, [beginOperation, finishOperation, isCurrent, teacher])

  const redoComponents = useCallback(async (componentIds: string[]) => {
    if (!job || !componentIds.length || !storageKeys) return
    const { generation, controller } = beginOperation('generation')
    setGenerating(true)
    try {
      const retryKey = `${job.job_id}:${[...componentIds].sort().join(',')}`
      const pending = readPendingRetry(storageKeys.pendingRetry)
      const retryRequestId = pending?.retryKey === retryKey && Date.now() - pending.createdAt < 86400_000
        ? pending.requestId : requestId()
      localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({ retryKey, requestId: retryRequestId, createdAt: pending?.requestId === retryRequestId ? pending.createdAt : Date.now() }))
      const response = await authFetch(`/api/marketing/jobs/${job.job_id}/retry`, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ request_id: retryRequestId, component_ids: componentIds }),
      })
      const data = await readJson<{ job_id?: number; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return
      if (!response.ok || !data.job_id) throw new Error(errorMessage(data, '单项重做失败'))
      localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({ retryKey, requestId: retryRequestId, childJobId: data.job_id, createdAt: pending?.requestId === retryRequestId ? pending.createdAt : Date.now() }))
      await pollJob(data.job_id, generation)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('generation', generation)) return
      setGenerating(false)
      toast.error(error instanceof Error ? error.message : '单项重做失败')
      finishOperation('generation', generation)
    }
  }, [beginOperation, finishOperation, isCurrent, job, pollJob, storageKeys])

  const saveAssetCopy = useCallback(async (assetId: number, updates: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> => {
    const { generation, controller } = beginOperation('edit')
    try {
      const response = await authFetch(`/api/marketing/assets/${assetId}`, {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({ updates }),
      })
      const data = await readJson<{ ok?: boolean; content?: Record<string, unknown>; detail?: unknown }>(response)
      if (!isCurrent('edit', generation)) return { ok: false }
      if (!response.ok || !data.content) return { ok: false, error: editErrorMessage(data) }
      // F11：新文案写入 ref，后续轮询/恢复回包带编辑前快照时由 mergeEditedCopy 合并回来
      editedCopyRef.current.set(assetId, JSON.stringify(data.content))
      // 编辑后本地刷新：只替换该资产文案，不重拉任务、不动图片。
      setJob((current) => current ? ({
        ...current,
        assets: current.assets.map((asset) => asset.id === assetId ? { ...asset, content_text: JSON.stringify(data.content) } : asset),
      }) : current)
      toast.success('文案已保存，图片不受影响')
      return { ok: true }
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('edit', generation)) return { ok: false }
      return { ok: false, error: '文案保存失败，请稍后再试。' }
    } finally {
      finishOperation('edit', generation)
    }
  }, [beginOperation, finishOperation, isCurrent])

  const selectedEvidenceBrand = brands.find((brand) => brand.id === brandId)
  const allFailedIds = useMemo(() => (job?.components || []).filter((component) => component.status === 'failed').map((component) => component.component_id), [job])
  const channelStats = useMemo(() => {
    const list = job?.channels || []
    const done = list.filter((id) => {
      const components = (job?.components || []).filter((component) => component.channel === id)
      return components.length > 0 && components.every((component) => component.status === 'succeeded')
    }).length
    const failed = list.filter((id) => (job?.components || []).some((component) => component.channel === id && component.status === 'failed')).length
    return { total: list.length, done, failed }
  }, [job])
  const settingsSummary = useMemo(() => {
    const contactSummary = contactMode === 'none' ? '不展示' : contactMode === 'text' ? '展示文字' : '展示二维码'
    const styleSummary = VISUAL_STYLES.find((item) => item.id === visualStyle)?.label || '商务简洁'
    const evidenceSummary = evidenceType === 'none' ? '不引用' : '已发布诊断'
    const specSummary = `${resolution.toUpperCase()}${momentsLayout === 'grid' ? ' · 朋友圈九宫格' : ''}`
    return `导师：${teacher?.name || '默认'} · 视觉风格：${styleSummary} · 联系方式：${contactSummary} · 二维码：${qrReference ? '已验证' : '未上传'} · 证据来源：${evidenceSummary} · 输出规格：${specSummary}`
  }, [contactMode, evidenceType, momentsLayout, qrReference, resolution, teacher, visualStyle])

  const resolveRequestIssue = (action: PackageIssueAction) => {
    if (action === 'select_published_diagnosis' || action === 'review_settings') {
      setSettingsOpen(true)
      window.requestAnimationFrame(() => {
        evidenceSettingsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
        evidenceSettingsRef.current?.querySelector<HTMLSelectElement>('select')?.focus()
      })
      return
    }
    if (action === 'use_evergreen') {
      invalidateComposer()
      setEvidenceType('none')
      setBrandId(null)
      setChannels((current) => current.filter((channel) => channel !== 'diagnosis_case'))
      setSettingsOpen(false)
      return
    }
    if (action === 'remove_diagnosis_case') {
      invalidateComposer()
      setChannels((current) => current.filter((channel) => channel !== 'diagnosis_case'))
    }
  }

  const selectQuickTask = (item: typeof QUICK_TASKS[number]) => {
    invalidateComposer()
    setQuickTask(item.id)
    setBrief(item.brief)
    if (item.preselectChannels?.length) {
      setChannels((current) => Array.from(new Set([...current, ...item.preselectChannels!])) as ChannelId[])
    }
  }

  const saveStrategyCell = (key: keyof Strategy, value: string) => {
    // N1：生成进行期拒绝九格保存——保存会 invalidateComposer 把在途任务从 UI 抹掉，
    // 服务端 job 继续计费但用户不可见，再点生成就是第二个付费 job。
    if (!strategy || generating) return
    invalidateComposer()
    setStrategy({ ...strategy, [key]: value })
  }

  const openHistory = async (item: JobState) => {
    // N3：生成进行期禁止打开历史版本——setJob 会把跟踪中的任务顶出结果区
    if (generating) return
    const { generation, controller } = beginOperation('history')
    try {
      const response = await authFetch(`/api/marketing/jobs/${item.job_id}`, { signal: controller.signal })
      const data = await readJson<{ job?: JobState; detail?: unknown }>(response)
      if (!isCurrent('history', generation)) return
      if (!response.ok || !data.job) {
        toast.error(errorMessage(data, '该历史版本已不可读取'))
        return
      }
      setJob(mergeEditedCopy(data.job))
      setStrategy(data.job.strategy || null)
      if (data.job.teacher) setTeacher(data.job.teacher)
      setHistoryOpen(false)
    } catch (error) {
      if ((error as Error)?.name !== 'AbortError' && isCurrent('history', generation)) toast.error('该历史版本已不可读取')
    } finally {
      finishOperation('history', generation)
    }
  }

  const downloadAllCopy = () => {
    if (!job) return
    const sections = job.channels.map((id) => {
      const asset = job.assets.find((item) => assetSlot(item) === `${id}:copy`)
      const rows = flattenCopy(parseCopy(asset))
      const meta = CHANNELS.find((item) => item.id === id)
      return `【${meta?.short || id}】\n${rows.map((row) => `${row.label}\n${row.text}`).join('\n\n')}`
    }).filter((section) => section.trim().length > 0)
    if (!sections.length) return
    downloadText(`GEO推广包-全部文案-#${job.job_id}.txt`, sections.join('\n\n====================\n\n'))
  }

  if (mode === 'deal') return <DealStudio onBack={() => setMode('normal')} />

  const matchedQuickTask = QUICK_TASKS.find((item) => item.id === quickTask && item.brief === brief)
  // F1：生成（含局部重试轮询、丢响应恢复）进行期锁定 composer 输入区与高级设置触发器
  const composerLocked = generating

  return (
    <main className="gcc-shell" data-testid="geo-content-center">
      {/* §12.3:当前客户 / 当前对象 / 推荐理由 / 外部动作 / 渠道 / payer / 算力
          必须在主按钮上方同屏可见;过期时显示「重新准备」而不是空白页(§12.2)。 */}
      <XiaobangPrefillRegion prefill={xiaobangPrefill} error={xiaobangPrefillError} />
      <header className="gcc-header">
        <div>
          <div className="gcc-eyebrow"><Sparkles size={14} aria-hidden /> GEO 获客内容中心</div>
          <h1>你今天想推广什么？</h1>
          <p>一句话说清楚，AI 帮你做出一整套能直接发的获客内容：海报、朋友圈、小红书、抖音、信息图、聊天素材。</p>
        </div>
        <div className="gcc-header-actions">
          <button className="gcc-quiet-button" onClick={() => { setHistoryOpen((value) => !value); void loadHistory() }} aria-expanded={historyOpen} aria-label="历史版本">
            <History size={16} aria-hidden /><span>历史版本</span>
          </button>
        </div>
      </header>

      {historyOpen && (
        <aside className="gcc-history" aria-label="历史版本">
          <div className="gcc-history-head"><span>历史版本</span><button onClick={() => setHistoryOpen(false)} aria-label="关闭历史"><X size={16} /></button></div>
          {/* N3：生成进行期锁定历史条目，防止历史任务把跟踪中的任务顶出结果区 */}
          {composerLocked && <p className="gcc-empty-copy">任务生成中，落地后可查看历史版本。</p>}
          {history.length === 0 ? <p className="gcc-empty-copy">还没有生成记录。</p> : history.map((item) => (
            <button key={item.job_id} disabled={composerLocked} onClick={() => void openHistory(item)}>
              <span>{item.strategy?.core_angle || `推广包 #${item.job_id}`}</span>
              <small>{isShowcaseDealJob(item) && <em className="gcc-history-tag">晒成交</em>}{STATUS_TEXT[item.status] || item.status} · {item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : ''}</small>
            </button>
          ))}
        </aside>
      )}

      <section className="gcc-composer" aria-label="创建推广包" ref={composerRef}>
        <div className="gcc-input-wrap">
          <WandSparkles size={20} aria-hidden />
          <textarea
            value={brief}
            onChange={(event) => { invalidateComposer(); setBrief(event.target.value) }}
            placeholder="一句话说明你想推广什么，例如：向手上有装修老板资源的服务商，推广我的 GEO 诊断服务，邀请他先体验一次"
            maxLength={500}
            rows={3}
            aria-label="一句话说明你想推广什么"
            disabled={composerLocked}
          />
          <div className="gcc-input-meta">
            {composerLocked
              ? <span className="gcc-filled-tip"><Loader2 className="gcc-spin" size={13} aria-hidden /> 任务正在后台生成，完成后才能修改设置</span>
              : matchedQuickTask
                ? <span className="gcc-filled-tip"><Check size={13} aria-hidden /> 已按快捷入口「{matchedQuickTask.label}」填入，可直接改</span>
                : <span>一个受众 · 一个卖点 · 一个行动</span>}
            <span>{brief.length} / 500 字</span>
          </div>
        </div>

        <div className="gcc-quick-row" aria-label="快捷入口">
          <span>快捷入口</span>
          <div className="gcc-quick-chips">
            {QUICK_TASKS.map((item) => {
              const QuickIcon = item.icon
              return (
                <button key={item.id} className={quickTask === item.id ? 'is-active' : ''} disabled={composerLocked} onClick={() => selectQuickTask(item)}>
                  <QuickIcon size={15} aria-hidden /> {item.label}
                </button>
              )
            })}
            <button onClick={() => setMode('deal')} disabled={composerLocked}>
              <BadgeDollarSign size={15} aria-hidden /> 晒成交 <span className="gcc-new-tag">新</span>
            </button>
          </div>
        </div>

        <div className="gcc-core-grid">
          <fieldset className="gcc-channel-picker">
            <div className="gcc-section-title">
              <span className="gcc-title-left"><LayoutGrid size={15} aria-hidden /> 要做哪些渠道</span>
              <span className="gcc-title-hint">已选 {channels.length} 个 · 生成后按渠道分组展示，失败的可以单独重做</span>
            </div>
            <div className="gcc-channel-options">
              {CHANNELS.map((channel) => {
                const selected = channels.includes(channel.id)
                const ChannelIcon = channel.icon
                return (
                  <button key={channel.id} className={selected ? 'is-selected' : ''} aria-pressed={selected} disabled={composerLocked} onClick={() => { invalidateComposer(); setChannels((current) => selected ? current.filter((id) => id !== channel.id) : [...current, channel.id]) }}>
                    <span aria-hidden>{selected && <Check size={12} />}</span><ChannelIcon size={15} aria-hidden />{channel.short}
                  </button>
                )
              })}
            </div>
          </fieldset>
          <section className="gcc-evidence-state" aria-label="证据与品牌状态">
            <div>
              <span>证据与品牌</span>
              <strong>{evidenceType === 'none' ? '常青内容，不引用客户诊断数字' : selectedEvidenceBrand ? `${selectedEvidenceBrand.name} · 仅使用已发布诊断` : '等待选择证据客户'}</strong>
              <small>{evidenceType === 'none'
                ? (serviceBrand?.configured
                  ? `品牌：${serviceBrand.name} · 成品展示该公司名${serviceBrand.has_logo ? '与 Logo' : ''}`
                  : '成品将不含品牌信息')
                : '生成前会再次核验发布状态与访问权限'}</small>
              {evidenceType === 'none' && !serviceBrand?.configured && (
                <a className="gcc-diagnosis-link" href="/agent/whitelabel">去品牌信息设置</a>
              )}
            </div>
            <button onClick={() => setSettingsOpen(true)} disabled={composerLocked}>调整</button>
          </section>
        </div>

        <section className="gcc-strategy-section" aria-label="AI 理解摘要">
          <div className="gcc-section-title">
            <span className="gcc-title-left"><Zap size={15} aria-hidden /> AI 会这样理解你这句话</span>
            <span className="gcc-title-hint">生成前先核对，点任意一格可直接改</span>
          </div>
          <div className="gcc-strategy-note">
            <Info size={15} aria-hidden />
            这九项是整套内容的骨架，生成前可以随时改；改完再点生成，不重复计费。
          </div>
          <div className="gcc-strategy-grid" data-testid="strategy-grid">
            {STRATEGY_CELLS.map((cell) => (
              <StrategyCell
                key={cell.key}
                label={cell.label}
                icon={cell.icon}
                value={strategy?.[cell.key] || ''}
                disabled={composerLocked}
                onSave={(next) => saveStrategyCell(cell.key, next)}
              />
            ))}
            <div className="gcc-strategy-cell" data-strategy-key="平台渠道">
              <small><LayoutGrid size={13} aria-hidden />平台渠道</small>
              <p className={channels.length ? '' : 'is-placeholder'}>
                {channels.length
                  ? `${channels.map((id) => CHANNELS.find((item) => item.id === id)?.short || id).join('、')} · 在上面勾选调整`
                  : '在上面勾选要做的渠道'}
              </p>
            </div>
          </div>
        </section>

        <button
          className="gcc-settings-trigger"
          onClick={() => setSettingsOpen((value) => !value)}
          aria-expanded={settingsOpen}
          disabled={composerLocked}
          title={composerLocked ? '任务正在后台生成，完成后才能修改设置' : undefined}
        >
          <span><Settings2 size={16} aria-hidden /> 高级设置</span>
          <span className="gcc-settings-summary">{settingsSummary}</span>
          <ChevronDown size={16} className={settingsOpen ? 'is-open' : ''} aria-hidden />
        </button>

        {settingsOpen && (
          <div className="gcc-settings-panel">
            <div className="gcc-settings-columns">
              <fieldset>
                <legend><WandSparkles size={13} aria-hidden /> 策略导师</legend>
                <div className="gcc-inline-setting">
                  <select value={teacher ? `${teacher.teacher_id}@${teacher.version}` : ''} onChange={(event) => { invalidateComposer(); const next = teachers.find((item) => `${item.teacher_id}@${item.version}` === event.target.value) || null; setTeacher(next) }}>
                    {teachers.map((item) => <option key={`${item.teacher_id}@${item.version}`} value={`${item.teacher_id}@${item.version}`}>{item.name} · v{item.version}</option>)}
                  </select>
                  <button onClick={() => void setDefaultTeacher()}>设为默认</button>
                </div>
                <p className="gcc-sg-note">{teacher?.system_method || '导师决定整套内容的策略口径与措辞风格，与导师版本一起冻结进快照。'}</p>
              </fieldset>

              <fieldset>
                <legend><Sparkles size={13} aria-hidden /> 视觉风格</legend>
                <div className="gcc-style-chips">
                  {VISUAL_STYLES.map((item) => (
                    <button key={item.id} className={visualStyle === item.id ? 'is-active' : ''} aria-pressed={visualStyle === item.id} onClick={() => { invalidateComposer(); setVisualStyle(item.id) }}>{item.label}</button>
                  ))}
                </div>
                <p className="gcc-sg-note">影响海报、信息图、封面的配色与版式，不改变文案。</p>
              </fieldset>

              <fieldset>
                <legend><MessagesSquare size={13} aria-hidden /> 联系方式</legend>
                <div className="gcc-segmented">
                  {([['none', '不展示'], ['text', '文字'], ['qr', '上传二维码']] as const).map(([value, label]) => <button key={value} className={contactMode === value ? 'is-active' : ''} onClick={() => { invalidateComposer(); setContactMode(value); if (value !== 'qr') setQrError('') }}>{label}</button>)}
                </div>
                {contactMode === 'text' && <input className="gcc-contact-input" value={contactText} onChange={(event) => { invalidateComposer(); setContactText(event.target.value) }} placeholder="仅使用你明确输入的联系方式" maxLength={120} />}
                <p className="gcc-sg-note">默认不出现在任何成品图与文案里；选「文字」或「二维码」后才会进入 prompt、成图与文案，并过全链校验。</p>
              </fieldset>

              <fieldset>
                <legend><QrCode size={13} aria-hidden /> 二维码</legend>
                <label className={`gcc-qr-upload ${qrReference ? 'is-valid' : ''}`}>
                  <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => {
                    const file = event.target.files?.[0]
                    // F8：先清空 value，上传失败后重选同一文件也能再次触发 onChange
                    event.target.value = ''
                    void uploadQr(file)
                  }} />
                  {uploadingQr ? <Loader2 className="gcc-spin" size={16} aria-hidden /> : qrReference ? <Check size={16} aria-hidden /> : <QrCode size={16} aria-hidden />}
                  <span>{qrReference ? `已验证：${qrReference.payload_preview}` : '上传二维码图片（PNG / JPG）'}</span>
                </label>
                {qrError && <p className="gcc-inline-error" role="alert">{qrError}</p>}
                <p className="gcc-sg-note">只使用你上传的这一张，AI 不会自己画；生成后逐张校验扫出来的内容与你上传的一致。联系方式选「上传二维码」时才会出现在成品里。</p>
              </fieldset>

              <fieldset ref={evidenceSettingsRef}>
                <legend><ShieldCheck size={13} aria-hidden /> 真实证据（可选）</legend>
                <select aria-label="证据来源" value={evidenceType} onChange={(event) => { invalidateComposer(); const next = event.target.value as 'none' | 'latest_diagnosis'; setEvidenceType(next); if (next === 'none') setBrandId(null) }}>
                  <option value="none">不引用客户证据</option>
                  <option value="latest_diagnosis">引用某个客户的最新已发布诊断</option>
                </select>
                {evidenceType !== 'none' && <select aria-label="证据客户" value={brandId || ''} onChange={(event) => { invalidateComposer(); setBrandId(event.target.value ? Number(event.target.value) : null) }}><option value="">请选择客户（不会默认选第一个）</option>{brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.name}</option>)}</select>}
                <a className="gcc-diagnosis-link" href="/history">查看已有诊断记录</a>
                <p className="gcc-sg-note">普通推广不强制引用证据；只有引用诊断数字时，系统才核验后台真实数据。</p>
              </fieldset>

              <fieldset>
                <legend><Info size={13} aria-hidden /> 品牌信息</legend>
                <p className="gcc-sg-note" data-testid="gcc-brand-status">
                  {serviceBrand?.configured
                    ? `已配置品牌：${serviceBrand.name}，成品将展示该公司名${serviceBrand.has_logo ? '与 Logo' : ''}。`
                    : '未配置品牌：成品将不含品牌信息，可去品牌信息设置填写公司名称与 Logo。'}
                </p>
                <a className="gcc-diagnosis-link" href="/agent/whitelabel">{serviceBrand?.configured ? '修改品牌信息' : '去品牌信息设置'}</a>
              </fieldset>

              <fieldset>
                <legend><BookOpen size={13} aria-hidden /> 产品事实库</legend>
                <p className="gcc-sg-note" data-testid="gcc-facts-status">
                  {productFacts?.version
                    ? `产品事实库 v${productFacts.version} · ${productFacts.fact_count ?? 0} 条，平台真实能力已核验，生成文案时自动引用。`
                    : '产品事实库暂时读取失败，不影响正常生成。'}
                </p>
              </fieldset>

              <fieldset>
                <legend><ImageIcon size={13} aria-hidden /> 输出规格</legend>
                <div className="gcc-spec-two">
                  <label>清晰度
                    <select aria-label="清晰度" value={resolution} onChange={(event) => { invalidateComposer(); setResolution(event.target.value as ResolutionId) }}>
                      {RESOLUTIONS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                    </select>
                  </label>
                  <label>朋友圈版式
                    <select aria-label="朋友圈版式" value={momentsLayout} onChange={(event) => { invalidateComposer(); setMomentsLayout(event.target.value as MomentsLayoutId) }}>
                      {MOMENTS_LAYOUT_OPTIONS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                    </select>
                  </label>
                </div>
                <p className="gcc-sg-note">高清与九宫格出图更多，生成时间更长，按实际产出计费。</p>
              </fieldset>

              <fieldset>
                <legend><Flame size={13} aria-hidden /> 关联近期热点</legend>
                <label className="gcc-switch-row"><input type="checkbox" checked={trend} onChange={(event) => { invalidateComposer(); setTrend(event.target.checked) }} /><span aria-hidden><i /></span><b>{trend ? '尝试关联' : '不关联'}</b></label>
                <p className="gcc-sg-note">当前没有可靠热点源时自动生成常青内容，不会编造“近期热搜”。</p>
              </fieldset>
            </div>
          </div>
        )}

        <div className="gcc-generate-row">
          <span className="gcc-trust"><ShieldCheck size={15} aria-hidden /> 素材只存你的私有空间 · 二维码只认你上传的那张 · 联系方式默认不出现在成品里</span>
          {/* 创建前提醒预览:有提醒时按钮变为显式确认动作(P1-2 诚实化) */}
          {strategyWarnings.length > 0 && <WarningsBar warnings={strategyWarnings} scope="job" />}
          <button className="gcc-primary" onClick={() => void generate()} disabled={generating || understanding || brief.trim().length < 4}>
            {generating ? <Loader2 className="gcc-spin" size={17} aria-hidden /> : <Sparkles size={17} aria-hidden />}
            {generating
              ? '正在生成完整推广包'
              : (strategyWarnings.length > 0 || (job?.warnings?.length ?? 0) > 0 ? '知道了，继续生成' : '生成完整推广包')}
          </button>
          {/* 🔴 [包三 · 图文] 等待期给的是**人话**,不是一个转圈。
              她要判断的只有两件事:还要多久、能不能走开。不说,她就会守着屏幕,
              或者以为卡住了反复点(而点一次就是一次扣费确认)。
              没有真实进度可报时,**不编百分比** —— 编出来的进度条比没有更糟。 */}
          {generating && (
            <p data-testid="gcc-generating-wait" className="gcc-trust">
              大概要一两分钟。可以先去做别的,回来这一页会自己更新;不用重复点。
            </p>
          )}
          {/* 🔴 [#65] 价格看得见。取不到价一个字都不显示 —— FeatureCostBadge 的空态是
              「价目待配置」(给开发看的),在 C 补上 feature_pricing 行之前不能漏给用户。 */}
          {bundleCost != null && !generating && (
            <span data-testid="gcc-bundle-cost" className="gcc-trust">
              <FeatureCostBadge featureCode={bundleCode} variant="inline" prefix="这次" />
            </span>
          )}
        </div>
      </section>

      {requestIssue && (
        <section className="gcc-recovery" role="alert" data-error-code={requestIssue.code}>
          <AlertTriangle size={21} aria-hidden />
          <div>
            <h2>{requestIssue.message}</h2>
            <p>{requestIssue.reason}</p>
            {requestIssue.repair_hint && <p className="gcc-recovery-repair"><em>AI 可修</em>{requestIssue.repair_hint}</p>}
            {requestIssue.rule_version && <p className="gcc-recovery-rule">规则版本 {requestIssue.rule_version}</p>}
          </div>
          <div className="gcc-recovery-actions">
            {requestIssue.actions.map((action) => <button key={action.id} onClick={() => resolveRequestIssue(action.id)}>{action.label}</button>)}
            {requestIssue.code === 'PUBLISHED_DIAGNOSIS_EVIDENCE_NOT_FOUND' && <a href="/history">查看诊断记录</a>}
          </div>
        </section>
      )}
      {pageError && <div className="gcc-alert" role="alert"><AlertTriangle size={17} aria-hidden /><span>{pageError}</span><button onClick={() => setPageError('')}>知道了</button></div>}

      <section className="gcc-results" aria-label="推广包结果">
        {!job && !generating ? (
          <div className="gcc-empty-result">
            <div><WandSparkles size={28} aria-hidden /></div>
            <h2>完整内容包会出现在这里</h2>
            <p>按渠道分段展示：朋友圈、小红书、抖音封面、海报、信息图与聊天素材的拟真预览、文案与可执行操作。</p>
          </div>
        ) : (
          <>
            <div className="gcc-status-panel">
              <div className="gcc-result-bar">
                <div className="gcc-result-meta">
                  <span className={`gcc-status ${job?.status || 'generating'}`}>{job ? STATUS_TEXT[job.status] : '生成中'}</span>
                  {job && (
                    <span>
                      {channelStats.total} 个渠道 · <b>{channelStats.done} 个已完成</b>
                      {channelStats.failed > 0 ? ` · ${channelStats.failed} 个需重做` : ''}
                    </span>
                  )}
                  {job?.teacher && <span>策略导师：{job.teacher.name} · v{job.teacher.version}</span>}
                  {job && <span>{job.is_revision ? `修订版本 ${job.revision_no || ''}` : `版本 #${job.job_id}`}</span>}
                </div>
                <div className="gcc-header-actions">
                  {allFailedIds.length > 0 && (
                    <button className="gcc-btn" onClick={() => void redoComponents(allFailedIds)} disabled={generating}>
                      <RefreshCw size={15} aria-hidden /> 只重做失败项
                    </button>
                  )}
                  {job && (
                    <button className="gcc-btn" onClick={downloadAllCopy}>
                      <Download size={15} aria-hidden /> 下载全部文案
                    </button>
                  )}
                  <button className="gcc-btn" onClick={() => {
                    composerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                    composerRef.current?.querySelector('textarea')?.focus()
                  }}>
                    <Pencil size={15} aria-hidden /> 改需求重生成
                  </button>
                </div>
              </div>
              {(job?.evidence?.source_note || job?.trend || job?.contact?.mode === 'none') && (
                <div className="gcc-source-notes">
                  {job?.evidence?.source_note && <div className="gcc-source-note"><ShieldCheck size={14} aria-hidden />{job.evidence.source_note}</div>}
                  {job?.trend && <div className="gcc-source-note"><Flame size={14} aria-hidden />{job.trend.used ? `热点钩子：${job.trend.topic || '已验证主题'} · ${job.trend.observed_at || '已记录时间'}${job.trend.source ? ` · ${job.trend.source}` : ''}` : job.trend.requested ? `热点未采用：${job.trend.reason || '相关性或证据不足'}；本版使用常青内容` : '本版未请求热点，使用常青内容'}</div>}
                  {job?.contact?.mode === 'none' && <div className="gcc-source-note"><ShieldCheck size={14} aria-hidden />联系方式未展示；历史重做沿用该冻结设置</div>}
                </div>
              )}
              {(job?.warnings?.length ?? 0) > 0 && <WarningsBar warnings={job!.warnings!} scope="job" />}
            </div>
            <div className="gcc-channel-sections">
              {(job?.channels || channels).map((id) => (
                <ChannelSection
                  key={id}
                  channelId={id}
                  job={job}
                  generating={generating}
                  objectUrls={assetObjectUrls}
                  onRetry={(ids) => void redoComponents(ids)}
                  onSaveCopy={saveAssetCopy}
                />
              ))}
            </div>
          </>
        )}
      </section>
    </main>
  )
}
