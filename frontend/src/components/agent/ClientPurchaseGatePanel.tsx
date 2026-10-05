/**
 * [微单 C-6 2026-07-28] 客户线上购买门控 · 服务商侧唯一入口。
 *
 * 从 Agent/PromotionCenter 迁至客户售价页(Agent/PricingCenter 置顶):
 * Owner 使用动线 = 给客户定价时顺手决定"这个客户能不能线上直购"。
 * 总开关(主账号默认)置顶 + 每客户三态(inherit/allow/offline_only)同页客户行呈现。
 * 后端 API 与门控判定零改动 —— 本组件只读写既有三个端点并回填后端算好的结果。
 */
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { agentApi } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';

interface GateCustomer {
  customer_user_id: number;
  display_name?: string;
  phone_masked?: string;
  online_purchase_override?: string;
  can_purchase_online?: boolean;
}

export default function ClientPurchaseGatePanel(): JSX.Element {
  const [allowDefault, setAllowDefault] = useState(true);
  const [saving, setSaving] = useState(false);
  const [overrideSavingId, setOverrideSavingId] = useState<number | null>(null);
  const [items, setItems] = useState<GateCustomer[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const reload = async () => {
    setLoading(true);
    try {
      const c = await agentApi.promotionCustomers(50, 0);
      setItems((c.items || []) as GateCustomer[]);
      setTotal(c.total || 0);
      if (typeof c.default_allow_client_online_purchase === 'boolean') {
        setAllowDefault(c.default_allow_client_online_purchase);
      }
    } catch {
      /* 列表加载失败 · 保持空态(总开关仍可用) */
    }
    setLoading(false);
  };

  useEffect(() => {
    void reload();
  }, []);

  const handleToggle = async (next: boolean) => {
    setSaving(true);
    const previous = allowDefault;
    setAllowDefault(next);
    try {
      const res = await agentApi.setClientPurchaseSettings(next);
      setAllowDefault(res.allow_client_online_purchase);
      toast.success(next ? '已允许客户线上购买' : '已改为仅线下 · 客户购买将提示联系你办理');
      await reload();
    } catch (e: unknown) {
      setAllowDefault(previous);
      toast.error(formatApiErrorForDisplay(e, '设置失败 · 请重试', 'agent'));
    } finally {
      setSaving(false);
    }
  };

  const handleOverride = async (customerUserId: number, override: string) => {
    setOverrideSavingId(customerUserId);
    try {
      const res = await agentApi.setCustomerPurchaseOverride(customerUserId, override);
      setItems((prev) => prev.map((c) => (
        c.customer_user_id === customerUserId
          ? {
              ...c,
              online_purchase_override: res.online_purchase_override,
              can_purchase_online: res.can_purchase_online,
            }
          : c
      )));
      toast.success('已保存');
    } catch (e: unknown) {
      toast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'agent'));
    } finally {
      setOverrideSavingId(null);
    }
  };

  return (
    <Card data-testid="client-purchase-gate-panel">
      <CardHeader>
        <CardTitle className="text-base">客户线上购买</CardTitle>
        <p className="text-xs text-muted-foreground">
          走线下收费的客户可以关掉线上购买入口。客户侧只会看到「联系您的推荐人办理」,不会出现你的名字。
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 总开关(主账号默认)· 置顶 */}
        <div
          className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-3 sm:flex-row sm:items-center sm:justify-between"
          data-testid="client-purchase-gate-master"
        >
          <div className="min-w-0">
            <div className="text-sm font-medium">允许名下客户线上购买</div>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {allowDefault
                ? '开启中 · 客户可以自己在线上充值和购买算力。'
                : '已关闭 · 你的客户将无法在线上直接购买,需联系你办理。单个客户可在下方列表里单独放行。'}
            </p>
          </div>
          <Switch
            checked={allowDefault}
            disabled={saving}
            onCheckedChange={(v) => void handleToggle(v === true)}
            aria-label="允许名下客户线上购买"
          />
        </div>

        {/* 客户级三态 · 与售价同页,一个客户一行 */}
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-medium">
            按客户单独设置
            <Badge variant="outline" className="text-[11px]">{total} 位客户</Badge>
          </div>
          {loading && <p className="text-xs text-muted-foreground">加载中…</p>}
          {!loading && items.length === 0 && (
            <p className="text-xs text-muted-foreground">
              暂无绑定客户 · 客户通过你的推广链接注册后会出现在这里。
            </p>
          )}
          {!loading && items.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="p-2.5 text-left">客户</th>
                    <th className="p-2.5 text-left">手机</th>
                    <th className="p-2.5 text-left">线上购买</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => (
                    <tr key={c.customer_user_id} className="border-b last:border-b-0">
                      <td className="p-2.5">
                        {c.display_name || `用户编号 ${c.customer_user_id}`}
                        <span className="ml-1 text-xs text-muted-foreground">编号 {c.customer_user_id}</span>
                      </td>
                      <td className="p-2.5 text-xs">{c.phone_masked || '-'}</td>
                      <td className="p-2.5">
                        <select
                          className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                          value={c.online_purchase_override || 'inherit'}
                          disabled={overrideSavingId === c.customer_user_id}
                          aria-label={`客户 ${c.customer_user_id} 线上购买设置`}
                          onChange={(e) => void handleOverride(c.customer_user_id, e.target.value)}
                        >
                          <option value="inherit">
                            跟随默认({allowDefault ? '允许线上购买' : '仅线下'})
                          </option>
                          <option value="allow">允许线上购买</option>
                          <option value="offline_only">仅线下(联系我办理)</option>
                        </select>
                        <div className="mt-1 text-[11px] text-muted-foreground">
                          {c.can_purchase_online === false ? '当前:不能线上购买' : '当前:可线上购买'}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
