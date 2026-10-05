// 营销军师 · 生成营销素材向导(4 步) — 主题感知 · 移动可用
// 这个页面驱动的是「用户侧」素材 API(前缀 /api/marketing),与 admin 控制台的 ./api(/api/admin/marketing)不同,
// 因此这里直接用 authFetch 调用,不引 ./api。
// 口径铁律:对客金额一律「算力」· 指标叫「相关转化」· 不出现任何承诺词。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Check, ChevronLeft, ChevronRight, Copy as CopyIcon, Download, History,
  ImageIcon, Loader2, RefreshCw, Save, Sparkles, Type as TypeIcon, Users, Wand2,
} from 'lucide-react'
import { toast } from 'sonner'
import { authFetch } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import { FadeIn, Panel, LoadingState, EmptyState, ErrorState, PowerAndYuan } from './ui'

// ============ 本地类型(用户 API 未在合约文件中定型 · 这里做防御式解析)============
type MaterialKind = 'poster' | 'copy'

interface Template { id: number; name: string; scene_type?: string; material_kind?: string }
interface RawTemplate {
  id?: number; template_id?: number; name?: string; title?: string
  scene_type?: string; scene?: string; material_kind?: string
}
interface Asset {
  id: number; material_kind?: string; content_text?: string
  url_stored?: string; thumbnail_url?: string; status?: string
}
interface GenerateResp {
  ok?: boolean; block_reason?: string; job_id?: number
  assets?: Asset[]; asset?: Asset; status?: string
}
interface JobResp { id?: number; status?: string; assets?: Asset[]; asset?: Asset }

// 历史记录(my-materials 真实返回结构:job 分组 + 嵌套资产 · 之前按平铺解析导致物料库永远空)
interface HistoryAsset {
  id: number; kind?: string; url?: string; thumb?: string
  text?: string; slot?: string; rights_confirmed?: number
}
interface HistoryJob {
  job_id: number; material_kind?: string; status?: string
  created_at?: string; error_summary?: string; assets: HistoryAsset[]
}

// ============ 静态配置 ============
// 营销目的按"为谁做"联动(老板 2026-07-05 点破:终端客户经营自己,不可能替平台拉充值):
// 选客户品牌 = 替他的生意做物料;选自己 = 推广自己(用户=推广服务;admin=平台自营销)
interface Purpose { key: string; label: string; hint: string; recommended?: boolean }
const PURPOSES_CLIENT: Purpose[] = [
  { key: 'opening', label: '开业 / 上新', hint: '替客户的门店或新品造势', recommended: true },
  { key: 'promo', label: '促销活动', hint: '客户自己的限时优惠宣传' },
  { key: 'invite', label: '活动邀请', hint: '邀请客户的顾客参加沙龙 / 专场活动' },
  { key: 'daily', label: '日常专业形象', hint: '轻分享不硬卖,帮客户维持朋友圈存在感' },
]
const PURPOSES_SELF_USER: Purpose[] = [
  { key: 'acquire', label: '获客拉新', hint: '推广你自己的 AI 搜索优化服务', recommended: true },
  { key: 'callback', label: '老客唤回', hint: '唤回你自己的沉默老客户' },
  { key: 'notice', label: '活动通知', hint: '把新活动或权益告诉客户' },
]
const PURPOSES_SELF_ADMIN: Purpose[] = [
  { key: 'recharge', label: '拉新充值', hint: '吸引新用户完成首次充值,推荐搭配首充双倍活动', recommended: true },
  { key: 'repurchase', label: '促复购', hint: '唤回老客户再次使用或充值' },
  { key: 'restock', label: '服务商补货', hint: '提醒服务商库存不足,该进货了' },
  { key: 'notice', label: '活动通知', hint: '把新活动或权益告诉客户' },
]

// 「用示例填写」:每个营销目的一套现成文案,一键灌入表单(可改)· 无承诺词
const EXAMPLES: Record<string, InputFields> = {
  recharge: {
    title: '新用户专享礼', subtitle: '首充双倍 · 算力加量不加价',
    selling_point: 'AI 搜索推荐看得见,做成才扣,失败自动退', contact: '详询您的专属顾问',
    date: '即日起至 7 月 31 日',
  },
  repurchase: {
    title: '老朋友,回来看看', subtitle: '您的品牌数据又有新变化',
    selling_point: '诊断报告已更新,继续优化被 AI 推荐的机会', contact: '联系您的专属顾问查看详情',
    date: '本周内有效',
  },
  restock: {
    title: '算力库存提醒', subtitle: '库存偏低,补货享配货加赠',
    selling_point: '提前备货不断档,客户交付更从容', contact: '登录经营后台即可进货',
    date: '本月配货政策见后台',
  },
  notice: {
    title: '本月活动上新', subtitle: '充值加赠 · 新功能上线',
    selling_point: '海报物料 / 朋友圈文案一键生成,获客更省事', contact: '打开 OmniRank 查看活动详情',
    date: '活动时间以页面公示为准',
  },
  // —— 替客户的生意做物料(与后端 _AI_FILL_FALLBACK 同源口径)——
  opening: {
    title: '开业大吉 · 恭迎莅临', subtitle: '新店开业 · 好礼相迎',
    selling_point: '开业当周进店有礼,欢迎老朋友带新朋友', contact: '到店详询', date: '开业当周有效',
  },
  promo: {
    title: '本周特惠进行中', subtitle: '限时优惠 · 数量有限',
    selling_point: '热销单品直降,先到先得', contact: '详询店内 / 微信', date: '本周内有效',
  },
  invite: {
    title: '诚邀您的莅临', subtitle: '专场活动 · 座位有限',
    selling_point: '现场交流答疑,更有伴手礼相赠', contact: '回复本条即可报名', date: '活动时间见下方',
  },
  daily: {
    title: '今日分享', subtitle: '一条实用小知识',
    selling_point: '干货分享,持续为您更新行业实用内容', contact: '关注我,每天一条干货', date: '每日更新',
  },
  // —— 推广自己的服务 ——
  acquire: {
    title: '让 AI 搜索推荐你的品牌', subtitle: '客户在问 AI,答案里要有你',
    selling_point: '诊断+优化+监测一站式,做成才扣、失败自动退', contact: '加微信免费聊聊', date: '本月可约',
  },
  callback: {
    title: '好久不见,近况如何?', subtitle: '您的品牌数据有新变化',
    selling_point: '上次的优化还有后续空间,来看看最新进展', contact: '随时找我,一杯茶的时间', date: '本周有空随时约',
  },
}

const KINDS: { key: MaterialKind; label: string; icon: typeof ImageIcon }[] = [
  { key: 'poster', label: '海报图片', icon: ImageIcon },
  { key: 'copy', label: '话术文案', icon: TypeIcon },
]

// 外发渠道:老板拍板(2026-07-05)只留站内信,其余砍掉;站内信纯文字,海报的家=历史记录
// 模板场景码 → 人话(英文 code 界面零裸露;未知场景不显示,原码留 title hover)
const SCENE_LABELS: Record<string, string> = {
  moments: '朋友圈', salon: '线下沙龙', opening: '开业',
  recharge_activity: '充值活动', knowledge_card: '知识卡', invitation: '邀请函',
}

