/**
 * V3.5 Follow-up 批 4 · Admin 资金与额度对账(IA 合并 · 2026-05-26)
 *
 * 路由: /admin/inventory-audit
 * 责任: 3 tabs 合并原"库存对账快照" + "对账日报历史" + "异常明细"
 *   - 当前对账 · 4 池等式即时计算
 *   - 历史记录 · 30 条 audit runs
 *   - 异常明细 · drift only 过滤
 *
 * IA 收敛说明:原 InventoryAuditHistory.tsx 内容内嵌为 HistoryTab(同文件)
 * 路由 /admin/inventory-audit-history 仍然指向同组件 · sidebar 入口已删除
 */
import { useEffect, useState } from 'react';
import { adminApi, formatPoints } from '@/lib/v35w2Api';
import { formatApiErrorForDisplay } from '@/lib/api';
import { adminW4Api } from '@/lib/v35w3Api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Scale, AlertTriangle, CheckCircle, RefreshCw, Play } from 'lucide-react';
import { toast } from 'sonner';
import { statusLabel, triggerByLabel } from '@/lib/v35Terminology';

// ============================================================
// 类型定义
// ============================================================

interface SnapshotSummary {
  agent_total_points: number;
  // [2026-07-29 返修] 账实相符式:diff = wallet_total − ledger_total
  agent_frozen_points: number;
  wallet_total_points: number;
  ledger_total_points: number;
  // ↓ 审计留痕分项 · 不参与差额判定
  customer_total_points: number;
  platform_consumed_points: number;
  historical_purchased_points: number;
  historical_admin_adjust_points: number;
  diff_points: number;
  diff_status: string;
  snapshot_at: string;
}

interface AuditRun {
  id: number;
  run_at: string;
  triggered_by: string;
  agent_total_paid: number;
  agent_total_bonus: number;
  customer_total_tool: number;
  customer_total_publish: number;
  customer_total_bonus: number;
  platform_consumed: number;
  historical_purchased: number;
  historical_admin_adjust: number;
  refunded_or_revoked: number;
  diff_paid: number;
  diff_bonus: number;
  diff_publish: number;
  has_drift: boolean;
  notes?: string;
  // [2026-07-29 返修] status='failed' = 对账程序自己没跑完(与"账不平"是两回事)
  status?: string;
  error_message?: string | null;
  agent_total_frozen?: number;
  wallet_total?: number | null;
  ledger_total?: number | null;
}

// ============================================================
// 主组件 · 3 tabs
// ============================================================

