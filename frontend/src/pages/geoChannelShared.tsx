/**
 * GEO 获客内容中心 · 共用件（Phase 3B 抽取）
 *
 * 普通推广（GeoContentCenter）与晒成交（DealStudio）共用的：
 * 任务/资产类型、渠道元数据（含晒成交渠道）、文案展开/下载工具、
 * 人话错误、幂等锚点读写，以及渠道分段拟真预览组件。
 * 从 GeoContentCenter.tsx 原样迁出，行为不变；晒成交渠道为新增。
 */
import { useState } from 'react'
import {
  AlertTriangle,
  BarChart3,
  Check,
  Copy,
  Download,
  Eye,
  FileText,
  Heart,
  Image as ImageIcon,
  Loader2,
  MessageCircle,
  MessagesSquare,
  Pencil,
  Play,
  RefreshCw,
  ShieldCheck,
  Star,
  Video,
  BadgeDollarSign,
  PartyPopper,
  ScrollText,
  ClipboardList,
  MessageSquareHeart,
} from 'lucide-react'
import { toast } from 'sonner'
import { useIsMobile } from '@/hooks/use-mobile'

import { authFetch } from '@/lib/api'
import { copyToClipboard } from '@/lib/copyUtils'
import { safeRandomUUID } from '@/lib/safeRandomUUID'

/* ---------- 类型 ---------- */
export type Teacher = {
  teacher_id: string
  name: string
  version: string
  system_method?: string
  source?: string
}

export type Strategy = {
  audience: string
  audience_status: string
  action_resistance: string
  human_problem: string
  core_angle: string
  single_value: string
  evidence_statement: string
  single_action: string
  source?: string
}

export type Asset = {
  id: number
  asset_kind?: string
  kind?: string
  bundle_slot?: string
  slot?: string
  url_stored?: string
  url?: string
  thumbnail_url?: string
  thumb?: string
  download_url?: string
  content_text?: string
  text?: string
}

export type ComponentState = {
  component_id: string
  channel: ChannelId
  kind: 'copy' | 'image'
  status: 'pending' | 'succeeded' | 'failed'
  slot?: { slot: string; size: string }
}

/* 合规提醒(Owner 2026-07-22 warn-not-block):后端 job_public_state 透出;
   SSOT 2026-07-23 五问合同:每条带 {code, message(哪处有问题), reason(为什么影响交付),
   repair_hint(AI 可以局部修什么), actions(人如何继续), rule_version(规则版本)};
   渲染一律琥珀色提醒,绝不用红色失败样式。 */
export type JobWarning = {
  code: string
  message: string
  reason?: string
  repair_hint?: string
  actions?: Array<{ id: string; label: string }>
  rule_version?: string
  channel?: string
  component_id?: string
}

export type JobState = {
  job_id: number
  status: 'pending' | 'generating' | 'partial_success' | 'succeeded' | 'failed' | 'blocked'
  error_summary?: string
  assets: Asset[]
  components: ComponentState[]
  channels: ChannelId[]
  warnings?: JobWarning[]
  quick_task?: string
  teacher?: Teacher
  strategy?: Strategy
  evidence?: { facts?: Array<Record<string, unknown>>; source_note?: string }
  trend?: { requested?: boolean; used?: boolean; reason?: string; source?: string; observed_at?: string; topic?: string }
  contact?: { mode?: string; text?: string }
  created_at?: string
  is_revision?: boolean
  revision_no?: number
}

export type ChannelId =
  | 'professional_poster' | 'moments' | 'xiaohongshu' | 'douyin'
  | 'infographic' | 'private_chat' | 'diagnosis_case'
  | 'deal_poster' | 'deal_chat' | 'deal_data_card' | 'deal_story' | 'deal_feedback_card'

export type ChannelMeta = { id: ChannelId; label: string; short: string; icon: typeof ImageIcon }

export const CHANNELS: ChannelMeta[] = [
  { id: 'professional_poster', label: '专业海报', short: '海报', icon: ImageIcon },
  { id: 'moments', label: '朋友圈图片 + 三种文案', short: '朋友圈', icon: MessageCircle },
  { id: 'xiaohongshu', label: '小红书封面 / 卡片 / 正文', short: '小红书', icon: FileText },
  { id: 'douyin', label: '抖音封面 / 口播 / 分镜', short: '抖音', icon: Video },
  { id: 'infographic', label: '信息图', short: '信息图', icon: BarChart3 },
  { id: 'private_chat', label: '私聊邀约 / 咨询话术', short: '聊天素材', icon: MessagesSquare },
  { id: 'diagnosis_case', label: '真实数据战报 / 诊断案例卡', short: '案例卡', icon: ShieldCheck },
]

