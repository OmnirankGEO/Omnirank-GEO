/**
 * 定价系数动态配置 (D5 · admin-only)
 *
 * 路由: /admin/pricing-config
 * - 全局: 出厂折扣 / 积分换算 / 报价&媒体默认 / 体验包 / 进货赠送(后台改 → 热生效·无需重启)
 * - per-agent: 平台给单个服务商设 报价/零售/出厂折扣 override(优先级 admin > 服务商自设 > 全局默认)
 *
 * 关联: docs/AI-CONTEXT/PRICING_COEFFICIENT_DYNAMIC_PLAN_2026-06-04.md (D5)
 */
import { useState, useEffect } from 'react';
import { adminApi } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SlidersHorizontal, Search, Save, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';

export default function PricingConfig() {
  const [cfg, setCfg] = useState<any>(null);
  const [catalogVersion, setCatalogVersion] = useState('');
  const [cfgBusy, setCfgBusy] = useState(false);
  const [agentId, setAgentId] = useState('');
  const [ov, setOv] = useState<any>(null);
  const [ovBusy, setOvBusy] = useState(false);

  const loadCfg = async () => {
    try {
      const r = await adminApi.globalPricingConfigGet();
      setCfg(r.config);
      setCatalogVersion(r.catalog_version);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载全局配置失败 · 请重试', 'admin'));
    }
  };
  useEffect(() => { loadCfg(); }, []);

  const num = (v: any) => (v === '' || v === null || v === undefined ? undefined : Number(v));

  const saveCfg = async () => {
    if (!cfg || !catalogVersion) return;
    setCfgBusy(true);
    try {
      await adminApi.globalPricingConfigPut({
        expected_catalog_version: catalogVersion,
        wholesale_numer: num(cfg.wholesale_numer),
        wholesale_denom: num(cfg.wholesale_denom),
        agent_purchase_bonus_rate: num(cfg.agent_purchase_bonus_rate),
      });
      toast.success('全局定价配置已保存 · 已热生效(无需重启)');
      loadCfg();
    } catch (e: any) {
      if (e?.response?.status === 409) {
        toast.error('配置已被其他管理员更新；本地输入仍保留，请刷新后重新编辑');
      } else {
        toast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
      }
    } finally { setCfgBusy(false); }
  };

  const loadOv = async () => {
    const id = parseInt(agentId);
    if (!id) { toast.error('服务方编号必填'); return; }
    try {
      const r = await adminApi.agentPricingOverrideGet(id);
      setOv({ agent_user_id: id, ...(r.override || {}) });
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    }
  };

  const saveOv = async () => {
    if (!ov) return;
    if (!catalogVersion) { toast.error('价格版本尚未加载，请刷新后重试'); return; }
    setOvBusy(true);
    try {
      await adminApi.agentPricingOverridePut(ov.agent_user_id, {
        expected_catalog_version: catalogVersion,
        quote_markup_override: num(ov.quote_markup_override),
        sku_markup_override: num(ov.sku_markup_override),
        wholesale_numer: num(ov.wholesale_numer),
        wholesale_denom: num(ov.wholesale_denom),
        note: ov.note || undefined,
      });
      toast.success('已保存 · 优先级 admin > 服务商自设 > 全局默认');
      await loadCfg();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '保存失败 · 请重试', 'admin'));
    } finally { setOvBusy(false); }
  };

  const clearOv = async () => {
    if (!ov?.agent_user_id) return;
    if (!catalogVersion) { toast.error('价格版本尚未加载，请刷新后重试'); return; }
    setOvBusy(true);
    try {
      await adminApi.agentPricingOverrideDelete(ov.agent_user_id, catalogVersion);
      toast.success('已清空 · 该服务商回落全局默认');
      await loadCfg();
      loadOv();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '清空失败 · 请重试', 'admin'));
    } finally { setOvBusy(false); }
  };

  const field = (obj: any, set: (o: any) => void, key: string, label: string, hint?: string) => (
    <div className="space-y-1">
      <Label>{label}</Label>
      <Input
        value={obj?.[key] ?? ''}
        onChange={(e) => set({ ...obj, [key]: e.target.value })}
        placeholder={hint}
      />
    </div>
  );

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-3xl">
      <div className="flex items-center gap-2">
        <SlidersHorizontal className="w-6 h-6" />
        <h1 className="text-2xl font-bold">定价系数配置</h1>
      </div>

      <Card>
        <CardHeader><CardTitle>全局定价系数（改后热生效 · 无需重启）</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          {cfg ? (
            <>
              <div className="grid grid-cols-2 gap-4">
                {field(cfg, setCfg, 'wholesale_numer', '出厂折扣分子', '默认 200')}
                {field(cfg, setCfg, 'wholesale_denom', '出厂折扣分母', '默认 325（示例 9 折 = 225/325）')}
                {field(cfg, setCfg, 'agent_purchase_bonus_rate', '进货赠送比例', '默认 0.10')}
              </div>
              <Button onClick={saveCfg} disabled={cfgBusy}>
                <Save className="w-4 h-4 mr-1" /> 保存全局配置
              </Button>
              <p className="text-xs text-muted-foreground">
                此处仅调【全链路真正生效】的系数。出厂折扣改动同步影响 SKU 量纲校验与新订单出厂价（已下单按锁定值不重算）。
                <br />· 报价系数默认 → 走「系统设置」· 媒体加价系数 → 走「发布通道配置」· 算力换算 / 体验包暂不在此（避免改了不生效）。
                <br />· 充值档位 / 进货档位 / 赠送阶梯为结构化数组，走 API。
              </p>
            </>
          ) : <p className="text-sm text-muted-foreground">加载中…</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>单个服务商系数覆盖（admin 强制 · 优先级最高）</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Input
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              placeholder="服务方编号（user_id）"
            />
            <Button variant="outline" onClick={loadOv}><Search className="w-4 h-4 mr-1" />查询</Button>
          </div>
          {ov && (
            <>
              <div className="grid grid-cols-2 gap-4">
                {field(ov, setOv, 'quote_markup_override', '报价系数 override', '1.0-5.0 · 空=不覆盖')}
                {field(ov, setOv, 'sku_markup_override', '算力包零售系数 override', '>0 · 空=不覆盖')}
                {field(ov, setOv, 'wholesale_numer', '出厂折扣分子 override', '空=回落全局')}
                {field(ov, setOv, 'wholesale_denom', '出厂折扣分母 override', '空=回落全局')}
              </div>
              {field(ov, setOv, 'note', '备注', '可选')}
              <div className="flex gap-2">
                <Button onClick={saveOv} disabled={ovBusy}><Save className="w-4 h-4 mr-1" />保存覆盖</Button>
                <Button variant="outline" onClick={clearOv} disabled={ovBusy}>
                  <RotateCcw className="w-4 h-4 mr-1" />清空（回落全局）
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
