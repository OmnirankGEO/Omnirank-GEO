// 营销军师 · 设置(人话版)
// 开关按职责分三张卡:军师大脑 / 执行动作 / 实验功能;每个开关都写清"开着/关着分别意味着什么"。
// 全部走 fetchPolicies() + fetchSignalRules() 真实数据;翻转均经 useConfirmDialog 明示后果。
// 界面不出现任何英文 key;排查所需的原始 key 一律放在行容器 title 属性里(悬停可见)。
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertOctagon, Save, ShieldAlert, SlidersHorizontal, ToggleLeft, ToggleRight, Waypoints } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import type { OpportunityGroup, Policy } from '../types'
import { FLAG_LABEL, GROUP_LABEL } from '../types'
import { fetchPolicies, fetchSignalRules, patchPolicy, setKillSwitch } from '../api'
import { ErrorState, FadeIn, LoadingState, Panel, PowerAndYuan } from './ui'

const KILL_KEY = 'marketing_agent.kill_switch'

// 每个开关的人话呈现:中文名 + "开/关分别意味着什么"(文案已拍板 · 逐字使用)
const FLAG_DISPLAY: Record<string, { name: string; sub: string }> = {
  'marketing_agent.enabled': {
    name: '军师值班(总闸)',
    sub: '开:每天自动巡逻经营数据,产出营销建议;关:军师完全休眠,什么都不做。',
  },
  'marketing_agent.execute.enabled': {
    name: '允许执行',
    sub: '开:批准后的方案才可能被真正执行;关:只出建议,一律不动手。',
  },
  'marketing_agent.notification.enabled': {
    name: '触达发送',
    sub: '开:批准后的站内信、企业微信真实发出;关:只做彩排记账,不打扰任何用户。',
  },
  'marketing_agent.grant.enabled': {
    name: '赠送算力入账',
    sub: '开:首充双倍等活动真实到账;关:只记彩排账,用户钱包纹丝不动。',
  },
  'marketing_agent.control_group.enabled': {
    name: '效果对照实验',
    sub: '开:留出部分用户作对照组以验证效果;样本量足够前建议保持关闭。',
  },
  'marketing_agent.advisor_llm.enabled': {
    name: 'AI 润色建议文案',
    sub: '开:军师用大模型把建议写得更有说服力;关:使用固定模板生成。',
  },
}

// 分组卡:开关按职责归位,一眼看懂哪些只"想"、哪些会"动手"
const FLAG_GROUPS: { title: string; hint: string; keys: string[] }[] = [
  {
    title: '军师大脑',
    hint: '只产建议 · 不动手',
    keys: ['marketing_agent.enabled', 'marketing_agent.advisor_llm.enabled'],
  },
  {
    title: '执行动作',
    hint: '每一项都会真实影响用户',
    keys: ['marketing_agent.execute.enabled', 'marketing_agent.notification.enabled', 'marketing_agent.grant.enabled'],
  },
  {
    title: '实验功能',
    hint: '度量净效果用 · 按需开启',
    keys: ['marketing_agent.control_group.enabled'],
  },
]

// 每个闸翻开时的后果说明(确认弹窗里给人看清楚要发生什么)
const FLAG_CONSEQUENCE: Record<string, string> = {
  'marketing_agent.enabled':
    '开启后:军师开始定期巡逻经营数据并自动撰写营销建议进入待审队列。仅"建议",不会自动执行任何触达或发放。',
  'marketing_agent.execute.enabled':
    '开启后:已通过人工审批的方案才可能进入自动执行通道。关闭时所有方案只停留在建议态,永不落地。',
  'marketing_agent.grant.enabled':
    '开启后:经审批的方案可向用户账户真实发放算力(受下方赠送上限硬约束)。这是花钱动作,请确认算力上限与风控无误。',
  'marketing_agent.notification.enabled':
    '开启后:经审批的方案可向用户实际发送站内信 / 企业微信(受频控与免打扰时段约束)。用户会真实收到。',
  'marketing_agent.control_group.enabled':
    '开启后:部分受众会被留作对照组、不接受触达,用于度量"相关转化"的净效果。',
  'marketing_agent.advisor_llm.enabled':
    '开启后:由大模型起草 / 润色营销建议文案(只进待审队列,绝不自动执行)。关闭则回退为固定模板。',
}

