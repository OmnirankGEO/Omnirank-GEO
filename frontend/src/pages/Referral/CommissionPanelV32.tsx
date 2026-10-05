/**
 * CommissionPanelV32 — v3.2 佣金面板
 *
 * 展示 pending_commissions 表的数据（状态机版本）
 * - pending:      待结算（T+3 后自动到账）
 * - settled:      已结算（可使用/提现）
 * - frozen:       冻结（反作弊 / 大额首充待审）
 * - refunded_cancelled: 因退款自动取消
 *
 * 与老版 CommissionPanel（commission_records 表）并行存在
 * 新用户默认看这个版本
 */

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Coins, Clock, CheckCircle2, ShieldAlert, XCircle, Loader2 } from 'lucide-react';

type CommissionStatus = 'pending' | 'settled' | 'frozen' | 'refunded_cancelled' | 'refund_clawback';

interface CommissionSummary {
  pending_yuan: number;
  pending_count: number;
  settled_yuan: number;
  settled_count: number;
  frozen_yuan: number;
  frozen_count: number;
  cancelled_yuan: number;
  this_month_yuan: number;
}

interface CommissionRecord {
  id: number;
  source_user_name: string;
  order_id: string;
  amount_yuan: number;
  level: 1 | 2;
  status: CommissionStatus;
  created_at: string | null;
  available_at: string | null;
  settled_at: string | null;
  frozen_reason: string | null;
}

const TAB_OPTIONS: { value: CommissionStatus; label: string; icon: typeof Clock }[] = [
  { value: 'pending', label: '待结算', icon: Clock },
  { value: 'settled', label: '已结算', icon: CheckCircle2 },
  { value: 'frozen', label: '冻结审核', icon: ShieldAlert },
  { value: 'refunded_cancelled', label: '被取消', icon: XCircle },
];

