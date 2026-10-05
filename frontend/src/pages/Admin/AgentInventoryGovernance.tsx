/**
 * 管理员 · 服务商库存与关系治理(工单 v3 §P1-2)
 *
 * 路由: /admin/agent-inventory
 *
 * 存在的理由:后端三个端点在生产尖 `00466fd7` 上**已经存在**且可用,
 *   但**没有任何页面接它们** —— 服务商付了钱,管理员只能进数据库手工改。
 *   本页只做界面接线,**不新建后端实现**(工单 §P1-2 明令)。
 *
 * 三个动作必须分开,不能混:
 *   1. 订单入库 / 纠错减少 —— 增加库存必须选真实订单(孤儿铸造护栏)
 *   2. 代客户划拨         —— 走与服务商侧**同一套**关系解析
 *   3. 🔴 关系管理         —— R2 的**唯一**合法通道:全系统只有这里能改上下游关系
 */
import { useCallback, useEffect, useState } from 'react';
import { authFetch, formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { lazyToast } from '@/lib/lazyToast';
import { Boxes, RefreshCw, AlertTriangle } from 'lucide-react';

interface LotDriftRow {
  agent_user_id: number;
  wallet_side: number;
  lot_side: number;
  drift_points: number;
}

interface MutationPreview {
  before: { paid_inventory_points: number; bonus_inventory_points: number };
  after: { paid_inventory_points: number; bonus_inventory_points: number };
}

const num = (v: string) => parseInt(v || '0', 10) || 0;

export default function AgentInventoryGovernance() {
  return (
    <div className="space-y-4 p-4 sm:p-6">
      <h1 className="flex items-center gap-2 text-lg font-semibold">
        <Boxes className="h-4 w-4" />服务商库存与关系治理
      </h1>
      <p className="text-xs text-muted-foreground">
        这里的每个动作都会留下操作人、时间、原因和变更前后值。增加库存必须关联真实订单。
      </p>

      <Tabs defaultValue="adjust">
        <TabsList className="grid w-full grid-cols-2 sm:inline-flex sm:w-auto lg:grid-cols-4">
          <TabsTrigger value="adjust">订单入库 / 纠错</TabsTrigger>
          <TabsTrigger value="allocate">代客户划拨</TabsTrigger>
          <TabsTrigger value="relationship">关系管理</TabsTrigger>
          <TabsTrigger value="drift">库存对账</TabsTrigger>
        </TabsList>

        <TabsContent value="adjust" className="pt-4"><AdjustPanel /></TabsContent>
        <TabsContent value="allocate" className="pt-4"><AllocatePanel /></TabsContent>
        <TabsContent value="relationship" className="pt-4"><RelationshipPanel /></TabsContent>
        <TabsContent value="drift" className="pt-4"><LotDriftPanel /></TabsContent>
      </Tabs>
    </div>
  );
}

// ============================================================
// 1. 订单入库 / 纠错减少
// ============================================================

function AdjustPanel() {
  const [agentId, setAgentId] = useState('');
  const [direction, setDirection] = useState<'increase' | 'decrease'>('increase');
  const [paid, setPaid] = useState('');
  const [bonus, setBonus] = useState('');
  const [orderId, setOrderId] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<MutationPreview | null>(null);

  // 🔴 增加方向必须带真实订单号 —— 无订单号的孤儿铸造在后端就会被护栏拒。
  //    前端提前说清楚,而不是让管理员填完一屏再被 422 打回(§P1-3「订单缺失」那一行)。
  const needsOrder = direction === 'increase' && !orderId.trim();

  const submit = async () => {
    if (!num(agentId)) return lazyToast.error('请填写服务商账号 ID');
    if (num(paid) + num(bonus) <= 0) return lazyToast.error('请填写要调整的算力数量');
    if (reason.trim().length < 2) return lazyToast.error('请填写原因(至少 2 个字)');
    if (needsOrder) return lazyToast.error('增加库存需要关联真实订单');
    setBusy(true);
    try {
      const res = await authFetch('/api/admin/agent-inventory/adjust', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_user_id: num(agentId),
          direction,
          paid_points: num(paid),
          bonus_points: num(bonus),
          related_order_id: orderId.trim() || null,
          reason: reason.trim(),
        }),
      });
      if (!res.ok) throw await res.json().catch(() => new Error('提交失败'));
      const data = await res.json();
      setPreview({ before: data.before, after: data.after });
      lazyToast.success('已调整 · 变更前后值见下方');
      // 🔴 成功也**不清空表单**:管理员经常连续处理同一个服务商的多笔。
      //    失败更不清空(§0.4 第 1 条 / §4.5 末条)。
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '调整失败', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3"><CardTitle className="text-sm">调整服务商库存</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <Label className="text-xs">服务商账号 ID</Label>
            <Input value={agentId} onChange={(e) => setAgentId(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">方向</Label>
            <div className="mt-1 flex gap-2">
              <Button
                type="button" size="sm"
                variant={direction === 'increase' ? 'default' : 'outline'}
                onClick={() => setDirection('increase')}
              >订单入库(增加)</Button>
              <Button
                type="button" size="sm"
                variant={direction === 'decrease' ? 'default' : 'outline'}
                onClick={() => setDirection('decrease')}
              >纠错减少</Button>
            </div>
          </div>
          <div>
            <Label className="text-xs">充值库存算力</Label>
            <Input type="number" min={0} value={paid} onChange={(e) => setPaid(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">赠送库存算力</Label>
            <Input type="number" min={0} value={bonus} onChange={(e) => setBonus(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">关联订单号{direction === 'increase' ? '(必填)' : '(可选)'}</Label>
            <Input value={orderId} onChange={(e) => setOrderId(e.target.value)} placeholder="对公转账对应的真实订单号" />
          </div>
          <div>
            <Label className="text-xs">原因(必填)</Label>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如:对公转账 ¥5000 已到账" />
          </div>
        </div>
        {needsOrder && (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            增加库存需要关联真实订单 —— 请先在上方填入订单号。
          </p>
        )}
        <Button onClick={submit} disabled={busy} size="sm">提交调整</Button>
        {preview && (
          <div className="rounded-lg border border-border bg-muted/30 p-3 text-xs">
            <div className="font-medium">变更前后</div>
            <div className="mt-1 tabular-nums">
              充值库存 {preview.before.paid_inventory_points.toLocaleString()} → {preview.after.paid_inventory_points.toLocaleString()}
            </div>
            <div className="tabular-nums">
              赠送库存 {preview.before.bonus_inventory_points.toLocaleString()} → {preview.after.bonus_inventory_points.toLocaleString()}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ============================================================
// 2. 代客户划拨
// ============================================================

function AllocatePanel() {
  const [agentId, setAgentId] = useState('');
  const [customerId, setCustomerId] = useState('');
  const [paid, setPaid] = useState('');
  const [bonus, setBonus] = useState('');
  const [version, setVersion] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!num(agentId) || !num(customerId)) return lazyToast.error('请填写服务商与客户账号 ID');
    if (num(paid) + num(bonus) <= 0) return lazyToast.error('请填写要划拨的算力数量');
    if (reason.trim().length < 2) return lazyToast.error('请填写原因(至少 2 个字)');
    setBusy(true);
    try {
      const res = await authFetch('/api/admin/agent-inventory/allocate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_user_id: num(agentId),
          customer_user_id: num(customerId),
          paid_points: num(paid),
          bonus_points: num(bonus),
          binding_expected_version: num(version) || null,
          reason: reason.trim(),
        }),
      });
      if (!res.ok) throw await res.json().catch(() => new Error('提交失败'));
      lazyToast.success('划拨完成');
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '划拨失败', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3"><CardTitle className="text-sm">代服务商划拨给客户</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        {/* 🔴 工单 §P1-2 第 2 条:日常渠道转售**不得**借道纠错接口。
            这句不是提示语,是把两个动作的边界写在操作者眼前。 */}
        <p className="text-xs text-muted-foreground">
          这里只处理「服务商已收款、需要平台代为把算力发给客户」。
          服务商之间的进货转售请走对方自己的进货流程，不要用纠错功能顶替。
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <Label className="text-xs">服务商账号 ID</Label>
            <Input value={agentId} onChange={(e) => setAgentId(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">客户账号 ID</Label>
            <Input value={customerId} onChange={(e) => setCustomerId(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">充值算力</Label>
            <Input type="number" min={0} value={paid} onChange={(e) => setPaid(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">赠送算力</Label>
            <Input type="number" min={0} value={bonus} onChange={(e) => setBonus(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">归属版本号(客户尚无归属时必填)</Label>
            <Input value={version} onChange={(e) => setVersion(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">原因(必填)</Label>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
        </div>
        <Button onClick={submit} disabled={busy} size="sm">提交划拨</Button>
      </CardContent>
    </Card>
  );
}

// ============================================================
// 3. 🔴 关系管理 —— R2 的唯一合法通道
// ============================================================

function RelationshipPanel() {
  const [userId, setUserId] = useState('');
  const [upstreamId, setUpstreamId] = useState('');
  const [bps, setBps] = useState('10000');
  const [expectedVersion, setExpectedVersion] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!num(userId)) return lazyToast.error('请填写下级服务商账号 ID');
    if (!num(expectedVersion)) return lazyToast.error('请填写当前关系版本号(防并发覆盖)');
    if (reason.trim().length < 2) return lazyToast.error('请填写原因(至少 2 个字)');
    setBusy(true);
    try {
      const res = await authFetch(
        `/api/admin/user-governance/users/${num(userId)}/channel-relationship`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            // 留空 = 改为直接挂平台根渠道(等于解除现有上游)
            upstream_user_id: num(upstreamId) || null,
            cost_multiplier_bps: num(bps) || 10000,
            expected_version: num(expectedVersion),
            reason: reason.trim(),
          }),
        }
      );
      if (!res.ok) throw await res.json().catch(() => new Error('提交失败'));
      lazyToast.success('关系已更新 · 已写入关系历史版本');
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '关系更新失败', 'agent'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3"><CardTitle className="text-sm">上下游关系与进货系数</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
          <div className="flex items-center gap-1 font-medium">
            <AlertTriangle className="h-3.5 w-3.5" />这是全系统唯一能改上下游关系的地方
          </div>
          <p className="mt-1 leading-relaxed text-muted-foreground">
            关系一旦建立就长期有效，不随对方身份变化而失效；下级升级为服务商后仍然是同一上游的下级。
            运行时的任何功能都不会自动新增、改写或解除关系。每次变更都会写一条关系历史版本。
          </p>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <Label className="text-xs">下级服务商账号 ID</Label>
            <Input value={userId} onChange={(e) => setUserId(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">上游账号 ID(留空 = 直接挂平台)</Label>
            <Input value={upstreamId} onChange={(e) => setUpstreamId(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">进货系数(基点 · 10000 = 与上游成本持平)</Label>
            <Input type="number" min={10000} value={bps} onChange={(e) => setBps(e.target.value)} />
          </div>
          <div>
            <Label className="text-xs">当前关系版本号(必填)</Label>
            <Input value={expectedVersion} onChange={(e) => setExpectedVersion(e.target.value)} />
          </div>
          <div className="sm:col-span-2">
            <Label className="text-xs">原因(必填)</Label>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如:双方线下签署渠道合作协议" />
          </div>
        </div>
        <Button onClick={submit} disabled={busy} size="sm">提交关系变更</Button>
      </CardContent>
    </Card>
  );
}

// ============================================================
// 4. 库存对账(lot 漂移)
// ============================================================

function LotDriftPanel() {
  const [rows, setRows] = useState<LotDriftRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [filter, setFilter] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await authFetch('/api/admin/agent-inventory/lot-drift');
      if (!res.ok) throw await res.json().catch(() => new Error('读取失败'));
      const data = await res.json();
      setRows(data.agents || []);
      setTotal(data.drift_total_points ?? null);
    } catch (e: any) {
      // 🔴 读取失败**不得**把未知伪装成 0(§0.3):清空数字,显式报失败 + 重试。
      setError(true);
      setRows([]);
      setTotal(null);
      lazyToast.error(formatApiErrorForDisplay(e, '库存对账读取失败', 'agent'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const shown = filter.trim()
    ? rows.filter((r) => String(r.agent_user_id).includes(filter.trim()))
    : rows;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between gap-2 text-sm">
          <span>库存对账 · 钱包与批次差额</span>
          <Button size="sm" variant="ghost" onClick={load} disabled={loading}>
            <RefreshCw className={loading ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
            <div className="font-medium">暂时无法读取库存对账</div>
            <Button size="sm" variant="outline" className="mt-2" onClick={load}>重新读取</Button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="按服务商账号 ID 筛选"
                className="max-w-[220px]"
              />
              {total !== null && (
                <Badge variant="outline" className="tabular-nums">净差额 {total.toLocaleString()}</Badge>
              )}
            </div>
            {shown.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {loading ? '正在读取…' : '当前没有差额记录。'}
              </p>
            ) : (
              <div className="divide-y divide-border rounded-lg border border-border">
                {shown.map((r) => (
                  <div key={r.agent_user_id} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
                    <div>
                      <div className="font-medium">服务商 #{r.agent_user_id}</div>
                      <div className="text-xs tabular-nums text-muted-foreground">
                        钱包 {r.wallet_side.toLocaleString()} · 批次 {r.lot_side.toLocaleString()}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="tabular-nums">{r.drift_points.toLocaleString()}</Badge>
                      {/* 出口:不能只给一串差额(§P1-2 末句) */}
                      <a
                        href={`/admin/inventory-audit-history?agent_user_id=${r.agent_user_id}`}
                        className="text-xs text-primary underline underline-offset-2"
                      >
                        查看关联流水 →
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
