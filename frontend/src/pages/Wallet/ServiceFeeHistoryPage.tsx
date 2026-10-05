/**
 * V3.3.1 服务费 8 态流水页
 *
 * 路由:/wallet/service-fee-history
 * 仅 L2 可见
 */

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { fetchServiceFeeHistory, type ServiceFeeHistoryRow } from '@/lib/serviceFeeApi';
import { useNavigate } from 'react-router-dom';

const STATUS_LABEL: Record<string, string> = {
  pending: '退款期内 · 暂不可提',
  settled: '可处理',
  converted: '已转换',
  withdraw_requested: '提现审核中',
  withdrawn: '已提现',
  cancelled: '已取消',
  clawback: '已追索',
  rejected: '提现被拒',
};

const STATUS_COLOR: Record<string, string> = {
  pending: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  settled: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  converted: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  withdraw_requested: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300',
  withdrawn: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
  cancelled: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400',
  clawback: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  rejected: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300',
};

export default function ServiceFeeHistoryPage() {
  const [rows, setRows] = useState<ServiceFeeHistoryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [filterStatus, setFilterStatus] = useState<string>('all');
  const [offset, setOffset] = useState(0);
  const navigate = useNavigate();
  const limit = 20;

  const refresh = async () => {
    setLoading(true);
    const r = await fetchServiceFeeHistory(
      filterStatus === 'all' ? undefined : filterStatus,
      limit,
      offset,
    );
    setRows(r.data);
    setTotal(r.total);
    setLoading(false);
  };

  useEffect(() => { refresh(); }, [filterStatus, offset]);

  return (
    <div className="container mx-auto max-w-4xl py-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">服务费流水</h1>
        <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>返回</Button>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <span className="text-sm text-muted-foreground">共 {total} 条</span>
          <Select value={filterStatus} onValueChange={(v) => { setFilterStatus(v); setOffset(0); }}>
            <SelectTrigger className="w-44">
              <SelectValue placeholder="筛选状态" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="pending">退款期内</SelectItem>
              <SelectItem value="settled">可处理</SelectItem>
              <SelectItem value="converted">已转换</SelectItem>
              <SelectItem value="withdraw_requested">提现审核中</SelectItem>
              <SelectItem value="withdrawn">已提现</SelectItem>
              <SelectItem value="cancelled">已取消</SelectItem>
              <SelectItem value="clawback">已追索</SelectItem>
              <SelectItem value="rejected">提现被拒</SelectItem>
            </SelectContent>
          </Select>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="py-10 text-center text-sm text-muted-foreground">加载中...</div>
          ) : rows.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">暂无数据</div>
          ) : (
            <div className="space-y-2">
              {rows.map((row) => (
                <div
                  key={row.id}
                  className="flex items-center justify-between rounded-md border px-3 py-2 hover:bg-muted/30"
                >
                  <div className="flex flex-col text-xs">
                    <span className="font-mono">
                      #{row.id} · {row.source_order_type} · 客户#{row.source_user_id}
                    </span>
                    <span className="text-muted-foreground mt-0.5">
                      {new Date(row.created_at).toLocaleString('zh-CN')}
                    </span>
                    {row.clawback_reason && (
                      <span className="text-red-500 mt-0.5">
                        追索原因:{row.clawback_reason}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="text-right text-sm">
                      <div className="font-mono">¥{row.amount_yuan.toFixed(2)}</div>
                      <div className="text-xs text-muted-foreground">
                        基数 ¥{row.net_cash_revenue_yuan?.toFixed(2)} × {(row.service_fee_rate * 100).toFixed(0)}%
                      </div>
                    </div>
                    <Badge className={STATUS_COLOR[row.status] || ''} variant="secondary">
                      {STATUS_LABEL[row.status] || row.status}
                    </Badge>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="mt-4 flex items-center justify-between text-xs">
            <Button
              size="sm" variant="ghost"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              上一页
            </Button>
            <span className="text-muted-foreground">
              {Math.min(offset + 1, total)} - {Math.min(offset + limit, total)} / {total}
            </span>
            <Button
              size="sm" variant="ghost"
              disabled={offset + limit >= total}
              onClick={() => setOffset(offset + limit)}
            >
              下一页
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
