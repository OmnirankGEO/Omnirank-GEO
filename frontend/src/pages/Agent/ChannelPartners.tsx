/**
 * 服务商 · 发展下级服务商(工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12 §P0-2)
 *
 * 路由: /agent/channel-partners
 *
 * 存在的理由(2026-08-12 P0 事故的**直接卡点**):
 *   系统对下级身份有硬分流 —— 普通客户走商业绑定,服务商走渠道关系。
 *   但服务商**没有任何入口**给自己发展下级服务商(渠道关系此前只有管理员能建),
 *   于是操作者转而尝试"把服务商绑成客户",被守卫挡住,链路当场中断。
 *   这一页就是那个缺掉的入口。
 *
 * 为什么要审批:渠道关系直接决定分润链与进货成本系数,一条边改变平台/上游/下游三方的钱。
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { authFetch, formatApiErrorForDisplay } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { lazyToast } from '@/lib/lazyToast';
import { ArrowLeft, Search, Users } from 'lucide-react';

/** 关系查询结果 —— 只在**已有有向关系**时才有内容(工单 v3 §P0-1)。
 *
 * 🔴 改造前这个接口对任意账号 ID 都回展示名 + 身份,等于一个账号枚举面:
 *    随便试 ID 就能问出"此号注册没有 / 是不是服务商"。现在无关系一律 404,
 *    与"账号不存在"和"查自己的上游"同一份响应(§0.1 R5)。
 */
interface RelationResult {
  target_user_id: number;
  target_display_name: string;
  relation: 'customer' | 'downstream_partner' | 'both';
  target_identity: 'service_provider' | 'level0';
  headline: string;
  effect_note: string;
  entry_route: string;
}

interface PartnerRequestRow {
  id: number;
  target_user_id: number;
  target_display_name: string | null;
  proposed_cost_multiplier_bps: number;
  status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  reason: string;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
}

const STATUS_LABEL: Record<PartnerRequestRow['status'], string> = {
  pending: '等待平台审批',
  approved: '已通过',
  rejected: '已驳回',
  cancelled: '已撤回',
};

