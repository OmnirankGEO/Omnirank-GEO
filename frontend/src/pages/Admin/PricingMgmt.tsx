/**
 * V3.5 W2 · Admin 价格管理(SKU 内核)
 *
 * 路由: /admin/pricing
 * 责任: admin 编辑 sku_templates · 可见 platform_cost_cents 等 raw cost
 * SKU 量纲铁律: points_granted × 200 == wholesale_cents × 325
 */
import { useEffect, useState } from 'react';
import { adminApi, formatCents } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { DollarSign, RefreshCw, AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';

interface AdminSKU {
  sku_template_id: number;
  sku_key: string;
  category: string;
  display_name: string;
  subtitle?: string;
  points_granted: number;
  wholesale_cents: number;
  retail_cents: number;
  is_active: boolean;
}

const CATEGORIES = ['credit_pack', 'scenario_pack', 'addon_pack'];
const CATEGORY_LABELS: Record<string, string> = {
  credit_pack: '通用算力包',
  scenario_pack: '场景包',
  addon_pack: '按需加购',
};

export default function PricingMgmt() {
  const [tab, setTab] = useState('credit_pack');
  const [items, setItems] = useState<AdminSKU[]>([]);
  const [loading, setLoading] = useState(false);

  const reload = async (cat?: string) => {
    setLoading(true);
    try {
      const r = await adminApi.pricingList({ category: cat });
      setItems(r.items || []);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(tab); }, [tab]);

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <DollarSign className="w-6 h-6" />
          <h1 className="text-2xl font-bold">算力包管理 · 平台模板</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={() => reload(tab)}>
          <RefreshCw className="w-4 h-4 mr-1" /> 刷新
        </Button>
      </div>

      <Card className="bg-yellow-50/30 border-yellow-300">
        <CardContent className="py-3 flex items-center gap-2 text-sm">
          <AlertTriangle className="w-4 h-4 text-yellow-600" />
          <span title="算力 × 200 = 出厂价(分) × 325">
            算力与出厂价比例不匹配则保存会被拒绝(平台铁律)· 鼠标悬停查看公式
          </span>
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {CATEGORIES.map((c) => (
            <TabsTrigger key={c} value={c}>{CATEGORY_LABELS[c]}</TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={tab} className="pt-4">
          <Card>
            <CardContent className="p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="text-left p-3">编码</th>
                    <th className="text-left p-3">展示名</th>
                    <th className="text-right p-3">出厂算力</th>
                    <th className="text-right p-3">出厂价</th>
                    <th className="text-right p-3">建议零售</th>
                    <th className="text-right p-3">建议毛利率</th>
                    <th className="text-left p-3">状态</th>
                    <th className="text-right p-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.length === 0 && (
                    <tr><td colSpan={8} className="text-center p-6 text-muted-foreground">暂无算力包</td></tr>
                  )}
                  {items.map((s) => {
                    const margin = s.wholesale_cents > 0
                      ? ((s.retail_cents - s.wholesale_cents) / s.wholesale_cents * 100).toFixed(1)
                      : '0';
                    return (
                      <tr key={s.sku_template_id} className="border-b">
                        <td className="p-3 font-mono text-xs">{s.sku_key}</td>
                        <td className="p-3">{s.display_name}{s.subtitle && <div className="text-xs text-muted-foreground">{s.subtitle}</div>}</td>
                        <td className="p-3 text-right font-mono">{s.points_granted.toLocaleString()}</td>
                        <td className="p-3 text-right font-mono">{formatCents(s.wholesale_cents)}</td>
                        <td className="p-3 text-right font-mono">{formatCents(s.retail_cents)}</td>
                        <td className="p-3 text-right text-xs">{margin}%</td>
                        <td className="p-3">{s.is_active ? <Badge>上架</Badge> : <Badge variant="secondary">下架</Badge>}</td>
                        <td className="p-3 text-right">
                          <EditDialog sku={s} onDone={() => reload(tab)} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function EditDialog({ sku, onDone }: { sku: AdminSKU; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [points, setPoints] = useState(String(sku.points_granted));
  const [wholesale, setWholesale] = useState(String((sku.wholesale_cents / 100).toFixed(2)));
  const [retail, setRetail] = useState(String((sku.retail_cents / 100).toFixed(2)));
  const [active, setActive] = useState(sku.is_active);
  const [busy, setBusy] = useState(false);

  const pInt = parseInt(points || '0');
  const wCents = Math.round(parseFloat(wholesale || '0') * 100);
  const violates = pInt * 200 !== wCents * 325;

  const submit = async () => {
    setBusy(true);
    try {
      await adminApi.pricingPut(sku.sku_template_id, {
        points_granted: pInt,
        wholesale_cents: wCents,
        retail_cents: Math.round(parseFloat(retail || '0') * 100),
        is_active: active,
      });
      toast.success('保存成功');
      setOpen(false);
      onDone();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button size="sm" variant="outline">编辑</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>编辑算力包 · {sku.sku_key}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div><Label>出厂算力</Label><Input type="number" value={points} onChange={(e) => setPoints(e.target.value)} /></div>
            <div><Label>出厂价(元)</Label><Input type="number" step="0.01" value={wholesale} onChange={(e) => setWholesale(e.target.value)} /></div>
          </div>
          {violates && (
            <div className="bg-red-50 border border-red-300 p-2 rounded text-xs text-red-700">
              ⚠️ 比例不匹配 · 算力 × 200 = {pInt * 200} · 出厂价(分)× 325 = {wCents * 325} · 调整后保存
            </div>
          )}
          <div><Label>建议零售价(元)</Label><Input type="number" step="0.01" value={retail} onChange={(e) => setRetail(e.target.value)} /></div>
          <p className="text-xs text-muted-foreground">
            平台真实成本在 /admin/feature-pricing 单独管理 · 这里仅管算力包出厂价
          </p>
          <div className="flex items-center gap-2">
            <Switch checked={active} onCheckedChange={setActive} />
            <Label>{active ? '上架' : '下架'}</Label>
          </div>
        </div>
        <DialogFooter>
          <Button onClick={submit} disabled={busy || violates}>保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