// 数值配置项的人话标签 + 单位;赠送类为算力,额外显示约合人民币。
const CONFIG_META: Record<string, { label: string; unit: string; power?: boolean }> = {
  'touch.daily_global_cap': { label: '每日触达上限', unit: '人次' },
  'night_dnd_start': { label: '免打扰开始时间', unit: '点' },
  'night_dnd_end': { label: '免打扰结束时间', unit: '点' },
  'freq_days': { label: '同一用户触达间隔天数', unit: '天' },
  'grant.cap_per_user': { label: '单人赠送上限', unit: '算力', power: true },
  'grant.cap_per_batch': { label: '单批赠送上限', unit: '算力', power: true },
  'grant.cap_per_day': { label: '单日赠送上限', unit: '算力', power: true },
  'grant.hard_ceiling': { label: '单笔发放硬顶', unit: '算力', power: true },
  'material.daily_limit': { label: '物料日生成上限', unit: '次' },
}

// 信号规则的人话名 + 一句"什么情况触发、军师会做什么"(界面不露英文规则代号)
const RULE_META: Record<string, { name: string; desc: string }> = {
  pending_recover: { name: '充值挂单挽回', desc: '发现超 24 小时未支付的充值订单,提醒用户完成支付' },
  register_no_diagnosis: { name: '新用户激活引导', desc: '注册 7 天还没做首次诊断的用户,引导做免费诊断' },
  trial_exhausted_active: { name: '体验用尽推首充', desc: '体验算力用完的人群,推荐首充双倍' },
  written_not_published: { name: '写完未发布提醒', desc: '文章写好但一直没发布的用户,讲清发布的价值' },
  first_compliant: { name: '客户达标报喜', desc: '客户关键词首次达标,恭喜并建议沉淀成案例卡' },
  provider_restock: { name: '服务商补货提醒', desc: '服务商算力库存低于近期消耗时,提醒补货' },
  conversion_low: { name: '充值转化诊断', desc: '注册转首充比例偏低时,给运营出一份漏斗诊断' },
  no_recharge_streak: { name: '连续无充值提案', desc: '连续多日没有付费充值时,起草限时加赠活动草案' },
  high_value_silent: { name: '高价值用户唤回', desc: '高消费用户沉默 30 天,建议做唤回触达' },
  feature_underused: { name: '冷门功能推广', desc: '使用率最低的功能,建议做卖点触达' },
}