export default function ChannelPartners() {
  const [targetId, setTargetId] = useState('');
  const [relation, setRelation] = useState<RelationResult | null>(null);
  const [relationMiss, setRelationMiss] = useState(false);
  const [checking, setChecking] = useState(false);
  const [bps, setBps] = useState('11000');
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [rows, setRows] = useState<PartnerRequestRow[]>([]);

  const reload = useCallback(async () => {
    try {
      const res = await authFetch('/api/agent/channel-partners/requests');
      if (!res.ok) throw await res.json().catch(() => new Error('读取失败'));
      const data = await res.json();
      setRows(data.items || []);
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '读取申请列表失败', 'agent'));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const checkRelation = async () => {
    const id = parseInt(targetId || '0', 10);
    if (!id) return lazyToast.error('请输入对方的账号 ID');
    setChecking(true);
    setRelation(null);
    setRelationMiss(false);
    try {
      const res = await authFetch(`/api/agent/channel-partners/preflight?target_user_id=${id}`);
      if (res.status === 404) {
        // 🔴 "没有关系" 与 "账号不存在" 共用同一条路径,这里也只有一句话。
        //    不得根据别的线索补充"其实这个号存在" —— 那就把不可区分性拆掉了。
        setRelationMiss(true);
        return;
      }
      if (!res.ok) throw await res.json().catch(() => new Error('查询失败'));
      setRelation(await res.json());
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '查询失败', 'agent'));
    } finally {
      setChecking(false);
    }
  };

  const submit = async () => {
    const id = parseInt(targetId || '0', 10);
    if (!id) return lazyToast.error('请输入对方的账号 ID');
    if (reason.trim().length < 2) return lazyToast.error('请填写申请理由(至少 2 个字)');
    setSubmitting(true);
    try {
      const res = await authFetch('/api/agent/channel-partners/requests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_user_id: id,
          proposed_cost_multiplier_bps: parseInt(bps || '10000', 10),
          reason: reason.trim(),
        }),
      });
      if (!res.ok) throw await res.json().catch(() => new Error('提交失败'));
      lazyToast.success('申请已提交 · 等待平台审批');
      setRelation(null);
      setRelationMiss(false);
      setTargetId('');
      setReason('');
      void reload();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '提交失败', 'agent'));
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async (id: number) => {
    try {
      const res = await authFetch(`/api/agent/channel-partners/requests/${id}/cancel`, { method: 'POST' });
      if (!res.ok) throw await res.json().catch(() => new Error('撤回失败'));
      lazyToast.success('已撤回');
      void reload();
    } catch (e: any) {
      lazyToast.error(formatApiErrorForDisplay(e, '撤回失败', 'agent'));
    }
  };

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex items-center gap-2">
        <Link to="/agent/inventory" className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <Users className="h-4 w-4" />发展下级服务商
        </h1>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">查询已有关系</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            这里只能查到<strong>你自己的客户和下线</strong>。查不到不代表账号不存在，
            只表示你和对方目前没有平台记录在案的关系。
          </p>
          <div className="flex gap-2">
            <Input
              value={targetId}
              onChange={(e) => setTargetId(e.target.value)}
              placeholder="对方的账号 ID"
              className="max-w-[220px]"
            />
            <Button onClick={checkRelation} disabled={checking} variant="outline" size="sm">
              <Search className="mr-1 h-3.5 w-3.5" />查询
            </Button>
          </div>

          {relation && (
            <div className="rounded-lg border border-border bg-muted/30 p-3 text-sm">
              <div className="font-medium">
                {relation.target_display_name}(#{relation.target_user_id})· {relation.headline}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{relation.effect_note}</p>
            </div>
          )}
          {relationMiss && (
            <div className="rounded-lg border border-border bg-muted/30 p-3 text-sm">
              {/* 🔴 一句话到底:陌生账号、不存在的账号、自己的上游 —— 全部落这一条。 */}
              <div className="font-medium">未找到该账号</div>
              <p className="mt-1 text-xs text-muted-foreground">
                你可以核对账号 ID 后重新查询；如果你们线下已谈好合作，直接在下方提交申请由平台核实。
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 🔴 申请不再依赖"先查出对方是谁":查询面已收口为关系查询,
          陌生账号的身份不再回显(§P0-2)。合作是否成立由**平台审批**判定 ——
          这也与 R2「只有管理员能改关系」一致。 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">提交合作申请(由平台审批)</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <Label className="text-xs">进货系数(基点 · 10000 = 与你成本持平)</Label>
              <Input type="number" min={10000} value={bps} onChange={(e) => setBps(e.target.value)} />
              <p className="mt-1 text-[11px] text-muted-foreground">
                不得低于 10000 —— 下级的进货价不能低于你的有效成本。最终生效值以平台审批确认为准。
              </p>
            </div>
            <div>
              <Label className="text-xs">申请理由</Label>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如:线下已签合作协议" />
            </div>
          </div>
          <Button onClick={submit} disabled={submitting} size="sm">提交申请</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">我的申请</CardTitle>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">还没有提交过合作申请。</p>
          ) : (
            <div className="space-y-2">
              {rows.map((row) => (
                <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3 text-sm">
                  <div>
                    <div className="font-medium">
                      {row.target_display_name || `账号 #${row.target_user_id}`}
                      <span className="ml-2 text-xs text-muted-foreground">系数 {row.proposed_cost_multiplier_bps}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">{row.reason}</p>
                    {row.decision_note && (
                      <p className="text-xs text-muted-foreground">平台结论:{row.decision_note}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={row.status === 'approved' ? 'default' : 'outline'}>
                      {STATUS_LABEL[row.status]}
                    </Badge>
                    {row.status === 'pending' && (
                      <Button size="sm" variant="ghost" onClick={() => cancel(row.id)}>撤回</Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
