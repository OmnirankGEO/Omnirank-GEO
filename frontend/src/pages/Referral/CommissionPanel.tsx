/**
 * CommissionPanel - 佣金明细面板
 * 包含佣金列表、月度统计、佣金转积分功能
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
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ArrowRightLeft,
  Coins,
  TrendingUp,
  Loader2,
  AlertCircle,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { authFetch } from "@/lib/api";

// ========== 类型定义 ==========

interface CommissionRecord {
  id: number;
  time: string;              // 由 created_at 映射
  referred_user: string;     // 由 referred_name 映射
  recharge_amount: number;   // 由 trigger_amount_cents / 100 映射 (元)
  commission_paid: number;   // = commission_yuan
  commission_bonus: number;  // = commission_yuan * EXPANSION_RATE (0.2)
}

// [CTO-15.23 2026-05-20] 后端 api/referral_api.py:230-243 返 raw DB row
// 字段:id / created_at / referred_name / trigger_amount_cents / commission_yuan / status / ...
// 需在 fetch 层映射到 CommissionRecord · 否则 r.time.slice() 等访问会崩
interface RawCommissionRow {
  id: number;
  created_at: string;
  referred_name?: string | null;
  referred_id?: number;
  trigger_amount_cents: number;
  commission_yuan: number | string;  // psycopg2 NUMERIC 可能转 string
  status: string;
  level?: number;
  commission_rate?: number | string;
}

// 后端佣金膨胀比(跟 api/referral_api.py EXPANSION_RATE 同步 · 20%)
const EXPANSION_RATE = 0.2;

interface MonthlySummary {
  month: string;
  total_paid: number;
  total_bonus: number;
  count: number;
}

// ========== 佣金转积分对话框 ==========

function ConvertDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [amount, setAmount] = useState("");
  const [converting, setConverting] = useState(false);
  const [result, setResult] = useState<{
    success: boolean;
    message: string;
  } | null>(null);

  const numAmount = parseFloat(amount) || 0;
  const paidPortion = numAmount;
  const bonusPortion = Math.round(numAmount * 0.2 * 100) / 100;

  const handleConvert = async () => {
    if (numAmount <= 0) return;
    setConverting(true);
    setResult(null);
    try {
      const res = await authFetch("/api/referral/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: numAmount }),
      });
      if (res.ok) {
        const data = await res.json();
        setResult({ success: true, message: data.message || "转换成功" });
        setAmount("");
      } else if (res.status === 404) {
        setResult({ success: false, message: "此功能即将上线" });
      } else {
        const err = await res.json().catch(() => ({}));
        setResult({
          success: false,
          message: (err as Record<string, string>).detail || "转换失败，请稍后重试",
        });
      }
    } catch {
      setResult({ success: false, message: "网络错误，请稍后重试" });
    } finally {
      setConverting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md bg-card border-border">
        <DialogHeader>
          <DialogTitle className="text-foreground">收益转算力</DialogTitle>
          <DialogDescription className="text-muted-foreground">
            将服务收益转入算力钱包。收益本金转为充值算力,额外膨胀 20% 转为赠送算力。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <label className="text-sm font-medium text-foreground">
              转换金额 (元)
            </label>
            <Input
              type="number"
              min="0"
              step="0.01"
              placeholder="请输入转换金额"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className="bg-secondary border-border"
            />
          </div>

          {numAmount > 0 && (
            <div className="rounded-xl bg-muted p-4 space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">本金(充值算力)</span>
                <span className="font-medium text-foreground">
                  +{paidPortion.toFixed(2)} 元
                </span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">膨胀 20%(赠送算力)</span>
                <span className="font-medium text-emerald-400">
                  +{bonusPortion.toFixed(2)} 元
                </span>
              </div>
              <div className="border-t border-border pt-2 flex items-center justify-between text-sm">
                <span className="text-muted-foreground">合计到账算力</span>
                <span className="font-semibold text-foreground">
                  {Math.round((paidPortion + bonusPortion) * 130)} 算力
                </span>
              </div>
            </div>
          )}

          {result && (
            <div
              className={cn(
                "rounded-lg p-3 text-sm",
                result.success
                  ? "bg-emerald-500/10 text-emerald-400"
                  : "bg-destructive/10 text-destructive"
              )}
            >
              {result.message}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            className="rounded-lg"
          >
            取消
          </Button>
          <Button
            onClick={handleConvert}
            disabled={numAmount <= 0 || converting}
            className="bg-foreground text-background hover:bg-foreground/90 rounded-lg"
          >
            {converting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            确认转换
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ========== 月度统计卡片 ==========

function MonthlySummaryCards({
  records,
}: {
  records: CommissionRecord[];
}) {
  // 按月聚合
  const summaryMap = new Map<string, MonthlySummary>();
  records.forEach((r) => {
    const month = r.time.slice(0, 7); // YYYY-MM
    const existing = summaryMap.get(month);
    if (existing) {
      existing.total_paid += r.commission_paid;
      existing.total_bonus += r.commission_bonus;
      existing.count += 1;
    } else {
      summaryMap.set(month, {
        month,
        total_paid: r.commission_paid,
        total_bonus: r.commission_bonus,
        count: 1,
      });
    }
  });

  const summaries = Array.from(summaryMap.values())
    .sort((a, b) => b.month.localeCompare(a.month))
    .slice(0, 3);

  if (summaries.length === 0) {
    return null;
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
      {summaries.map((s) => (
        <Card key={s.month} className="bg-card border-border rounded-xl">
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground mb-1">{s.month}</p>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-semibold text-foreground">
                {s.total_paid.toFixed(2)}
              </span>
              <span className="text-xs text-muted-foreground">本金</span>
              <span className="text-sm text-emerald-400">
                +{s.total_bonus.toFixed(2)}
              </span>
              <span className="text-xs text-muted-foreground">膨胀</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {s.count} 笔收益
            </p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ========== 主组件 ==========

export function CommissionPanel() {
  const [records, setRecords] = useState<CommissionRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [convertOpen, setConvertOpen] = useState(false);

  const limit = 20;
  const totalPages = Math.ceil(total / limit);

  const fetchCommissions = useCallback(async (p: number) => {
    setLoading(true);
    setError(null);
    try {
      // [CTO-15.23 2026-05-20 follow-up] 后端 api/referral_api.py:222 签名 `limit + offset`
      // 之前传 page=${p}&limit=20 · 后端忽略 page · 永远拿第一页
      const offset = (p - 1) * limit;
      const res = await authFetch(
        `/api/referral/commissions?offset=${offset}&limit=${limit}`
      );
      if (res.status === 404) {
        setRecords([]);
        setTotal(0);
        setError("placeholder");
        return;
      }
      if (!res.ok) {
        throw new Error("加载收益数据失败");
      }
      // [CTO-15.23 2026-05-20] 后端 api/referral_api.py:244 返 {success, data: [...]} 嵌套 envelope
      // raw row 字段 = id / created_at / referred_name / trigger_amount_cents / commission_yuan / ...
      // 需在 fetch 层映射成 CommissionRecord(time/referred_user/recharge_amount/commission_paid/commission_bonus)
      // 否则下游 MonthlySummaryCards `r.time.slice(0,7)` 在真有数据时崩
      const json = await res.json();
      if (json?.success) {
        const raw: RawCommissionRow[] = Array.isArray(json.data) ? json.data : [];
        const mapped: CommissionRecord[] = raw.map(r => {
          const yuan = typeof r.commission_yuan === 'string'
            ? parseFloat(r.commission_yuan)
            : (r.commission_yuan ?? 0);
          return {
            id: r.id,
            time: r.created_at,
            referred_user: r.referred_name || `用户编号 ${r.referred_id ?? '?'}`,
            recharge_amount: (r.trigger_amount_cents ?? 0) / 100,
            commission_paid: yuan,
            commission_bonus: yuan * EXPANSION_RATE,
          };
        });
        setRecords(mapped);
        // 后端没返 total · 临时用当前页 length(分页准确度低 · 长期 BE 补 total)
        // 如果返满 limit 条 · 推断"可能还有下一页"· 假定 total ≥ p*limit
        const hasMore = mapped.length === limit;
        setTotal(hasMore ? p * limit + 1 : (p - 1) * limit + mapped.length);
      } else {
        setRecords([]);
        setTotal(0);
      }
    } catch (err: unknown) {
      if (error !== "placeholder") {
        setError(
          err instanceof Error ? err.message : "加载失败，请稍后重试"
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCommissions(page);
  }, [page, fetchCommissions]);

  // 占位 UI
  if (error === "placeholder") {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
          <Coins className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-medium text-foreground mb-2">
          收益明细即将上线
        </h3>
        <p className="text-sm text-muted-foreground max-w-sm">
          收益追踪功能正在开发中，上线后您可以查看每笔推荐收益的详细信息。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 月度统计 */}
      <MonthlySummaryCards records={records} />

      {/* 操作栏 */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-foreground">
          收益记录 ({total} 条)
        </h3>
        <Button
          onClick={() => setConvertOpen(true)}
          className="bg-foreground text-background hover:bg-foreground/90 rounded-lg"
        >
          <ArrowRightLeft className="mr-2 h-4 w-4" />
          收益转算力
        </Button>
      </div>

      {/* 佣金表格 */}
      <Card className="bg-card border-border rounded-xl overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              <TableHead className="text-muted-foreground">时间</TableHead>
              <TableHead className="text-muted-foreground">推荐用户</TableHead>
              <TableHead className="text-muted-foreground text-right">
                充值金额
              </TableHead>
              <TableHead className="text-muted-foreground text-right">
                收益(本金)
              </TableHead>
              <TableHead className="text-muted-foreground text-right">
                膨胀(赠送算力)
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-12">
                  <Loader2 className="mx-auto h-6 w-6 animate-spin text-muted-foreground" />
                </TableCell>
              </TableRow>
            ) : records.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="text-center py-12 text-muted-foreground"
                >
                  暂无收益记录
                </TableCell>
              </TableRow>
            ) : (
              records.map((r) => (
                <TableRow key={r.id} className="border-border">
                  <TableCell className="text-sm text-foreground">
                    {r.time}
                  </TableCell>
                  <TableCell className="text-sm text-foreground">
                    {r.referred_user}
                  </TableCell>
                  <TableCell className="text-sm text-foreground text-right">
                    {r.recharge_amount.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-sm text-foreground text-right font-medium">
                    +{r.commission_paid.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-sm text-right">
                    <span className="text-emerald-400 font-medium">
                      +{r.commission_bonus.toFixed(2)}
                    </span>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </Card>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
            className="rounded-lg"
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="text-sm text-muted-foreground">
            {page} / {totalPages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="rounded-lg"
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      )}

      {/* 错误提示 */}
      {error && error !== "placeholder" && (
        <div className="flex items-center gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {/* 转换对话框 */}
      <ConvertDialog open={convertOpen} onOpenChange={setConvertOpen} />
    </div>
  );
}