// data_status 徽章:诚实披露每条规则数据是否已接线。
const DATA_STATUS_META: Record<string, { label: string; cls: string }> = {
  wired: { label: '已接真实数据', cls: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30' },
  wired_aggregate: { label: '按人群聚合接入', cls: 'bg-sky-500/10 text-sky-600 border-sky-500/30' },
  needs_data: { label: '待接数据 · 暂不生效', cls: 'bg-muted text-muted-foreground border-border' },
}

interface SignalRule { rule_key: string; group: string; data_status: string }

// 找到指定 key 的策略(兼容后端可能带前缀:精确匹配优先,否则按后缀命中)
function findPolicy(policies: Policy[], key: string): Policy | undefined {
  return policies.find((p) => p.key === key)
    ?? policies.find((p) => p.key.endsWith(`.${key}`) || p.key.endsWith(key))
}

function metaForConfigKey(key: string): { label: string; unit: string; power?: boolean } {
  if (CONFIG_META[key]) return CONFIG_META[key]
  const hit = Object.keys(CONFIG_META).find((k) => key === k || key.endsWith(`.${k}`) || key.endsWith(k))
  // 未知配置项不露英文 key:显示占位中文名,原始 key 在行 title 属性里(悬停可见)
  return hit ? CONFIG_META[hit] : { label: '未命名配置项(悬停查看)', unit: '' }
}

// 未知分组兜底中文(原值在行 title 悬停可查),不露英文
function groupLabel(group: string): string {
  return GROUP_LABEL[group as OpportunityGroup] ?? '未分组'
}

export function Settings() {
  const [policies, setPolicies] = useState<Policy[]>([])
  const [flagKeys, setFlagKeys] = useState<string[]>([])
  const [configKeys, setConfigKeys] = useState<string[]>([])
  const [rules, setRules] = useState<SignalRule[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [confirmDialog, askConfirm] = useConfirmDialog()

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [pol, sig] = await Promise.all([fetchPolicies(), fetchSignalRules()])
      setPolicies(pol.policies ?? [])
      setFlagKeys(pol.flag_keys ?? [])
      setConfigKeys(pol.config_keys ?? [])
      setRules(sig.rules ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // 三组开关(排除急停 —— 急停单独一块);以后端 flag_keys 为准做交集(后端未回则全显)
  const visibleGroups = useMemo(() => {
    return FLAG_GROUPS
      .map((g) => ({
        ...g,
        keys: g.keys.filter((k) => flagKeys.length === 0 || flagKeys.includes(k)),
      }))
      .filter((g) => g.keys.length > 0)
  }, [flagKeys])

  const killPolicy = findPolicy(policies, KILL_KEY)
  const killOn = Boolean(killPolicy?.value_jsonb?.enabled)

  // 数值配置项(以后端 config_keys 为准;回退到 CONFIG_META 已知集合)
  const configList = useMemo(() => {
    const source = configKeys.length ? configKeys : Object.keys(CONFIG_META)
    return source
      .map((k) => ({ key: k, policy: findPolicy(policies, k) }))
      .filter((x): x is { key: string; policy: Policy } => Boolean(x.policy))
  }, [configKeys, policies])

  const toggleFlag = useCallback(async (key: string, current: boolean) => {
    const label = FLAG_DISPLAY[key]?.name ?? FLAG_LABEL[key] ?? '此开关'
    const consequence = FLAG_CONSEQUENCE[key] ?? '翻转此开关将改变营销军师的自主行为范围。'
    const turningOn = !current
    const ok = await askConfirm({
      title: `${turningOn ? '开启' : '关闭'} · ${label}`,
      description: turningOn
        ? consequence
        : `将关闭「${label}」。已进行的建议/审批不受影响,但该能力后续不再自动生效。`,
      confirmLabel: turningOn ? '确认开启' : '确认关闭',
      danger: turningOn && /grant|execute|notification/.test(key),
    })
    if (!ok) return
    setBusyKey(key)
    try {
      await patchPolicy(key, { enabled: turningOn })
      toast.success(`${label} 已${turningOn ? '开启' : '关闭'}`)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '更新失败')
    } finally {
      setBusyKey(null)
    }
  }, [askConfirm, load])

  const toggleKill = useCallback(async () => {
    const turningOn = !killOn
    const ok = await askConfirm({
      title: turningOn ? '启用急停?' : '解除急停?',
      description: turningOn
        ? '急停将瞬时冻结营销军师的一切自主执行:所有触达发送、算力发放、活动上线立即停止,已排队动作不再落地。此为最高优先级开关,凌驾于其它所有开关之上。'
        : '解除急停后,各开关恢复到各自的开/关状态,自主执行按原策略继续。请确认已排查完风险再解除。',
      confirmLabel: turningOn ? '立即急停' : '确认解除',
      danger: true,
    })
    if (!ok) return
    setBusyKey(KILL_KEY)
    try {
      await setKillSwitch(turningOn)
      toast.success(turningOn ? '已急停:一切自主执行已冻结' : '已解除急停')
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败')
    } finally {
      setBusyKey(null)
    }
  }, [askConfirm, killOn, load])

  const saveConfig = useCallback(async (key: string, n: number) => {
    const meta = metaForConfigKey(key)
    setBusyKey(key)
    try {
      await patchPolicy(key, { value: n })
      toast.success(`${meta.label} 已保存为 ${n}${meta.unit}`)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusyKey(null)
    }
  }, [load])

  if (loading) return <LoadingState text="加载设置…" />
  if (error) return <ErrorState text={error} onRetry={load} />

  return (
    <div className="space-y-5">
      {/* 1) 开关分组卡:军师大脑 / 执行动作 / 实验功能 */}
      {visibleGroups.length === 0 ? (
        <Panel title="军师开关">
          <div className="py-6 text-center text-xs text-muted-foreground">未读取到可配置的开关。</div>
        </Panel>
      ) : (
        visibleGroups.map((g) => (
          <Panel
            key={g.title}
            title={g.title}
            action={<span className="text-[11px] text-muted-foreground">{g.hint}</span>}
          >
            <ul className="divide-y divide-border">
              {g.keys.map((key, i) => {
                const d = FLAG_DISPLAY[key]
                if (!d) return null
                const p = findPolicy(policies, key)
                const on = Boolean(p?.value_jsonb?.enabled)
                const busy = busyKey === key
                return (
                  <FadeIn key={key} delay={i * 0.02}>
                    {/* title 放原始 key,仅供排查悬停查看,界面不露英文 */}
                    <li title={key} className="flex items-center justify-between gap-3 py-3">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-foreground">{d.name}</div>
                        <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{d.sub}</div>
                      </div>
                      <Button
                        variant={on ? 'default' : 'outline'}
                        size="sm"
                        disabled={busy}
                        onClick={() => toggleFlag(key, on)}
                        className="shrink-0"
                      >
                        {on ? <ToggleRight className="size-3.5" /> : <ToggleLeft className="size-3.5" />}
                        {on ? '已开启' : '已关闭'}
                      </Button>
                    </li>
                  </FadeIn>
                )
              })}
            </ul>
          </Panel>
        ))
      )}

      {/* 2) 急停 */}
      <Panel title="急停开关">
        <div
          title={KILL_KEY}
          className={cn(
            'flex items-center justify-between gap-3 rounded-lg border p-3',
            killOn ? 'border-rose-500/40 bg-rose-500/10' : 'border-border bg-muted/40',
          )}
        >
          <div className="flex min-w-0 items-start gap-2.5">
            <AlertOctagon className={cn('mt-0.5 size-5 shrink-0', killOn ? 'text-rose-600' : 'text-muted-foreground')} />
            <div className="min-w-0">
              <div className="text-sm font-semibold text-foreground">最高优先级 · 一键冻结一切自主执行</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                凌驾于上方所有开关之上。触达、发放、活动上线全部立即停止。
              </div>
            </div>
          </div>
          <Button
            variant={killOn ? 'default' : 'destructive'}
            size="sm"
            disabled={busyKey === KILL_KEY}
            onClick={toggleKill}
            className="shrink-0"
          >
            <ShieldAlert className="size-3.5" />
            {killOn ? '解除急停' : '立即急停'}
          </Button>
        </div>
        <AnimatePresence>
          {killOn && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-600">
                急停生效中 —— 营销军师的所有自主执行已冻结。排查完成后点「解除急停」恢复。
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </Panel>

      {/* 3) 数值配置 */}
      <Panel
        title="安全上限与频控"
        action={<span className="flex items-center gap-1 text-[11px] text-muted-foreground"><SlidersHorizontal className="size-3.5" /> 触达频控 · 免打扰 · 赠送上限</span>}
      >
        {configList.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted-foreground">未读取到数值配置项。</div>
        ) : (
          <ul className="divide-y divide-border">
            {configList.map(({ key, policy }, i) => (
              <ConfigRow
                key={key}
                policyKey={key}
                policy={policy}
                busy={busyKey === key}
                delay={i * 0.02}
                onSave={saveConfig}
              />
            ))}
          </ul>
        )}
      </Panel>

      {/* 4) 巡逻信号规则 · 数据接线状态 */}
      <Panel
        title="巡逻信号规则"
        action={<span className="flex items-center gap-1 text-[11px] text-muted-foreground"><Waypoints className="size-3.5" /> 军师靠这些信号发现机会 · {rules.length} 条</span>}
      >
        {rules.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted-foreground">
            暂无信号规则。规则由后端规则库定义,接线后将在此如实展示每条的数据状态。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">规则</th>
                  <th className="py-2 pr-3 font-medium">机会分组</th>
                  <th className="py-2 font-medium">数据状态</th>
                </tr>
              </thead>
              <tbody>
                {rules.map((r) => {
                  const rm = RULE_META[r.rule_key] ?? { name: '其他规则(悬停查看)', desc: '' }
                  const meta = DATA_STATUS_META[r.data_status]
                    ?? { label: '状态待确认', cls: 'bg-muted text-muted-foreground border-border' }
                  return (
                    <tr key={r.rule_key} title={r.rule_key} className="border-b border-border/60 last:border-0">
                      <td className="py-2.5 pr-3">
                        <div className="text-[13px] font-medium text-foreground">{rm.name}</div>
                        {rm.desc && <div className="mt-0.5 text-[11px] text-muted-foreground">{rm.desc}</div>}
                      </td>
                      <td className="py-2.5 pr-3 text-xs text-muted-foreground">{groupLabel(r.group)}</td>
                      <td className="py-2.5">
                        <span
                          title={r.data_status}
                          className={cn('inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px]', meta.cls)}
                        >
                          {meta.label}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {confirmDialog}
    </div>
  )
}

// 单条数值配置行:只读展示 + 可选就地编辑(小号数字输入 + 保存)
function ConfigRow({ policyKey, policy, busy, delay, onSave }: {
  policyKey: string
  policy: Policy
  busy: boolean
  delay: number
  onSave: (key: string, n: number) => void
}) {
  const meta = metaForConfigKey(policyKey)
  const current = typeof policy.value_jsonb?.value === 'number' ? policy.value_jsonb.value : 0
  const [draft, setDraft] = useState<string>(String(current))

  useEffect(() => { setDraft(String(current)) }, [current])

  const parsed = Number(draft)
  const changed = draft.trim() !== '' && Number.isFinite(parsed) && parsed !== current

  return (
    <FadeIn delay={delay}>
      {/* title 放原始 key,仅供排查悬停查看,界面不露英文 */}
      <li title={policyKey} className="flex items-center justify-between gap-3 py-3">
        <div className="min-w-0">
          <div className="text-sm font-medium text-foreground">{meta.label}</div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            当前:{meta.power
              ? <PowerAndYuan points={current} />
              : <span className="tabular-nums">{current.toLocaleString()} {meta.unit}</span>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <input
            type="number"
            inputMode="numeric"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            className="h-7 w-24 rounded-md border border-border bg-background px-2 text-right text-sm tabular-nums text-foreground outline-none focus-visible:ring-2 focus-visible:ring-brand"
          />
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !changed}
            onClick={() => onSave(policyKey, parsed)}
          >
            <Save className="size-3.5" /> 保存
          </Button>
        </div>
      </li>
    </FadeIn>
  )
}