/* 晒成交渠道（后端 CHANNEL_CONTRACTS deal_*，另有 moments/xiaohongshu/douyin 复用） */
export const DEAL_CHANNELS: ChannelMeta[] = [
  { id: 'deal_poster', label: '喜报海报', short: '喜报海报', icon: PartyPopper },
  { id: 'deal_chat', label: '聊天晒单', short: '聊天晒单', icon: MessageSquareHeart },
  { id: 'deal_data_card', label: '数据卡片', short: '数据卡片', icon: ClipboardList },
  { id: 'deal_story', label: '签约故事长图', short: '签约故事', icon: ScrollText },
  { id: 'deal_feedback_card', label: '反馈卡片', short: '反馈卡片', icon: BadgeDollarSign },
]

export const ALL_CHANNEL_META: ChannelMeta[] = [...CHANNELS, ...DEAL_CHANNELS]

/* 晒成交包判定：优先 quick_task=showcase_deal（晒成交全选复用渠道如只勾
   朋友圈/小红书/抖音时，渠道里没有任何 deal_*，这是唯一可靠口径）；
   旧数据缺 quick_task 字段时回退 deal_* 渠道启发式。两边历史共用。 */
export const DEAL_ONLY_CHANNEL_IDS: ReadonlySet<ChannelId> = new Set([
  'deal_poster', 'deal_chat', 'deal_data_card', 'deal_story', 'deal_feedback_card',
])

export function isShowcaseDealJob(item: { quick_task?: string; channels?: ChannelId[] }): boolean {
  if (item.quick_task === 'showcase_deal') return true
  return Array.isArray(item.channels) && item.channels.some((channel) => DEAL_ONLY_CHANNEL_IDS.has(channel))
}

export function channelMeta(id: ChannelId): ChannelMeta | undefined {
  return ALL_CHANNEL_META.find((item) => item.id === id)
}

export const CHANNEL_SUB: Record<ChannelId, string> = {
  professional_poster: '3:4 · 1 张',
  moments: '单图或九宫格 · 多版文案任选',
  xiaohongshu: '封面 + 卡片 + 正文',
  douyin: '9:16 封面 + 口播稿 + 分镜',
  infographic: '3:4 · 1 张',
  private_chat: '私聊话术 + 9:16 示例对话图',
  diagnosis_case: '3:4 · 只用冻结证据',
  deal_poster: '3:4 · 只用确认单事实',
  deal_chat: '9:16 · 基于已确认打码素材',
  deal_data_card: '3:4 · 只用已确认数字',
  deal_story: '9:16 · 按确认单讲全过程',
  deal_feedback_card: '3:4 · 原话来自确认单',
}

export const STATUS_TEXT: Record<JobState['status'], string> = {
  pending: '草稿',
  generating: '生成中',
  partial_success: '部分成功',
  succeeded: '已完成',
  failed: '生成失败',
  blocked: '需要修改',
}

/* ---------- 幂等锚点 / 请求工具（与主流程同构） ---------- */
export type PendingRequest = { requestId: string; payloadHash: string; jobId?: number; createdAt: number; notFoundSince?: number }
export type PendingRetry = { retryKey: string; requestId: string; childJobId?: number; createdAt: number; notFoundSince?: number }

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

/* 非 secure-context（http 非 localhost 部署）没有 crypto.subtle：降级 FNV-1a。
   hash 仅用于同键比较（丢响应后判断是否复用同一幂等键），不需要密码学强度。 */
function fnv1a(input: string, seed: number): string {
  let hash = seed >>> 0
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

export async function payloadHash(value: unknown) {
  const text = canonicalJson(value)
  if (typeof crypto !== 'undefined' && typeof crypto.subtle?.digest === 'function') {
    const data = new TextEncoder().encode(text)
    const digest = await crypto.subtle.digest('SHA-256', data)
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
  }
  return `fnv1a:${fnv1a(text, 0x811c9dc5)}${fnv1a(text, 0x9dc5811c)}`
}

export function readPendingRequest(storageKey: string | null): PendingRequest | null {
  if (!storageKey) return null
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || 'null') as PendingRequest | null
    if (!value || typeof value.requestId !== 'string' || typeof value.payloadHash !== 'string' || !Number.isFinite(value.createdAt)) return null
    if (Date.now() - value.createdAt > 7 * 86400_000) return null
    return value
  } catch { return null }
}

export function readPendingRetry(storageKey: string | null): PendingRetry | null {
  if (!storageKey) return null
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || 'null') as PendingRetry | null
    if (!value || typeof value.retryKey !== 'string' || typeof value.requestId !== 'string' || !Number.isFinite(value.createdAt)) return null
    if (Date.now() - value.createdAt > 86400_000) return null
    return value
  } catch { return null }
}

export function requestId() {
  return safeRandomUUID()
}

export async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text()
  if (!text) return {} as T
  try { return JSON.parse(text) as T } catch { return {} as T }
}

