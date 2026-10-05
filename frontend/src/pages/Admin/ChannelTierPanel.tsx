/**
 * 渠道等级后台
 *
 * Admin-only control panel for channel tier configuration, agent overrides,
 * founder seats, service package defaults, and tier history.
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { adminApi, type ChannelTierConfig } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import * as channelTierTerminology from '@/lib/channelTierTerminology';
import {
  AlertTriangle,
  Calculator,
  History,
  RefreshCw,
  Save,
  ShieldCheck,
  Trophy,
} from 'lucide-react';
import { toast } from 'sonner';

type TierKey = 'certified' | 'preferred' | 'strategic';
type FoundingKey = keyof ChannelTierConfig['founding'];
type MarginKey = keyof ChannelTierConfig['margin_label_thresholds'];

const TIER_LABELS: Record<TierKey | 'none', string> = {
  none: channelTierTerminology.CHANNEL_TIER_LABELS.none,
  certified: channelTierTerminology.CHANNEL_TIER_LABELS.certified,
  preferred: channelTierTerminology.CHANNEL_TIER_LABELS.preferred,
  strategic: channelTierTerminology.CHANNEL_TIER_LABELS.strategic,
};

const TIER_ORDER: TierKey[] = ['certified', 'preferred', 'strategic'];
const MARGIN_LABELS: Record<string, string> = {
  loss_heavy_bps: '严重亏损线（%）',
  loss_light_bps: '轻度亏损线（%）',
  healthy_bps: '健康利润线（%）',
  profit_excellent_bps: '优秀利润线（%）',
  hard_block_bps: '异常利润拦截线（%）',
};

function numberValue(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function datetimeLocalValue(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function ChannelTierPanel({ embedded = false }: { embedded?: boolean }) {
  const [config, setConfig] = useState<ChannelTierConfig | null>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  const [founderSeats, setFounderSeats] = useState<{ used: number; cap: number; remaining: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [catalogVersion, setCatalogVersion] = useState('');
  const [conflicted, setConflicted] = useState(false);
  const [overrideMessage, setOverrideMessage] = useState<string | null>(null);
  const [overrideTier, setOverrideTier] = useState<Record<number, TierKey>>({});
  const [overrideUntil, setOverrideUntil] = useState<Record<number, string>>({});
  const [overrideNote, setOverrideNote] = useState<Record<number, string>>({});

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      if (embedded) {
        const configRes = await adminApi.channelTierConfigGet();
        setConfig(configRes.config);
        setCatalogVersion(configRes.catalog_version);
        setConflicted(false);
        setOverrideMessage(configRes.environment_override?.active
          ? configRes.environment_override.message || '环境配置覆盖中'
          : null);
        return;
      }
      const [configRes, agentsRes, seatsRes, historyRes] = await Promise.all([
        adminApi.channelTierConfigGet(),
        adminApi.channelTierAgents({ limit: 100, offset: 0 }),
        adminApi.channelTierFounderSeats(),
        adminApi.channelTierHistory(80),
      ]);
      setConfig(configRes.config);
      setCatalogVersion(configRes.catalog_version);
      setConflicted(false);
      setOverrideMessage(configRes.environment_override?.active
        ? configRes.environment_override.message || '环境配置覆盖中'
        : null);
      setAgents(agentsRes.items || []);
      setFounderSeats({ used: seatsRes.used, cap: seatsRes.cap, remaining: seatsRes.remaining });
      setHistory(historyRes.items || []);
    } catch (e) {
      toast.error(formatApiErrorForDisplay(e, '加载渠道等级后台失败', 'admin'));
    } finally {
      setLoading(false);
    }
  }, [embedded]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const updateTier = (tier: TierKey, field: 'min_yuan' | 'bonus_rate' | 'is_enabled' | 'description', value: string | boolean) => {
    setConfig((prev) => prev ? ({
        ...prev,
        agent_tier_config: {
          ...prev.agent_tier_config,
          [tier]: {
            ...prev.agent_tier_config[tier],
            [field]: field === 'bonus_rate'
              ? Number(value) / 100
              : (field === 'min_yuan' ? Number(value) : value),
          },
        },
      }) : prev);
  };

  const updateFounding = (field: FoundingKey, value: string) => {
    setConfig((prev) => prev ? ({
        ...prev,
        founding: {
          ...prev.founding,
          [field]: field.includes('bonus') ? Number(value) / 100 : Number(value),
        },
      }) : prev);
  };

  const updateMargin = (field: MarginKey, value: string) => {
    setConfig((prev) => prev ? ({
        ...prev,
        margin_label_thresholds: {
          ...prev.margin_label_thresholds,
          [field]: Math.round(Number(value) * 100),
        },
      }) : prev);
  };

  const saveConfig = async () => {
    if (!config || !catalogVersion) return;
    setSaving(true);
    try {
      const res = await adminApi.channelTierConfigPut(config, catalogVersion);
      setConfig(res.config);
      setCatalogVersion(res.catalog_version);
      setConflicted(false);
      toast.success('渠道等级配置已保存');
      await loadAll();
    } catch (e: any) {
      if (e?.response?.status === 409) {
        setConflicted(true);
        toast.error('配置已被其他管理员更新，本地修改已保留，请刷新后重新确认');
      } else {
        toast.error(formatApiErrorForDisplay(e, '保存失败 · 请检查门槛顺序和奖励比例', 'admin'));
      }
    } finally {
      setSaving(false);
    }
  };

  const setOverride = async (agent: any) => {
    const tier = overrideTier[agent.agent_user_id] || 'certified';
    const untilRaw = overrideUntil[agent.agent_user_id];
    try {
      await adminApi.channelTierSetOverride(agent.agent_user_id, {
        tier,
        until: untilRaw ? new Date(untilRaw).toISOString() : undefined,
        note: overrideNote[agent.agent_user_id] || '管理员设置专项保底权益',
      });
      toast.success(`已为 ${agent.display_name || agent.username} 设置${TIER_LABELS[tier]}专项保底权益`);
      await loadAll();
    } catch (e) {
      toast.error(formatApiErrorForDisplay(e, '专项保底权益设置失败', 'admin'));
    }
  };

  const clearOverride = async (agent: any) => {
    try {
      await adminApi.channelTierSetOverride(agent.agent_user_id, {
        clear: true,
        note: '管理员清空专项保底权益，继续按自然进货等级执行',
      });
      toast.success('已清空专项保底权益，继续按自然进货等级执行');
      await loadAll();
    } catch (e) {
      toast.error(formatApiErrorForDisplay(e, '清空专项保底权益失败', 'admin'));
    }
  };

  const evaluateAgent = async (agent: any) => {
    try {
      await adminApi.channelTierEvaluate(agent.agent_user_id);
      toast.success('已重新计算该服务商等级');
      await loadAll();
    } catch (e) {
      toast.error(formatApiErrorForDisplay(e, '重算失败', 'admin'));
    }
  };

  if (!config) {
    return (
      <div className="min-h-screen bg-slate-950 p-8 text-slate-100">
        <div className="mx-auto max-w-7xl rounded-2xl border border-slate-800 bg-slate-900/80 p-8">
          <div className="flex items-center gap-3 text-slate-300">
            <RefreshCw className="h-5 w-5 animate-spin" />
            正在加载渠道等级后台...
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`${embedded ? 'rounded-xl' : 'min-h-screen p-6'} bg-slate-950 text-slate-100`}>
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-sm text-emerald-200">
              <ShieldCheck className="h-4 w-4" />
              管理员专用 · 渠道奖励计划默认关闭
            </div>
            <h1 className="text-3xl font-semibold tracking-tight">{embedded ? '渠道奖励' : '渠道等级后台'}</h1>
            <p className="mt-2 max-w-3xl text-sm text-slate-400">
              {embedded
                ? '仅管理认证、优选、战略三档的启停、门槛、进货奖励和适用说明。保存只影响新订单。'
                : '管理服务商等级门槛、进货赠送、创始席位、默认算力包和专项保底权益。此页不改历史订单，也不会虚增滚动进货额。'}
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            {embedded && (
              <Button asChild variant="ghost" className="text-slate-300 hover:bg-slate-800 hover:text-slate-100">
                <Link to="/admin/channel-tier">高级渠道管理</Link>
              </Button>
            )}
            <Button variant="outline" onClick={loadAll} disabled={loading} className="border-slate-700 bg-slate-900 text-slate-100 hover:bg-slate-800">
              <RefreshCw className="mr-2 h-4 w-4" />
              刷新
            </Button>
            <Button onClick={saveConfig} disabled={saving || conflicted || !catalogVersion || !!overrideMessage} className="bg-emerald-500 text-slate-950 hover:bg-emerald-400">
              <Save className="mr-2 h-4 w-4" />
              保存配置
            </Button>
          </div>
        </header>

        {overrideMessage && (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-200">
            <AlertTriangle className="mr-2 inline h-4 w-4" />{overrideMessage}。当前只能查看，无法保存。
          </div>
        )}
        {conflicted && (
          <div className="rounded-xl border border-orange-500/40 bg-orange-500/10 p-4 text-sm text-orange-200">
            <AlertTriangle className="mr-2 inline h-4 w-4" />配置已被其他管理员更新。本地修改尚未丢失；请点击刷新读取最新版本后重新编辑。
          </div>
        )}

        {!embedded && <section className="grid gap-4 lg:grid-cols-4">
          <MetricCard title="创始席位" value={`${founderSeats?.used ?? 0}/${founderSeats?.cap ?? 0}`} sub={`剩余 ${founderSeats?.remaining ?? 0} 个`} icon={<Trophy className="h-5 w-5" />} />
          <MetricCard title="奖励有效期" value={`${config.bonus_validity_months} 个月`} sub="奖励到期后不再可用" icon={<History className="h-5 w-5" />} />
          <MetricCard title="默认成本系数" value={String(config.k_default)} sub="用于普通客户算力包成本兜底" icon={<Calculator className="h-5 w-5" />} />
          <MetricCard title="在册服务商" value={String(agents.length)} sub="可管理渠道等级" icon={<ShieldCheck className="h-5 w-5" />} />
        </section>}

        <section className={`grid gap-6 ${embedded ? '' : 'xl:grid-cols-[1.1fr_0.9fr]'}`}>
          <Panel title="等级门槛与进货奖励">
            <div className="grid gap-3">
              {TIER_ORDER.map((tier) => (
                <div key={tier} className="grid gap-3 rounded-xl border border-slate-800 bg-slate-950/60 p-4 md:grid-cols-[140px_110px_150px_150px_1fr] md:items-end">
                  <div>
                    <div className="font-medium text-slate-100">{TIER_LABELS[tier]}</div>
                    <div className="text-sm text-slate-500">滚动 12 个月进货额达到门槛后进入该等级</div>
                  </div>
                  <Field label="是否启用">
                    <div className="flex h-10 items-center"><Switch checked={config.agent_tier_config[tier].is_enabled !== false} onCheckedChange={(checked) => updateTier(tier, 'is_enabled', checked)} /></div>
                  </Field>
                  <Field label="门槛(元)">
                    <Input
                      type="number"
                      value={numberValue(config.agent_tier_config[tier].min_yuan)}
                      onChange={(e) => updateTier(tier, 'min_yuan', e.target.value)}
                      className="border-slate-700 bg-slate-900 text-slate-100"
                    />
                  </Field>
                  <Field label="赠送比例（%）">
                    <Input
                      type="number"
                      value={Math.round(numberValue(config.agent_tier_config[tier].bonus_rate) * 100)}
                      onChange={(e) => updateTier(tier, 'bonus_rate', e.target.value)}
                      className="border-slate-700 bg-slate-900 text-slate-100"
                    />
                  </Field>
                  <Field label="适用说明">
                    <Textarea
                      value={String(config.agent_tier_config[tier].description || '')}
                      onChange={(e) => updateTier(tier, 'description', e.target.value)}
                      className="min-h-10 border-slate-700 bg-slate-900 text-slate-100"
                    />
                  </Field>
                </div>
              ))}
            </div>
          </Panel>

          {!embedded && <Panel title="创始席位与奖励有效期">
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="创始席位上限">
                <Input type="number" value={numberValue(config.founding.cap)} onChange={(e) => updateFounding('cap', e.target.value)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
              <Field label="首单额外奖励(%)">
                <Input type="number" value={Math.round(numberValue(config.founding.first_order_extra_bonus) * 100)} onChange={(e) => updateFounding('first_order_extra_bonus', e.target.value)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
              <Field label="首单门槛(元)">
                <Input type="number" value={numberValue(config.founding.min_first_order_yuan)} onChange={(e) => updateFounding('min_first_order_yuan', e.target.value)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
              <Field label="奖励有效期(月)">
                <Input type="number" value={numberValue(config.bonus_validity_months)} onChange={(e) => setConfig((prev) => prev ? ({ ...prev, bonus_validity_months: Number(e.target.value) }) : prev)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
              <Field label="算力包默认成本系数">
                <Input type="number" value={numberValue(config.k_default)} onChange={(e) => setConfig((prev) => prev ? ({ ...prev, k_default: Number(e.target.value) }) : prev)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
            </div>
            <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
              <AlertTriangle className="mr-2 inline h-4 w-4" />
              专项保底权益不虚增滚动进货额；自然评级持续计算，达到更高等级时自动按更高赠送比例执行。
            </div>
          </Panel>}
        </section>

        {!embedded && <>
        <Panel title="毛利标签阈值">
          <div className="grid gap-4 md:grid-cols-5">
            {Object.entries(config.margin_label_thresholds || {}).map(([key, value]) => (
              <Field key={key} label={MARGIN_LABELS[key] || '利润判断线（%）'}>
                <Input type="number" value={numberValue(value) / 100} onChange={(e) => updateMargin(key as MarginKey, e.target.value)} className="border-slate-700 bg-slate-900 text-slate-100" />
              </Field>
            ))}
          </div>
        </Panel>

        <section className="grid gap-6 xl:grid-cols-[1.3fr_0.7fr]">
          <Panel title="服务商等级与专项保底权益">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[980px] text-left text-sm">
                <thead className="text-slate-400">
                  <tr className="border-b border-slate-800">
                    <th className="py-2 pr-3">服务商</th>
                    <th className="py-2 pr-3">当前等级</th>
                    <th className="py-2 pr-3">近 12 个月净实付</th>
                    <th className="py-2 pr-3">专项保底权益</th>
                    <th className="py-2 pr-3">保底到期</th>
                    <th className="py-2 pr-3">备注</th>
                    <th className="py-2 pr-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {agents.map((agent) => (
                    <tr key={agent.agent_user_id} className="border-b border-slate-900 align-top">
                      <td className="py-3 pr-3">
                        <div className="font-medium text-slate-100">{agent.display_name || agent.username}</div>
                        <div className="text-xs text-slate-500">服务商编号 {agent.agent_user_id}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <Badge className="bg-slate-800 text-slate-100 hover:bg-slate-800">
                          {TIER_LABELS[(agent.channel_tier || 'none') as TierKey | 'none'] || agent.channel_tier}
                        </Badge>
                        {agent.tier_override && (
                          <div className="mt-2 text-xs text-amber-300">人工: {TIER_LABELS[agent.tier_override as TierKey]}</div>
                        )}
                      </td>
                      <td className="py-3 pr-3 text-slate-300">¥{Number(agent.rolling_12m_yuan || 0).toLocaleString()}</td>
                      <td className="py-3 pr-3">
                        <Select
                          value={overrideTier[agent.agent_user_id] || agent.tier_override || 'certified'}
                          onValueChange={(value) => setOverrideTier((prev) => ({ ...prev, [agent.agent_user_id]: value as TierKey }))}
                        >
                          <SelectTrigger className="w-[150px] border-slate-700 bg-slate-900 text-slate-100">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent className="border-slate-700 bg-slate-900 text-slate-100">
                            {TIER_ORDER.map((tier) => (
                              <SelectItem key={tier} value={tier}>{TIER_LABELS[tier]}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </td>
                      <td className="py-3 pr-3">
                        <Input
                          type="datetime-local"
                          value={overrideUntil[agent.agent_user_id] ?? datetimeLocalValue(agent.tier_override_until)}
                          onChange={(e) => setOverrideUntil((prev) => ({ ...prev, [agent.agent_user_id]: e.target.value }))}
                          className="w-[190px] border-slate-700 bg-slate-900 text-slate-100"
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <Textarea
                          value={overrideNote[agent.agent_user_id] ?? ''}
                          onChange={(e) => setOverrideNote((prev) => ({ ...prev, [agent.agent_user_id]: e.target.value }))}
                          placeholder={agent.tier_override_note || '操作原因'}
                          className="min-h-[38px] w-[220px] border-slate-700 bg-slate-900 text-slate-100"
                        />
                      </td>
                      <td className="space-y-2 py-3 pr-3">
                        <Button size="sm" onClick={() => setOverride(agent)} className="w-full bg-emerald-500 text-slate-950 hover:bg-emerald-400">
                          设置
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => evaluateAgent(agent)} className="w-full border-slate-700 bg-slate-900 text-slate-100 hover:bg-slate-800">
                          重算
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => clearOverride(agent)} className="w-full text-slate-300 hover:bg-slate-800">
                          清空保底
                        </Button>
                      </td>
                    </tr>
                  ))}
                  {agents.length === 0 && (
                    <tr>
                      <td colSpan={7} className="py-10 text-center text-slate-500">暂无服务商等级记录</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel title="等级变更历史">
            <div className="max-h-[680px] space-y-3 overflow-y-auto pr-2">
              {history.map((row) => (
                <div key={row.id || `${row.agent_user_id}-${row.created_at}`} className="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-medium text-slate-100">服务商 #{row.agent_user_id}</div>
                    <Badge variant="outline" className="border-slate-700 text-slate-300">{historySourceLabel(row.trigger_source)}</Badge>
                  </div>
                  <div className="mt-2 text-sm text-slate-400">
                    {TIER_LABELS[(row.from_tier || 'none') as TierKey | 'none'] || row.from_tier}
                    <span className="px-2 text-slate-600">→</span>
                    {TIER_LABELS[(row.to_tier || 'none') as TierKey | 'none'] || row.to_tier}
                  </div>
                  <div className="mt-2 text-xs text-slate-500">{row.created_at}</div>
                  {row.note && <div className="mt-2 text-xs text-slate-400">{row.note}</div>}
                </div>
              ))}
              {history.length === 0 && <div className="py-12 text-center text-slate-500">暂无等级变更历史</div>}
            </div>
          </Panel>
        </section>
        </>}
      </div>
    </div>
  );
}

function MetricCard({ title, value, sub, icon }: { title: string; value: string; sub: string; icon: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5">
      <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-300">
        {icon}
      </div>
      <div className="text-sm text-slate-400">{title}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-50">{value}</div>
      <div className="mt-2 text-xs text-slate-500">{sub}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/80 p-5 shadow-xl shadow-black/10">
      <h2 className="mb-4 text-lg font-semibold text-slate-100">{title}</h2>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <Label className="text-xs text-slate-400">{label}</Label>
      {children}
    </div>
  );
}

function historySourceLabel(source: string): string {
  return ({
    admin_override: '管理员调整',
    admin_clear: '恢复自动评级',
    purchase: '进货后自动更新',
    scheduled: '定期自动更新',
  } as Record<string, string>)[source] || '系统更新';
}
