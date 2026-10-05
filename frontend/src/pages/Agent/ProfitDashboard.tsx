/**
 * ProfitDashboard - 代理经营总览
 * 代理专属 · V3.5 工厂经营盘子(进货 → 卖货 → 结算)+ 自身工具消耗
 * [2026-05-30 D1] 代理 18%/3% 推荐佣金已废(纯工厂模式取代)· 删推荐返佣辅区
 */
import { useState, useEffect, useCallback } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  ArrowRight,
  Loader2,
  AlertCircle,
  Wallet,
  BarChart3,
  ArrowDownRight,
  TrendingUp,
  Boxes,
  Users,
  Package,
  CheckCircle2,
  Circle,
  Lightbulb,
  Receipt,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { authFetch } from "@/lib/api";
import { AgentLevelGate } from "@/components/AgentLevelGate";
import { useNavigate } from "react-router-dom";

// ========== 类型定义 ==========

interface WalletTransaction {
  id: number;
  feature_code: string;
  feature_name: string;
  points_deducted: number;
  created_at: string;
  // [GAPS#3 2026-06-04] 后端 /api/wallet/transactions 已返回这些字段(point_transactions.* + brands JOIN)· 前端读上填表
  description?: string | null;   // 消耗内容
  point_type?: string | null;    // 扣费算力类型
  brand_name?: string | null;    // 关联客户(brand_id LEFT JOIN brands.name)
}

// 扣费算力类型 → 人话(王姐口径 · 禁工程词)
const POINT_TYPE_LABEL: Record<string, string> = {
  paid: "充值算力",
  bonus: "赠送算力",
  commission: "服务收益",
  frozen: "冻结中",
};

// ========== 经营损益:KPI 卡 + 运算符 + 明细行 ==========

function PnlCard({
  label, value, tone, sub, emphasize,
}: { label: string; value: number; tone?: 'income' | 'cost' | 'net'; sub?: string; emphasize?: boolean }) {
  const valueColor =
    tone === 'income' || tone === 'net' ? 'text-emerald-500'
    : tone === 'cost' ? 'text-orange-500'
    : 'text-foreground';
  const prefix = tone === 'cost' ? '−' : '';
  return (
    <div className={cn(
      'flex-1 rounded-xl border p-3',
      emphasize ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-border bg-muted/40',
    )}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn('mt-1 text-lg font-bold tabular-nums', valueColor)}>
        {prefix}¥{(value ?? 0).toFixed(2)}
      </p>
      {sub && <p className="mt-0.5 text-[11px] text-muted-foreground">{sub}</p>}
    </div>
  );
}

function PnlOp({ char }: { char: string }) {
  return (
    <div className="flex shrink-0 items-center justify-center px-1 text-base font-semibold text-muted-foreground">
      {char}
    </div>
  );
}

function Line({ k, v, strong }: { k: string; v: string; strong?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted-foreground">{k}</span>
      <span className={cn('tabular-nums', strong ? 'font-semibold text-foreground' : 'text-foreground')}>{v}</span>
    </div>
  );
}

function StatusRow({ icon: Icon, text, tone }: { icon: any; text: string; tone?: 'good' | 'warn' | 'muted' }) {
  const color = tone === 'good' ? 'text-emerald-500' : tone === 'warn' ? 'text-orange-500' : 'text-muted-foreground';
  return (
    <div className="flex items-center gap-2 text-sm">
      <Icon className={cn('h-4 w-4 shrink-0', color)} />
      <span className="text-foreground/90">{text}</span>
    </div>
  );
}

// ========== 内部主组件 ==========