export const MACHINE_CODE_RE = /^[a-z0-9_.:-]+$/i

/* 提醒(warning)人话兜底:后端 warning 一般自带 message,缺 message 时按 code 兜底。 */
export const WARNING_MESSAGES: Record<string, string> = {
  forbidden_claim: '文案含承诺/保证类表述，请确认都能兑现再发布',
  number_without_evidence: '文案里的数字没有对应证据来源，发布前请核对',
  testimonial_without_evidence: '文案包含客户评价类表述，但没有对应证据，请确认其真实性',
  unlabelled_simulated_chat: '聊天示意内容建议显著标注「示例对话」，避免被当成真实记录',
  contact_forbidden_when_none: '联系方式已关闭，但文案里出现了联系方式类内容，请确认是否需要',
  explicit_contact_missing: '已开启联系方式，但文案里没有出现你填写的联系方式',
  brand_name_missing: '成图里没有出现品牌名，确认是否需要品牌露出',
  brand_name_forbidden_when_anonymous: '匿名晒单的成图里疑似出现了品牌名，请检查后再发布',
  exact_copy_missing: '成图上的文字与文案不完全一致，请核对',
  visual_number_without_evidence: '成图上出现了没有证据来源的数字，请核对',
  contact_visible_when_disabled: '联系方式已关闭，但成图里疑似出现联系方式/二维码，请检查',
  contact_text_missing: '成图里没有出现你填写的联系方式',
  strategy_promise_claim: '策略里有承诺/保证类表述，请确认都能兑现',
  diagnosis_case_requires_frozen_evidence: '案例卡引用的诊断数据建议先发布诊断报告；本版已按常青口径生成',
  ai_extraction_beyond_materials: 'AI 提取的内容超出了你提供的材料，请核对',
  redaction_manual_review_suggested: '素材疑似有隐私内容但未能自动定位，建议人工复核一遍打码',
}

export function warningMessage(warning: JobWarning | { code?: unknown; message?: unknown }): string {
  const message = String(warning?.message || '').trim()
  if (message) return message
  const code = String(warning?.code || '')
  return WARNING_MESSAGES[code] || '请核对后再发布'
}

export function errorMessage(data: unknown, fallback: string) {
  if (!data || typeof data !== 'object') return fallback
  const detail = (data as { detail?: unknown }).detail
  if (typeof detail === 'string') {
    const known: Record<string, string> = {
      qr_requires_exactly_one_decodable_code: '这个文件没有且仅有一个可扫码二维码，请换一张清晰原图。',
      qr_reference_token_invalid: '二维码验证已失效，请重新上传原图。',
      published_diagnosis_evidence_not_found: '这个客户暂时没有可用于推广的已发布诊断。',
      strategy_contains_forbidden_claim: '文案含广告法违禁极限词（如「最/第一/顶级」类），请改掉后再提交。',
      asset_copy_only_editable: '只有文案成品可以编辑，已生成的图片不能改。',
      asset_copy_edit_empty: '没有检测到改动，未提交。',
      deal_draft_request_id_conflict: '这笔成交草稿已被另一份内容占用，已为你新开一稿。',
      deal_draft_required: '请先完成成交信息整理，再生成晒单内容。',
      deal_sheet_edit_empty: '没有检测到改动，未提交。',
      redaction_preview_required: '请先生成打码预览，再确认。',
      redaction_region_not_found: '这个打码区域已不存在，请刷新素材状态。',
      redaction_strength_invalid: '打码强度只支持三档。',
      redaction_box_invalid: '框选区域太小或超出图片范围，请重新框选。',
      redaction_action_invalid: '不支持的打码操作。',
      materials_count_invalid: '每批最多上传 9 张图片。',
      materials_total_exceeded: '一笔成交最多上传 20 张素材。',
      request_id_invalid: '请求标识不合法，请刷新页面重试。',
    }
    if (known[detail]) return known[detail]
    if (detail.startsWith('asset_copy_field_not_editable')) return '这个字段不允许编辑，已保留原文。'
    if (detail.startsWith('deal_sheet_field_unknown')) return '确认单里这个字段不允许编辑，已保留原文。'
    // Stable machine codes are useful in logs, not in customer-facing copy.
    if (MACHINE_CODE_RE.test(detail)) return fallback
    return detail
  }
  if (detail && typeof detail === 'object') {
    const value = detail as { message?: string; code?: string }
    if (value.message) return value.message
    if (value.code && !MACHINE_CODE_RE.test(value.code)) return value.code
    return fallback
  }
  return fallback
}

