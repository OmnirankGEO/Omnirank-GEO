import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, Plus, RefreshCw, Save, Search, Send } from 'lucide-react';
import { toast } from 'sonner';
import { adminApi, type InventoryPurchaseCatalogItem, type PricingPublicationStatus } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  describeCatalogChanges,
  sortCatalogDrafts,
  toCatalogDraft,
  validateCatalogDrafts,
  yuanInputToCents,
  MAX_AGENT_PURCHASE_AMOUNT_YUAN,
  type InventoryPurchaseCatalogDraft,
} from '@/lib/inventoryPurchaseCatalog';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

const NEW_PREVIEW = {
  base_points: 0,
  bonus_points: 0,
  total_points: 0,
  discount_source: '',
  tier_at_order: '',
  bonus_rate_bps: 0,
  quote_fingerprint: '',
};

function isConflict(error: unknown): boolean {
  return (error as { response?: { status?: number } })?.response?.status === 409;
}

export function InventoryPurchaseCatalogPanel() {
  const [catalogVersion, setCatalogVersion] = useState('');
  const [before, setBefore] = useState<InventoryPurchaseCatalogItem[]>([]);
  const [draft, setDraft] = useState<InventoryPurchaseCatalogDraft[]>([]);
  const [overrideMessage, setOverrideMessage] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState('只影响保存后的新订单，历史及待支付订单不变');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  const [previewAgentId, setPreviewAgentId] = useState('');
  const [agentPreview, setAgentPreview] = useState<InventoryPurchaseCatalogItem[] | null>(null);
  const [publication, setPublication] = useState<PricingPublicationStatus | null>(null);
  const [publicationReason, setPublicationReason] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [confirmDialog, askConfirm] = useConfirmDialog();

  const loadProcurementPublication = useCallback(async () => {
    const response = await adminApi.pricingPublicationDryRun([]);
    setPublication(response.data);
    return response.data;
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await adminApi.inventoryPurchaseCatalogGet();
      const options = response.options || [];
      setCatalogVersion(response.catalog_version);
      setBefore(options);
      setDraft(sortCatalogDrafts(options.map(toCatalogDraft)));
      setAgentPreview(null);
      setOverrideMessage(response.environment_override.active
        ? response.environment_override.message || '环境配置覆盖中'
        : null);
      setSaveNotice(response.save_notice || '只影响保存后的新订单，历史及待支付订单不变');
      setConflicted(false);
      await loadProcurementPublication().catch(() => {
        setPublication(null);
        toast.warning('价目生效状态暂时无法读取，已保留当前编辑内容');
      });
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '加载进货价目表失败', 'admin'));
    } finally {
      setLoading(false);
    }
  }, [loadProcurementPublication]);

  useEffect(() => { void load(); }, [load]);

  const updateRow = (index: number, patch: Partial<InventoryPurchaseCatalogDraft>) => {
    setAgentPreview(null);
    setDraft((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, ...patch } : row));
  };

  const addRow = () => {
    setAgentPreview(null);
    const nextSort = draft.length === 0 ? 0 : Math.max(...draft.map((row) => row.sort_order)) + 1;
    const canEnable = draft.filter((row) => row.is_enabled).length < 3;
    setDraft((current) => [...current, {
      draft_id: safeRandomUUID(),
      option_id: null,
      amount_cents: 0,
      amount_yuan: '',
      is_enabled: canEnable,
      sort_order: nextSort,
      reward_description: '保存后按适用渠道奖励自动计算',
      preview: NEW_PREVIEW,
    }]);
  };

  const moveRow = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= draft.length) return;
    setAgentPreview(null);
    setDraft((current) => {
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next.map((row, rowIndex) => ({ ...row, sort_order: rowIndex }));
    });
  };

  const save = async () => {
    const validation = validateCatalogDrafts(draft);
    if (validation) return toast.error(validation);
    const changes = describeCatalogChanges(before, draft);
    if (changes.length === 0) return toast.info('没有需要保存的修改');
    const confirmed = await askConfirm({
      title: '确认保存进货价目表？',
      description: `${changes.join('；')}。${saveNotice}`,
      confirmLabel: '确认保存',
    });
    if (!confirmed) return;

    setSaving(true);
    try {
      const response = await adminApi.inventoryPurchaseCatalogPut({
        expected_catalog_version: catalogVersion,
        options: sortCatalogDrafts(draft).map((row) => ({
          option_id: row.option_id,
          amount_cents: yuanInputToCents(row.amount_yuan)!,
          is_enabled: row.is_enabled,
          sort_order: row.sort_order,
        })),
      });
      setCatalogVersion(response.catalog_version);
      setBefore(response.options);
      setDraft(sortCatalogDrafts(response.options.map(toCatalogDraft)));
      setAgentPreview(null);
      setConflicted(false);
      await loadProcurementPublication().catch(() => {
        setPublication(null);
        toast.warning('设置已保存，但暂时无法读取生效状态，请稍后刷新');
      });
      toast.success('设置已保存 · 还需点击“让新价目生效”才会更新服务商进货页');
    } catch (error) {
      if (isConflict(error)) {
        setConflicted(true);
        toast.error('配置已被其他管理员更新，本地草稿已保留');
      } else {
        toast.error(formatApiErrorForDisplay(error, '保存进货价目表失败', 'admin'));
      }
    } finally {
      setSaving(false);
    }
  };

  const publishProcurement = async () => {
    const current = publication ?? await loadProcurementPublication();
    const cleanReason = publicationReason.trim();
    if (!current.publishable) {
      toast.error(current.blockers[0] || '进货价目表暂不能生效');
      return;
    }
    if (current.needs_publication_count === 0) {
      toast.info('服务商进货页已经是最新价目');
      return;
    }
    if (!cleanReason) {
      toast.error('请填写本次调整原因');
      return;
    }
    if (!(await askConfirm({
      title: '让新的进货价目立即生效？',
      description: '只更新服务商之后看到的新进货报价；已有报价、待支付订单和历史订单均保持原价。',
      confirmLabel: '确认生效',
    }))) return;

    setPublishing(true);
    try {
      await adminApi.pricingPublicationPublish({
        reason: cleanReason,
        expected_epoch: current.review_contract.config_epoch,
        reviewed_target_mode: current.review_contract.target_mode,
        reviewed_service_user_ids: current.review_contract.service_user_ids,
        reviewed_scopes: current.review_contract.scopes,
      });
      setPublicationReason('');
      await loadProcurementPublication();
      toast.success('新的进货价目已生效 · 服务商刷新后即可看到');
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '价目生效失败 · 请重新检查', 'admin'));
      await loadProcurementPublication().catch(() => undefined);
    } finally {
      setPublishing(false);
    }
  };

  const loadAgentPreview = async () => {
    const id = Number(previewAgentId.trim());
    if (!Number.isSafeInteger(id) || id <= 0) return toast.error('请输入有效的服务商编号');
    try {
      const response = await adminApi.inventoryPurchaseCatalogGet(id);
      setAgentPreview(response.options || []);
      toast.success('已按该服务商的专属规则生成预览');
    } catch (error) {
      toast.error(formatApiErrorForDisplay(error, '服务商预览加载失败', 'admin'));
    }
  };

  return (
    <Card>
      {confirmDialog}
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="text-base">进货价目表</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">设置服务商进货页显示的固定金额。先保存修改，再点击“让新价目生效”；已有报价和订单不会改变。</p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
            </Button>
            <Button size="sm" onClick={() => void save()} disabled={saving || loading || conflicted || !!overrideMessage}>
              <Save className="mr-1 h-4 w-4" />保存价目表
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {overrideMessage && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
            <AlertTriangle className="mr-2 inline h-4 w-4" />环境配置覆盖中：{overrideMessage}。当前只能查看，无法保存。
          </div>
        )}
        {conflicted && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-orange-500/40 bg-orange-500/10 p-3 text-sm">
            <span>配置已被其他管理员更新。本地草稿仍保留，请对照后再决定是否刷新。</span>
            <Button variant="outline" size="sm" onClick={() => void load()}>刷新最新配置</Button>
          </div>
        )}
        {publication && (
          <div className={`rounded-md border p-3 ${
            publication.publishable && publication.needs_publication_count === 0
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-amber-500/40 bg-amber-500/10'
          }`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-2">
                {publication.publishable && publication.needs_publication_count === 0
                  ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                  : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />}
                <div>
                  <div className="text-sm font-medium">
                    {publication.publishable && publication.needs_publication_count === 0
                      ? '服务商进货页已是最新价目'
                      : '设置已保存，但服务商进货页尚未更新'}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    进货价目可以单独生效，不会被其他服务商的客户售价问题影响。
                  </p>
                  {publication.blockers.length > 0 && (
                    <p className="mt-1 text-xs text-destructive">{publication.blockers[0]}</p>
                  )}
                </div>
              </div>
              {publication.needs_publication_count > 0 && (
                <div className="flex w-full flex-col gap-2 sm:w-auto sm:min-w-[360px] sm:flex-row">
                  <Input
                    value={publicationReason}
                    onChange={(event) => setPublicationReason(event.target.value)}
                    placeholder="调整原因，例如：更新 7 月进货档位"
                    maxLength={500}
                    aria-label="进货价目生效原因"
                  />
                  <Button
                    onClick={() => void publishProcurement()}
                    disabled={publishing || !publication.publishable || !publicationReason.trim()}
                    className="shrink-0"
                  >
                    <Send className="mr-1 h-4 w-4" />{publishing ? '生效中…' : '让新价目生效'}
                  </Button>
                </div>
              )}
            </div>
          </div>
        )}
        <div className="rounded-md border bg-muted/20 p-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-1">
              <label htmlFor="catalog-preview-agent" className="text-sm font-medium">按服务商预览到账</label>
              <Input id="catalog-preview-agent" inputMode="numeric" value={previewAgentId} onChange={(event) => setPreviewAgentId(event.target.value)} placeholder="输入服务商编号" />
            </div>
            <Button variant="outline" onClick={() => void loadAgentPreview()}><Search className="mr-1 h-4 w-4" />生成预览</Button>
          </div>
          {agentPreview && (
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              {agentPreview.filter((row) => row.is_enabled).map((row) => (
                <div key={row.option_id} className="rounded-md border bg-background p-3 text-sm">
                  <div className="font-medium">¥{row.amount_cents / 100} 进货</div>
                  <div className="mt-1 text-muted-foreground">预计到账 {row.preview.total_points.toLocaleString()} 算力</div>
                  <div className="text-xs text-muted-foreground">{row.reward_description}</div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-left">
                <th className="p-3">进货金额（元）</th>
                <th className="p-3">是否启用</th>
                <th className="p-3">排序</th>
                <th className="p-3">适用奖励说明</th>
                <th className="p-3 text-right">调整</th>
              </tr>
            </thead>
            <tbody>
              {draft.map((row, index) => (
                <tr key={row.draft_id} className="border-b">
                  <td className="p-3"><Input inputMode="decimal" max={MAX_AGENT_PURCHASE_AMOUNT_YUAN} value={row.amount_yuan} onChange={(event) => updateRow(index, { amount_yuan: event.target.value })} aria-label={`第 ${index + 1} 档进货金额`} /></td>
                  <td className="p-3"><Switch checked={row.is_enabled} onCheckedChange={(checked) => updateRow(index, { is_enabled: checked })} aria-label={`第 ${index + 1} 档是否启用`} /></td>
                  <td className="p-3"><Input type="number" min={0} step={1} value={row.sort_order} onChange={(event) => updateRow(index, { sort_order: Number(event.target.value) })} aria-label={`第 ${index + 1} 档排序`} /></td>
                  <td className="p-3 text-muted-foreground">{row.reward_description || '按适用渠道奖励自动计算'}</td>
                  <td className="p-3">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" onClick={() => moveRow(index, -1)} disabled={index === 0} aria-label="上移档位"><ArrowUp className="h-4 w-4" /></Button>
                      <Button variant="ghost" size="icon" onClick={() => moveRow(index, 1)} disabled={index === draft.length - 1} aria-label="下移档位"><ArrowDown className="h-4 w-4" /></Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Button variant="outline" onClick={addRow}><Plus className="mr-1 h-4 w-4" />新增档位</Button>
          <p className="text-xs text-muted-foreground">最多启用 3 个固定档位；服务商仍可自由金额进货。{saveNotice}</p>
        </div>
      </CardContent>
    </Card>
  );
}