const STEPS = ['选客户', '定目的', '生成成品', '确认外发'] as const

// 表单字段与字数上限
const FIELD_META: { key: keyof InputFields; label: string; max: number; ph: string; area?: boolean }[] = [
  { key: 'title', label: '海报标题', max: 20, ph: '如:首充双倍 · 算力加量不加价' },
  { key: 'subtitle', label: '副标题', max: 30, ph: '一句话点题(可留空)' },
  { key: 'selling_point', label: '卖点', max: 50, ph: '核心利益点,逗号分隔', area: true },
  { key: 'contact', label: '联系方式', max: 40, ph: '如:微信 / 电话 / 二维码说明' },
  { key: 'date', label: '日期', max: 20, ph: '如:即日起至 7 月 31 日' },
]

interface InputFields {
  title: string; subtitle: string; selling_point: string; contact: string; date: string
}
const EMPTY_FIELDS: InputFields = { title: '', subtitle: '', selling_point: '', contact: '', date: '' }

// ============ 防御式解析辅助 ============
async function readJson<T>(res: Response): Promise<T | null> {
  return (await res.json().catch(() => null)) as T | null
}
function normTemplates(raw: unknown): Template[] {
  const list: RawTemplate[] = Array.isArray(raw)
    ? (raw as RawTemplate[])
    : ((raw as { templates?: RawTemplate[]; items?: RawTemplate[] })?.templates
      ?? (raw as { items?: RawTemplate[] })?.items ?? [])
  return list
    .map((t) => ({
      id: Number(t.id ?? t.template_id ?? 0),
      name: String(t.name ?? t.title ?? '未命名模板'),
      scene_type: t.scene_type ?? t.scene,
      material_kind: t.material_kind,
    }))
    .filter((t) => t.id > 0)
}
function priceFor(pricing: unknown, kind: MaterialKind): number | null {
  if (!pricing || typeof pricing !== 'object') return null
  const p = pricing as Record<string, unknown>
  const direct = p[kind]
  if (typeof direct === 'number') return direct
  const nested = (p.pricing ?? p.prices) as Record<string, unknown> | undefined
  if (nested && typeof nested[kind] === 'number') return nested[kind] as number
  const items = (p.items ?? p.list) as unknown
  if (Array.isArray(items)) {
    const hit = items.find(
      (i) => (i as { material_kind?: string; kind?: string })?.material_kind === kind
        || (i as { kind?: string })?.kind === kind,
    ) as { points?: number; cost_points?: number } | undefined
    if (hit) return hit.points ?? hit.cost_points ?? null
  }
  return null
}
function collectAssets(d: GenerateResp | JobResp | null): Asset[] {
  if (!d) return []
  if (Array.isArray(d.assets)) return d.assets.filter((a) => a && a.id != null)
  if (d.asset && d.asset.id != null) return [d.asset]
  return []
}
const DONE_STATES = new Set(['done', 'success', 'succeeded', 'completed', 'finished', 'ready'])
const FAIL_STATES = new Set(['failed', 'error', 'blocked', 'cancelled'])
const DRAFT_KEY = 'mktg_material_wizard_draft_v1'

