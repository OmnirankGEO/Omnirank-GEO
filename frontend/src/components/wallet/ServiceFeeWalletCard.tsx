/**
 * V3.3.1 服务费钱包卡片(仅 L2 显示)
 *
 * 显示:
 *   - 待结算冻结(pending)
 *   - 可处理余额(settled)
 *   - 历史已转换(converted)
 *   - 历史已提现(withdrawn)
 *   - 待清算债务(debt)红色提示
 * 三按钮:
 *   - [转换为充值积分 +20%]
 *   - [申请人工提现]
 *   - [查看明细]
 *
 * 关联:
 * - 决策书 §11.2
 * - IDENTITY_DECISIONS_LOCK Q17-Q22 / Q12-Q16
 */

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { AlertTriangle, Wallet, ArrowRight, FileText } from 'lucide-react';
import {
  fetchServiceFeeBalance,
  type ServiceFeeBalanceResponse,
} from '@/lib/serviceFeeApi';
import { ConvertToPointsDialog } from './ConvertToPointsDialog';
import { WithdrawalRequestDialog } from './WithdrawalRequestDialog';
import { useNavigate } from 'react-router-dom';

export function ServiceFeeWalletCard() {
  const [data, setData] = useState<ServiceFeeBalanceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [convertOpen, setConvertOpen] = useState(false);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const navigate = useNavigate();

  const refresh = async () => {
    setLoading(true);
    const r = await fetchServiceFeeBalance();
    setData(r);
    setLoading(false);
  };

  useEffect(() => { refresh(); }, []);

  // 仅 L2 显示 · L1/L0 严禁见"服务费"(§3.1.2)
  if (!data?.success || !data.is_l2) return null;

  const sf = data.data;
  const conv = data.conversion;
  const wd = data.withdrawal;

  return (
    <Card className="border-amber-200/40 dark:border-amber-800/40">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <div className="flex items-center gap-2">
          <Wallet className="h-4 w-4 text-amber-600 dark:text-amber-400" />
          <span className="font-medium">渠道服务费(待结算)</span>
          {data.agent_tier && (
            <Badge variant="outline" className="ml-1 capitalize">{data.agent_tier}</Badge>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate('/wallet/service-fee-history')}
        >
          <FileText className="mr-1 h-3.5 w-3.5" />明细
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <div className="text-muted-foreground text-xs">退款期内 · 暂不可提</div>
            <div className="font-mono text-base">¥{sf.pending_yuan.toFixed(2)}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">可处理余额</div>
            <div className="font-mono text-base text-emerald-700 dark:text-emerald-400">
              ¥{sf.settled_yuan.toFixed(2)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">历史已转换</div>
            <div className="font-mono text-sm text-muted-foreground">¥{sf.converted_yuan.toFixed(2)}</div>
          </div>
          <div>
            <div className="text-muted-foreground text-xs">历史已提现</div>
            <div className="font-mono text-sm text-muted-foreground">¥{sf.withdrawn_yuan.toFixed(2)}</div>
          </div>
        </div>

        {sf.debt_yuan > 0 && (
          <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs dark:border-red-800 dark:bg-red-950/40">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 text-red-500" />
              <div>
                <div className="font-medium text-red-700 dark:text-red-300">
                  待清算服务费 ¥{sf.debt_yuan.toFixed(2)}
                </div>
                <div className="text-red-600/80 dark:text-red-300/80 mt-0.5">
                  异常退款追索 · 后续服务费/消费将自动抵扣 · 详见明细
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="flex flex-wrap gap-2 pt-1">
          <Button
            size="sm"
            disabled={
              !conv?.enabled || sf.settled_yuan <= 0 || loading
            }
            onClick={() => setConvertOpen(true)}
          >
            转换为充值算力
            {conv?.bonus_rate && (
              <Badge variant="secondary" className="ml-2">
                +{Math.round(conv.bonus_rate * 100)}%
              </Badge>
            )}
            <ArrowRight className="ml-1 h-3.5 w-3.5" />
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!wd?.enabled || sf.settled_yuan < (wd?.min_amount_yuan ?? 100) || loading}
            onClick={() => setWithdrawOpen(true)}
          >
            申请人工提现
          </Button>
        </div>

        {!data.kyc_passed && (
          <div className="text-xs text-muted-foreground">
            ⓘ 提现需通过实名认证(KYC)+ 绑定实名收款账户
          </div>
        )}
      </CardContent>

      <ConvertToPointsDialog
        open={convertOpen}
        onOpenChange={setConvertOpen}
        settledYuan={sf.settled_yuan}
        bonusRate={conv?.bonus_rate ?? 0.2}
        monthlyAvailable={conv?.monthly_available_yuan ?? 0}
        onSuccess={refresh}
      />
      <WithdrawalRequestDialog
        open={withdrawOpen}
        onOpenChange={setWithdrawOpen}
        settledYuan={sf.settled_yuan}
        minAmount={wd?.min_amount_yuan ?? 100}
        kycPassed={!!data.kyc_passed}
        bankVerified={!!data.bank_account_verified}
        onSuccess={refresh}
      />
    </Card>
  );
}
