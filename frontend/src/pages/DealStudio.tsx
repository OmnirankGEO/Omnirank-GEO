/**
 * 晒成交（Deal Studio）— Phase 3B 完整实现。
 *
 * 四步单页流：① 提供素材（图片/语音/表单三输入混合）→ ② 确认成交信息
 * （12 字段确认单，tentative 待确认高亮，编辑即确认）→ ③ 隐私打码
 * （自动检测/框选新增/点选恢复/强度三档/逐张预览确认）→ ④ 生成整套内容
 * （showcase_deal 内容包，渠道分段复用 geoChannelShared.ChannelSection）。
 *
 * 工程模式与主流程（GeoContentCenter）同构：作用域 localStorage 锚点
 * （u{id}:p{permVer}）、持久 request_id 幂等、丢响应 by-request 恢复 + 404
 * 宽限、迟到响应代际围栏、重复提交防护、局部重试、人话错误。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BadgeDollarSign,
  Check,
  ChevronDown,
  CircleAlert,
  Download,
  Eraser,
  History,
  ImagePlus,
  Info,
  ListChecks,
  Loader2,
  Mic,
  MousePointerSquareDashed,
  Pencil,
  Plus,
  QrCode,
  ScanSearch,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Undo2,
  WandSparkles,
  X,
} from 'lucide-react'
import { toast } from 'sonner'

import { authFetch } from '@/lib/api'
import { useAuth } from '@/context/AuthContext'
import {
  ChannelSection,
  STATUS_TEXT,
  WarningsBar,
  assetSlot,
  channelMeta,
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
  warningMessage,
  type ChannelId,
  type JobState,
  type JobWarning,
} from './geoChannelShared'
import './GeoContentCenter.css'
import './DealStudio.css'

/* ---------- 确认单契约（与 services/marketing/deal_intake.py SHEET_FIELDS 对齐） ---------- */
type SheetStatus = 'confirmed' | 'tentative'
type SheetEntry = { value: string; status: SheetStatus; provenance?: string }

const SHEET_FIELDS: Array<{ key: string; label: string; multiline?: boolean; span2?: boolean }> = [
  { key: 'what_happened', label: '发生了什么', multiline: true, span2: true },
  { key: 'deal_amount', label: '成交金额' },
  { key: 'deal_time', label: '成交时间' },
  { key: 'customer_industry', label: '客户行业' },
  { key: 'service_content', label: '服务内容' },
  { key: 'deal_reason', label: '成交原因', multiline: true, span2: true },
  { key: 'customer_praise', label: '客户认可点' },
  { key: 'target_channels', label: '目标渠道' },
  { key: 'quotable_lines', label: '可用原话（会放进晒单文案）', multiline: true, span2: true },
  { key: 'image_materials', label: '图片素材说明' },
  { key: 'privacy_redaction_items', label: '需要遮住的隐私信息' },
  { key: 'contact_and_qr', label: '联系方式与二维码' },
]
const SHEET_FIELD_KEYS = SHEET_FIELDS.map((field) => field.key)

/* 第一步紧凑表单的字段（确认单字段的子集；知道多少填多少） */
const INTAKE_FORM_FIELDS: Array<{ key: string; label: string; multiline?: boolean; placeholder?: string }> = [
  { key: 'deal_amount', label: '成交金额', placeholder: '例如：3 万；不方便公开就写「五位数」' },
  { key: 'deal_time', label: '成交时间', placeholder: '例如：2026 年 6 月' },
  { key: 'customer_industry', label: '客户行业', placeholder: '例如：全屋定制（家装）' },
  { key: 'service_content', label: '服务内容', placeholder: '例如：GEO 诊断 + 3 个月优化' },
  { key: 'deal_reason', label: '成交原因', placeholder: '例如：老客户转介绍，看过诊断报告样例' },
  { key: 'customer_praise', label: '客户认可点', placeholder: '例如：报告讲得清楚，先看到数据再决定' },
  { key: 'what_happened', label: '简单说说这单怎么成的', multiline: true, placeholder: '用自己的话讲一遍过程就行' },
]
const INTAKE_FORM_KEYS = INTAKE_FORM_FIELDS.map((field) => field.key)

/* 晒成交可选渠道：5 个 deal_* + 朋友圈/小红书/抖音复用 */
const DEAL_CHANNEL_PICKER: ChannelId[] = [
  'deal_poster', 'deal_chat', 'deal_data_card', 'deal_story', 'deal_feedback_card',
  'moments', 'xiaohongshu', 'douyin',
]
const DEFAULT_DEAL_CHANNELS: ChannelId[] = ['deal_poster', 'deal_chat', 'deal_data_card', 'deal_story', 'moments']

/* 打码契约（与 services/marketing/redaction.py 对齐） */
const REGION_KIND_LABELS: Record<string, string> = {
  name: '姓名/昵称', avatar: '头像', phone: '手机号', wechat: '微信号', address: '地址',
  idcard: '证件号', bank: '银行卡号', order_no: '订单号', contract_no: '合同编号',
  seal: '公章', signature: '签名', customer_name: '客户名', amount: '金额', other: '其他隐私',
}
const ADD_KIND_OPTIONS = ['other', 'name', 'customer_name', 'phone', 'wechat', 'amount', 'address', 'order_no', 'contract_no', 'avatar']
const STRENGTH_OPTIONS = [
  { id: 'light', label: '细腻' },
  { id: 'medium', label: '适中（推荐）' },
  { id: 'heavy', label: '更重更稳' },
] as const
type StrengthId = typeof STRENGTH_OPTIONS[number]['id']

const VISUAL_STYLES = [
  { id: 'festive', label: '喜庆红金（晒单默认）' },
  { id: 'business', label: '商务简洁' },
  { id: 'bright', label: '明亮活泼' },
  { id: 'tech_dark', label: '科技深色' },
] as const
const RESOLUTIONS = [
  { id: '1k', label: '标准 1K（默认）' },
  { id: '2k', label: '高清 2K' },
  { id: '4k', label: '超清 4K' },
] as const
const MOMENTS_LAYOUT_OPTIONS = [
  { id: 'single', label: '单图（默认）' },
  { id: 'grid', label: '九宫格 · 9 张' },
] as const

type RedactionRegion = { id: string; kind: string; box: number[]; source: string; active: boolean }
type RedactionWarning = { code: string; message?: string }
type RedactionState = {
  auto_regions: RedactionRegion[]
  manual_regions: RedactionRegion[]
  restored_ids: string[]
  strength: string
  status: string
  detection: string
  warnings?: RedactionWarning[]
  has_preview: boolean
  has_output: boolean
}
type DealMaterial = {
  material_id: string
  width: number | null
  height: number | null
  size_bytes: number | null
  original_filename: string
  ocr_text: string
  extracted: boolean
  redaction: RedactionState
}
type DealDraft = {
  id: number
  brand_id: number | null
  request_id: string
  form: Record<string, string>
  materials: DealMaterial[]
  sheet: Record<string, unknown>
  status: string
  created_at?: string
  updated_at?: string
}

/* ---------- localStorage 作用域锚点（照抄主流程 u{id}:p{permVer} 模式） ---------- */
const DEAL_ANCHOR_KEY_PREFIX = 'geo_deal_studio_draft_v1'
const DEAL_PENDING_REQUEST_KEY_PREFIX = 'geo_deal_studio_pending_request_v1'
const DEAL_PENDING_RETRY_KEY_PREFIX = 'geo_deal_studio_pending_retry_v1'

type DealAnchor = {
  requestId: string
  draftId?: number
  step?: number
  maxStep?: number
  channels?: ChannelId[]
  anonymous?: boolean
  contactMode?: 'none' | 'text' | 'qr'
  visualStyle?: string
  resolution?: string
  momentsLayout?: string
  updatedAt: number
}

function readDealAnchor(storageKey: string | null): DealAnchor | null {
  if (!storageKey) return null
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || 'null') as DealAnchor | null
    if (!value || typeof value.requestId !== 'string' || !Number.isFinite(value.updatedAt)) return null
    if (Date.now() - value.updatedAt > 7 * 86400_000) return null
    return value
  } catch { return null }
}

/* ---------- 确认单读取 ---------- */
function sheetEntryOf(sheet: Record<string, unknown> | null | undefined, key: string): SheetEntry {
  const raw = sheet?.[key]
  if (raw && typeof raw === 'object') {
    const entry = raw as Record<string, unknown>
    return {
      value: String(entry.value ?? ''),
      status: entry.status === 'confirmed' ? 'confirmed' : 'tentative',
      provenance: typeof entry.provenance === 'string' ? entry.provenance : 'customer_asserted',
    }
  }
  return { value: '', status: 'tentative' }
}

function sheetValuesOf(sheet: Record<string, unknown> | null | undefined): Record<string, string> {
  const values: Record<string, string> = {}
  SHEET_FIELD_KEYS.forEach((key) => { values[key] = sheetEntryOf(sheet, key).value })
  return values
}

function sheetStatusOf(sheet: Record<string, unknown> | null | undefined): Record<string, SheetStatus> {
  const statuses: Record<string, SheetStatus> = {}
  SHEET_FIELD_KEYS.forEach((key) => { statuses[key] = sheetEntryOf(sheet, key).status })
  return statuses
}

/* AI 提取标注(Owner 2026-07-22 提醒不阻断):sheet._meta.warnings 逐字段提醒。 */
type SheetWarning = { code: string; field?: string; message?: string }

function sheetWarningsOf(sheet: Record<string, unknown> | null | undefined): SheetWarning[] {
  const meta = sheet?._meta
  if (!meta || typeof meta !== 'object') return []
  const warnings = (meta as Record<string, unknown>).warnings
  if (!Array.isArray(warnings)) return []
  return warnings.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const entry = item as Record<string, unknown>
    return [{
      code: String(entry.code || 'ai_extraction_beyond_materials'),
      field: typeof entry.field === 'string' ? entry.field : undefined,
      message: typeof entry.message === 'string' ? entry.message : undefined,
    }]
  })
}

function sheetHasValues(sheet: Record<string, unknown> | null | undefined): boolean {
  return SHEET_FIELD_KEYS.some((key) => sheetEntryOf(sheet, key).value.trim().length > 0)
}

function serializeChannels(channels: ChannelId[]): string {
  return channels.map((id) => channelMeta(id)?.short || id).join('、')
}

function buildDealBrief(values: Record<string, string>, anonymous: boolean): string {
  const core = (values.what_happened || '').trim() || (values.service_content || '').trim() || '一笔真实成交'
  const industry = (values.customer_industry || '').trim()
  let text = anonymous
    ? `晒出一笔真实成交（客户与品牌均已匿名）：${core}`
    : `晒出一笔真实成交：${core}${industry ? `（客户行业：${industry}）` : ''}`
  if (text.length < 4) text = '晒出一笔真实成交'
  return text.slice(0, 500)
}

function redactionRegions(material: DealMaterial | null | undefined): Array<RedactionRegion & { restored: boolean }> {
  if (!material) return []
  const state = material.redaction || ({} as RedactionState)
  const restored = new Set(state.restored_ids || [])
  return [...(state.auto_regions || []), ...(state.manual_regions || [])].map((region) => ({
    ...region,
    restored: !region.active || restored.has(String(region.id)),
  }))
}