export function editErrorMessage(data: unknown) {
  const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : null
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const value = detail as { code?: unknown; message?: unknown; repair_hint?: unknown }
    const code = String(value.code || '')
    if (code === 'asset_copy_edit_rejected') {
      // 五问合同:优先用后端 message + repair_hint(AI 可修提示),不落机器码。
      const message = typeof value.message === 'string' && value.message ? value.message : '改后的文案含广告法违禁极限词，请改掉这些词再保存。'
      const hint = typeof value.repair_hint === 'string' && value.repair_hint ? `（AI 可修：${value.repair_hint}）` : ''
      return message + hint
    }
  }
  return errorMessage(data, '文案保存失败，请稍后再试。')
}

/* ---------- 文案 / 资产工具 ---------- */
export function assetSlot(asset: Asset) { return String(asset.bundle_slot || asset.slot || '') }
export function assetUrl(asset: Asset) { return String(asset.download_url || asset.url_stored || asset.url || '') }
export function assetText(asset: Asset) { return String(asset.content_text || asset.text || '') }

export function parseCopy(asset?: Asset): Record<string, unknown> | null {
  if (!asset) return null
  try {
    const value = JSON.parse(assetText(asset))
    return value && typeof value === 'object' ? value as Record<string, unknown> : null
  } catch {
    return assetText(asset) ? { body: assetText(asset) } : null
  }
}

export const COPY_FIELD_NAMES: Record<string, string> = {
  title: '标题', body: '正文', cta: '唯一行动', source_note: '依据', script: '口播稿',
  shots: '分镜', tags: '推荐标签', card_outline: '卡片提纲', opening: '首次私聊',
  follow_up: '跟进话术', objection_reply: '异议回复', restrained: '克制',
  professional: '专业', friendly: '朋友式', facts: '真实事实',
}

export function flattenCopy(value: unknown, prefix = ''): Array<{ label: string; text: string }> {
  if (typeof value === 'string' && value.trim()) return [{ label: prefix || '文案', text: value }]
  if (Array.isArray(value)) {
    const text = value.flatMap((item) => {
      if (typeof item === 'string') return [item]
      if (item && typeof item === 'object') {
        const fact = item as { label?: unknown; value?: unknown }
        if (fact.label != null && fact.value != null) return [`${String(fact.label)}：${String(fact.value)}`]
      }
      return []
    }).join(prefix === '推荐标签' ? ' ' : '\n')
    return text ? [{ label: prefix || '标签', text }] : []
  }
  if (!value || typeof value !== 'object') return []
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    flattenCopy(child, COPY_FIELD_NAMES[key] || key),
  )
}

export function downloadText(name: string, text: string) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export async function downloadImage(url: string, name: string) {
  const response = await authFetch(url)
  if (!response.ok) throw new Error('download_failed')
  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(objectUrl)
}

export function humanFailureReason(job: JobState | null) {
  const raw = String(job?.error_summary || '').trim()
  if (raw && !MACHINE_CODE_RE.test(raw)) return raw
  return '生成服务暂时波动，重试这一项通常就能恢复；已成功的内容会原样保留。'
}

/* ---------- 合规提醒(琥珀色可折叠提醒条;不阻断任何操作,绝不红色) ---------- */
export function channelWarnings(job: JobState | null, channelId: ChannelId): JobWarning[] {
  return (job?.warnings || []).filter((warning) =>
    (warning.channel || '') === channelId || String(warning.component_id || '').startsWith(`${channelId}:`),
  )
}