function ProfitDashboardInner() {
  const navigate = useNavigate();
  const [biz, setBiz] = useState<any>(null);  // V3.5 经营总览(/api/agent/finance/overview)
  const [recentCosts, setRecentCosts] = useState<WalletTransaction[]>([]);
  const [totalConsumed, setTotalConsumed] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // 经营总览(V3.5 主线)+ 自身工具消耗 · 各自独立成败
      const [bizRes, costRes] = await Promise.all([
        authFetch("/api/agent/finance/overview"),
        authFetch("/api/wallet/transactions?type=consume&page=1&limit=5"),
      ]);

      if (bizRes.ok) {
        try { setBiz(await bizRes.json()); } catch { setBiz(null); }
      }

      if (costRes.ok) {
        const costData = await costRes.json();
        setRecentCosts(costData.transactions || []);
        setTotalConsumed(costData.total_points || 0);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-destructive/10 p-4 text-sm text-destructive m-6">
        <AlertCircle className="h-4 w-4 shrink-0" />
        {error}
      </div>
    );
  }

  const costYuan = totalConsumed / 130;
  // 本月状态(纯展示 · 由现有 biz 数据派生)
  const orders = biz?.pnl?.orders ?? 0;
  const availPoints = (biz?.inventory?.paid_points ?? 0) + (biz?.inventory?.bonus_points ?? 0);

  return (
    <div className="p-4 sm:p-6 max-w-7xl mx-auto space-y-6 pb-24 lg:pb-6">
      {/* 经营看板头部 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted">
              <BarChart3 className="h-5 w-5" />
            </div>
            <h1 className="text-2xl font-bold text-foreground">经营总览</h1>
            {biz?.label && <Badge variant="outline" className="ml-0.5 shrink-0">{biz.label}</Badge>}
          </div>
          <div className="flex items-center gap-1.5 pl-0.5 text-xs text-muted-foreground">
            {['进货', '卖货', '结算'].map((s, i) => (
              <span key={s} className="flex items-center gap-1.5">
                {i > 0 && <ArrowRight className="h-3 w-3 opacity-40" />}
                <span className="rounded-md bg-muted px-2 py-0.5 text-foreground/70">{s}</span>
              </span>
            ))}
          </div>
        </div>
        <Button
          onClick={() => navigate("/pricing")}
          className="shrink-0 bg-foreground text-background hover:bg-foreground/90 rounded-lg"
        >
          <Wallet className="mr-2 h-4 w-4" />
          去报价方案
          <ArrowRight className="ml-2 h-4 w-4" />
        </Button>
      </div>

      {/* 经营数据兜底空态(端点异常/无数据时不留白) */}
      {!biz && (
        <Card className="bg-card border-border rounded-xl">
          <CardContent className="py-10 text-center">
            <BarChart3 className="h-8 w-8 text-muted-foreground mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">经营数据暂时无法加载，或你还没有客户成交记录</p>
          </CardContent>
        </Card>
      )}

      {/* V3.5 经营损益 P&L(主线)— 横向公式 KPI */}
      {biz?.pnl && (
        <Card className="bg-card border-border rounded-xl">
          <CardHeader className="pb-3">
            <CardTitle className="text-base text-foreground flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-emerald-500" /> 经营损益
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              客户付款 − 进货成本 − 平台费 − 代扣税 = 你的净收益
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-col gap-2 lg:flex-row lg:items-stretch lg:gap-2">
              <PnlCard label="客户付款(GMV)" value={biz.pnl.gmv} tone="income" sub={`本月成交 ${biz.pnl.orders ?? 0} 笔`} />
              <PnlOp char="−" />
              <PnlCard label="进货成本" value={biz.pnl.factory_cost} tone="cost" />
              <PnlOp char="−" />
              <PnlCard
                label="平台费"
                value={biz.pnl.platform_fee}
                tone="cost"
                sub={biz.pnl.gmv > 0 ? `${Math.round((biz.pnl.platform_fee / biz.pnl.gmv) * 100)}%` : undefined}
              />
              <PnlOp char="−" />
              <PnlCard label="代扣税" value={biz.pnl.tax} tone="cost" />
              <PnlOp char="=" />
              <PnlCard label="我的净收益" value={biz.pnl.net_margin} tone="net" emphasize />
            </div>
          </CardContent>
        </Card>
      )}

      {/* 业务卡 ×3 + 本月状态辅助卡 */}
      {biz && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Card className="bg-card border-border rounded-xl">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm"><Wallet className="h-4 w-4 text-muted-foreground" />提现结算</CardTitle>
            </CardHeader>
            <CardContent className="text-sm space-y-1.5">
              <Line k="冻结中(T+3)" v={`¥${(biz.settlement?.frozen ?? 0).toFixed(2)}`} />
              <Line k="可提现" v={`¥${(biz.settlement?.available ?? 0).toFixed(2)}`} strong />
              <Line k="申请中" v={`¥${(biz.settlement?.pending ?? 0).toFixed(2)}`} />
              <Line k="已打款" v={`¥${(biz.settlement?.paid ?? 0).toFixed(2)}`} />
            </CardContent>
          </Card>
          <Card className="bg-card border-border rounded-xl">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm"><Boxes className="h-4 w-4 text-muted-foreground" />算力库存</CardTitle>
            </CardHeader>
            <CardContent className="text-sm space-y-1.5">
              <Line k="可用库存" v={`${availPoints.toLocaleString()} 算力`} strong />
              <Line k="累计进货" v={`${(biz.inventory?.total_purchased_points ?? 0).toLocaleString()} 算力`} />
              <Line k="累计开算力" v={`${(biz.inventory?.total_allocated_points ?? 0).toLocaleString()} 算力`} />
            </CardContent>
          </Card>
          <Card className="bg-card border-border rounded-xl">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm"><Users className="h-4 w-4 text-muted-foreground" />我的客户</CardTitle>
            </CardHeader>
            <CardContent className="text-sm space-y-1.5">
              <Line k="客户数" v={`${biz.customers?.count ?? 0}`} strong />
              <Line k="预存未消耗算力" v={`${(biz.customers?.credit_outstanding_points ?? 0).toLocaleString()} 算力`} />
            </CardContent>
          </Card>
          {/* 本月状态(提示区 · 派生 · 不抢主内容) */}
          <Card className="bg-muted/30 border-border rounded-xl">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm"><Lightbulb className="h-4 w-4 text-emerald-500" />本月状态</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2.5">
              {orders > 0
                ? <StatusRow icon={CheckCircle2} tone="good" text={`本月成交 ${orders} 笔`} />
                : <StatusRow icon={Circle} tone="muted" text="本期暂无成交" />}
              {availPoints > 0
                ? <StatusRow icon={CheckCircle2} tone="good" text="库存充足" />
                : <StatusRow icon={Package} tone="warn" text="暂无库存 · 建议进货" />}
              <StatusRow icon={ArrowRight} tone="muted" text={orders > 0 ? "关注客户消耗与结算" : "可先去报价方案"} />
            </CardContent>
          </Card>
        </div>
      )}

      {/* 我的消耗(代理自身工具成本)— 表格 */}
      <Card className="bg-card border-border rounded-xl">
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base text-foreground flex items-center gap-2">
                <ArrowDownRight className="h-4 w-4 text-orange-500" />
                我的消耗
              </CardTitle>
              <CardDescription className="text-muted-foreground mt-1">
                累计消耗 {totalConsumed.toLocaleString()} 算力 (~{costYuan.toFixed(2)} 元) · 你自己使用工具的成本(非客户进货)
              </CardDescription>
            </div>
            <Badge variant="outline" className="shrink-0">近 5 笔</Badge>
          </div>
        </CardHeader>
        <CardContent>
          {recentCosts.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <Receipt className="h-6 w-6 text-muted-foreground/60" />
              </div>
              <p className="text-sm font-medium">暂无消耗记录</p>
              <p className="mt-1 text-xs text-muted-foreground">你使用 AI 工具产生的消耗会显示在这里</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">时间</th>
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">消耗工具</th>
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">消耗内容</th>
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">消耗算力</th>
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">类型</th>
                    <th className="py-2 pr-4 font-medium whitespace-nowrap">关联客户</th>
                    <th className="py-2 font-medium whitespace-nowrap">备注</th>
                  </tr>
                </thead>
                <tbody>
                  {recentCosts.map((tx) => (
                    <tr key={tx.id} className="border-b border-border last:border-0">
                      <td className="py-2.5 pr-4 text-muted-foreground whitespace-nowrap">{new Date(tx.created_at).toLocaleDateString("zh-CN")}</td>
                      <td className="py-2.5 pr-4 text-foreground">{tx.feature_name || tx.feature_code}</td>
                      <td className="py-2.5 pr-4 text-muted-foreground">{tx.description || "—"}</td>
                      <td className="py-2.5 pr-4 font-medium text-orange-500 whitespace-nowrap tabular-nums">-{tx.points_deducted}</td>
                      <td className="py-2.5 pr-4 text-muted-foreground">{tx.point_type ? (POINT_TYPE_LABEL[tx.point_type] || tx.point_type) : "—"}</td>
                      <td className="py-2.5 pr-4 text-muted-foreground">{tx.brand_name || "—"}</td>
                      <td className="py-2.5 text-muted-foreground">—</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ========== 导出组件（带等级门控） ==========

export function ProfitDashboard() {
  return (
    <AgentLevelGate requiredLevel={1}>
      <ProfitDashboardInner />
    </AgentLevelGate>
  );
}
