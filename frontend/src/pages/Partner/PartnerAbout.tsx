/**
 * PartnerAbout — 合作伙伴计划介绍页
 *
 * 职责:
 *   - 读 /api/partner/about 判断 enabled
 *   - flag off: 显示"即将开放"
 *   - flag on + L0: 显示权益 + "立即申请"入口（跳 Step1）
 *   - flag on + L1+: 显示"已是代理"+ "查看已签协议" 入口
 *
 * 入口来源: CEndDrawer 抽屉设置菜单 / AgentLevelGate 跳转 / 直接访问 /partner/about
 */

import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Briefcase, Shield, Sparkles, ArrowRight, CheckCircle2, Clock, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { authFetch } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';

interface AboutResponse {
  enabled: boolean;
  message?: string;
  current_agreement_version?: string;
  benefits?: {
    l1_commission_rate: number;
    l2_commission_rate: number;
    bonus_inflation_rate: number;
    features: string[];
  };
  requirements?: {
    real_name_verify: boolean;
    id_card_required: boolean;
    agreement_required: boolean;
    review_sla_days: number;
    monthly_apply_limit: number;
  };
}

export default function PartnerAbout() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const isAgent = (user?.agent_level ?? 0) >= 1;

  const [data, setData] = useState<AboutResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [applyStatus, setApplyStatus] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const resp = await authFetch('/api/partner/about');
        if (resp.ok) setData((await resp.json()) as AboutResponse);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (!data?.enabled || isAgent) return;
    (async () => {
      try {
        const resp = await authFetch('/api/partner/apply/status');
        if (resp.ok) {
          const s = await resp.json();
          if (s?.latest_application?.status) {
            setApplyStatus(s.latest_application.status);
          }
        }
      } catch { /* ignore */ }
    })();
  }, [data?.enabled, isAgent]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // flag off
  if (!data?.enabled) {
    return (
      <div className="max-w-2xl mx-auto p-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Clock className="h-5 w-5 text-muted-foreground" />
              合作伙伴计划即将开放
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>{data?.message || '合作伙伴计划即将开放，敬请期待。'}</p>
            <p>已是服务商的用户身份保留不变，服务收益结算照常进行。</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-6">
      {/* 头部 */}
      <div className="flex items-start gap-4">
        <div className="h-12 w-12 rounded-xl bg-foreground/5 border border-border flex items-center justify-center">
          <Briefcase className="h-6 w-6 text-foreground" />
        </div>
        <div className="flex-1">
          <h1 className="text-xl font-semibold text-foreground">合作伙伴计划</h1>
          <p className="text-sm text-muted-foreground mt-1">
            OmniRank 官方服务商 · 身份证实名审核 · 签电子协议即生效
          </p>
        </div>
      </div>

      {/* 已是代理 */}
      {isAgent && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <CheckCircle2 className="h-5 w-5 text-emerald-400" />
              您已是服务商
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">收益结算、白标、CRM 已启用。</p>
            <div className="flex gap-2">
              <Button asChild size="sm" variant="outline">
                <Link to="/partner/status">查看申请记录</Link>
              </Button>
              <Button asChild size="sm" variant="outline">
                <Link to={`/partner/agreement/${data.current_agreement_version || 'v2.0'}`}>
                  查看协议
                </Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 权益 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Sparkles className="h-5 w-5 text-amber-400" />
            服务商权益
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <Stat
              label="客户售价"
              value="自定"
              hint="售价由你定 · 丰俭由人"
            />
            <Stat
              label="服务收益"
              value="可提现"
              hint="客户成交后进收益结算"
            />
            <Stat
              label="膨胀奖励"
              value={`+${((data.benefits?.bonus_inflation_rate || 0.20) * 100).toFixed(0)}%`}
              hint="收益转赠送额外加赠"
            />
            <Stat label="审核时效" value={`≤ ${data.requirements?.review_sla_days || 3} 天`} hint="AI + 人工" />
          </div>
          <div className="pt-2 border-t border-border">
            <p className="text-xs text-muted-foreground mb-2">工具特权：</p>
            <div className="flex flex-wrap gap-2">
              {(data.benefits?.features || []).map(f => (
                <span
                  key={f}
                  className="text-xs px-2.5 py-1 rounded-full bg-foreground/5 border border-border text-foreground"
                >
                  {f}
                </span>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 申请要求 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Shield className="h-5 w-5 text-foreground" />
            申请要求
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <Requirement>身份证实名认证（正面、反面照片）</Requirement>
          <Requirement>签署《服务商申请协议 {data.current_agreement_version || 'v2.0'}》</Requirement>
          <Requirement>完成表单填写（推广场景、预期规模）</Requirement>
          <Requirement>
            30 天内被拒 / 撤回限 {data.requirements?.monthly_apply_limit || 3} 次
          </Requirement>
        </CardContent>
      </Card>

      {/* 申请中提示 */}
      {applyStatus === 'pending' || applyStatus === 'manual_review' ? (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardContent className="py-4 flex items-center gap-3">
            <Clock className="h-5 w-5 text-amber-400" />
            <div>
              <p className="text-sm font-medium text-foreground">您有审核中的申请</p>
              <p className="text-xs text-muted-foreground mt-0.5">提交后请留意审核结果通知</p>
            </div>
            <Button asChild size="sm" variant="outline" className="ml-auto">
              <Link to="/partner/status">查看状态</Link>
            </Button>
          </CardContent>
        </Card>
      ) : null}

      {/* 操作按钮 */}
      {!isAgent && applyStatus !== 'pending' && applyStatus !== 'manual_review' && (
        <div className="flex gap-3">
          <Button
            className="flex-1"
            onClick={() => navigate('/partner/apply')}
          >
            立即申请
            <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
          <Button asChild variant="outline">
            <Link to={`/partner/agreement/${data.current_agreement_version || 'v2.0'}`}>
              先看协议
            </Link>
          </Button>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-border rounded-lg p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold text-foreground mt-1">{value}</div>
      {hint && <div className="text-[10px] text-muted-foreground mt-0.5">{hint}</div>}
    </div>
  );
}

function Requirement({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <CheckCircle2 className="h-4 w-4 text-emerald-400 mt-0.5 shrink-0" />
      <span className="text-sm text-foreground">{children}</span>
    </div>
  );
}