export default function InventoryAudit() {
  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      <div className="flex items-center gap-2">
        <Scale className="w-6 h-6" />
        <h1 className="text-2xl font-bold">资金与算力对账</h1>
      </div>

      <p className="text-sm text-muted-foreground">
        每天 02:45 自动核对工具库存、客户算力和已消费算力。发现差额(超过 1 算力)即标记为异常。保留最近 30 条对账记录。
      </p>

      <Tabs defaultValue="current">
        <TabsList>
          <TabsTrigger value="current">当前对账</TabsTrigger>
          <TabsTrigger value="history">历史记录</TabsTrigger>
          <TabsTrigger value="drift">异常明细</TabsTrigger>
        </TabsList>
        <TabsContent value="current" className="pt-4">
          <SnapshotTab />
        </TabsContent>
        <TabsContent value="history" className="pt-4">
          <HistoryTab driftOnly={false} />
        </TabsContent>
        <TabsContent value="drift" className="pt-4">
          <HistoryTab driftOnly={true} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ============================================================
// Tab 1 · 当前对账(原 InventoryAudit.tsx 内容)
// ============================================================

function SnapshotTab() {
  const [s, setS] = useState<SnapshotSummary | null>(null);

  const reload = async () => {
    try {
      const r = await adminApi.auditSummary();
      setS(r as SnapshotSummary);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    }
  };

  useEffect(() => { reload(); }, []);

  if (!s) return <div className="text-sm text-muted-foreground">加载中...</div>;

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={reload}><RefreshCw className="w-4 h-4 mr-1" />重新计算</Button>
      </div>
      <Card className={s.diff_status === 'ok' ? 'border-green-500' : 'border-red-500'}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            {s.diff_status === 'ok' ? (
              <><CheckCircle className="w-5 h-5 text-green-600" /> 对账平衡</>
            ) : (
              <><AlertTriangle className="w-5 h-5 text-red-600" /> 对账异常 · 差异 {formatPoints(Math.abs(s.diff_points))} 算力</>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="bg-muted/50 p-4 rounded text-sm space-y-1">
            <div>账面结存(可用 {s.agent_total_points.toLocaleString()} + 冻结中 {s.agent_frozen_points.toLocaleString()})
              = {s.wallet_total_points.toLocaleString()}</div>
            <div>- 全部进出记录净额 = {s.ledger_total_points.toLocaleString()}</div>
            <div className="border-t pt-1 mt-1 font-bold">
              = 差额: <span className={s.diff_status === 'ok' ? 'text-green-600' : 'text-red-600'}>{s.diff_points}</span>
            </div>
          </div>
          <p className="text-xs text-muted-foreground mt-3">
            核对方式:账面上还剩多少,和所有进出记录加起来应该剩多少,两者必须一致。
            下面四项只是明细参考,不参与差额判定。
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            快照时间 @ {new Date(s.snapshot_at).toLocaleString()}
          </p>
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <PoolMetric label="客户授权总和" value={s.customer_total_points} />
        <PoolMetric label="平台已消费" value={s.platform_consumed_points} />
        <PoolMetric label="历史进货" value={s.historical_purchased_points} />
        <PoolMetric label="管理员调整" value={s.historical_admin_adjust_points} />
      </div>
    </div>
  );
}

function PoolMetric({ label, value }: { label: string; value: number }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-xs text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-xl font-bold font-mono">{value.toLocaleString()}</div>
      </CardContent>
    </Card>
  );
}

// ============================================================
// Tab 2/3 · 历史记录(可 drift 过滤)· 原 InventoryAuditHistory.tsx 内容内嵌
// ============================================================

function HistoryTab({ driftOnly: initialDriftOnly }: { driftOnly: boolean }) {
  const [items, setItems] = useState<AuditRun[]>([]);
  const [driftOnly, setDriftOnly] = useState(initialDriftOnly);
  const [running, setRunning] = useState(false);

  const reload = async () => {
    try {
      const r = await adminW4Api.auditHistory(30, driftOnly);
      setItems(r.items || []);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '加载失败 · 请重试', 'admin'));
    }
  };

  useEffect(() => { reload(); }, [driftOnly]);

  const runNow = async () => {
    setRunning(true);
    try {
      const r = await adminW4Api.auditRun() as any;
      toast.success(r.has_drift ? `对账完成 · 异常(编号 ${r.run_id})` : `对账完成 · 正常(编号 ${r.run_id})`);
      await reload();
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '对账失败 · 请重试', 'admin'));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {!initialDriftOnly && (
            <>
              <Switch checked={driftOnly} onCheckedChange={setDriftOnly} />
              <Label className="text-sm">只看异常</Label>
            </>
          )}
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={reload}><RefreshCw className="w-4 h-4 mr-1" />刷新</Button>
          <Button onClick={runNow} disabled={running}><Play className="w-4 h-4 mr-1" />{running ? '对账中...' : '立即核对'}</Button>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="text-left p-3">编号</th>
                <th className="text-left p-3">时间</th>
                <th className="text-left p-3">触发</th>
                <th className="text-right p-3">工具充值库存</th>
                <th className="text-right p-3">工具赠送库存</th>
                <th className="text-right p-3">客户算力</th>
                <th className="text-right p-3">客户发布算力</th>
                <th className="text-right p-3">已消费</th>
                <th className="text-right p-3">充值库存差额</th>
                <th className="text-left p-3">状态</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr><td colSpan={10} className="text-center p-6 text-muted-foreground">暂无对账记录</td></tr>
              )}
              {items.map((r) => (
                <tr key={r.id} className={`border-b ${r.has_drift ? 'bg-red-50/40' : ''}`}>
                  <td className="p-3 font-mono text-xs">#{r.id}</td>
                  <td className="p-3 text-xs">{new Date(r.run_at).toLocaleString()}</td>
                  <td className="p-3"><Badge variant="outline" className="text-xs">{triggerByLabel(r.triggered_by)}</Badge></td>
                  <td className="p-3 text-right font-mono">{r.agent_total_paid.toLocaleString()}</td>
                  <td className="p-3 text-right font-mono">{r.agent_total_bonus.toLocaleString()}</td>
                  <td className="p-3 text-right font-mono">{r.customer_total_tool.toLocaleString()}</td>
                  <td className="p-3 text-right font-mono">{r.customer_total_publish.toLocaleString()}</td>
                  <td className="p-3 text-right font-mono">{r.platform_consumed.toLocaleString()}</td>
                  <td className={`p-3 text-right font-mono ${Math.abs(r.diff_paid) > 1 ? 'text-red-600 font-bold' : ''}`}>
                    {r.diff_paid}
                  </td>
                  <td className="p-3">
                    {/* [2026-07-29 返修] "程序没跑成" 和 "账不平" 是两回事,不能都显示成"漂移"
                        —— 失败行的差额是 0,只标"漂移"会让人以为差 0 算力还告警。 */}
                    {r.status === 'failed' ? (
                      <Badge variant="destructive" title={r.error_message || ''}>
                        <AlertTriangle className="w-3 h-3 mr-1" />核对未跑完
                      </Badge>
                    ) : r.has_drift ? (
                      <Badge variant="destructive"><AlertTriangle className="w-3 h-3 mr-1" />{statusLabel('drift', 'audit', 'admin')}</Badge>
                    ) : (
                      <Badge className="bg-green-600"><CheckCircle className="w-3 h-3 mr-1" />{statusLabel('ok', 'audit', 'admin')}</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