// ============================================================================
export function MaterialStudio({ mode = 'admin' }: { mode?: 'admin' | 'user' }) {
  const [confirmDialog, askConfirm] = useConfirmDialog()

  // 视图:做素材(向导)/ 历史记录(老板拍板:生成的东西要有个家)
  const [view, setView] = useState<'studio' | 'history'>('studio')

  // 向导状态
  const [step, setStep] = useState(1)
  const [audience, setAudience] = useState<string | null>(null)
  const [purpose, setPurpose] = useState<string>('recharge')
  const [kind, setKind] = useState<MaterialKind>('poster')
  const [templateId, setTemplateId] = useState<number | null>(null)
  const [fields, setFields] = useState<InputFields>(EMPTY_FIELDS)

  // 模板 + 价格
  const [templates, setTemplates] = useState<Template[]>([])
  const [pricing, setPricing] = useState<unknown>(null)
  const [tplLoading, setTplLoading] = useState(true)
  const [tplError, setTplError] = useState<string | null>(null)

  // 生成(genElapsed:加载动画的秒表,老板点名"要有加载动画直到出图")
  const [generating, setGenerating] = useState(false)
  const [assets, setAssets] = useState<Asset[]>([])
  const [genError, setGenError] = useState<string | null>(null)
  const [genElapsed, setGenElapsed] = useState(0)
  useEffect(() => {
    if (!generating) return
    setGenElapsed(0)
    const t = window.setInterval(() => setGenElapsed((s) => s + 1), 1000)
    return () => window.clearInterval(t)
  }, [generating])

  // 历史记录(= 我的物料库,job 分组)
  const [myMaterials, setMyMaterials] = useState<HistoryJob[]>([])
  const [myLoading, setMyLoading] = useState(false)
  const [myError, setMyError] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<number | null>(null)

  // ===== 选品牌 · 一键预填(2026-07-05 前端返工:参考图02 左栏 + 老板点名"一键填写")=====
  const [brands, setBrands] = useState<{ id: number; name: string }[]>([])
  const [brandsLoading, setBrandsLoading] = useState(false)
  const [brandId, setBrandId] = useState<number | null>(null)  // null = 不关联品牌·手动填写

  useEffect(() => {
    // admin 与用户统一走品牌选择器(替谁做物料就选谁),同一数据源 my-clients
    let alive = true
    setBrandsLoading(true)
    ;(async () => {
      try {
        const res = await authFetch('/api/my-clients?page=1&page_size=50')
        const data = await readJson<{ clients?: { id: number; name?: string; brand_name?: string }[]; data?: { id: number; name?: string; brand_name?: string }[] }>(res)
        const list = (data?.clients ?? data?.data ?? []) as { id: number; name?: string; brand_name?: string }[]
        if (alive) setBrands(list.filter((b) => b && b.id).map((b) => ({ id: b.id, name: String(b.name ?? b.brand_name ?? `品牌 #${b.id}`) })))
      } catch { /* 列表拉不到不拦路,可手动填写 */ }
      finally { if (alive) setBrandsLoading(false) }
    })()
    return () => { alive = false }
  }, [mode])

  // 预填:overwrite=false 只补空字段(选品牌时自动);true 全量覆盖(点「一键填写」时)
  const prefillFromBrand = useCallback(async (bid: number, overwrite: boolean) => {
    try {
      const res = await authFetch(`/api/marketing/brand-prefill/${bid}`)
      if (!res.ok) return false
      const data = await readJson<{ ok?: boolean; fields?: Partial<InputFields> }>(res)
      const pf = data?.fields || {}
      setFields((prev) => {
        const next = { ...prev }
        ;(Object.keys(EMPTY_FIELDS) as (keyof InputFields)[]).forEach((k) => {
          const v = (pf[k] ?? '').toString().trim()
          if (!v) return
          if (overwrite || !next[k].trim()) next[k] = v.slice(0, FIELD_META.find((f) => f.key === k)?.max ?? 50)
        })
        return next
      })
      return true
    } catch { return false }
  }, [])

  const pickBrand = useCallback(async (bid: number | null) => {
    setBrandId(bid)
    if (bid == null) return
    const ok = await prefillFromBrand(bid, false)
    if (ok) toast('已用品牌资料预填,可随时修改')
  }, [prefillFromBrand])

  const fillExample = useCallback(() => {
    const ex = EXAMPLES[purpose] ?? EXAMPLES.recharge
    setFields({ ...ex })
    toast('已填入示例文案,按您的业务改一改更好')
  }, [purpose])

  // 「AI 帮我写」:LLM 按品牌资料+目的现写全套文案(后端 fail-soft 回落示例,永不空手)
  const [aiFilling, setAiFilling] = useState(false)
  const aiFill = useCallback(async () => {
    setAiFilling(true)
    try {
      const res = await authFetch('/api/marketing/ai-fill', {
        method: 'POST',
        body: JSON.stringify({ brand_id: brandId, purpose }),
      })
      const data = await readJson<{ ok?: boolean; fields?: Partial<InputFields>; source?: string }>(res)
      if (res.ok && data?.fields) {
        setFields((prev) => ({ ...prev, ...Object.fromEntries(
          Object.entries(data.fields!).filter(([, v]) => String(v ?? '').trim()),
        ) }) as InputFields)
        toast(data.source === 'ai' ? 'AI 已按您的品牌写好,改改就能用' : '已填入推荐文案,可随时修改')
      } else {
        fillExample()  // 网络/权限异常也不空手
      }
    } catch {
      fillExample()
    } finally {
      setAiFilling(false)
    }
  }, [brandId, purpose, fillExample])

  // 进入即就绪:品牌列表加载完自动选第一个并预填(草稿/手动选择优先,不覆盖)
  const autoPicked = useRef(false)
  useEffect(() => {
    if (autoPicked.current || brandsLoading) return
    if (brandId != null || audience != null) { autoPicked.current = true; return }
    if (brands.length === 0) return
    autoPicked.current = true
    setAudience(`brand_${brands[0].id}`)
    void pickBrand(brands[0].id)
  }, [brands, brandsLoading, brandId, audience, pickBrand])

  // [返工 R7] 挂载时恢复本地草稿(saveDraft 的另一半)
  useEffect(() => {
    try {
      const raw = localStorage.getItem(DRAFT_KEY)
      if (!raw) return
      const d = JSON.parse(raw) as Partial<{ step: number; audience: string | null; purpose: string;
        kind: MaterialKind; templateId: number | null; fields: InputFields; brandId: number | null }>
      if (typeof d.step === 'number') setStep(Math.min(4, Math.max(1, d.step)))
      if (d.audience !== undefined) setAudience(d.audience)
      if (typeof d.purpose === 'string') setPurpose(d.purpose)
      if (d.kind === 'copy' || d.kind === 'poster') setKind(d.kind)
      if (d.templateId !== undefined) setTemplateId(d.templateId)
      if (d.fields && typeof d.fields === 'object') setFields({ ...EMPTY_FIELDS, ...d.fields })
      if (d.brandId !== undefined) setBrandId(d.brandId)
    } catch { /* 草稿损坏则忽略,从空白开始 */ }
  }, [])

  const loadTemplates = useCallback(async () => {
    setTplLoading(true)
    setTplError(null)
    try {
      const [tplRes, priceRes] = await Promise.all([
        authFetch('/api/marketing/templates'),
        authFetch('/api/marketing/pricing'),
      ])
      if (!tplRes.ok) throw new Error('模板加载失败')
      const tplRaw = await readJson<unknown>(tplRes)
      setTemplates(normTemplates(tplRaw))
      if (priceRes.ok) setPricing(await readJson<unknown>(priceRes))
    } catch (e) {
      setTplError(e instanceof Error ? e.message : '模板加载失败')
    } finally {
      setTplLoading(false)
    }
  }, [])

  const loadMyMaterials = useCallback(async () => {
    setMyLoading(true)
    setMyError(null)
    try {
      const res = await authFetch('/api/marketing/my-materials')
      if (!res.ok) throw new Error('历史记录加载失败')
      const raw = await readJson<{ materials?: unknown[] }>(res)
      const rows = Array.isArray(raw?.materials) ? raw.materials : []
      // 真实结构 = job 分组 + 嵌套资产(之前按平铺 Asset 解析,全被 filter 掉 → 永远空库)
      const jobs: HistoryJob[] = rows
        .map((r) => {
          const j = r as Partial<HistoryJob> & { assets?: unknown }
          if (j.job_id == null) return null
          const assets = (Array.isArray(j.assets) ? j.assets : [])
            .map((a) => a as HistoryAsset)
            .filter((a) => a && a.id != null)
          return { ...j, job_id: Number(j.job_id), assets } as HistoryJob
        })
        .filter((j): j is HistoryJob => j != null)
      setMyMaterials(jobs)
    } catch (e) {
      setMyError(e instanceof Error ? e.message : '历史记录加载失败')
    } finally {
      setMyLoading(false)
    }
  }, [])

  useEffect(() => { void loadTemplates() }, [loadTemplates])
  useEffect(() => { if (step === 4 || view === 'history') void loadMyMaterials() }, [step, view, loadMyMaterials])

  const cost = useMemo(() => priceFor(pricing, kind), [pricing, kind])

  // 目的清单按"为谁做"联动(选客户=替他生意;选自己=推广自己/平台);选中项失效时归位推荐项
  const purposes = useMemo(
    () => (brandId != null ? PURPOSES_CLIENT : (mode === 'admin' ? PURPOSES_SELF_ADMIN : PURPOSES_SELF_USER)),
    [brandId, mode],
  )
  useEffect(() => {
    if (!purposes.some((p) => p.key === purpose)) setPurpose(purposes[0].key)
  }, [purposes, purpose])
  // 模板自带类型且会覆盖后端 material_kind(「海报呢」根因)—— 切类型后不匹配的已选模板必须清掉
  useEffect(() => {
    if (templateId == null) return
    const t = templates.find((x) => x.id === templateId)
    if (t && t.material_kind && t.material_kind !== kind) setTemplateId(null)
  }, [kind, templates, templateId])
  const filledCount = FIELD_META.filter((f) => fields[f.key].trim().length > 0).length
  const canGenerate = templateId != null && fields.title.trim().length > 0 && !generating

  // 轮询任务(最长约 240s:出图 ~63s/张,套装多张 + 重试;后端 R4 异步化后这里是唯一等待点)
  const pollJob = useCallback(async (jobId: number): Promise<Asset[]> => {
    for (let i = 0; i < 80; i++) {
      await new Promise((r) => setTimeout(r, 3000))
      const res = await authFetch(`/api/marketing/jobs/${jobId}`)
      if (!res.ok) continue
      const job = await readJson<JobResp>(res)
      const st = (job?.status || '').toLowerCase()
      const got = collectAssets(job)
      if (got.length > 0 || DONE_STATES.has(st)) return got
      if (FAIL_STATES.has(st)) throw new Error('生成失败,请稍后重试')
    }
    throw new Error('生成超时,请稍后在物料库查看')
  }, [])

  // 图文一起出(复审小步优化):海报模式默认顺手配一条话术(+话术档算力,勾选可关),
  // 朋友圈最终效果才是完整的"图+文";两个 job 独立计费,各自做成才扣、失败自动退
  const [withCopy, setWithCopy] = useState(true)
  const copyTemplate = useMemo(() => templates.find((t) => t.material_kind === 'copy') ?? null, [templates])
  const pairCopy = kind === 'poster' && withCopy && copyTemplate != null
  const copyCost = useMemo(() => priceFor(pricing, 'copy'), [pricing])
  const totalCost = cost == null ? null : cost + (pairCopy && copyCost != null ? copyCost : 0)
  const genLabel = kind === 'copy' ? '生成话术' : pairCopy ? '生成海报和话术' : '生成海报'

  const runOneJob = useCallback(async (tplId: number, mkind: MaterialKind): Promise<Asset[]> => {
    const res = await authFetch('/api/marketing/generate', {
      method: 'POST',
      body: JSON.stringify({
        template_id: tplId,
        material_kind: mkind,
        input_fields: { ...fields },
        brand_id: brandId,  // 选了品牌 → 后端反哺卖点/联系方式 + 归属校验
        purpose,            // 营销目的直通文案 prompt(P0-B 批)
      }),
    })
    const data = await readJson<GenerateResp>(res)
    // 违规拦截:零扣费
    if (data && data.ok === false && data.block_reason) throw new Error('__BLOCKED__')
    if (!res.ok) throw new Error((data?.block_reason) || '生成失败,请稍后重试')
    let got = collectAssets(data)
    if (got.length === 0 && data?.job_id != null) got = await pollJob(data.job_id)
    return got
  }, [fields, brandId, purpose, pollJob])

  const handleGenerate = useCallback(async () => {
    if (templateId == null) { toast.error('请先在上一步选择模板'); return }
    setGenerating(true)
    setGenError(null)
    setAssets([])
    try {
      const tasks: Promise<Asset[]>[] = [runOneJob(templateId, kind)]
      if (pairCopy && copyTemplate) tasks.push(runOneJob(copyTemplate.id, 'copy'))
      const settled = await Promise.allSettled(tasks)
      const main = settled[0]
      if (main.status === 'rejected') {
        const msg = main.reason instanceof Error ? main.reason.message : '生成失败'
        if (msg === '__BLOCKED__') { toast.error('文案含违规词,请修改后重试'); return }
        throw new Error(msg)
      }
      let got = [...main.value]
      const side = settled[1]
      if (side) {
        if (side.status === 'fulfilled') {
          got = [...got, ...side.value]
        } else {
          // 配套话术没做成不拖累海报(被守卫拦=没扣;做失败=自动退)
          toast('配套话术这次没出来(该部分未扣或已自动退),海报不受影响')
        }
      }
      if (got.length === 0) throw new Error('未返回可用素材,请重试')
      setAssets(got)
      toast.success('已生成,请到右侧预览最终效果')
    } catch (e) {
      const msg = e instanceof Error ? e.message : '生成失败'
      setGenError(msg === '__BLOCKED__' ? '文案含违规词,请修改后重试' : msg)
      toast.error(msg === '__BLOCKED__' ? '文案含违规词,请修改后重试' : msg)
    } finally {
      setGenerating(false)
    }
  }, [templateId, kind, pairCopy, copyTemplate, runOneJob])

  const handleConfirmAsset = useCallback(async (assetId: number) => {
    const ok = await askConfirm({
      title: '确认可外发这条物料?',
      description: '确认后该物料可进入触达流程。最终触达渠道以审批结果为准。',
      confirmLabel: '确认可外发',
    })
    if (!ok) return
    setConfirmingId(assetId)
    try {
      const res = await authFetch(`/api/marketing/materials/${assetId}/confirm`, { method: 'POST' })
      if (!res.ok) throw new Error('确认失败')
      toast.success('已标记为可外发')
      await loadMyMaterials()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '确认失败')
    } finally {
      setConfirmingId(null)
    }
  }, [askConfirm, loadMyMaterials])

  const goNext = () => {
    if (step === 1 && !audience) { toast.error('请选择一个目标客户'); return }
    if (step === 2 && templateId == null) { toast.error('请选择一个模板'); return }
    setStep((s) => Math.min(4, s + 1))
  }
  const goPrev = () => setStep((s) => Math.max(1, s - 1))
  // [返工 R7] 真·本地草稿(原为只弹 toast 的假按钮)
  const saveDraft = () => {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({ step, audience, purpose, kind, templateId, fields, brandId }))
      toast('已保存当前草稿', { description: '下次进入向导自动恢复(仅存本机浏览器)' })
    } catch {
      toast.error('本机存储不可用,草稿未保存')
    }
  }

  // ============================ 渲染 ============================
  return (
    <div className={cn('mx-auto w-full max-w-4xl', mode === 'user' && 'px-1')}>
      {confirmDialog}

      {/* 顶部标题 + 做素材/历史记录 切换 */}
      <FadeIn>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Wand2 className="size-5 text-brand" />
            <div>
              <h2 className="text-base font-semibold text-foreground">生成营销素材向导</h2>
              <p className="text-xs text-muted-foreground">选客户 → 定目的 → AI 生成 → 确认外发,4 步搞定海报与话术</p>
            </div>
          </div>
          {/* 历史记录一等入口(老板拍板:生成的东西要有个家;站内信不带图,海报从这里下载外发)*/}
          <div className="inline-flex rounded-lg border border-border bg-muted p-1">
            {([
              { key: 'studio', label: '做素材', icon: Sparkles },
              { key: 'history', label: '历史记录', icon: History },
            ] as const).map((v) => {
              const active = view === v.key
              const Icon = v.icon
              return (
                <button
                  key={v.key}
                  type="button"
                  onClick={() => setView(v.key)}
                  className={cn(
                    'flex min-h-9 items-center gap-1.5 rounded-md px-3 text-sm transition-colors',
                    active ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  <Icon className="size-4" /> {v.label}
                </button>
              )
            })}
          </div>
        </div>
      </FadeIn>

      {view === 'history' ? (
        <FadeIn>
          <Panel
            title="历史记录"
            action={<Button variant="outline" size="sm" onClick={() => void loadMyMaterials()}><RefreshCw className="size-3.5" /> 刷新</Button>}
          >
            <MaterialsHistory
              jobs={myMaterials} loading={myLoading} error={myError}
              onRetry={loadMyMaterials}
              onConfirm={handleConfirmAsset} confirmingId={confirmingId}
            />
          </Panel>
        </FadeIn>
      ) : (
      <>
      {/* 步骤条:完成度驱动(三栏同屏,不再一次一步)*/}
      <Stepper
        done1={brandId != null || audience === 'manual'}
        done2={templateId != null}
        done3={assets.length > 0}
        onFinal={step === 4}
      />

      {/* 工作区(2026-07-05 老板排版令):顶部横条选品牌;下方三栏 = 目的 | 资料 | 最终成品 */}
      <div className="mt-4">
        {step < 4 ? (
          <>
            <FadeIn>
              <BrandStrip
                brands={brands} loading={brandsLoading}
                brandId={brandId} picked={audience === 'manual' || brandId != null}
                onPick={(bid) => { setAudience(bid == null ? 'manual' : `brand_${bid}`); void pickBrand(bid) }}
              />
            </FadeIn>
            <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1fr_1.25fr]">
              <FadeIn delay={0.03}>
                <StepPurpose
                  purposes={purposes} purpose={purpose} setPurpose={setPurpose}
                  kind={kind} setKind={setKind}
                  templates={templates} templateId={templateId} setTemplateId={setTemplateId}
                  loading={tplLoading} error={tplError} onRetry={loadTemplates}
                  cost={cost}
                />
              </FadeIn>
              <FadeIn delay={0.06}>
                <StepFill
                  fields={fields} setFields={setFields}
                  cost={totalCost} generating={generating}
                  hasResult={assets.length > 0} genLabel={genLabel}
                  canPairCopy={kind === 'poster' && copyTemplate != null}
                  withCopy={withCopy} setWithCopy={setWithCopy} copyCost={copyCost}
                  onGenerate={handleGenerate}
                  canPrefill={brandId != null}
                  onPrefill={() => { if (brandId != null) void prefillFromBrand(brandId, true).then((ok) => ok && toast('已按品牌资料重新填写')) }}
                  aiFilling={aiFilling} onAiFill={() => void aiFill()}
                />
              </FadeIn>
              <FadeIn delay={0.09}>
                <FinalProduct
                  kind={kind}
                  brandName={brandId != null ? brands.find((b) => b.id === brandId)?.name : undefined}
                  generating={generating} genElapsed={genElapsed}
                  assets={assets} genError={genError}
                  onGenerate={handleGenerate}
                />
              </FadeIn>
            </div>
          </>
        ) : (
          <AnimatePresence mode="wait">
            <motion.div key="done" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.2 }}>
              <StepDone
                assets={assets}
                myMaterials={myMaterials} loading={myLoading} error={myError}
                onRetry={loadMyMaterials}
                onConfirm={handleConfirmAsset} confirmingId={confirmingId}
              />
            </motion.div>
          </AnimatePresence>
        )}
      </div>

      {/* 底部操作栏(照图02:左下草稿 · 右下上一步 + 主按钮)*/}
      <div className="sticky bottom-0 z-10 mt-5 flex items-center justify-between gap-2 border-t border-border bg-background/95 py-3 backdrop-blur">
        <Button variant="ghost" size="lg" className="min-h-11" onClick={saveDraft}>
          <Save className="size-4" /> 保存草稿
        </Button>
        <div className="flex items-center gap-2">
          {step === 4 && (
            <Button variant="outline" size="lg" className="min-h-11" onClick={() => setStep(1)}>
              <ChevronLeft className="size-4" /> 回到工作区
            </Button>
          )}
          {step < 4 ? (
            assets.length > 0 ? (
              <Button size="lg" className="min-h-11" onClick={() => setStep(4)}>
                下一步:确认外发 <ChevronRight className="size-4" />
              </Button>
            ) : (
              <Button
                size="lg" className="min-h-11"
                onClick={handleGenerate}
                disabled={generating || templateId == null || fields.title.trim().length === 0}
              >
                {generating ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                {genLabel}
              </Button>
            )
          ) : (
            <Button size="lg" className="min-h-11" onClick={() => { setAssets([]); setStep(1) }}>
              再做一个
            </Button>
          )}
        </div>
      </div>
      </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 顶部横条 · 为谁做物料(老板 2026-07-05 排版令:客户选择不配占一整栏,改横条)
function BrandStrip({ brands, loading, brandId, picked, onPick }: {
  brands: { id: number; name: string }[]
  loading: boolean
  brandId: number | null
  picked: boolean
  onPick: (bid: number | null) => void
}) {
  const chip = (active: boolean) => cn(
    'flex min-h-9 shrink-0 items-center gap-1.5 rounded-full border px-3 text-sm whitespace-nowrap transition-colors',
    active ? 'border-brand/50 bg-brand/5 text-foreground ring-1 ring-brand/30' : 'border-border bg-card text-muted-foreground hover:bg-muted/40 hover:text-foreground',
  )
  return (
    <div className="rounded-xl border border-border bg-card px-3 py-2.5">
      <div className="flex items-center gap-2 overflow-x-auto pb-0.5">
        <span className="shrink-0 text-xs font-medium text-foreground">为谁做物料</span>
        {loading ? (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" /> 加载品牌…
          </span>
        ) : (
          <>
            <button type="button" onClick={() => onPick(null)} className={chip(picked && brandId == null)}>
              {picked && brandId == null && <Check className="size-3.5 text-brand" />}
              自己 · 手动填写
            </button>
            {brands.map((b) => {
              const active = brandId === b.id
              return (
                <button key={b.id} type="button" onClick={() => onPick(b.id)} className={chip(active)}>
                  {active ? <Check className="size-3.5 text-brand" /> : <Users className="size-3.5" />}
                  {b.name}
                </button>
              )
            })}
            {brands.length === 0 && (
              <span className="text-xs text-muted-foreground">还没有客户品牌,先「手动填写」;录入客户资料后这里可一键选。</span>
            )}
          </>
        )}
      </div>
      <p className="mt-1.5 text-[11px] text-muted-foreground">
        选客户品牌 = 替他的生意做物料(标题 / 卖点 / 联系方式自动用他的资料);选「自己」= 推广你自己的服务。
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 完成度驱动(三栏同屏后,进度不再等于"当前页码",而是"做到哪了")
function Stepper({ done1, done2, done3, onFinal }: {
  done1: boolean; done2: boolean; done3: boolean; onFinal: boolean
}) {
  const doneFlags = [done1, done2, done3, false]
  // 当前高亮 = 第一个未完成的步;全完成且在外发页 = 第 4 步
  const current = onFinal ? 4 : (doneFlags.findIndex((d) => !d) + 1 || 4)
  return (
    <div className="flex items-center gap-1 overflow-x-auto">
      {STEPS.map((label, i) => {
        const n = i + 1
        const done = n === 4 ? false : doneFlags[i]
        const active = !done && n === current
        return (
          <div key={label} className="flex min-w-0 flex-1 items-center gap-1">
            <div className={cn(
              'flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs whitespace-nowrap transition-colors',
              active && 'border-brand/40 bg-brand/10 text-foreground',
              done && 'border-transparent bg-muted text-muted-foreground',
              !active && !done && 'border-border text-muted-foreground',
            )}>
              <span className={cn(
                'flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums',
                active ? 'bg-brand text-primary-foreground' : done ? 'bg-muted-foreground/20 text-muted-foreground' : 'bg-muted text-muted-foreground',
              )}>
                {done ? <Check className="size-3" /> : n}
              </span>
              <span className="hidden sm:inline">{label}</span>
            </div>
            {n < STEPS.length && <div className="hidden h-px flex-1 bg-border sm:block" />}
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
function StepPurpose({
  purposes, purpose, setPurpose, kind, setKind, templates, templateId, setTemplateId,
  loading, error, onRetry, cost,
}: {
  purposes: Purpose[]
  purpose: string; setPurpose: (v: string) => void
  kind: MaterialKind; setKind: (v: MaterialKind) => void
  templates: Template[]; templateId: number | null; setTemplateId: (v: number) => void
  loading: boolean; error: string | null; onRetry: () => void
  cost: number | null
}) {
  // 模板自带类型且会静默覆盖素材类型开关 → 只展示与当前类型匹配的模板(「海报呢」根因修复)
  const visibleTemplates = templates.filter((t) => !t.material_kind || t.material_kind === kind)
  return (
    <div className="space-y-4">
      <Panel title="② 选营销目的">
        {/* 单列上下结构:窄栏(三栏布局的中栏)劈两列会把文字挤成竖排 */}
        <div className="grid grid-cols-1 gap-2">
          {purposes.map((p) => {
            const active = purpose === p.key
            return (
              <button
                key={p.key}
                type="button"
                onClick={() => setPurpose(p.key)}
                className={cn(
                  'flex min-h-11 flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors',
                  active ? 'border-brand/50 bg-brand/5 ring-1 ring-brand/30' : 'border-border bg-card hover:bg-muted/40',
                )}
              >
                <span className="flex w-full items-center gap-2">
                  <span className={cn('flex size-4 shrink-0 items-center justify-center rounded-full border',
                    active ? 'border-brand' : 'border-muted-foreground/40')}>
                    {active && <span className="size-2 rounded-full bg-brand" />}
                  </span>
                  <span className="text-sm font-medium text-foreground">{p.label}</span>
                  {p.recommended && <Badge variant="brand" className="px-1.5 py-0 text-[10px]">推荐</Badge>}
                </span>
                <span className="pl-6 text-[11px] leading-relaxed text-muted-foreground">{p.hint}</span>
              </button>
            )
          })}
        </div>
        <div className="mt-3 rounded-lg bg-muted/40 p-3 text-[11px] leading-relaxed text-muted-foreground">
          💡 目的说明:根据您的选择,AI 将生成针对性的海报和话术,提高触达转化。
        </div>
      </Panel>

      <Panel title="选素材类型 · 选模板"
        action={cost != null ? (
          <span className="text-xs text-muted-foreground">
            本次消耗 <span className="font-medium text-foreground"><PowerAndYuan points={cost} /></span>
          </span>
        ) : undefined}
      >
        {/* 素材类型切换 */}
        <div className="mb-4 inline-flex rounded-lg border border-border bg-muted p-1">
          {KINDS.map((k) => {
            const active = kind === k.key
            const Icon = k.icon
            return (
              <button
                key={k.key}
                type="button"
                onClick={() => setKind(k.key)}
                className={cn(
                  'flex min-h-11 items-center gap-1.5 rounded-md px-3 text-sm transition-colors',
                  active ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                <Icon className="size-4" /> {k.label}
              </button>
            )
          })}
        </div>

        {/* 模板选择 */}
        {loading ? (
          <LoadingState />
        ) : error ? (
          <ErrorState text={error} onRetry={onRetry} />
        ) : visibleTemplates.length === 0 ? (
          <EmptyState
            title={kind === 'poster' ? '暂无海报模板' : '暂无话术模板'}
            hint="可以切换上方素材类型看另一类;新模板由运营配置后出现在这里。"
          />
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {visibleTemplates.map((t) => {
              const active = templateId === t.id
              return (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setTemplateId(t.id)}
                  className={cn(
                    'flex min-h-11 items-center justify-between gap-2 rounded-lg border p-3 text-left transition-colors',
                    active ? 'border-brand/50 bg-brand/5 ring-1 ring-brand/30' : 'border-border bg-card hover:bg-muted/40',
                  )}
                >
                  <span className="min-w-0" title={t.scene_type}>
                    <span className="block truncate text-sm font-medium text-foreground">{t.name}</span>
                    {t.scene_type && SCENE_LABELS[t.scene_type] && (
                      <span className="block truncate text-[11px] text-muted-foreground">适合:{SCENE_LABELS[t.scene_type]}</span>
                    )}
                  </span>
                  {active && <Check className="size-4 shrink-0 text-brand" />}
                </button>
              )
            })}
          </div>
        )}
      </Panel>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 中栏 · 填写资料(老板排版令:中间是填写资料)
function StepFill({
  fields, setFields, cost, generating, hasResult, genLabel, onGenerate,
  canPairCopy, withCopy, setWithCopy, copyCost,
  canPrefill, onPrefill, aiFilling, onAiFill,
}: {
  fields: InputFields; setFields: (f: InputFields) => void
  cost: number | null
  generating: boolean; hasResult: boolean; genLabel: string
  onGenerate: () => void
  canPairCopy: boolean; withCopy: boolean; setWithCopy: (v: boolean) => void; copyCost: number | null
  canPrefill?: boolean; onPrefill?: () => void
  aiFilling?: boolean; onAiFill?: () => void
}) {
  const set = (key: keyof InputFields, v: string) => setFields({ ...fields, [key]: v })
  return (
    <div className="space-y-4">
      <Panel title="③ 填写资料">
        {/* AI 原生(老板拍板):要填的地方必有 AI 代填——一个按钮全搞定(品牌资料+LLM 现写+示例兜底)*/}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Button
            size="sm" className="min-h-9"
            disabled={aiFilling}
            onClick={onAiFill}
            title="AI 按所选品牌的卖点/联系方式和营销目的,现写一套海报文案"
          >
            {aiFilling ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
            AI 帮我写
          </Button>
          <Button
            variant="ghost" size="sm" className="min-h-9 text-muted-foreground"
            disabled={!canPrefill}
            onClick={onPrefill}
            title={canPrefill ? '只用品牌资料重新填(不经 AI)' : '先在左侧选一个品牌'}
          >
            <Wand2 className="size-3.5" /> 用品牌资料填
          </Button>
        </div>
        <div className="grid grid-cols-1 gap-3">
          {FIELD_META.map((f) => {
            const val = fields[f.key]
            return (
              <label key={f.key} className="block">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs font-medium text-foreground">{f.label}</span>
                  <span className="text-[11px] tabular-nums text-muted-foreground">{val.length}/{f.max}</span>
                </div>
                {f.area ? (
                  <textarea
                    value={val}
                    maxLength={f.max}
                    placeholder={f.ph}
                    rows={2}
                    onChange={(e) => set(f.key, e.target.value)}
                    className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-brand"
                  />
                ) : (
                  <input
                    value={val}
                    maxLength={f.max}
                    placeholder={f.ph}
                    onChange={(e) => set(f.key, e.target.value)}
                    className="min-h-11 w-full rounded-lg border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-brand"
                  />
                )}
              </label>
            )
          })}
        </div>

        {/* 图文一起出:海报默认配一条话术(明码标价可关;两个 job 各自做成才扣) */}
        {canPairCopy && (
          <label className="mt-3 flex cursor-pointer items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2.5 text-xs text-foreground">
            <input
              type="checkbox"
              checked={withCopy}
              onChange={(e) => setWithCopy(e.target.checked)}
              className="size-4 shrink-0"
            />
            <span>
              同时配一条朋友圈话术{copyCost != null ? <>(+<PowerAndYuan points={copyCost} />)</> : ''},图文一起出,右侧直接看最终效果
            </span>
          </label>
        )}

        <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-xs text-muted-foreground">
            {cost != null ? <>本次共消耗 <span className="font-medium text-foreground"><PowerAndYuan points={cost} /></span> · 做成才扣、失败自动退</> : '标题为必填'}
          </span>
          <Button
            size="lg"
            className="min-h-11"
            onClick={onGenerate}
            disabled={generating || fields.title.trim().length === 0}
          >
            {generating ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
            {hasResult ? '重新生成' : genLabel}
          </Button>
        </div>
      </Panel>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 右栏 · 最终成品(老板排版令:右边就是最终成品的地方——渲染成发出去的样子)
function FinalProduct({ kind, brandName, generating, genElapsed, assets, genError, onGenerate }: {
  kind: MaterialKind; brandName?: string
  generating: boolean; genElapsed: number
  assets: Asset[]; genError: string | null
  onGenerate: () => void
}) {
  const copyAsset = assets.find((a) => (a.material_kind ?? '') === 'copy' || (!a.url_stored && !a.thumbnail_url && !!a.content_text))
  const posters = assets.filter((a) => a !== copyAsset && (a.url_stored || a.thumbnail_url))
  return (
    <Panel title="④ 最终成品"
      action={!generating && assets.length > 0 ? (
        <Button variant="outline" size="sm" onClick={onGenerate}><RefreshCw className="size-3.5" /> 重新生成</Button>
      ) : undefined}
    >
      {generating ? (
        <GeneratingSkeleton kind={kind} elapsed={genElapsed} />
      ) : genError ? (
        <ErrorState text={genError} onRetry={onGenerate} />
      ) : assets.length > 0 ? (
        <>
          <MomentsMock copyText={copyAsset?.content_text} posters={posters} brandName={brandName} />

          {/* 成品动作:复制文案 / 下载海报 */}
          <div className="mt-3 flex flex-wrap justify-center gap-2">
            {copyAsset?.content_text && (
              <Button variant="outline" size="sm" className="min-h-9" onClick={() => void copyText(copyAsset.content_text!)}>
                <CopyIcon className="size-3.5" /> 复制文案
              </Button>
            )}
            {posters.map((p, i) => (
              <Button key={p.id} asChild variant="outline" size="sm" className="min-h-9">
                <a href={p.url_stored || p.thumbnail_url} download target="_blank" rel="noreferrer">
                  <Download className="size-3.5" /> 下载海报{posters.length > 1 ? ` ${i + 1}` : ''}
                </a>
              </Button>
            ))}
          </div>

          {/* 外发渠道:只有站内信(老板拍板);站内信纯文字,海报的家在「历史记录」 */}
          <div className="mt-4 border-t border-border pt-3">
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              <span className="font-medium text-foreground">外发渠道:站内信。</span>
              站内信只发文字和链接,海报请「下载」后发朋友圈 / 微信;所有成品都存在右上角「历史记录」。最终触达以审批结果为准。
            </p>
          </div>
        </>
      ) : (
        <div className="flex min-h-[380px] flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border bg-muted/20 p-6 text-center">
          <ImageIcon className="size-9 text-muted-foreground opacity-50" />
          <div className="text-sm font-medium text-foreground">成品会出现在这里</div>
          <p className="max-w-[260px] text-xs leading-relaxed text-muted-foreground">
            左边定目的、中间填资料(或点「AI 帮我写」),点「生成」后,这里直接渲染发出去的最终效果。
          </p>
        </div>
      )}
    </Panel>
  )
}

// 朋友圈样式渲染(老板:"渲染好的比如朋友圈,生成好以后的最终效果才对")
function MomentsMock({ copyText: text, posters, brandName }: {
  copyText?: string; posters: Asset[]; brandName?: string
}) {
  const name = brandName || '我的品牌'
  return (
    <div className="mx-auto w-full max-w-[340px] rounded-xl border border-border bg-card p-3 shadow-sm">
      <div className="flex items-center gap-2">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-brand/15 text-sm font-semibold text-brand">
          {name.slice(0, 1)}
        </span>
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-foreground">{name}</div>
          <div className="text-[11px] text-muted-foreground">刚刚 · 朋友圈效果预览</div>
        </div>
      </div>
      {text && <p className="mt-2 text-sm leading-relaxed whitespace-pre-wrap text-foreground">{text}</p>}
      {posters.length > 0 && (
        <div className={cn('mt-2 grid gap-1.5', posters.length === 1 ? 'grid-cols-1' : 'grid-cols-2')}>
          {posters.map((p) => (
            <div key={p.id} className="overflow-hidden rounded-lg">
              <PosterImage src={p.url_stored || p.thumbnail_url} alt="海报成品" />
            </div>
          ))}
        </div>
      )}
      {!text && posters.length === 0 && (
        <p className="mt-2 text-xs text-muted-foreground">(本次生成暂无可预览内容,请到「历史记录」查看)</p>
      )}
    </div>
  )
}

// 生成中的骨架动画:海报=竖版占位块脉动 + 秒表;话术=行骨架
function GeneratingSkeleton({ kind, elapsed }: { kind: MaterialKind; elapsed: number }) {
  return (
    <div className="flex flex-col items-center gap-4 py-4 text-center">
      {kind === 'poster' ? (
        <div className="relative aspect-[3/4] w-full max-w-[240px] overflow-hidden rounded-xl border border-border bg-muted">
          <div className="absolute inset-0 animate-pulse bg-gradient-to-b from-muted via-muted/60 to-muted" />
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
            <Loader2 className="size-7 animate-spin text-brand" />
            <span className="text-xs font-medium text-foreground">AI 正在画海报…</span>
          </div>
        </div>
      ) : (
        <div className="w-full max-w-sm space-y-2 rounded-xl border border-border bg-muted/30 p-4">
          {['w-full', 'w-11/12', 'w-3/4', 'w-3/5'].map((w) => (
            <div key={w} className={cn('h-3 animate-pulse rounded bg-muted', w)} />
          ))}
          <div className="flex items-center justify-center gap-2 pt-2">
            <Loader2 className="size-4 animate-spin text-brand" />
            <span className="text-xs font-medium text-foreground">AI 正在写话术…</span>
          </div>
        </div>
      )}
      <div className="space-y-1">
        <div className="text-xs tabular-nums text-muted-foreground">
          已等待 {Math.floor(elapsed / 60) > 0 ? `${Math.floor(elapsed / 60)} 分 ` : ''}{elapsed % 60} 秒
          {kind === 'poster' ? ' · 出图一般 1–2 分钟' : ' · 一般几十秒内完成'}
        </div>
        <div className="max-w-sm text-[11px] text-muted-foreground">
          可以先离开本页,结果会保存在「历史记录」;做成才扣算力、失败自动退。
        </div>
      </div>
    </div>
  )
}

// 复制话术到剪贴板(历史/预览共用)
async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast('话术已复制,直接粘贴就能用')
  } catch {
    toast.error('复制失败,请长按/选中文本手动复制')
  }
}

// 带加载动画的海报图:onLoad 前一直脉动骨架(老板点名"直到加载出来")
function PosterImage({ src, alt }: { src?: string; alt: string }) {
  const [loaded, setLoaded] = useState(false)
  const [broken, setBroken] = useState(false)
  if (!src || broken) {
    return (
      <div className="flex aspect-[3/4] w-full flex-col items-center justify-center gap-2 bg-muted/40 text-muted-foreground">
        <ImageIcon className="size-8 opacity-50" />
        <span className="px-3 text-center text-[11px]">{broken ? '图片暂时打不开,稍后在「历史记录」重试' : '图片保存中,稍后到「历史记录」查看'}</span>
      </div>
    )
  }
  return (
    <div className="relative aspect-[3/4] w-full overflow-hidden bg-muted/40">
      {!loaded && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="absolute inset-0 animate-pulse bg-muted" />
          <Loader2 className="relative size-6 animate-spin text-brand" />
        </div>
      )}
      <img
        src={src}
        alt={alt}
        onLoad={() => setLoaded(true)}
        onError={() => setBroken(true)}
        className={cn('absolute inset-0 h-full w-full object-cover transition-opacity duration-500', loaded ? 'opacity-100' : 'opacity-0')}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
function StepDone({
  assets, myMaterials, loading, error, onRetry, onConfirm, confirmingId,
}: {
  assets: Asset[]
  myMaterials: HistoryJob[]; loading: boolean; error: string | null; onRetry: () => void
  onConfirm: (id: number) => void; confirmingId: number | null
}) {
  return (
    <div className="space-y-4">
      <Panel>
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <span className="flex size-11 items-center justify-center rounded-full bg-brand/10">
            <Check className="size-6 text-brand" />
          </span>
          <div className="text-sm font-medium text-foreground">素材已生成完成</div>
          <div className="max-w-md text-xs text-muted-foreground">
            {assets.length > 0
              ? `本次生成 ${assets.length} 条素材,已存入「历史记录」。海报可下载外发;确认可外发后即可进入站内信触达流程。`
              : '素材已存入「历史记录」,可在下方下载或确认可外发。'}
          </div>
        </div>
      </Panel>

      <Panel title="历史记录"
        action={<Button variant="outline" size="sm" onClick={onRetry}><RefreshCw className="size-3.5" /> 刷新</Button>}
      >
        <MaterialsHistory
          jobs={myMaterials} loading={loading} error={error}
          onRetry={onRetry} onConfirm={onConfirm} confirmingId={confirmingId}
        />
      </Panel>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 历史记录:job 卡片流(含生成中/失败·已退,异步生成才有交代)· 「做素材」页与第 4 步共用
const JOB_STATUS_META: Record<string, { label: string; cls: string; pulse?: boolean }> = {
  created: { label: '排队中', cls: 'bg-muted text-muted-foreground', pulse: true },
  generating: { label: '生成中', cls: 'bg-brand/10 text-foreground', pulse: true },
  succeeded: { label: '已完成', cls: 'bg-brand/10 text-foreground' },
  failed: { label: '失败 · 算力已退', cls: 'bg-destructive/10 text-destructive' },
  blocked: { label: '未过合规检查 · 零扣费', cls: 'bg-amber-500/10 text-amber-600 dark:text-amber-400' },
}
const KIND_LABELS: Record<string, string> = { poster: '海报', copy: '话术', bundle: '套装' }

function formatJobTime(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function MaterialsHistory({ jobs, loading, error, onRetry, onConfirm, confirmingId }: {
  jobs: HistoryJob[]; loading: boolean; error: string | null; onRetry: () => void
  onConfirm: (id: number) => void; confirmingId: number | null
}) {
  if (loading) return <LoadingState text="加载历史记录…" />
  if (error) return <ErrorState text={error} onRetry={onRetry} />
  if (jobs.length === 0) {
    return <EmptyState title="还没有生成记录" hint="在「做素材」生成第一条营销素材,海报和话术都会保存在这里,随时下载复用。" />
  }
  return (
    <div className="space-y-3">
      {jobs.map((j) => {
        const status = (j.status ?? '').toLowerCase()
        const partialFree = status === 'succeeded' && (j.error_summary ?? '').includes('partial_free')
        const meta = JOB_STATUS_META[status] ?? { label: status || '未知状态', cls: 'bg-muted text-muted-foreground' }
        return (
          <div key={j.job_id} className="rounded-xl border border-border bg-card p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-foreground">
                {KIND_LABELS[j.material_kind ?? ''] ?? '素材'}
              </span>
              <span className={cn('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]', meta.cls)}>
                {meta.pulse && <Loader2 className="size-3 animate-spin" />}
                {partialFree ? '部分成功 · 整单免单' : meta.label}
              </span>
              <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{formatJobTime(j.created_at)}</span>
            </div>
            {j.assets.length > 0 ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {j.assets.map((a) => (
                  <HistoryAssetCard key={a.id} asset={a} onConfirm={onConfirm} confirmingId={confirmingId} />
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                {meta.pulse ? '正在生成,点右上角「刷新」看最新进度;做成才扣算力。' :
                  status === 'failed' ? '这次没做成,算力已自动退回,可回「做素材」再试一次。' :
                  status === 'blocked' ? '文案含违规用语被拦下(零扣费),修改文案后可重新生成。' : '暂无成品。'}
              </p>
            )}
          </div>
        )
      })}
    </div>
  )
}

function HistoryAssetCard({ asset, onConfirm, confirmingId }: {
  asset: HistoryAsset; onConfirm: (id: number) => void; confirmingId: number | null
}) {
  const isCopy = (asset.kind ?? '') === 'copy' || (!asset.url && !asset.thumb && !!asset.text)
  const confirmed = Number(asset.rights_confirmed ?? 0) === 1
  const src = asset.url || asset.thumb
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-card">
      {isCopy ? (
        <p className="line-clamp-6 min-h-20 p-3 text-xs whitespace-pre-wrap text-foreground">{asset.text || '(空)'}</p>
      ) : (
        <PosterImage src={src} alt="海报物料" />
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border p-2">
        <div className="flex items-center gap-1">
          {isCopy ? (
            asset.text && (
              <Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={() => void copyText(asset.text!)}>
                <CopyIcon className="size-3.5" /> 复制
              </Button>
            )
          ) : (
            src && (
              <Button asChild variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs">
                <a href={src} download target="_blank" rel="noreferrer"><Download className="size-3.5" /> 下载</a>
              </Button>
            )
          )}
        </div>
        {confirmed ? (
          <Badge variant="brand" className="text-[10px]">已可外发</Badge>
        ) : (
          <Button
            variant="outline" size="sm" className="h-8 text-xs"
            onClick={() => onConfirm(asset.id)}
            disabled={confirmingId === asset.id}
          >
            {confirmingId === asset.id ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
            确认可外发
          </Button>
        )}
      </div>
    </div>
  )
}