export function WarningsBar({ warnings, scope = 'channel' }: { warnings: JobWarning[]; scope?: 'channel' | 'job' }) {
  const [open, setOpen] = useState(false)
  if (!warnings.length) return null
  return (
    <div className="gcc-warnings" data-warnings-scope={scope}>
      <button
        type="button"
        className="gcc-warnings-toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <AlertTriangle size={14} aria-hidden />
        {scope === 'job' ? `${warnings.length} 条发布前提醒` : `${warnings.length} 条提醒`}
        <span className="gcc-warnings-caret">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <ul className="gcc-warnings-list">
          {warnings.map((warning, index) => (
            <li key={`${warning.code}-${index}`}>
              {/* 五问合同(SSOT §6):哪处有问题/为什么影响交付/AI 可修什么/人如何继续/规则版本 */}
              <span className="gcc-warning-message">{warningMessage(warning)}</span>
              {warning.reason && <span className="gcc-warning-reason">{warning.reason}</span>}
              {warning.repair_hint && (
                <span className="gcc-warning-repair">
                  <em>AI 可修</em>
                  {warning.repair_hint}
                </span>
              )}
              {(warning.actions?.length ?? 0) > 0 && (
                <span className="gcc-warning-actions">
                  {warning.actions!.map((action) => <i key={action.id}>{action.label}</i>)}
                </span>
              )}
              {warning.rule_version && <span className="gcc-warning-rule">规则版本 {warning.rule_version}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export type EditField = {
  key: string
  label: string
  kind: 'text' | 'list' | 'map'
  value: string
  mapValue?: Record<string, string>
}

export function buildEditFields(data: Record<string, unknown> | null): EditField[] {
  if (!data) return []
  return Object.entries(data).flatMap(([key, value]): EditField[] => {
    const label = COPY_FIELD_NAMES[key] || key
    if (typeof value === 'string') return [{ key, label, kind: 'text', value }]
    if (Array.isArray(value) && value.every((item) => typeof item === 'string')) {
      return [{ key, label, kind: 'list', value: (value as string[]).join('\n') }]
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const entries = Object.entries(value as Record<string, unknown>)
      if (entries.length && entries.every(([, item]) => typeof item === 'string')) {
        const mapValue: Record<string, string> = {}
        entries.forEach(([childKey, childValue]) => { mapValue[childKey] = String(childValue) })
        return [{ key, label, kind: 'map', value: '', mapValue }]
      }
    }
    return []
  })
}

/* ---------- 渠道分段（拟真预览 + 文案 + 操作） ---------- */
export type ChannelSectionProps = {
  channelId: ChannelId
  job: JobState | null
  generating: boolean
  objectUrls: Record<number, string>
  onRetry: (componentIds: string[]) => void
  onSaveCopy: (assetId: number, updates: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>
}

export function ChannelSection({ channelId, job, generating, objectUrls, onRetry, onSaveCopy }: ChannelSectionProps) {
  const meta = channelMeta(channelId)
  const label = meta?.label || '渠道成品'
  const Icon = meta?.icon || ImageIcon
  const images = job?.assets.filter((asset) => assetSlot(asset).startsWith(`${channelId}:image:`)) || []
  const copyAsset = job?.assets.find((asset) => assetSlot(asset) === `${channelId}:copy`)
  const copyData = parseCopy(copyAsset)
  const copyRows = flattenCopy(copyData)
  const tags = copyRows.find((row) => row.label === '推荐标签')?.text || ''
  const textRows = copyRows.filter((row) => row.label !== '推荐标签')
  const components = job?.components.filter((component) => component.channel === channelId) || []
  const warnings = channelWarnings(job, channelId)
  const failed = components.filter((component) => component.status === 'failed')
  const failedIds = failed.map((component) => component.component_id)
  const imageFailed = failed.some((component) => component.kind === 'image')
  const copyFailed = failed.some((component) => component.kind === 'copy')
  const badge = !job || job.status === 'generating' || job.status === 'pending'
    ? { text: '生成中', cls: 'gray' }
    : failed.length
      ? { text: '需重做', cls: 'amber' }
      : components.length && components.every((component) => component.status === 'succeeded')
        ? { text: '已完成', cls: 'green' }
        : job.status === 'failed' || job.status === 'blocked'
          ? { text: '未完成', cls: 'red' }
          : { text: '生成中', cls: 'gray' }

  const [activeImageId, setActiveImageId] = useState<number | null>(null)
  const [editFields, setEditFields] = useState<EditField[] | null>(null)
  const [editError, setEditError] = useState('')
  const [saving, setSaving] = useState(false)
  const activeImage = images.find((asset) => asset.id === activeImageId) || images[0]
  /* 🔴 [包三 · 图文] 手机上「下载」这个词没有意义 —— 文件是进相册的。
     用她所在设备上成立的那个词:手机说「保存到手机」,桌面说「下载图片」。 */
  const isPhone = useIsMobile()
  const activeImageUrl = activeImage ? objectUrls[activeImage.id] : ''

  const copyAllText = [...textRows, ...(tags ? [{ label: '推荐标签', text: tags }] : [])]
    .map((row) => `${row.label}\n${row.text}`).join('\n\n')

  const saveEdit = async () => {
    if (!copyAsset || !editFields) return
    const updates: Record<string, unknown> = {}
    editFields.forEach((field) => {
      const original = copyData?.[field.key]
      if (field.kind === 'text') {
        if (field.value !== original) updates[field.key] = field.value
        return
      }
      if (field.kind === 'list') {
        const next = field.value.split('\n').map((item) => item.trim()).filter(Boolean)
        if (JSON.stringify(next) !== JSON.stringify(original)) updates[field.key] = next
        return
      }
      const next: Record<string, string> = {}
      Object.entries(field.mapValue || {}).forEach(([childKey, childValue]) => { next[childKey] = childValue })
      if (JSON.stringify(next) !== JSON.stringify(original)) updates[field.key] = next
    })
    if (!Object.keys(updates).length) {
      setEditError('没有检测到改动，未提交。')
      return
    }
    setSaving(true)
    setEditError('')
    try {
      const result = await onSaveCopy(copyAsset.id, updates)
      if (result.ok) setEditFields(null)
      else setEditError(result.error || '文案保存失败，请稍后再试。')
    } finally {
      setSaving(false)
    }
  }

  const openEdit = () => {
    setEditError('')
    setEditFields(buildEditFields(copyData))
  }

  const skippedFields = copyData
    ? Object.keys(copyData).some((key) => !editFields?.some((field) => field.key === key))
    : false

  return (
    <section className="gcc-channel-section" data-channel-section={channelId} aria-label={label}>
      <div className="gcc-channel-head">
        <span className="gcc-channel-title">
          <Icon size={17} aria-hidden />
          {meta?.short || channelId} <span className="gcc-channel-sub">{CHANNEL_SUB[channelId]}</span>
        </span>
        <span className={`gcc-badge ${badge.cls}`}>{badge.text === '已完成' && <Check size={12} aria-hidden />}{badge.text}</span>
      </div>
      {warnings.length > 0 && <WarningsBar warnings={warnings} />}
      <div className="gcc-channel-body">
        <div className="gcc-preview-zone">
          <ChannelMock
            channelId={channelId}
            label={label}
            images={images}
            activeImage={activeImage}
            activeImageUrl={activeImageUrl}
            objectUrls={objectUrls}
            copyData={copyData}
            generating={generating}
            imageFailed={imageFailed}
          />
          {images.length > 1 && (
            <div className="gcc-thumb-row">
              {images.map((asset, index) => (
                <button key={asset.id} className={activeImage?.id === asset.id ? 'is-active' : ''} onClick={() => setActiveImageId(asset.id)} aria-label={`第 ${index + 1} 张`}>
                  {objectUrls[asset.id] ? <img src={objectUrls[asset.id]} alt={`${label}第 ${index + 1} 张`} /> : <Loader2 className="gcc-spin" size={15} aria-hidden />}
                </button>
              ))}
            </div>
          )}
          <PreviewCaption channelId={channelId} hasImage={Boolean(activeImage)} imageFailed={imageFailed} />
        </div>
        <div className="gcc-copy-zone">
          {failed.length > 0 && (
            <>
              <div className="gcc-fail-panel" role="alert">
                <h4><AlertTriangle size={16} aria-hidden />{copyFailed ? '文案没通过质检，这一版不能发' : '图片没通过视觉质检，不能发'}</h4>
                <p>{humanFailureReason(job)}</p>
                <div className="gcc-fail-actions">
                  <button className="gcc-btn small danger-ghost" onClick={() => onRetry(failedIds)} disabled={generating}>
                    <RefreshCw size={13} aria-hidden /> 局部重试
                  </button>
                </div>
              </div>
              <span className="gcc-keep-note"><ShieldCheck size={13} aria-hidden />局部重试不重复扣费，其他渠道已生成的内容原样保留</span>
            </>
          )}
          {textRows.length > 0 && (
            <div className="gcc-copy-content">
              {textRows.map((row, index) => (
                <article key={`${row.label}-${index}`}>
                  <div><small>{row.label}</small><button onClick={() => void copyToClipboard(row.text).then((ok) => ok ? toast.success(`${row.label}已复制`) : toast.error('复制失败'))}><Copy size={13} aria-hidden /> 复制</button></div>
                  <p>{row.text}</p>
                </article>
              ))}
            </div>
          )}
          {tags && (
            <div className="gcc-tags">
              <div><small>推荐标签</small><button onClick={() => void copyToClipboard(tags).then((ok) => ok ? toast.success('标签已复制') : toast.error('复制失败'))}><Copy size={13} aria-hidden /> 复制标签</button></div>
              <p>{tags}</p>
            </div>
          )}
          {!textRows.length && !failed.length && (
            generating
              ? <div className="gcc-copy-loading"><Loader2 className="gcc-spin" size={18} aria-hidden /> 正在写渠道原生文案并核对证据</div>
              : job ? <div className="gcc-copy-failed"><AlertTriangle size={17} aria-hidden /> 文案未通过证据质检</div> : null
          )}
          {editFields && (
            <div className="gcc-edit-form">
              <h4><Pencil size={15} aria-hidden /> 编辑文案（只改文字，不动已生成的图片）</h4>
              {editFields.map((field, fieldIndex) => field.kind === 'map' ? (
                <div key={field.key} className="gcc-edit-field">
                  <span>{field.label}</span>
                  {Object.entries(field.mapValue || {}).map(([childKey, childValue]) => (
                    <label key={childKey} className="gcc-edit-field">
                      <span>{COPY_FIELD_NAMES[childKey] || childKey}</span>
                      <textarea
                        value={childValue}
                        rows={2}
                        maxLength={500}
                        onChange={(event) => setEditFields((current) => current?.map((item, index) => index === fieldIndex
                          ? { ...item, mapValue: { ...(item.mapValue || {}), [childKey]: event.target.value } }
                          : item) || null)}
                      />
                    </label>
                  ))}
                </div>
              ) : (
                <label key={field.key} className="gcc-edit-field">
                  <span>{field.label}{field.kind === 'list' ? '（每行一条）' : ''}</span>
                  <textarea
                    value={field.value}
                    rows={field.kind === 'list' ? 4 : 2}
                    maxLength={field.kind === 'list' ? 1200 : 500}
                    onChange={(event) => setEditFields((current) => current?.map((item, index) => index === fieldIndex ? { ...item, value: event.target.value } : item) || null)}
                  />
                </label>
              ))}
              {skippedFields && <p className="gcc-edit-note">结构化数据（如真实事实表）不在此处编辑，可用「只重做这一项」重新生成。</p>}
              {editError && <p className="gcc-inline-error" role="alert">{editError}</p>}
              <div className="gcc-edit-actions">
                <button className="gcc-btn small ink" onClick={() => void saveEdit()} disabled={saving}>
                  {saving ? <Loader2 className="gcc-spin" size={14} aria-hidden /> : <Check size={14} aria-hidden />} 保存文案
                </button>
                <button className="gcc-btn small" onClick={() => { setEditFields(null); setEditError('') }} disabled={saving}>取消</button>
              </div>
            </div>
          )}
          <div className="gcc-item-actions">
            {copyRows.length > 0 && (
              <button className="gcc-btn small" onClick={() => void copyToClipboard(copyAllText).then((ok) => ok ? toast.success('文案已复制') : toast.error('复制失败'))}>
                <Copy size={13} aria-hidden /> 复制文案
              </button>
            )}
            {activeImage && (
              <button className="gcc-btn small" data-testid="gcc-save-image" onClick={() => void downloadImage(assetUrl(activeImage), `${label}.png`).catch(() => toast.error('图片下载失败'))}>
                <Download size={13} aria-hidden /> {isPhone ? '保存到手机' : '下载图片'}
              </button>
            )}
            {copyRows.length > 0 && (
              <button className="gcc-btn small" onClick={() => downloadText(`${label}.txt`, copyAllText)}>
                <FileText size={13} aria-hidden /> 下载文案
              </button>
            )}
            {copyAsset && !editFields && (
              <button className="gcc-btn small" onClick={openEdit}>
                <Pencil size={13} aria-hidden /> 编辑
              </button>
            )}
            {job && components.length > 0 && (
              <button className="gcc-btn small" onClick={() => onRetry(components.map((component) => component.component_id))} disabled={generating}>
                <RefreshCw size={13} aria-hidden /> 只重做这一项
              </button>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

export function PreviewCaption({ channelId, hasImage, imageFailed }: { channelId: ChannelId; hasImage: boolean; imageFailed: boolean }) {
  if (imageFailed) {
    return <span className="gcc-preview-caption is-amber"><AlertTriangle size={13} aria-hidden /> 图片未通过视觉质检，可局部重试</span>
  }
  if (channelId === 'private_chat' || channelId === 'deal_chat') {
    return <span className="gcc-preview-caption is-amber"><AlertTriangle size={13} aria-hidden /> 示例对话图按规范带「示例对话」标识</span>
  }
  if (channelId === 'xiaohongshu') {
    return <span className="gcc-preview-caption"><Eye size={13} aria-hidden /> 笔记效果示意 · 互动数为示例</span>
  }
  if (channelId === 'moments') {
    return <span className="gcc-preview-caption"><Eye size={13} aria-hidden /> 朋友圈效果示意 · 第 1 版文案</span>
  }
  if (channelId === 'douyin') {
    return <span className="gcc-preview-caption"><Eye size={13} aria-hidden /> 封面效果示意</span>
  }
  return <span className="gcc-preview-caption"><Eye size={13} aria-hidden /> {hasImage ? '成品预览 · 下载为 PNG' : '等待成图'}</span>
}

export function ChannelMock(props: {
  channelId: ChannelId
  label: string
  images: Asset[]
  activeImage?: Asset
  activeImageUrl: string
  objectUrls: Record<number, string>
  copyData: Record<string, unknown> | null
  generating: boolean
  imageFailed: boolean
}) {
  const { channelId, label, images, activeImage, activeImageUrl, objectUrls, copyData, generating, imageFailed } = props
  const placeholder = (
    <div className={`gcc-mock-placeholder ${imageFailed ? 'is-error' : ''}`}>
      {imageFailed
        ? <><AlertTriangle size={22} aria-hidden /><span>图片未通过视觉质检</span><small>已生成的文案保留，可只重做图片</small></>
        : <><Loader2 className="gcc-spin" size={22} aria-hidden /><span>视觉总监正在生成并质检中文成图</span></>}
    </div>
  )
  const copyText = (key: string) => {
    const value = copyData?.[key]
    return typeof value === 'string' ? value : ''
  }

  if (channelId === 'moments') {
    const tones = copyData?.tones && typeof copyData.tones === 'object' ? copyData.tones as Record<string, unknown> : null
    const firstTone = tones ? String(tones.restrained || tones.professional || tones.friendly || '') : ''
    const text = firstTone || copyText('body') || copyText('title') || '朋友圈文案生成后会显示在这里'
    return (
      <div className="gcc-mock-moments">
        <span className="gcc-m-avatar" aria-hidden>我</span>
        <div>
          <div className="gcc-m-name">我</div>
          <p className="gcc-m-text">{text}</p>
          {images.length > 1 ? (
            <div className="gcc-m-img is-grid">
              {images.slice(0, 9).map((asset) => objectUrls[asset.id]
                ? <img key={asset.id} src={objectUrls[asset.id]} alt={`${label}九宫格成图`} />
                : <span key={asset.id} className="gcc-m-img-empty"><Loader2 className="gcc-spin" size={14} aria-hidden /></span>)}
            </div>
          ) : (
            <div className="gcc-m-img">
              {activeImageUrl ? <img src={activeImageUrl} alt={`${label}最终成图`} /> : <span className="gcc-m-img-empty">{generating ? '成图生成中' : imageFailed ? '成图未过质检' : '成图占位'}</span>}
            </div>
          )}
          <div className="gcc-m-meta">
            <span>刚刚</span>
            <span className="gcc-m-icons"><Heart size={13} aria-hidden /><MessageCircle size={13} aria-hidden /></span>
          </div>
          <div className="gcc-m-likes">效果示意 · 实际以你发布的朋友圈为准</div>
        </div>
      </div>
    )
  }

  if (channelId === 'xiaohongshu') {
    const cover = images.find((asset) => assetSlot(asset).includes('xhs_cover')) || images[0]
    const coverUrl = cover ? objectUrls[cover.id] : ''
    const title = copyText('title') || '小红书标题生成后显示在这里'
    return (
      <div className="gcc-mock-xhs">
        <div className="gcc-x-cover">
          {coverUrl
            ? <img src={coverUrl} alt={`${label}最终成图`} />
            : <div className="gcc-x-empty"><span className="gcc-x-big">{generating ? '封面生成中…' : imageFailed ? '封面未过质检' : title}</span></div>}
        </div>
        <div className="gcc-x-body">
          <p className="gcc-x-title">{title}</p>
          <div className="gcc-x-author">
            <span className="gcc-xa" aria-hidden>我</span><span>我的获客笔记</span>
            <span className="gcc-x-engage">
              <span><Heart size={13} aria-hidden /> 示例</span>
              <span><Star size={13} aria-hidden /> 示例</span>
            </span>
          </div>
        </div>
      </div>
    )
  }

  if (channelId === 'douyin') {
    const hook = copyText('title') || '抖音封面标题生成后显示在这里'
    return (
      <div className="gcc-mock-douyin">
        {activeImageUrl && <img src={activeImageUrl} alt={`${label}最终成图`} />}
        <p className="gcc-d-hook">{hook}</p>
        <span className="gcc-d-play"><Play size={20} aria-hidden fill="currentColor" /></span>
        <span className="gcc-d-bottom"><b>@我的获客账号</b>口播稿与分镜在右侧 · ♫ 原声</span>
      </div>
    )
  }

  if (channelId === 'private_chat') {
    const opening = copyText('opening') || '首次私聊话术生成后显示在这里'
    const followUp = copyText('follow_up')
    return (
      <>
        <div className="gcc-mock-chat">
          <span className="gcc-c-demo"><AlertTriangle size={12} aria-hidden /> 示例对话 · 效果演示</span>
          <div className="gcc-c-row me">
            <div><p className="gcc-c-bubble">{opening}</p></div>
            <span className="gcc-c-avatar" aria-hidden>我</span>
          </div>
          <div className="gcc-c-row">
            <span className="gcc-c-avatar" aria-hidden>客</span>
            <div>
              <div className="gcc-c-name">客户（示例）</div>
              <p className="gcc-c-bubble">好的，先了解一下。</p>
            </div>
          </div>
          {followUp && (
            <div className="gcc-c-row me">
              <div><p className="gcc-c-bubble">{followUp}</p></div>
              <span className="gcc-c-avatar" aria-hidden>我</span>
            </div>
          )}
        </div>
        {activeImage && (
          <div className="gcc-mock-frame is-portrait" style={{ marginTop: 10 }}>
            {activeImageUrl ? <img src={activeImageUrl} alt={`${label}最终成图`} /> : placeholder}
          </div>
        )}
      </>
    )
  }

  /* 晒成交：喜报海报/数据卡片/反馈卡片 3:4；聊天晒单/签约故事长图 9:16 */
  const portrait = channelId === 'deal_chat' || channelId === 'deal_story'
  return (
    <div className={`gcc-mock-frame${portrait ? ' is-portrait' : ''}`}>
      {activeImageUrl ? <img src={activeImageUrl} alt={`${label}最终成图`} /> : placeholder}
    </div>
  )
}