function formatDate(iso: string | null): string {
  if (!iso) return '-';
  const d = new Date(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
}

function formatCountdown(availableAt: string | null): string {
  if (!availableAt) return '';
  const diff = new Date(availableAt).getTime() - Date.now();
  if (diff <= 0) return '即将结算';
  const hours = Math.floor(diff / (60 * 60 * 1000));
  if (hours >= 24) return `${Math.floor(hours / 24)} 天后结算`;
  return `${hours} 小时后结算`;
}

export function CommissionPanelV32() {
  const [summary, setSummary] = useState<CommissionSummary | null>(null);
  const [activeTab, setActiveTab] = useState<CommissionStatus>('pending');
  const [records, setRecords] = useState<CommissionRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);

  // 拉取汇总
  useEffect(() => {
    (async () => {
      try {
        const res = await authFetch('/api/wallet/commissions/summary');
        if (res.ok) {
          const data = await res.json();
          setSummary(data);
        }
      } catch (e) {
        console.error('load commission summary failed:', e);
      }
    })();
  }, []);

  // 拉取明细
  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const res = await authFetch(`/api/wallet/commissions?status=${activeTab}&page=${page}&limit=20`);
        if (res.ok) {
          const data = await res.json();
          setRecords(data.records || []);
        }
      } catch (e) {
        console.error('load commission list failed:', e);
      } finally {
        setLoading(false);
      }
    })();
  }, [activeTab, page]);

  return (
    <div className="space-y-4 p-4">
      <div>
        <h2 className="text-xl font-semibold">推广服务费</h2>
        <p className="text-sm text-muted-foreground mt-1">
          客户成交后产生服务收益 · T+3 自动结算
        </p>
      </div>

      {/* 顶部 3 张卡片 */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <SummaryCard
          title="待结算"
          value={summary?.pending_yuan || 0}
          count={summary?.pending_count || 0}
          icon={Clock}
          iconClass="text-amber-500"
          hint="T+3 观察期后自动到账"
        />
        <SummaryCard
          title="本月新增"
          value={summary?.this_month_yuan || 0}
          icon={Coins}
          iconClass="text-blue-500"
          hint="当月所有新增推广费"
        />
        <SummaryCard
          title="累计已结算"
          value={summary?.settled_yuan || 0}
          count={summary?.settled_count || 0}
          icon={CheckCircle2}
          iconClass="text-emerald-500"
          hint="已到账可使用/提现"
        />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b overflow-x-auto">
        {TAB_OPTIONS.map(({ value, label, icon: Icon }) => {
          const count = summary
            ? summary[`${value === 'refunded_cancelled' ? 'cancelled' : value}_count` as keyof CommissionSummary]
            : undefined;
          return (
            <button
              key={value}
              onClick={() => { setActiveTab(value); setPage(1); }}
              className={`flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 transition-colors whitespace-nowrap ${
                activeTab === value
                  ? 'border-primary text-foreground font-medium'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {typeof count === 'number' && count > 0 && (
                <span className="text-[10px] bg-muted px-1.5 py-0.5 rounded">{count}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* 列表 */}
      <Card>
        <CardContent className="p-0 divide-y">
          {loading ? (
            <div className="flex items-center justify-center py-10 text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              加载中...
            </div>
          ) : records.length === 0 ? (
            <div className="text-center py-10 text-sm text-muted-foreground">
              暂无记录
            </div>
          ) : (
            records.map((r) => <RecordRow key={r.id} record={r} />)
          )}
        </CardContent>
      </Card>

      {/* 分页 */}
      {records.length === 20 && (
        <div className="flex justify-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 1}
            onClick={() => setPage(p => Math.max(1, p - 1))}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage(p => p + 1)}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}

function SummaryCard({
  title, value, count, icon: Icon, iconClass, hint,
}: {
  title: string; value: number; count?: number;
  icon: typeof Coins; iconClass: string; hint: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-muted-foreground">{title}</span>
          <Icon className={`h-4 w-4 ${iconClass}`} />
        </div>
        <div className="text-2xl font-semibold">¥{value.toFixed(2)}</div>
        {typeof count === 'number' && (
          <div className="text-xs text-muted-foreground mt-1">{count} 笔</div>
        )}
        <div className="text-[11px] text-muted-foreground mt-2">{hint}</div>
      </CardContent>
    </Card>
  );
}

function RecordRow({ record: r }: { record: CommissionRecord }) {
  const levelLabel = r.level === 1 ? '直推收益' : '间推收益';
  const countdown = r.status === 'pending' ? formatCountdown(r.available_at) : '';

  const statusBadge = (() => {
    switch (r.status) {
      case 'pending':
        return <Badge variant="outline" className="text-amber-600 border-amber-500/40">待结算</Badge>;
      case 'settled':
        return <Badge variant="outline" className="text-emerald-600 border-emerald-500/40">已到账</Badge>;
      case 'frozen':
        return <Badge variant="outline" className="text-red-600 border-red-500/40">冻结审核</Badge>;
      case 'refunded_cancelled':
        return <Badge variant="outline" className="text-muted-foreground">已取消</Badge>;
      case 'refund_clawback':
        return <Badge variant="outline" className="text-orange-600 border-orange-500/40">退款追回</Badge>;
      default:
        // [UI 审计 P1] 未知状态 fallback "未知状态" · 不裸露后端英文枚举
        return <Badge variant="outline">未知状态</Badge>;
    }
  })();

  return (
    <div className="flex items-center justify-between gap-3 p-4">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium">{r.source_user_name || '匿名用户'}</span>
          <span className="text-xs text-muted-foreground">充值</span>
          {statusBadge}
          <span className="text-[11px] text-muted-foreground">{levelLabel}</span>
        </div>
        <div className="text-xs text-muted-foreground mt-1">
          {formatDate(r.created_at)}
          {countdown && ` · ${countdown}`}
          {r.frozen_reason && ` · ${r.frozen_reason}`}
        </div>
      </div>
      <div className="text-right shrink-0">
        <div className={`text-base font-semibold ${r.status === 'refunded_cancelled' ? 'line-through text-muted-foreground' : 'text-emerald-600'}`}>
          +¥{r.amount_yuan.toFixed(2)}
        </div>
      </div>
    </div>
  );
}
