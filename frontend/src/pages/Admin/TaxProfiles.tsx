/**
 * V3.5 W2 · Admin 代理税务配置
 *
 * 路由: /admin/tax-profiles
 * 责任: 编辑代理 tax profile(entity_type / default_tax_rate_bps / default_tax_mode / 发票能力)
 * 保存后影响后续订单税率 · 不回改历史 ledger
 */
import { useState } from 'react';
import { adminApi } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';
import { Receipt, Search, Save } from 'lucide-react';
import { toast } from 'sonner';

interface TaxProfile {
  agent_user_id: number;
  entity_type?: string | null;
  default_tax_rate_bps?: number | null;
  default_tax_mode?: string | null;
  tax_id?: string | null;
  invoice_capability?: string | null;
  notes?: string | null;
  updated_at?: string | null;
}

const ENTITY_TYPES = [
  { value: 'individual', label: '个人' },
  { value: 'individual_business', label: '个体工商户' },
  { value: 'company', label: '企业' },
  { value: 'partnership', label: '合伙主体' },
];
const TAX_MODES = [
  { value: 'withheld', label: '平台预扣' },
  { value: 'invoice_provided', label: '已提供发票' },
  { value: 'exempt_manual', label: '人工豁免' },
];
const INVOICE_CAPS = [
  { value: 'none', label: '不开票' },
  { value: 'general_invoice', label: '普通发票' },
  { value: 'special_invoice', label: '专用发票' },
];

export default function TaxProfiles() {
  const [searchId, setSearchId] = useState('');
  const [profile, setProfile] = useState<TaxProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const id = parseInt(searchId);
    if (!id) return toast.error('服务方编号必填');
    setLoading(true);
    try {
      const r = await adminApi.taxProfileGet(id);
      setProfile(r as TaxProfile);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    if (!profile) return;
    setBusy(true);
    try {
      await adminApi.taxProfilePut(profile.agent_user_id, {
        entity_type: profile.entity_type,
        default_tax_rate_bps: profile.default_tax_rate_bps,
        default_tax_mode: profile.default_tax_mode,
        tax_id: profile.tax_id,
        invoice_capability: profile.invoice_capability,
        notes: profile.notes,
      });
      toast.success('保存成功 · 仅影响后续订单 · 历史结算记录不回改');
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-3xl">
      <div className="flex items-center gap-2">
        <Receipt className="w-6 h-6" />
        <h1 className="text-2xl font-bold">服务方税务档案</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">查询服务方税务档案</CardTitle>
        </CardHeader>
        <CardContent className="flex gap-2">
          <Input value={searchId} onChange={(e) => setSearchId(e.target.value)} placeholder="服务方编号" />
          <Button onClick={load} disabled={loading}>
            <Search className="w-4 h-4 mr-1" /> 查询
          </Button>
        </CardContent>
      </Card>

      {profile && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              服务方 #{profile.agent_user_id} · 税务档案
            </CardTitle>
            {profile.updated_at && (
              <p className="text-xs text-muted-foreground">上次更新 {new Date(profile.updated_at).toLocaleString()}</p>
            )}
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <Label>主体类型</Label>
              <Select value={profile.entity_type || ''} onValueChange={(v) => setProfile({ ...profile, entity_type: v })}>
                <SelectTrigger><SelectValue placeholder="未设置" /></SelectTrigger>
                <SelectContent>
                  {ENTITY_TYPES.map((t) => <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>预扣税率(万分位 · 600 = 6%)</Label>
              <Input
                type="number"
                value={profile.default_tax_rate_bps ?? ''}
                onChange={(e) => setProfile({ ...profile, default_tax_rate_bps: e.target.value ? parseInt(e.target.value) : null })}
                placeholder="留空 = 用平台默认 600(6%)"
              />
            </div>
            <div>
              <Label>税务模式</Label>
              <Select value={profile.default_tax_mode || ''} onValueChange={(v) => setProfile({ ...profile, default_tax_mode: v })}>
                <SelectTrigger><SelectValue placeholder="未设置" /></SelectTrigger>
                <SelectContent>
                  {TAX_MODES.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>纳税人识别号</Label>
              <Input value={profile.tax_id || ''} onChange={(e) => setProfile({ ...profile, tax_id: e.target.value })} />
            </div>
            <div>
              <Label>发票能力</Label>
              <Select value={profile.invoice_capability || ''} onValueChange={(v) => setProfile({ ...profile, invoice_capability: v })}>
                <SelectTrigger><SelectValue placeholder="未设置" /></SelectTrigger>
                <SelectContent>
                  {INVOICE_CAPS.map((c) => <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>备注</Label>
              <Input value={profile.notes || ''} onChange={(e) => setProfile({ ...profile, notes: e.target.value })} />
            </div>
            <Button onClick={save} disabled={busy}>
              <Save className="w-4 h-4 mr-1" /> 保存
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