function formatBytes(size: number | null | undefined): string {
  const value = Number(size || 0)
  if (!Number.isFinite(value) || value <= 0) return ''
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)}MB`
  return `${Math.max(1, Math.round(value / 1024))}KB`
}

/* ---------- 授权素材图（私有存储,授权端点读取,objectURL 本地释放） ---------- */
function MaterialImage(props: {
  draftId: number
  materialId: string
  variant: 'original' | 'preview' | 'output'
  tick: number
  alt: string
  className?: string
  onReady?: (naturalWidth: number, naturalHeight: number) => void
}) {
  const { draftId, materialId, variant, tick, alt, className, onReady } = props
  const [url, setUrl] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    setUrl('')
    setFailed(false)
    void (async () => {
      try {
        const response = await authFetch(
          `/api/marketing/deal-drafts/${draftId}/materials/${materialId}/file?variant=${variant}`,
          { signal: controller.signal },
        )
        if (!response.ok) throw new Error('material_load_failed')
        const blob = await response.blob()
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      } catch {
        if (!controller.signal.aborted) setFailed(true)
      }
    })()
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [draftId, materialId, variant, tick])
  if (failed) {
    return <div className={`gcc-deal-img-state is-error ${className || ''}`} role="alert"><AlertTriangle size={16} aria-hidden /> 预览加载失败，请刷新重试</div>
  }
  if (!url) {
    return <div className={`gcc-deal-img-state ${className || ''}`}><Loader2 className="gcc-spin" size={17} aria-hidden /> 加载中</div>
  }
  return (
    <img
      src={url}
      alt={alt}
      className={className}
      draggable={false}
      onLoad={(event) => {
        const img = event.currentTarget
        onReady?.(img.naturalWidth, img.naturalHeight)
      }}
    />
  )
}

/* ---------- 语音录入（复用 社媒录音组件 的 WAV 16kHz 方案,POST 目标改为 deal voice） ---------- */
type RecState = 'idle' | 'recording' | 'recorded' | 'uploading'

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buf = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buf)
  const write = (offset: number, text: string) => { for (let index = 0; index < text.length; index++) view.setUint8(offset + index, text.charCodeAt(index)) }
  write(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); write(8, 'WAVE')
  write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  write(36, 'data'); view.setUint32(40, samples.length * 2, true)
  for (let index = 0; index < samples.length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(44 + index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true)
  }
  return new Blob([buf], { type: 'audio/wav' })
}

function DealVoiceRecorder(props: {
  disabled?: boolean
  maxDuration?: number
  uploadVoice: (blob: Blob) => Promise<{ ok: boolean; transcript?: string; error?: string }>
  onTranscribed: (text: string) => void
}) {
  const { disabled = false, maxDuration = 120, uploadVoice, onTranscribed } = props
  const [state, setState] = useState<RecState>('idle')
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState('')
  const streamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const chunksRef = useRef<Float32Array[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval>>()
  const startTimeRef = useRef(0)
  const wavBlobRef = useRef<Blob | null>(null)

  const cleanup = useCallback(() => {
    processorRef.current?.disconnect()
    audioCtxRef.current?.close().catch(() => {})
    streamRef.current?.getTracks().forEach((track) => track.stop())
    processorRef.current = null
    audioCtxRef.current = null
    streamRef.current = null
    chunksRef.current = []
    if (timerRef.current) clearInterval(timerRef.current)
  }, [])

  useEffect(() => () => cleanup(), [cleanup])

  const stopRecording = useCallback(() => {
    const duration = Math.floor((Date.now() - startTimeRef.current) / 1000)
    if (timerRef.current) clearInterval(timerRef.current)
    const total = chunksRef.current.reduce((sum, chunk) => sum + chunk.length, 0)
    const merged = new Float32Array(total)
    let offset = 0
    for (const chunk of chunksRef.current) { merged.set(chunk, offset); offset += chunk.length }
    cleanup()
    if (duration < 1 || total < 8000) {
      setError('录音太短，请多说几句')
      setState('idle')
      return
    }
    wavBlobRef.current = encodeWav(merged, 16000)
    setElapsed(duration)
    setState('recorded')
  }, [cleanup])

  const startRecording = useCallback(async () => {
    if (disabled) return
    setError('')
    wavBlobRef.current = null
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('需要 HTTPS 环境才能录音')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } })
      streamRef.current = stream
      const context = new AudioContext({ sampleRate: 16000 })
      audioCtxRef.current = context
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      processorRef.current = processor
      chunksRef.current = []
      processor.onaudioprocess = (event) => {
        chunksRef.current.push(new Float32Array(event.inputBuffer.getChannelData(0)))
      }
      source.connect(processor)
      processor.connect(context.destination)
      startTimeRef.current = Date.now()
      setState('recording')
      setElapsed(0)
      timerRef.current = setInterval(() => {
        const seconds = Math.floor((Date.now() - startTimeRef.current) / 1000)
        setElapsed(seconds)
        if (seconds >= maxDuration) stopRecording()
      }, 500)
    } catch (err) {
      const name = (err as { name?: string })?.name
      if (name === 'NotAllowedError') setError('麦克风权限被拒绝')
      else if (name === 'NotFoundError') setError('未检测到麦克风')
      else setError('录音失败，请重试')
    }
  }, [disabled, maxDuration, stopRecording])

  const sendVoice = useCallback(async () => {
    if (!wavBlobRef.current) return
    setState('uploading')
    setError('')
    const result = await uploadVoice(wavBlobRef.current)
    if (result.ok && result.transcript) {
      onTranscribed(result.transcript)
      wavBlobRef.current = null
      setState('idle')
      return
    }
    if (result.error) setError(result.error)
    setState('recorded')
  }, [onTranscribed, uploadVoice])

  const discard = useCallback(() => {
    wavBlobRef.current = null
    setState('idle')
    setElapsed(0)
    setError('')
  }, [])

  const formatTime = (seconds: number) => `${Math.floor(seconds / 60)}:${(seconds % 60).toString().padStart(2, '0')}`

  return (
    <div className="gcc-deal-voice" data-testid="deal-voice-recorder">
      {state === 'recording' && (
        <div className="gcc-deal-voice-bar is-recording">
          <span className="gcc-deal-rec-dot" aria-hidden />
          <span className="gcc-deal-voice-label">录音中 {formatTime(elapsed)}</span>
          <button type="button" onClick={stopRecording}><Square size={12} aria-hidden fill="currentColor" /> 停止</button>
        </div>
      )}
      {state === 'recorded' && (
        <div className="gcc-deal-voice-bar">
          <Mic size={15} aria-hidden />
          <span className="gcc-deal-voice-label">语音 {formatTime(elapsed)}</span>
          <span className="gcc-deal-voice-hint">点发送识别</span>
          <button type="button" className="gcc-deal-voice-discard" onClick={discard} aria-label="丢弃录音"><Trash2 size={14} aria-hidden /></button>
          <button type="button" className="gcc-deal-voice-send" onClick={() => void sendVoice()}><Send size={13} aria-hidden /> 发送</button>
        </div>
      )}
      {state === 'uploading' && (
        <div className="gcc-deal-voice-bar is-uploading">
          <Loader2 className="gcc-spin" size={15} aria-hidden />
          <span className="gcc-deal-voice-label">语音识别中…</span>
        </div>
      )}
      {state === 'idle' && (
        <button type="button" className="gcc-deal-mic-btn" onClick={() => void startRecording()} disabled={disabled} aria-label="开始录音">
          <Mic size={22} aria-hidden />
        </button>
      )}
      {state === 'idle' && <span className="gcc-deal-voice-tip">点麦克风开始，像跟朋友讲一样说这单怎么成的</span>}
      {error && <p className="gcc-inline-error" role="alert">{error}</p>}
    </div>
  )
}

/* ================================ 主组件 ================================ */
type OperationScope = 'draft' | 'analyze' | 'upload' | 'voice' | 'redact' | 'sheet' | 'generation' | 'history' | 'edit' | 'asset' | 'qr'

/* 二维码参考图凭证(与 GeoContentCenter 同构;/api/marketing/qr-reference 返回) */
type QrReference = {
  reference_id: string
  payload_hash: string
  file_sha256: string
  payload_preview: string
  reference_token: string
  organization_id?: number | null
  uploaded_at: string
}

const RECOVERY_404_GRACE_MS = 5 * 60_000
const RECOVERY_RETRY_MS = 1200
const DEAL_IMAGE_MAX = 15 * 1024 * 1024
const DEAL_IMAGE_TOTAL_MAX = 20

export default function DealStudio({ onBack }: { onBack: () => void }) {
  const { user: authUser, authorizationScope } = useAuth()
  const browserOwnerScope = authUser
    ? `u${authUser.id}:p${authUser.permission_version ?? 0}`
    : null
  const storageKeys = useMemo(() => browserOwnerScope ? ({
    anchor: `${DEAL_ANCHOR_KEY_PREFIX}:${browserOwnerScope}`,
    pendingRequest: `${DEAL_PENDING_REQUEST_KEY_PREFIX}:${browserOwnerScope}`,
    pendingRetry: `${DEAL_PENDING_RETRY_KEY_PREFIX}:${browserOwnerScope}`,
  }) : null, [browserOwnerScope])

  const [step, setStep] = useState(1)
  const [maxStep, setMaxStep] = useState(1)
  const [restoring, setRestoring] = useState(true)
  const [draft, setDraft] = useState<DealDraft | null>(null)
  const [requestIdState, setRequestIdState] = useState('')
  const [form, setForm] = useState<Record<string, string>>({})
  const [formOpen, setFormOpen] = useState(false)
  const [anonymous, setAnonymous] = useState(true)
  const [channels, setChannels] = useState<ChannelId[]>(DEFAULT_DEAL_CHANNELS)
  const [transcriptEdited, setTranscriptEdited] = useState<string | null>(null)
  const [sheetValues, setSheetValues] = useState<Record<string, string> | null>(null)
  const [sheetStatus, setSheetStatus] = useState<Record<string, SheetStatus> | null>(null)
  const [sheetWarnings, setSheetWarnings] = useState<SheetWarning[]>([])
  const [analyzing, setAnalyzing] = useState(false)
  const [reanalyzeConfirm, setReanalyzeConfirm] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [voiceBusy, setVoiceBusy] = useState(false)
  const [patching, setPatching] = useState(false)
  const [redactBusy, setRedactBusy] = useState('')
  const [activeMaterialId, setActiveMaterialId] = useState('')
  const [selectMode, setSelectMode] = useState(false)
  const [addKind, setAddKind] = useState('other')
  const [previewTick, setPreviewTick] = useState(0)
  const [selectRect, setSelectRect] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [contactMode, setContactMode] = useState<'none' | 'text' | 'qr'>('none')
  const [contactText, setContactText] = useState('')
  // 二维码联系方式(2026-07-23 外部审查 P2-2):与主流程同一上传闭环,
  // 上传时记组织上下文,生成前校验 reference_token。
  const [qrReference, setQrReference] = useState<QrReference | null>(null)
  const [qrError, setQrError] = useState('')
  const [uploadingQr, setUploadingQr] = useState(false)
  const [visualStyle, setVisualStyle] = useState<string>('festive')
  const [resolution, setResolution] = useState<string>('1k')
  const [momentsLayout, setMomentsLayout] = useState<string>('single')
  const [job, setJob] = useState<JobState | null>(null)
  const [generating, setGenerating] = useState(false)
  const [history, setHistory] = useState<JobState[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [assetObjectUrls, setAssetObjectUrls] = useState<Record<number, string>>({})
  const [pageError, setPageError] = useState('')

  const draftRef = useRef<DealDraft | null>(null)
  const requestIdRef = useRef('')
  const assetObjectUrlsRef = useRef<Record<number, string>>({})
  const editedCopyRef = useRef(new Map<number, string>())
  const operationSeq = useRef(0)
  const operations = useRef(new Map<OperationScope, { generation: number; controller: AbortController }>())
  const pollTimer = useRef<number | null>(null)
  const strengthTimer = useRef<number | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const dragStartRef = useRef<{ x: number; y: number } | null>(null)
  // F12：按 material_id 存实测尺寸，切素材后不会错用上一张的坐标系
  const measuredSizeRef = useRef<Record<string, { width: number; height: number }>>({})

  /* ---------- 代际围栏（迟到响应不得写入已被取代的视图） ---------- */
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
    operations.current.forEach((operation) => operation.controller.abort())
    operations.current.clear()
    if (pollTimer.current != null) window.clearTimeout(pollTimer.current)
    pollTimer.current = null
    if (strengthTimer.current != null) window.clearTimeout(strengthTimer.current)
    strengthTimer.current = null
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

  useEffect(() => () => cancelActive(), [cancelActive])
  useEffect(() => () => {
    Object.values(assetObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    assetObjectUrlsRef.current = {}
  }, [])

  /* 与主流程一致：确认新 owner 后只保留当前作用域的锚点键 */
  useLayoutEffect(() => {
    if (!browserOwnerScope) return
    const allowed = new Set([
      `${DEAL_ANCHOR_KEY_PREFIX}:${browserOwnerScope}`,
      `${DEAL_PENDING_REQUEST_KEY_PREFIX}:${browserOwnerScope}`,
      `${DEAL_PENDING_RETRY_KEY_PREFIX}:${browserOwnerScope}`,
    ])
    const prefixes = [DEAL_ANCHOR_KEY_PREFIX, DEAL_PENDING_REQUEST_KEY_PREFIX, DEAL_PENDING_RETRY_KEY_PREFIX]
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index)
      if (key && prefixes.some((prefix) => key.startsWith(`${prefix}:`)) && !allowed.has(key)) {
        localStorage.removeItem(key)
      }
    }
  }, [browserOwnerScope])

  const applyDraft = useCallback((next: DealDraft | null) => {
    draftRef.current = next
    setDraft(next)
  }, [])

  const applySheetState = useCallback((source: DealDraft) => {
    setSheetValues(sheetValuesOf(source.sheet))
    setSheetStatus(sheetStatusOf(source.sheet))
    setSheetWarnings(sheetWarningsOf(source.sheet))
  }, [])

  /* ---------- 锚点持久化（不含表单/转写等敏感内容,只存恢复所需锚点与偏好） ---------- */
  useEffect(() => {
    if (!storageKeys || !requestIdState || restoring) return
    const anchor: DealAnchor = {
      requestId: requestIdState,
      draftId: draft?.id,
      step,
      maxStep,
      channels,
      anonymous,
      contactMode,
      visualStyle,
      resolution,
      momentsLayout,
      updatedAt: Date.now(),
    }
    localStorage.setItem(storageKeys.anchor, JSON.stringify(anchor))
  }, [storageKeys, requestIdState, restoring, draft?.id, step, maxStep, channels, anonymous, contactMode, visualStyle, resolution, momentsLayout])

  const goToStep = useCallback((next: number) => {
    setStep(next)
    setMaxStep((current) => Math.max(current, next))
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  /* ---------- 刷新恢复：读草稿 → 恢复到所在步骤 ---------- */
  useEffect(() => {
    if (!storageKeys) { setRestoring(false); return }
    const anchor = readDealAnchor(storageKeys.anchor)
    if (!anchor) { setRestoring(false); return }
    requestIdRef.current = anchor.requestId
    setRequestIdState(anchor.requestId)
    if (Array.isArray(anchor.channels) && anchor.channels.length) {
      const valid = anchor.channels.filter((id): id is ChannelId => DEAL_CHANNEL_PICKER.includes(id))
      if (valid.length) setChannels(valid)
    }
    if (typeof anchor.anonymous === 'boolean') setAnonymous(anchor.anonymous)
    if (anchor.contactMode === 'none' || anchor.contactMode === 'text' || anchor.contactMode === 'qr') setContactMode(anchor.contactMode)
    if (VISUAL_STYLES.some((item) => item.id === anchor.visualStyle)) setVisualStyle(String(anchor.visualStyle))
    if (RESOLUTIONS.some((item) => item.id === anchor.resolution)) setResolution(String(anchor.resolution))
    if (MOMENTS_LAYOUT_OPTIONS.some((item) => item.id === anchor.momentsLayout)) setMomentsLayout(String(anchor.momentsLayout))
    if (!anchor.draftId) { setRestoring(false); return }
    const { generation, controller } = beginOperation('draft')
    void (async () => {
      try {
        const response = await authFetch(`/api/marketing/deal-drafts/${anchor.draftId}`, { signal: controller.signal })
        const data = await readJson<{ draft?: DealDraft; detail?: unknown }>(response)
        if (!isCurrent('draft', generation)) return
        if (!response.ok || !data.draft) {
          // 草稿不可读（跨账号/已清理）：清锚点重来,不恢复他人数据。
          localStorage.removeItem(storageKeys.anchor)
          return
        }
        applyDraft(data.draft)
        const restoredForm: Record<string, string> = {}
        INTAKE_FORM_KEYS.forEach((key) => {
          const value = data.draft!.form?.[key]
          if (typeof value === 'string' && value.trim()) restoredForm[key] = value
        })
        if (Object.keys(restoredForm).length) { setForm(restoredForm); setFormOpen(true) }
        const hasSheet = sheetHasValues(data.draft.sheet)
        if (hasSheet) applySheetState(data.draft)
        if (data.draft.materials.length) setActiveMaterialId(data.draft.materials[0].material_id)
        const hasPendingJob = Boolean(
          readPendingRequest(storageKeys.pendingRequest) || readPendingRetry(storageKeys.pendingRetry),
        )
        if (hasPendingJob) {
          // 生成锚点的恢复由下方 recovery effect 负责;先落到第 4 步等待任务。
          setStep(4)
          setMaxStep(4)
        } else {
          // 有确认单即允许回到第 4 步（生成页自身会对空确认单做防护）。
          const derivedMax = hasSheet ? 4 : 1
          const target = Math.min(Math.max(anchor.step || 1, 1), derivedMax)
          setStep(target)
          setMaxStep(Math.max(anchor.maxStep || 1, derivedMax, target))
        }
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError') setPageError('成交草稿恢复失败，可重新开始；服务端草稿仍在。')
      } finally {
        if (isCurrent('draft', generation)) setRestoring(false)
        finishOperation('draft', generation)
      }
    })()
    return () => controller.abort()
  }, [storageKeys, beginOperation, finishOperation, isCurrent, applyDraft, applySheetState])

  /* ---------- 草稿创建/复用（持久 request_id 幂等,重复点击不重复建单） ---------- */
  const createPromiseRef = useRef<Promise<DealDraft | null> | null>(null)
  const ensureDraft = useCallback(async (signal?: AbortSignal): Promise<DealDraft | null> => {
    if (draftRef.current) return draftRef.current
    if (createPromiseRef.current) return createPromiseRef.current
    const promise = (async (): Promise<DealDraft | null> => {
      const formPayload: Record<string, string> = {}
      INTAKE_FORM_KEYS.forEach((key) => {
        const value = (form[key] || '').trim()
        if (value) formPayload[key] = value
      })
      const channelText = serializeChannels(channels)
      if (channelText) formPayload.target_channels = channelText
      let rid = requestIdRef.current || requestIdState || requestId()
      requestIdRef.current = rid
      let response = await authFetch('/api/marketing/deal-drafts', {
        method: 'POST', signal, body: JSON.stringify({ request_id: rid, form: formPayload }),
      })
      let data = await readJson<{ created?: boolean; draft?: DealDraft; detail?: unknown }>(response)
      if (response.status === 409) {
        // 同一 request_id 承载了不同表单(锚点丢失 draftId 的极端场景)：换新键重建,绝不覆盖。
        rid = requestId()
        requestIdRef.current = rid
        response = await authFetch('/api/marketing/deal-drafts', {
          method: 'POST', signal, body: JSON.stringify({ request_id: rid, form: formPayload }),
        })
        data = await readJson(response)
      }
      if (!response.ok || !data.draft) throw new Error(errorMessage(data, '成交草稿创建失败，请重试'))
      requestIdRef.current = String(data.draft.request_id || rid)
      setRequestIdState(String(data.draft.request_id || rid))
      applyDraft(data.draft)
      return data.draft
    })()
    createPromiseRef.current = promise
    try { return await promise } finally { createPromiseRef.current = null }
  }, [applyDraft, channels, form, requestIdState])

  /* ---------- 第一步：图片上传 ---------- */
  const uploadImages = useCallback(async (files: File[]) => {
    if (!files.length) return
    const valid = files.filter((file) => /\.(png|jpe?g|webp)$/i.test(file.name) && file.size <= DEAL_IMAGE_MAX)
    if (valid.length !== files.length) toast.error('只支持 15MB 以内的 png/jpg/webp 图片，不合规的已跳过')
    if (!valid.length) return
    const existing = draftRef.current?.materials.length || 0
    if (existing + valid.length > DEAL_IMAGE_TOTAL_MAX) {
      toast.error(`一笔成交最多上传 ${DEAL_IMAGE_TOTAL_MAX} 张素材`)
      return
    }
    const { generation, controller } = beginOperation('upload')
    setUploading(true)
    setPageError('')
    try {
      const target = await ensureDraft(controller.signal)
      if (!target || !isCurrent('upload', generation)) return
      for (let index = 0; index < valid.length; index += 9) {
        const batch = valid.slice(index, index + 9)
        const body = new FormData()
        batch.forEach((file) => body.append('files', file))
        const response = await authFetch(`/api/marketing/deal-drafts/${target.id}/materials`, {
          method: 'POST', body, signal: controller.signal,
        })
        const data = await readJson<{ draft?: DealDraft; detail?: unknown }>(response)
        if (!isCurrent('upload', generation)) return
        if (!response.ok || !data.draft) throw new Error(errorMessage(data, '素材上传失败，请重试'))
        applyDraft(data.draft)
        if (!activeMaterialId && data.draft.materials.length) setActiveMaterialId(data.draft.materials[0].material_id)
      }
      toast.success(`已上传 ${valid.length} 张素材，原图只存你的私有空间`)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('upload', generation)) return
      const message = error instanceof Error ? error.message : '素材上传失败，请重试'
      setPageError(message)
      toast.error(message)
    } finally {
      if (isCurrent('upload', generation)) setUploading(false)
      finishOperation('upload', generation)
    }
  }, [activeMaterialId, applyDraft, beginOperation, ensureDraft, finishOperation, isCurrent])

  /* ---------- 第一步：语音 ---------- */
  const uploadVoice = useCallback(async (blob: Blob): Promise<{ ok: boolean; transcript?: string; error?: string }> => {
    const { generation, controller } = beginOperation('voice')
    setVoiceBusy(true)
    try {
      const target = await ensureDraft(controller.signal)
      if (!target) throw new Error('成交草稿创建失败，请重试')
      if (!isCurrent('voice', generation)) return { ok: false }
      const body = new FormData()
      body.append('file', blob, 'deal-voice.wav')
      const response = await authFetch(`/api/marketing/deal-drafts/${target.id}/voice`, {
        method: 'POST', body, signal: controller.signal,
      })
      const data = await readJson<{ transcript?: string; draft?: DealDraft; detail?: unknown }>(response)
      if (!isCurrent('voice', generation)) return { ok: false }
      if (!response.ok || !data.transcript) {
        return { ok: false, error: errorMessage(data, '语音识别失败，请重试或改用表单填写') }
      }
      if (data.draft) applyDraft(data.draft)
      setTranscriptEdited(null)
      return { ok: true, transcript: data.transcript }
    } catch (error) {
      if ((error as Error)?.name === 'AbortError') return { ok: false }
      return { ok: false, error: error instanceof Error ? error.message : '语音上传失败' }
    } finally {
      if (isCurrent('voice', generation)) setVoiceBusy(false)
      finishOperation('voice', generation)
    }
  }, [applyDraft, beginOperation, ensureDraft, finishOperation, isCurrent])

  /* ---------- 第一步 → 第二步：整理成交信息（analyze + 表单优先合并） ---------- */
  const analyze = useCallback(async () => {
    const hasForm = INTAKE_FORM_KEYS.some((key) => (form[key] || '').trim().length > 0)
    const hasMaterials = Boolean(draftRef.current?.materials.length)
    const hasVoice = Boolean((draftRef.current?.form?.voice_transcript || '').trim()) || transcriptEdited != null
    if (!hasForm && !hasMaterials && !hasVoice) {
      toast.error('先上传图片、说一段话或填写成交情况，再点整理')
      return
    }
    const { generation, controller } = beginOperation('analyze')
    setAnalyzing(true)
    setPageError('')
    try {
      const target = await ensureDraft(controller.signal)
      if (!target || !isCurrent('analyze', generation)) return
      const response = await authFetch(`/api/marketing/deal-drafts/${target.id}/analyze`, {
        method: 'POST', signal: controller.signal,
      })
      const data = await readJson<{ sheet?: Record<string, unknown>; draft?: DealDraft; detail?: unknown }>(response)
      if (!isCurrent('analyze', generation)) return
      if (!response.ok || !data.draft) throw new Error(errorMessage(data, '整理成交信息失败，请重试'))
      let nextDraft = data.draft
      const sheetNow = data.sheet || nextDraft.sheet || {}
      // 表单优先（与后端规则一致）：用户手填的值以 confirmed 覆盖 AI 提取。
      const updates: Record<string, { value: string }> = {}
      INTAKE_FORM_KEYS.forEach((key) => {
        const value = (form[key] || '').trim()
        if (value && value !== sheetEntryOf(sheetNow, key).value) updates[key] = { value }
      })
      // 用户改过的转写 = 用户亲口修正的「发生了什么」(确认单覆盖 AI 猜测,不编造)。
      const edited = (transcriptEdited || '').trim()
      if (edited && !(form.what_happened || '').trim()) {
        const value = edited.slice(0, 240)
        if (value !== sheetEntryOf(sheetNow, 'what_happened').value) updates.what_happened = { value }
      }
      const channelText = serializeChannels(channels)
      if (channelText && channelText !== sheetEntryOf(sheetNow, 'target_channels').value) {
        updates.target_channels = { value: channelText }
      }
      if (Object.keys(updates).length) {
        const patchResponse = await authFetch(`/api/marketing/deal-drafts/${target.id}`, {
          method: 'PATCH', signal: controller.signal, body: JSON.stringify({ sheet: updates }),
        })
        const patchData = await readJson<{ draft?: DealDraft; detail?: unknown }>(patchResponse)
        if (!isCurrent('analyze', generation)) return
        if (patchResponse.ok && patchData.draft) nextDraft = patchData.draft
      }
      applyDraft(nextDraft)
      applySheetState(nextDraft)
      setReanalyzeConfirm(false)
      goToStep(2)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('analyze', generation)) return
      const message = error instanceof Error ? error.message : '整理成交信息失败，请重试'
      setPageError(message)
      toast.error(message)
    } finally {
      if (isCurrent('analyze', generation)) setAnalyzing(false)
      finishOperation('analyze', generation)
    }
  }, [applyDraft, applySheetState, beginOperation, channels, ensureDraft, finishOperation, form, goToStep, isCurrent, transcriptEdited])

  const clickAnalyze = useCallback(() => {
    if (analyzing || uploading || voiceBusy) return
    if (sheetHasValues(draftRef.current?.sheet) && !reanalyzeConfirm) {
      // 已出过确认单：重新整理会覆盖手动修改,先显式确认。
      setReanalyzeConfirm(true)
      return
    }
    void analyze()
  }, [analyzing, analyze, reanalyzeConfirm, uploading, voiceBusy])

  /* ---------- 第二步：确认单保存（编辑即确认） ---------- */
  const saveSheetAndNext = useCallback(async () => {
    if (!draftRef.current || !sheetValues) { goToStep(3); return }
    const updates: Record<string, { value: string }> = {}
    SHEET_FIELD_KEYS.forEach((key) => {
      const value = (sheetValues[key] || '').trim()
      if (value !== sheetEntryOf(draftRef.current!.sheet, key).value) updates[key] = { value }
    })
    const { generation, controller } = beginOperation('sheet')
    setPatching(true)
    try {
      if (Object.keys(updates).length) {
        const response = await authFetch(`/api/marketing/deal-drafts/${draftRef.current.id}`, {
          method: 'PATCH', signal: controller.signal, body: JSON.stringify({ sheet: updates }),
        })
        const data = await readJson<{ sheet?: Record<string, unknown>; draft?: DealDraft; detail?: unknown }>(response)
        if (!isCurrent('sheet', generation)) return
        if (!response.ok || !data.draft) throw new Error(errorMessage(data, '确认单保存失败，请重试'))
        applyDraft(data.draft)
        applySheetState(data.draft)
        toast.success('确认单已保存')
      }
      goToStep(3)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('sheet', generation)) return
      const message = error instanceof Error ? error.message : '确认单保存失败，请重试'
      setPageError(message)
      toast.error(message)
    } finally {
      if (isCurrent('sheet', generation)) setPatching(false)
      finishOperation('sheet', generation)
    }
  }, [applyDraft, applySheetState, beginOperation, finishOperation, goToStep, isCurrent, sheetValues])

  /* ---------- 第三步：打码 ---------- */
  const redact = useCallback(async (
    materialId: string,
    action: 'auto' | 'add' | 'restore' | 'strength' | 'confirm',
    extra?: { kind?: string; box?: number[]; region_id?: string; strength?: string },
  ) => {
    if (!draftRef.current) return false
    const { generation, controller } = beginOperation('redact')
    setRedactBusy(`${materialId}:${action}`)
    setPageError('')
    try {
      const response = await authFetch(`/api/marketing/deal-drafts/${draftRef.current.id}/redact`, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ material_id: materialId, action, ...extra }),
      })
      const data = await readJson<{ redaction?: RedactionState; draft?: DealDraft; detail?: unknown }>(response)
      if (!isCurrent('redact', generation)) return false
      if (!response.ok || !data.draft) throw new Error(errorMessage(data, '打码操作失败，请重试'))
      applyDraft(data.draft)
      setPreviewTick((tick) => tick + 1)
      if (action === 'confirm') toast.success('这张素材的打码已确认，可用于生成')
      return true
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('redact', generation)) return false
      const message = error instanceof Error ? error.message : '打码操作失败，请重试'
      setPageError(message)
      toast.error(message)
      return false
    } finally {
      if (isCurrent('redact', generation)) setRedactBusy('')
      finishOperation('redact', generation)
    }
  }, [applyDraft, beginOperation, finishOperation, isCurrent])

  const changeStrength = useCallback((materialId: string, strength: StrengthId) => {
    if (strengthTimer.current != null) window.clearTimeout(strengthTimer.current)
    strengthTimer.current = window.setTimeout(() => {
      void redact(materialId, 'strength', { strength })
    }, 350)
  }, [redact])

  /* 框选新增：pointer 轨道 → 图片自然像素坐标 */
  const stageFraction = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width))),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / Math.max(1, rect.height))),
    }
  }, [])

  const activeMaterial = useMemo(() => {
    const materials = draft?.materials || []
    return materials.find((material) => material.material_id === activeMaterialId) || materials[0] || null
  }, [activeMaterialId, draft])

  const naturalSize = useCallback((): { width: number; height: number } => {
    const measured = activeMaterial ? measuredSizeRef.current[activeMaterial.material_id] : undefined
    const width = Number(activeMaterial?.width || 0) || measured?.width || 0
    const height = Number(activeMaterial?.height || 0) || measured?.height || 0
    return { width, height }
  }, [activeMaterial])

  const onSelectPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!selectMode || redactBusy) return
    event.currentTarget.setPointerCapture(event.pointerId)
    const point = stageFraction(event)
    dragStartRef.current = point
    setSelectRect({ x0: point.x, y0: point.y, x1: point.x, y1: point.y })
  }, [redactBusy, selectMode, stageFraction])

  const onSelectPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragStartRef.current) return
    const point = stageFraction(event)
    setSelectRect({ x0: dragStartRef.current.x, y0: dragStartRef.current.y, x1: point.x, y1: point.y })
  }, [stageFraction])

  const onSelectPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragStartRef.current || !selectRect || !activeMaterial) {
      dragStartRef.current = null
      setSelectRect(null)
      return
    }
    dragStartRef.current = null
    setSelectRect(null)
    const { width, height } = naturalSize()
    if (!width || !height) {
      toast.error('图片尺寸还没读出来，请稍候再框选')
      return
    }
    const x = Math.round(Math.min(selectRect.x0, selectRect.x1) * width)
    const y = Math.round(Math.min(selectRect.y0, selectRect.y1) * height)
    const w = Math.round(Math.abs(selectRect.x1 - selectRect.x0) * width)
    const h = Math.round(Math.abs(selectRect.y1 - selectRect.y0) * height)
    if (w < 6 || h < 6) return
    void redact(activeMaterial.material_id, 'add', { kind: addKind, box: [x, y, w, h] })
  }, [activeMaterial, addKind, naturalSize, redact, selectRect])

  /* ---------- 第四步：生成 ---------- */
  const pollJob = useCallback(async (jobId: number, generation: number) => {
    if (!isCurrent('generation', generation)) return
    try {
      const response = await authFetch(`/api/marketing/jobs/${jobId}`, {
        signal: operations.current.get('generation')?.controller.signal,
      })
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
        if (data.job.status === 'succeeded') toast.success('晒单内容包已生成')
        if (data.job.status === 'partial_success') toast.warning('部分成品已就绪，失败项可单独重做')
      }
    } catch (error) {
      if (!isCurrent('generation', generation)) return
      setGenerating(false)
      setPageError(error instanceof Error ? error.message : '任务状态读取失败')
      finishOperation('generation', generation)
    }
  }, [finishOperation, isCurrent, mergeEditedCopy, storageKeys])

  const loadHistory = useCallback(async () => {
    const { generation, controller } = beginOperation('history')
    try {
      const response = await authFetch('/api/marketing/my-materials?limit=30', { signal: controller.signal })
      const data = await readJson<{ materials?: JobState[] }>(response)
      if (!isCurrent('history', generation)) return
      if (!response.ok) throw new Error(errorMessage(data, '历史版本加载失败'))
      // F14：晒成交历史按 quick_task=showcase_deal 口径过滤（旧数据回退 deal_* 渠道
      // 启发式），只勾朋友圈/小红书/抖音的晒单包也不会漏掉。
      setHistory((data.materials || []).filter((item) =>
        Array.isArray(item.channels) && isShowcaseDealJob(item),
      ))
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('history', generation)) return
      toast.error(error instanceof Error ? error.message : '历史版本加载失败')
    } finally {
      finishOperation('history', generation)
    }
  }, [beginOperation, finishOperation, isCurrent])

  /* ---------- 二维码联系方式上传(复用 /api/marketing/qr-reference,与主流程同构) ---------- */
  const uploadQr = useCallback(async (file?: File) => {
    if (!file) return
    const { generation, controller } = beginOperation('qr')
    setUploadingQr(true)
    setQrReference(null)
    setQrError('')
    try {
      const formData = new FormData()
      formData.append('file', file)
      const response = await authFetch('/api/marketing/qr-reference', { method: 'POST', body: formData, signal: controller.signal })
      const data = await readJson<{ qr_reference?: QrReference; detail?: unknown }>(response)
      if (!isCurrent('qr', generation)) return
      if (!response.ok || !data.qr_reference) throw new Error(errorMessage(data, '二维码无法扫码'))
      setQrReference(data.qr_reference)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('qr', generation)) return
      setQrError(error instanceof Error ? error.message : '二维码上传失败')
    } finally {
      if (isCurrent('qr', generation)) setUploadingQr(false)
      finishOperation('qr', generation)
    }
  }, [beginOperation, finishOperation, isCurrent])

  /* 确认单 contact_and_qr 字段联动:联系方式三态选择写回确认单文本 */
  const contactSheetText = useCallback((): string => {
    if (anonymous || contactMode === 'none') return '不展示联系方式'
    if (contactMode === 'text' && contactText.trim()) return `文字联系方式：${contactText.trim()}`
    if (contactMode === 'qr' && qrReference) return `二维码联系方式：${qrReference.payload_preview || '已验证'}`
    return '不展示联系方式'
  }, [anonymous, contactMode, contactText, qrReference])

  const generate = useCallback(async () => {
    if (!storageKeys) return
    if (!draftRef.current) {
      toast.error('先回到第一步整理成交信息')
      return
    }
    if (!sheetHasValues(draftRef.current.sheet) && !sheetValues) {
      toast.error('先回到第一步整理成交信息，再生成')
      return
    }
    if (!channels.length) {
      toast.error('至少选择一个渠道成品')
      return
    }
    if (!anonymous && contactMode === 'text' && !contactText.trim()) {
      toast.error('请输入要展示的文字联系方式，或保持不展示')
      return
    }
    if (!anonymous && contactMode === 'qr' && !qrReference) {
      toast.error('请先上传并通过扫码校验的二维码，或保持不展示')
      return
    }
    const { generation, controller } = beginOperation('generation')
    // N2：setGenerating(true) 移到下方锚点写入之后（与 GeoContentCenter 同构）——
    // payloadHash 等待窗口内被抢占时 generating 从未置 true，不会泄漏锁定
    setPageError('')
    try {
      // 确认单 contact_and_qr 字段联动:生成前把联系方式三态选择写回草稿
      // 确认单,冻结进 job 的 sheet_snapshot 与本次实际选择一致。
      const contactLine = contactSheetText()
      const sheetBefore = sheetValues || sheetValuesOf(draftRef.current.sheet)
      if ((sheetBefore.contact_and_qr || '').trim() !== contactLine) {
        const patchResponse = await authFetch(`/api/marketing/deal-drafts/${draftRef.current.id}`, {
          method: 'PATCH', signal: controller.signal,
          body: JSON.stringify({ sheet: { contact_and_qr: { value: contactLine } } }),
        })
        const patchData = await readJson<{ draft?: DealDraft; detail?: unknown }>(patchResponse)
        if (!isCurrent('generation', generation)) return
        if (!patchResponse.ok || !patchData.draft) throw new Error(errorMessage(patchData, '确认单联系方式保存失败，请重试'))
        applyDraft(patchData.draft)
        applySheetState(patchData.draft)
      }
      const sheetSnapshot = sheetValuesOf(draftRef.current.sheet)
      const brief = buildDealBrief(sheetSnapshot, anonymous)
      const requestPayload = {
        brief,
        quick_task: 'showcase_deal',
        deal_draft_id: draftRef.current.id,
        channels,
        contact: {
          mode: anonymous ? 'none' : contactMode,
          text: !anonymous && contactMode === 'text' ? contactText.trim() : '',
          qr_reference: !anonymous && contactMode === 'qr' ? qrReference : null,
        },
        associate_recent_trend: false,
        resolution,
        moments_layout: momentsLayout,
        visual_style: visualStyle,
      }
      // F2：幂等键输入并入确认单内容、草稿版本与已确认打码素材列表（仅参与 hash，
      // 不进 POST body）。否则丢响应后改确认单/打码再生成，hash 逐字节相同会静默
      // 复用旧 request_id，拿回用旧确认单冻结的任务。
      const signature = await payloadHash({
        ...requestPayload,
        sheet: sheetSnapshot,
        draft_updated_at: draftRef.current.updated_at || '',
        confirmed_redaction_materials: (draftRef.current.materials || [])
          .filter((material) => material.redaction?.status === 'confirmed')
          .map((material) => material.material_id)
          .sort(),
      })
      if (!isCurrent('generation', generation)) return
      const durablePending = readPendingRequest(storageKeys.pendingRequest)
      const durableRequestId = durablePending?.payloadHash === signature ? durablePending.requestId : requestId()
      localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({
        requestId: durableRequestId,
        payloadHash: signature,
        createdAt: durablePending?.requestId === durableRequestId ? durablePending.createdAt : Date.now(),
      }))
      // N2：锚点落盘后才上锁、同时清旧结果视图（hash 窗口内不闪空态）；
      // 此后被抢占也有锚点可恢复，generating 不会泄漏
      setJob(null)
      setGenerating(true)
      const response = await authFetch('/api/marketing/content-packages', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ ...requestPayload, request_id: durableRequestId }),
      })
      const data = await readJson<{ job_id?: number; status?: string; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return
      if (!response.ok || !data.job_id) {
        // 422 = 任务未创建,清掉恢复锚点,避免刷新后轮询幻影任务。
        if (response.status === 422) localStorage.removeItem(storageKeys.pendingRequest)
        throw new Error(errorMessage(data, '晒单内容包创建失败，请检查当前设置'))
      }
      localStorage.setItem(storageKeys.pendingRequest, JSON.stringify({
        requestId: durableRequestId,
        payloadHash: signature,
        jobId: data.job_id,
        createdAt: durablePending?.requestId === durableRequestId ? durablePending.createdAt : Date.now(),
      }))
      await pollJob(data.job_id, generation)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('generation', generation)) return
      const message = error instanceof Error ? error.message : '晒单内容包创建失败'
      setPageError(message)
      setGenerating(false)
      toast.error(message)
      finishOperation('generation', generation)
    }
  }, [anonymous, applyDraft, applySheetState, beginOperation, channels, contactMode, contactSheetText, contactText, finishOperation, isCurrent, momentsLayout, pollJob, qrReference, resolution, sheetValues, storageKeys, visualStyle])

  /* 丢响应恢复：by-request 幂等键 + 404 宽限（与主流程同构） */
  useEffect(() => {
    if (!storageKeys || restoring) return
    const pending = readPendingRequest(storageKeys.pendingRequest)
    const pendingRetry = readPendingRetry(storageKeys.pendingRetry)
    if (!pending && !pendingRetry) return
    const recovery = pending
      ? { requestId: pending.requestId, jobId: pending.jobId, storageKey: storageKeys.pendingRequest, createdAt: pending.createdAt }
      : { requestId: pendingRetry!.requestId, jobId: pendingRetry!.childJobId, storageKey: storageKeys.pendingRetry, createdAt: pendingRetry!.createdAt }
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
          setPageError('未确认请求在宽限期后仍不存在，已允许重新生成')
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
        setStep(4)
        setMaxStep(4)
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
  }, [beginOperation, finishOperation, isCurrent, mergeEditedCopy, pollJob, restoring, storageKeys])

  /* ---------- 局部重试（不整包重做、不重复扣费、不覆盖成功资产） ---------- */
  const redoComponents = useCallback(async (componentIds: string[]) => {
    if (!job || !componentIds.length || !storageKeys) return
    const { generation, controller } = beginOperation('generation')
    setGenerating(true)
    try {
      const retryKey = `${job.job_id}:${[...componentIds].sort().join(',')}`
      const pending = readPendingRetry(storageKeys.pendingRetry)
      const retryRequestId = pending?.retryKey === retryKey && Date.now() - pending.createdAt < 86400_000
        ? pending.requestId : requestId()
      localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({
        retryKey, requestId: retryRequestId,
        createdAt: pending?.requestId === retryRequestId ? pending.createdAt : Date.now(),
      }))
      const response = await authFetch(`/api/marketing/jobs/${job.job_id}/retry`, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ request_id: retryRequestId, component_ids: componentIds }),
      })
      const data = await readJson<{ job_id?: number; detail?: unknown }>(response)
      if (!isCurrent('generation', generation)) return
      if (!response.ok || !data.job_id) throw new Error(errorMessage(data, '单项重做失败'))
      localStorage.setItem(storageKeys.pendingRetry, JSON.stringify({
        retryKey, requestId: retryRequestId, childJobId: data.job_id,
        createdAt: pending?.requestId === retryRequestId ? pending.createdAt : Date.now(),
      }))
      await pollJob(data.job_id, generation)
    } catch (error) {
      if ((error as Error)?.name === 'AbortError' || !isCurrent('generation', generation)) return
      setGenerating(false)
      toast.error(error instanceof Error ? error.message : '单项重做失败')
      finishOperation('generation', generation)
    }
  }, [beginOperation, finishOperation, isCurrent, job, pollJob, storageKeys])

  /* ---------- 文案编辑（只改文字,不动图片） ---------- */
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

  /* ---------- 成品图授权读取 → objectURL ---------- */
  const imageAssetSignature = useMemo(() => (
    (job?.assets || []).filter((asset) => assetSlot(asset).includes(':image:'))
      .map((asset) => `${asset.id}:${asset.download_url || asset.url_stored || asset.url || ''}`).join('|')
  ), [job])

  useEffect(() => {
    const images = (job?.assets || []).filter((asset) => assetSlot(asset).includes(':image:') && (asset.download_url || asset.url_stored || asset.url))
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
          const response = await authFetch(String(asset.download_url || asset.url_stored || asset.url), { signal: controller.signal })
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

  const openHistory = useCallback(async (item: JobState) => {
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
      setHistoryOpen(false)
      goToStep(4)
    } catch (error) {
      if ((error as Error)?.name !== 'AbortError' && isCurrent('history', generation)) toast.error('该历史版本已不可读取')
    } finally {
      finishOperation('history', generation)
    }
  }, [beginOperation, finishOperation, generating, goToStep, isCurrent, mergeEditedCopy])

  /* ---------- 再晒一笔：本地重置（服务端草稿与历史保留） ---------- */
  const resetAll = useCallback(() => {
    cancelActive()
    if (storageKeys) {
      localStorage.removeItem(storageKeys.anchor)
      localStorage.removeItem(storageKeys.pendingRequest)
      localStorage.removeItem(storageKeys.pendingRetry)
    }
    applyDraft(null)
    editedCopyRef.current.clear()
    requestIdRef.current = ''
    setRequestIdState('')
    setStep(1)
    setMaxStep(1)
    setForm({})
    setFormOpen(false)
    setAnonymous(true)
    setChannels(DEFAULT_DEAL_CHANNELS)
    setTranscriptEdited(null)
    setSheetValues(null)
    setSheetStatus(null)
    setJob(null)
    setGenerating(false)
    setActiveMaterialId('')
    setSelectMode(false)
    setContactMode('none')
    setContactText('')
    setPageError('')
    setReanalyzeConfirm(false)
  }, [applyDraft, cancelActive, storageKeys])

  const handleBack = useCallback(() => {
    cancelActive()
    onBack()
  }, [cancelActive, onBack])

  useEffect(() => {
    if (!browserOwnerScope) return
    void loadHistory()
  }, [browserOwnerScope, loadHistory])

  /* ---------- 派生状态 ---------- */
  const materials = draft?.materials || []
  const transcript = transcriptEdited ?? String(draft?.form?.voice_transcript || '')
  const tentativeFields = sheetValues && sheetStatus
    ? SHEET_FIELD_KEYS.filter((key) => sheetStatus[key] === 'tentative' && (sheetValues[key] || '').trim())
    : []
  const activeRegions = redactionRegions(activeMaterial).filter((region) => !region.restored)
  const allRegions = redactionRegions(activeMaterial)
  const unconfirmedMaterials = materials.filter((material) => material.redaction?.status !== 'confirmed')
  const confirmedMaterials = materials.length - unconfirmedMaterials.length
  const needsRedactionForChat = channels.includes('deal_chat')
  const allFailedIds = useMemo(() => (
    (job?.components || []).filter((component) => component.status === 'failed').map((component) => component.component_id)
  ), [job])
  const dealBusy = analyzing || uploading || voiceBusy

  const downloadAllCopy = useCallback(() => {
    if (!job) return
    const sections = (job.channels || []).map((id) => {
      const asset = job.assets.find((item) => assetSlot(item) === `${id}:copy`)
      const rows = flattenCopy(parseCopy(asset))
      const meta = channelMeta(id)
      return `【${meta?.short || id}】\n${rows.map((row) => `${row.label}\n${row.text}`).join('\n\n')}`
    }).filter((section) => section.trim().length > 0)
    if (!sections.length) return
    downloadText(`晒成交内容包-全部文案-#${job.job_id}.txt`, sections.join('\n\n====================\n\n'))
  }, [job])

  /* ================================ 渲染 ================================ */
  const stepSub = (() => {
    const materialCount = materials.length
    const confirmedText = confirmedMaterials ? `已确认 ${confirmedMaterials}/${materials.length} 张` : '逐张预览确认'
    return [
      materialCount || transcript
        ? `已收到 ${materialCount} 张图${transcript ? ' + 1 段录音' : ''}`
        : '图片 / 语音 / 填写，任意一种',
      tentativeFields.length ? `${tentativeFields.length} 项待你核对` : '逐项可改，编辑即确认',
      materialCount ? (unconfirmedMaterials.length ? confirmedText : '全部素材已确认') : '没有素材可跳过',
      job ? (STATUS_TEXT[job.status] || '已提交') : '确认后开始',
    ]
  })()

  const stepItems = [
    { n: 1, label: '提供素材', icon: ImagePlus },
    { n: 2, label: '确认成交信息', icon: ListChecks },
    { n: 3, label: '隐私打码', icon: ScanSearch },
    { n: 4, label: '生成整套内容', icon: Sparkles },
  ]

  return (
    <main className="gcc-shell" data-testid="deal-studio">
      <div className="gcc-deal-shell">
        <button className="gcc-deal-back" onClick={handleBack}>
          <ArrowLeft size={14} aria-hidden /> 返回内容中心
        </button>

        <header className="gcc-header">
          <div>
            <div className="gcc-eyebrow"><BadgeDollarSign size={14} aria-hidden /> 晒成交</div>
            <h1>把一次真实成交，变成一整套获客内容</h1>
            <p>上传素材或说一段话，AI 先整理出成交确认单让你核对，再帮你遮住隐私信息，最后生成喜报海报、聊天晒单、数据卡片等整套内容。</p>
          </div>
          <div className="gcc-header-actions">
            <button className="gcc-quiet-button" onClick={() => { setHistoryOpen((value) => !value); void loadHistory() }} aria-expanded={historyOpen} aria-label="历史版本">
              <History size={16} aria-hidden /><span>历史版本</span>
            </button>
          </div>
        </header>

        {historyOpen && (
          <aside className="gcc-history" aria-label="历史版本">
            <div className="gcc-history-head"><span>晒成交历史版本</span><button onClick={() => setHistoryOpen(false)} aria-label="关闭历史"><X size={16} /></button></div>
            {generating && <p className="gcc-empty-copy">任务生成中，落地后可查看历史版本。</p>}
            {history.length === 0 ? <p className="gcc-empty-copy">还没有晒成交生成记录。</p> : history.map((item) => (
              <button key={item.job_id} disabled={generating} onClick={() => void openHistory(item)}>
                <span>{item.strategy?.core_angle || `晒单内容包 #${item.job_id}`}</span>
                <small>{STATUS_TEXT[item.status] || item.status} · {item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : ''}</small>
              </button>
            ))}
          </aside>
        )}

        {/* ---------- 步骤条 ---------- */}
        <nav className="gcc-deal-stepper" aria-label="晒成交步骤">
          {stepItems.map((item) => {
            const StepIcon = item.icon
            const state = item.n === step ? 'is-current' : item.n < step ? 'is-done' : ''
            const attention = (item.n === 2 && tentativeFields.length > 0 && step !== 2)
              || (item.n === 3 && materials.length > 0 && unconfirmedMaterials.length > 0 && step !== 3)
            return (
              <button
                key={item.n}
                className={`gcc-deal-step ${state}${attention ? ' is-attention' : ''}`}
                onClick={() => { if (item.n <= maxStep && item.n !== step) goToStep(item.n) }}
                disabled={item.n > maxStep}
                aria-current={item.n === step ? 'step' : undefined}
              >
                <span className="gcc-deal-step-num">
                  {item.n < step ? <Check size={13} aria-hidden /> : <StepIcon size={13} aria-hidden />}
                </span>
                <span className="gcc-deal-step-text">
                  <b>{item.label}</b>
                  <span>{stepSub[item.n - 1]}</span>
                </span>
              </button>
            )
          })}
        </nav>

        {restoring && (
          <div className="gcc-deal-loading"><Loader2 className="gcc-spin" size={18} aria-hidden /> 正在恢复上次的进度…</div>
        )}

        {!restoring && step === 1 && (
          <>
            {/* ---------- 第一步：三输入卡 ---------- */}
            <div className="gcc-deal-intake-grid">
              <section className="gcc-deal-intake-card" aria-label="上传图片">
                <div className="gcc-deal-ic-head">
                  <span className="gcc-deal-ic-icon"><ImagePlus size={19} aria-hidden /></span>
                  <div><b>上传图片</b><small>聊天截图、合同、收据都行</small></div>
                </div>
                <div className="gcc-deal-thumb-row">
                  {materials.map((material, index) => (
                    <span key={material.material_id} className="gcc-deal-thumb" title={material.original_filename || `素材 ${index + 1}`}>
                      {draft && (
                        <MaterialImage
                          draftId={draft.id}
                          materialId={material.material_id}
                          variant="original"
                          tick={0}
                          alt={`成交素材第 ${index + 1} 张`}
                        />
                      )}
                    </span>
                  ))}
                  {uploading && <span className="gcc-deal-thumb is-loading"><Loader2 className="gcc-spin" size={16} aria-hidden /></span>}
                  <label className={`gcc-deal-thumb-add ${uploading ? 'is-disabled' : ''}`}>
                    <input
                      type="file"
                      accept=".png,.jpg,.jpeg,.webp"
                      multiple
                      data-testid="deal-image-input"
                      disabled={uploading}
                      onChange={(event) => {
                        const files = Array.from(event.target.files || [])
                        event.target.value = ''
                        void uploadImages(files)
                      }}
                    />
                    <Plus size={16} aria-hidden />
                    再加一张
                  </label>
                </div>
                <span className="gcc-deal-tip">已传 {materials.length} 张 · 一批最多 9 张，一共最多 20 张。原图只存你的私有空间，不会出现在成品里。</span>
              </section>

              <section className="gcc-deal-intake-card" aria-label="说一段话">
                <div className="gcc-deal-ic-head">
                  <span className="gcc-deal-ic-icon"><Mic size={19} aria-hidden /></span>
                  <div><b>说一段话</b><small>像跟朋友讲一样说这单怎么成的</small></div>
                </div>
                <DealVoiceRecorder
                  disabled={dealBusy}
                  uploadVoice={uploadVoice}
                  onTranscribed={() => toast.success('语音已转写，可下滑核对修改')}
                />
                {transcript && (
                  <label className="gcc-deal-transcript">
                    <span>转写文字（可改；改过的内容会作为「发生了什么」并入确认单）</span>
                    <textarea
                      value={transcript}
                      rows={4}
                      maxLength={4000}
                      aria-label="语音转写文字"
                      onChange={(event) => setTranscriptEdited(event.target.value)}
                    />
                  </label>
                )}
              </section>

              <section className="gcc-deal-intake-card" aria-label="填写成交情况">
                <div className="gcc-deal-ic-head">
                  <span className="gcc-deal-ic-icon"><Pencil size={19} aria-hidden /></span>
                  <div><b>填写成交情况</b><small>知道多少填多少，剩下的 AI 会标出来</small></div>
                </div>
                <button className="gcc-deal-form-toggle" onClick={() => setFormOpen((value) => !value)} aria-expanded={formOpen}>
                  <span>补充成交细节{Object.values(form).some((value) => value.trim()) ? '（已填写）' : '（选填）'}</span>
                  <ChevronDown size={15} className={formOpen ? 'is-open' : ''} aria-hidden />
                </button>
                {formOpen && (
                  <div className="gcc-deal-intake-form">
                    {INTAKE_FORM_FIELDS.map((field) => (
                      <label key={field.key} className="gcc-deal-field">
                        <span>{field.label}</span>
                        {field.multiline ? (
                          <textarea
                            value={form[field.key] || ''}
                            rows={3}
                            maxLength={500}
                            placeholder={field.placeholder}
                            onChange={(event) => setForm((current) => ({ ...current, [field.key]: event.target.value }))}
                          />
                        ) : (
                          <input
                            value={form[field.key] || ''}
                            maxLength={240}
                            placeholder={field.placeholder}
                            onChange={(event) => setForm((current) => ({ ...current, [field.key]: event.target.value }))}
                          />
                        )}
                      </label>
                    ))}
                    <label className="gcc-switch-row gcc-deal-anon-switch">
                      <input type="checkbox" checked={anonymous} onChange={(event) => setAnonymous(event.target.checked)} />
                      <span aria-hidden><i /></span>
                      <b>{anonymous ? '匿名晒单：不出现联系方式与品牌名' : '公开晒单：可选择展示联系方式'}</b>
                    </label>
                    <div className="gcc-deal-field">
                      <span>目标渠道</span>
                      <div className="gcc-deal-channel-chips">
                        {DEAL_CHANNEL_PICKER.map((id) => {
                          const meta = channelMeta(id)
                          const selected = channels.includes(id)
                          return (
                            <button
                              key={id}
                              className={selected ? 'is-selected' : ''}
                              aria-pressed={selected}
                              onClick={() => setChannels((current) => selected ? current.filter((item) => item !== id) : [...current, id])}
                            >
                              <span aria-hidden>{selected && <Check size={11} />}</span>{meta?.short || id}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                )}
              </section>
            </div>

            <div className="gcc-deal-intake-foot">
              <span className="gcc-trust">
                <Info size={15} aria-hidden />
                三种方式用哪一种都行，AI 会把图、录音、填写的内容合并成一份确认单
              </span>
              {reanalyzeConfirm && (
                <span className="gcc-deal-reanalyze" role="alert">
                  <AlertTriangle size={14} aria-hidden />
                  重新整理会用最新素材重出确认单，你在确认单上的手动修改会被覆盖。
                </span>
              )}
              <button className="gcc-btn ink" onClick={clickAnalyze} disabled={dealBusy}>
                {analyzing ? <Loader2 className="gcc-spin" size={15} aria-hidden /> : <WandSparkles size={15} aria-hidden />}
                {analyzing ? '正在整理成交信息' : reanalyzeConfirm ? '确认重新整理' : '整理成交信息'}
              </button>
            </div>
          </>
        )}

        {!restoring && step === 2 && sheetValues && sheetStatus && (
          <section className="gcc-deal-panel" aria-label="成交信息确认单">
            <div className="gcc-section-title">
              <span className="gcc-title-left"><ListChecks size={15} aria-hidden /> 成交信息确认单</span>
              <span className="gcc-title-hint">AI 整理自你提供的素材 · 每项都能改 · 黄色「待确认」请重点核对</span>
            </div>
            <div className="gcc-strategy-note">
              <ShieldCheck size={15} aria-hidden />
              所有内容均来自你上传的图片、录音和填写；AI 没把握的标「待确认」，不会替你编造客户名、订单号或金额。
            </div>
            <div className="gcc-deal-sheet-grid">
              {SHEET_FIELDS.map((field) => {
                const status = sheetStatus[field.key]
                const value = sheetValues[field.key] || ''
                if (field.key === 'target_channels') {
                  return (
                    <div key={field.key} className="gcc-deal-sheet-field">
                      <div className="gcc-deal-sf-head">
                        <label>{field.label}</label>
                        <span className="gcc-badge gray">可增删</span>
                      </div>
                      <div className="gcc-deal-channel-chips">
                        {DEAL_CHANNEL_PICKER.map((id) => {
                          const meta = channelMeta(id)
                          const selected = channels.includes(id)
                          return (
                            <button
                              key={id}
                              className={selected ? 'is-selected' : ''}
                              aria-pressed={selected}
                              onClick={() => {
                                const next = selected ? channels.filter((item) => item !== id) : [...channels, id]
                                setChannels(next)
                                setSheetValues((current) => current ? { ...current, target_channels: serializeChannels(next) } : current)
                                setSheetStatus((current) => current ? { ...current, target_channels: 'confirmed' } : current)
                              }}
                            >
                              <span aria-hidden>{selected && <Check size={11} />}</span>{meta?.short || id}
                            </button>
                          )
                        })}
                      </div>
                      <span className="gcc-deal-sf-note">生成时按这里勾选的渠道出成品。</span>
                    </div>
                  )
                }
                return (
                  <div key={field.key} className={`gcc-deal-sheet-field ${field.span2 ? 'span2' : ''} ${status === 'tentative' && value.trim() ? 'is-tentative' : ''}`}>
                    <div className="gcc-deal-sf-head">
                      <label>{field.label}</label>
                      {status === 'tentative' && value.trim()
                        ? <span className="gcc-badge amber">待确认</span>
                        : <span className="gcc-badge green"><Check size={12} aria-hidden />已确认</span>}
                    </div>
                    {field.multiline ? (
                      <textarea
                        value={value}
                        rows={3}
                        maxLength={field.key === 'quotable_lines' ? 500 : 240}
                        aria-label={field.label}
                        placeholder="没有就留空，AI 不会替你编"
                        onChange={(event) => {
                          const next = event.target.value
                          setSheetValues((current) => current ? { ...current, [field.key]: next } : current)
                          setSheetStatus((current) => current ? { ...current, [field.key]: 'confirmed' } : current)
                        }}
                      />
                    ) : (
                      <input
                        value={value}
                        maxLength={240}
                        aria-label={field.label}
                        placeholder="没有就留空，AI 不会替你编"
                        onChange={(event) => {
                          const next = event.target.value
                          setSheetValues((current) => current ? { ...current, [field.key]: next } : current)
                          setSheetStatus((current) => current ? { ...current, [field.key]: 'confirmed' } : current)
                        }}
                      />
                    )}
                    {status === 'tentative' && value.trim() && (
                      <span className="gcc-deal-sf-note">AI 从素材里整理，没十足把握，请核对或改写；改过的内容以你为准。</span>
                    )}
                    {sheetWarnings.some((warning) => warning.field === field.key) && (
                      <span className="gcc-deal-sf-note is-warning" data-sheet-warning={field.key}>
                        <AlertTriangle size={12} aria-hidden />
                        {warningMessage(sheetWarnings.find((warning) => warning.field === field.key) as JobWarning)}
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
            <div className="gcc-deal-panel-foot">
              <button className="gcc-btn" onClick={() => goToStep(1)}>
                <ArrowLeft size={14} aria-hidden /> 上一步：补充素材
              </button>
              <button className="gcc-btn ink" onClick={() => void saveSheetAndNext()} disabled={patching}>
                {patching ? <Loader2 className="gcc-spin" size={15} aria-hidden /> : <ShieldCheck size={15} aria-hidden />}
                {patching ? '正在保存确认单' : '下一步：隐私打码'}
              </button>
            </div>
          </section>
        )}

        {!restoring && step === 3 && (
          <section className="gcc-deal-panel" aria-label="隐私打码">
            <div className="gcc-section-title">
              <span className="gcc-title-left"><ScanSearch size={15} aria-hidden /> 隐私打码</span>
              <span className="gcc-title-hint">自动识别 + 手动框选 · 点打码块可恢复 · 逐张确认后才会用于生成</span>
            </div>

            {materials.length === 0 ? (
              <div className="gcc-deal-empty-redact">
                <Eraser size={24} aria-hidden />
                <h3>这笔成交没有图片素材</h3>
                <p>没有素材就不需要打码。聊天晒单等需要打码素材的组件会自动跳过，其余渠道照常生成。</p>
                <div className="gcc-deal-panel-foot is-embedded">
                  <button className="gcc-btn" onClick={() => goToStep(1)}>
                    <ArrowLeft size={14} aria-hidden /> 回去上传素材
                  </button>
                  <button className="gcc-btn ink" onClick={() => goToStep(4)}>
                    直接去生成 <ArrowRight size={14} aria-hidden />
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="gcc-deal-material-strip" role="tablist" aria-label="素材切换">
                  {materials.map((material, index) => {
                    const confirmed = material.redaction?.status === 'confirmed'
                    return (
                      <button
                        key={material.material_id}
                        role="tab"
                        aria-selected={activeMaterial?.material_id === material.material_id}
                        className={activeMaterial?.material_id === material.material_id ? 'is-active' : ''}
                        onClick={() => { setActiveMaterialId(material.material_id); setSelectMode(false) }}
                      >
                        {draft && (
                          <MaterialImage
                            draftId={draft.id}
                            materialId={material.material_id}
                            variant="original"
                            tick={0}
                            alt={`素材 ${index + 1}`}
                          />
                        )}
                        <span className="gcc-deal-ms-label">
                          素材 {index + 1}
                          {confirmed && <Check size={11} aria-hidden />}
                        </span>
                      </button>
                    )
                  })}
                </div>

                {activeMaterial && draft && (
                  <div className="gcc-deal-redact-grid">
                    {/* 左：图 */}
                    <div className="gcc-deal-stage">
                      <div className="gcc-deal-rs-head">
                        <span>
                          素材 {materials.findIndex((material) => material.material_id === activeMaterial.material_id) + 1} / {materials.length}
                          {activeMaterial.original_filename ? ` · ${activeMaterial.original_filename}` : ''}
                          {formatBytes(activeMaterial.size_bytes) ? ` · ${formatBytes(activeMaterial.size_bytes)}` : ''}
                        </span>
                        <span className={`gcc-badge ${activeMaterial.redaction?.status === 'confirmed' ? 'green' : 'gray'}`}>
                          {activeMaterial.redaction?.status === 'confirmed' ? '已确认' : activeMaterial.redaction?.has_preview ? '预览中' : '原图'}
                        </span>
                      </div>
                      <div
                        ref={stageRef}
                        className={`gcc-deal-stage-img ${selectMode ? 'is-selecting' : ''}`}
                        onPointerDown={onSelectPointerDown}
                        onPointerMove={onSelectPointerMove}
                        onPointerUp={onSelectPointerUp}
                      >
                        <MaterialImage
                          draftId={draft.id}
                          materialId={activeMaterial.material_id}
                          variant={activeMaterial.redaction?.has_preview || activeMaterial.redaction?.has_output ? 'preview' : 'original'}
                          tick={previewTick}
                          alt={`素材打码预览 ${activeMaterial.material_id}`}
                          onReady={(width, height) => { measuredSizeRef.current[activeMaterial.material_id] = { width, height } }}
                        />
                        {allRegions.map((region, index) => {
                          const { width, height } = naturalSize()
                          if (!width || !height || !Array.isArray(region.box) || region.box.length < 4) return null
                          const [x, y, w, h] = region.box
                          return (
                            <button
                              key={region.id}
                              className={`gcc-deal-region ${region.restored ? 'is-restored' : ''}`}
                              style={{
                                left: `${(x / width) * 100}%`,
                                top: `${(y / height) * 100}%`,
                                width: `${(w / width) * 100}%`,
                                height: `${(h / height) * 100}%`,
                              }}
                              title={region.restored ? '已恢复，点按重新打码' : '打码中，点按恢复这一块'}
                              onPointerDown={(event) => event.stopPropagation()}
                              onClick={(event) => {
                                event.stopPropagation()
                                if (redactBusy) return
                                if (region.restored) {
                                  void redact(activeMaterial.material_id, 'add', { kind: region.kind, box: region.box })
                                } else {
                                  void redact(activeMaterial.material_id, 'restore', { region_id: region.id })
                                }
                              }}
                            >
                              <span className="gcc-deal-region-tag">
                                {index + 1} {REGION_KIND_LABELS[region.kind] || REGION_KIND_LABELS.other}{region.restored ? ' · 已恢复' : ''}
                              </span>
                            </button>
                          )
                        })}
                        {selectRect && (
                          <span
                            className="gcc-deal-select-rect"
                            style={{
                              left: `${Math.min(selectRect.x0, selectRect.x1) * 100}%`,
                              top: `${Math.min(selectRect.y0, selectRect.y1) * 100}%`,
                              width: `${Math.abs(selectRect.x1 - selectRect.x0) * 100}%`,
                              height: `${Math.abs(selectRect.y1 - selectRect.y0) * 100}%`,
                            }}
                            aria-hidden
                          />
                        )}
                      </div>
                      <span className="gcc-deal-stage-hint">
                        {selectMode ? '在图上按住拖出要遮住的区域' : '显示的是服务端打码预览；绿色块为打码中区域，点按可恢复'}
                      </span>
                    </div>

                    {/* 右：控制 */}
                    <div className="gcc-deal-controls">
                      <button
                        className="gcc-btn ink gcc-deal-block-btn"
                        onClick={() => void redact(activeMaterial.material_id, 'auto')}
                        disabled={Boolean(redactBusy)}
                      >
                        {redactBusy === `${activeMaterial.material_id}:auto`
                          ? <Loader2 className="gcc-spin" size={15} aria-hidden />
                          : <ScanSearch size={15} aria-hidden />}
                        {allRegions.length ? `重新自动识别（当前 ${activeRegions.length} 处打码中）` : '一键全打码'}
                      </button>

                      <div className="gcc-deal-region-list" aria-label="打码区域列表">
                        {allRegions.length === 0 && (
                          <div className="gcc-deal-region-empty">还没有打码区域：点「一键全打码」自动识别，或框选手动新增。</div>
                        )}
                        {allRegions.map((region, index) => (
                          <div key={region.id} className={`gcc-deal-region-row ${region.restored ? 'is-restored' : ''}`}>
                            <span className="gcc-deal-r-idx">{index + 1}</span>
                            <span className="gcc-deal-r-name">{REGION_KIND_LABELS[region.kind] || REGION_KIND_LABELS.other}</span>
                            <span className="gcc-deal-r-kind">{region.source === 'auto' ? '自动识别' : '手动新增'}{region.restored ? ' · 已恢复' : ''}</span>
                            <button
                              className="gcc-deal-r-undo"
                              disabled={Boolean(redactBusy)}
                              onClick={() => {
                                if (region.restored) void redact(activeMaterial.material_id, 'add', { kind: region.kind, box: region.box })
                                else void redact(activeMaterial.material_id, 'restore', { region_id: region.id })
                              }}
                            >
                              <Undo2 size={12} aria-hidden />{region.restored ? '重打' : '恢复'}
                            </button>
                          </div>
                        ))}
                      </div>

                      <div className="gcc-deal-add-row">
                        <button
                          className={`gcc-btn gcc-deal-block-btn is-dashed ${selectMode ? 'is-armed' : ''}`}
                          onClick={() => setSelectMode((value) => !value)}
                          aria-pressed={selectMode}
                        >
                          <MousePointerSquareDashed size={15} aria-hidden />
                          {selectMode ? '框选进行中…点这里取消' : '框选新增打码区'}
                        </button>
                        {selectMode && (
                          <label className="gcc-deal-kind-select">
                            <span>区域类型</span>
                            <select value={addKind} onChange={(event) => setAddKind(event.target.value)}>
                              {ADD_KIND_OPTIONS.map((kind) => (
                                <option key={kind} value={kind}>{REGION_KIND_LABELS[kind] || kind}</option>
                              ))}
                            </select>
                          </label>
                        )}
                      </div>

                      <div className="gcc-deal-strength">
                        <div className="gcc-deal-s-head">
                          <span>打码强度</span>
                          <span>{STRENGTH_OPTIONS.find((item) => item.id === (activeMaterial.redaction?.strength || 'medium'))?.label || '适中（推荐）'}</span>
                        </div>
                        <input
                          type="range"
                          min={0}
                          max={2}
                          step={1}
                          aria-label="打码强度"
                          value={Math.max(0, STRENGTH_OPTIONS.findIndex((item) => item.id === (activeMaterial.redaction?.strength || 'medium')))}
                          disabled={Boolean(redactBusy)}
                          onChange={(event) => {
                            const next = STRENGTH_OPTIONS[Number(event.target.value)]
                            if (next) changeStrength(activeMaterial.material_id, next.id)
                          }}
                        />
                        <div className="gcc-deal-s-ends"><span>细腻</span><span>更糊更安全</span></div>
                      </div>

                      {activeMaterial.redaction?.status === 'confirmed' ? (
                        <div className="gcc-deal-confirm-check is-done">
                          <span className="gcc-deal-cbox"><Check size={12} aria-hidden /></span>
                          这张素材的打码已确认。再改动（恢复/新增/强度）会需要重新确认。
                        </div>
                      ) : (
                        <button
                          className="gcc-btn gcc-deal-block-btn gcc-deal-confirm-btn"
                          onClick={() => void redact(activeMaterial.material_id, 'confirm')}
                          disabled={Boolean(redactBusy) || !activeMaterial.redaction?.has_preview}
                          title={activeMaterial.redaction?.has_preview ? '确认这张的打码效果' : '先一键全打码或框选新增，生成预览后再确认'}
                        >
                          {redactBusy === `${activeMaterial.material_id}:confirm`
                            ? <Loader2 className="gcc-spin" size={15} aria-hidden />
                            : <Check size={15} aria-hidden />}
                          {activeMaterial.redaction?.has_preview ? '预览确认：这张可以用了' : '先打码生成预览，再确认'}
                        </button>
                      )}
                      {(activeMaterial.redaction?.warnings?.length ?? 0) > 0 && (
                        <div className="gcc-deal-warn" data-redaction-warnings={activeMaterial.material_id}>
                          <AlertTriangle size={14} aria-hidden />
                          {(activeMaterial.redaction?.warnings || []).map((warning) => warningMessage(warning)).join('；')}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {unconfirmedMaterials.length > 0 && (
                  <div className="gcc-deal-warn" role="alert">
                    <ShieldAlert size={15} aria-hidden />
                    还有 {unconfirmedMaterials.length} 张素材未确认打码（素材 {materials.map((material, index) => material.redaction?.status === 'confirmed' ? null : index + 1).filter(Boolean).join('、')}）：
                    聊天晒单等需要打码素材的组件生成时会自动跳过，不阻断其他渠道；确认过的素材才会进入成品。
                  </div>
                )}

                <div className="gcc-deal-panel-foot">
                  <button className="gcc-btn" onClick={() => goToStep(2)}>
                    <ArrowLeft size={14} aria-hidden /> 上一步：确认单
                  </button>
                  <button className="gcc-btn ink" onClick={() => goToStep(4)}>
                    下一步：生成整套内容 <ArrowRight size={14} aria-hidden />
                  </button>
                </div>
              </>
            )}
          </section>
        )}

        {!restoring && step === 4 && (
          <>
            <section className="gcc-deal-panel" aria-label="生成设置">
              <div className="gcc-section-title">
                <span className="gcc-title-left"><Sparkles size={15} aria-hidden /> 生成整套晒单内容</span>
                <span className="gcc-title-hint">按确认单出成品 · 只用你已确认的事实与打码素材</span>
              </div>

              <div className="gcc-deal-generate-grid">
                <fieldset>
                  <legend><BadgeDollarSign size={13} aria-hidden /> 渠道</legend>
                  <div className="gcc-deal-channel-chips">
                    {DEAL_CHANNEL_PICKER.map((id) => {
                      const meta = channelMeta(id)
                      const selected = channels.includes(id)
                      return (
                        <button
                          key={id}
                          className={selected ? 'is-selected' : ''}
                          aria-pressed={selected}
                          onClick={() => setChannels((current) => selected ? current.filter((item) => item !== id) : [...current, id])}
                        >
                          <span aria-hidden>{selected && <Check size={11} />}</span>{meta?.short || id}
                        </button>
                      )
                    })}
                  </div>
                </fieldset>

                <fieldset>
                  <legend><ShieldCheck size={13} aria-hidden /> 匿名与联系方式</legend>
                  <label className="gcc-switch-row gcc-deal-anon-switch">
                    <input type="checkbox" checked={anonymous} onChange={(event) => { setAnonymous(event.target.checked); if (event.target.checked) setContactMode('none') }} />
                    <span aria-hidden><i /></span>
                    <b>{anonymous ? '匿名晒单' : '公开晒单'}</b>
                  </label>
                  {!anonymous && (
                    <>
                      <div className="gcc-segmented gcc-deal-contact-seg">
                        {(['none', 'text', 'qr'] as const).map((value) => (
                          <button key={value} className={contactMode === value ? 'is-active' : ''} onClick={() => { setContactMode(value); if (value !== 'qr') setQrError('') }}>
                            {value === 'none' ? '不展示联系方式' : value === 'text' ? '展示文字' : '展示二维码'}
                          </button>
                        ))}
                      </div>
                      {contactMode === 'text' && (
                        <input
                          className="gcc-contact-input"
                          value={contactText}
                          maxLength={120}
                          placeholder="仅使用你明确输入的联系方式"
                          aria-label="联系方式文字"
                          onChange={(event) => setContactText(event.target.value)}
                        />
                      )}
                      {contactMode === 'qr' && (
                        <div className="gcc-deal-qr">
                          <label className={`gcc-qr-upload ${qrReference ? 'is-valid' : ''}`}>
                            <input
                              type="file"
                              accept="image/png,image/jpeg"
                              hidden
                              aria-label="上传二维码图片"
                              onChange={(event) => {
                                const file = event.target.files?.[0]
                                event.target.value = ''
                                if (file) void uploadQr(file)
                              }}
                            />
                            {uploadingQr ? <Loader2 className="gcc-spin" size={16} aria-hidden /> : qrReference ? <Check size={16} aria-hidden /> : <QrCode size={16} aria-hidden />}
                            <span>{qrReference ? `已验证：${qrReference.payload_preview}` : '上传二维码图片（PNG / JPG），服务端扫码校验'}</span>
                          </label>
                          {qrError && <p className="gcc-inline-error" role="alert">{qrError}</p>}
                        </div>
                      )}
                    </>
                  )}
                  <p className="gcc-deal-sf-note">{anonymous ? '匿名开启：联系方式强制不出现，品牌名也不会写进生成指令。' : '联系方式默认不展示；选择「展示文字」或「展示二维码」后才会进入 prompt、成图与文案，并同步写回确认单。'}</p>
                </fieldset>

                <fieldset>
                  <legend><WandSparkles size={13} aria-hidden /> 视觉风格（更换后重生成）</legend>
                  <div className="gcc-style-chips">
                    {VISUAL_STYLES.map((item) => (
                      <button key={item.id} className={visualStyle === item.id ? 'is-active' : ''} aria-pressed={visualStyle === item.id} onClick={() => setVisualStyle(item.id)}>
                        {item.label}
                      </button>
                    ))}
                  </div>
                </fieldset>

                <fieldset>
                  <legend><ImagePlus size={13} aria-hidden /> 输出规格</legend>
                  <div className="gcc-spec-two">
                    <label>清晰度
                      <select aria-label="清晰度" value={resolution} onChange={(event) => setResolution(event.target.value)}>
                        {RESOLUTIONS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                      </select>
                    </label>
                    {channels.includes('moments') && (
                      <label>朋友圈版式
                        <select aria-label="朋友圈版式" value={momentsLayout} onChange={(event) => setMomentsLayout(event.target.value)}>
                          {MOMENTS_LAYOUT_OPTIONS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                        </select>
                      </label>
                    )}
                  </div>
                </fieldset>
              </div>

              {tentativeFields.length > 0 && (
                <div className="gcc-deal-warn">
                  <CircleAlert size={15} aria-hidden />
                  还有 {tentativeFields.length} 项「待确认」：成交金额、成交时间只有你确认后才会作为事实出现在内容里。
                  <button className="gcc-deal-warn-link" onClick={() => goToStep(2)}>回第二步核对</button>
                </div>
              )}
              {needsRedactionForChat && materials.length > 0 && confirmedMaterials === 0 && (
                <div className="gcc-deal-warn is-strong" role="alert">
                  <ShieldAlert size={15} aria-hidden />
                  聊天晒单需要至少一张已确认打码的素材；当前一张都没有，生成时该组件会跳过（显示为失败项，可之后局部重试）。
                  <button className="gcc-deal-warn-link" onClick={() => goToStep(3)}>去打码</button>
                </div>
              )}
              {unconfirmedMaterials.length > 0 && confirmedMaterials > 0 && (
                <div className="gcc-deal-warn">
                  <ShieldAlert size={15} aria-hidden />
                  {unconfirmedMaterials.length} 张素材未确认打码，不会进入成品；已确认的 {confirmedMaterials} 张会用于聊天晒单等素材垫图。
                  <button className="gcc-deal-warn-link" onClick={() => goToStep(3)}>继续打码</button>
                </div>
              )}

              <div className="gcc-deal-panel-foot">
                <span className="gcc-trust">
                  <ShieldCheck size={15} aria-hidden />
                  只用确认单里的事实 · 原图不出私有空间 · 打码确认的素材才会进入成品
                </span>
                <div className="gcc-deal-foot-actions">
                  <button className="gcc-btn" onClick={resetAll}>
                    再晒一笔
                  </button>
                  <button className="gcc-primary" onClick={() => void generate()} disabled={generating}>
                    {generating ? <Loader2 className="gcc-spin" size={17} aria-hidden /> : <Sparkles size={17} aria-hidden />}
                    {generating ? '正在生成整套晒单内容' : job ? '按当前设置重生成整套' : '确认并生成整套内容'}
                  </button>
                </div>
              </div>
            </section>

            <section className="gcc-results" aria-label="晒单结果">
              {!job && !generating ? (
                <div className="gcc-empty-result">
                  <div><BadgeDollarSign size={28} aria-hidden /></div>
                  <h2>整套晒单内容会出现在这里</h2>
                  <p>按渠道分段展示：喜报海报、聊天晒单、数据卡片、签约故事长图与朋友圈/小红书/抖音成品，失败的可以单独重做。</p>
                </div>
              ) : (
                <>
                  <div className="gcc-status-panel">
                    <div className="gcc-result-bar">
                      <div className="gcc-result-meta">
                        <span className={`gcc-status ${job?.status || 'generating'}`}>{job ? STATUS_TEXT[job.status] : '生成中'}</span>
                        {job && <span>{job.is_revision ? `修订版本 ${job.revision_no || ''}` : `版本 #${job.job_id}`}</span>}
                        {job?.contact?.mode === 'none' && <span>联系方式未展示</span>}
                      </div>
                      <div className="gcc-header-actions">
                        {materials.length > 0 && (
                          <button className="gcc-btn" onClick={() => goToStep(3)}>
                            <Eraser size={15} aria-hidden /> 调整打码
                          </button>
                        )}
                        {allFailedIds.length > 0 && (
                          <button className="gcc-btn" onClick={() => void redoComponents(allFailedIds)} disabled={generating}>
                            <Undo2 size={15} aria-hidden /> 只重做失败项
                          </button>
                        )}
                        {job && (
                          <button className="gcc-btn" onClick={downloadAllCopy}>
                            <Download size={15} aria-hidden /> 下载全部文案
                          </button>
                        )}
                      </div>
                    </div>
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
          </>
        )}

        {pageError && (
          <div className="gcc-alert" role="alert">
            <AlertTriangle size={17} aria-hidden />
            <span>{pageError}</span>
            <button onClick={() => setPageError('')}>知道了</button>
          </div>
        )}
      </div>
    </main>
  )
}
