/**
 * AgreementGate — 服务商经营功能协议签约门控(统一拦截态)
 *
 * 包裹需要"已签服务商经营功能协议"才能使用的页面(获客推广 / 客户售价 / 对外品牌 / 收益结算)。
 * 明确区分 5 种状态,杜绝把"未签署"误报成"加载失败,请重试":
 *   1. loading           正在检查协议状态…(不闪错误)
 *   2. not_agent         当前账号暂未开通代理功能
 *   3. agreement_required请先签署服务商经营功能协议 → 去签署(带 returnTo 自动回原页)
 *   4. signed            正常渲染 children(此时才挂载子页 · 子页 useEffect/接口才会跑)
 *   5. error             页面暂时打不开(仅真 5xx / 网络失败)
 *
 * admin 豁免(对齐后端 auth/agreement_gate.py:require_signed_agreement)。
 */
import { ReactNode, useCallback, useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { useAuth } from '@/context/AuthContext';
import { agreementApi } from '@/lib/v35w3Api';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { FileSignature, Loader2, AlertTriangle, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react';

type GateState = 'loading' | 'not_agent' | 'agreement_required' | 'signed' | 'error';

export function AgreementGate({ children }: { children: ReactNode }) {
  const navigate = useEmbeddedNavigate();
  const location = useLocation();
  const { user, isLoading: authLoading } = useAuth();
  const isAdmin = user?.is_admin === true;
  const [state, setState] = useState<GateState>('loading');
  const [detailOpen, setDetailOpen] = useState(false);

  // [r13] 不靠 WalletContext 判代理(钱包接口失败会把真代理误判成普通用户)。
  // 一律调 agreementApi.status() 由后端权威判定:
  //   signed → children · unsigned/rejected/expired → agreement_required
  //   403 NOT_AGENT(仅代理可访问)→ not_agent · 网络/5xx → error
  const check = useCallback(() => {
    if (isAdmin) { setState('signed'); return; }            // admin 直接放行(对齐后端豁免)
    setState('loading');
    agreementApi.status()
      .then((r: any) => setState(r?.status === 'signed' ? 'signed' : 'agreement_required'))
      .catch((e: any) => {
        const code = e?.detail?.code;
        if (code === 'NOT_AGENT') setState('not_agent');
        else if (code === 'AGREEMENT_NOT_SIGNED') setState('agreement_required');
        else setState('error');                              // 仅真网络 / 5xx
      });
  }, [isAdmin]);

  useEffect(() => {
    if (authLoading) { setState('loading'); return; }
    check();
  }, [authLoading, check]);

  if (state === 'signed') return <>{children}</>;

  // returnTo 带完整站内位置(path + query + hash)· 签署后精确回原页
  const goSign = () => navigate(`/agent/agreement?returnTo=${encodeURIComponent(location.pathname + location.search + location.hash)}`);

  return (
    <div className="container mx-auto py-10 px-4 max-w-md">
      <Card>
        <CardContent className="p-6 flex flex-col items-center text-center gap-4">
          {state === 'loading' && (
            <>
              <Loader2 className="w-8 h-8 text-muted-foreground animate-spin" />
              <p className="text-sm text-muted-foreground">正在检查协议状态…</p>
            </>
          )}

          {state === 'agreement_required' && (
            <>
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
                <FileSignature className="w-6 h-6 text-primary" />
              </div>
              <h2 className="text-lg font-semibold">请先签署服务商经营功能协议</h2>
              <p className="text-sm text-muted-foreground">签署后可使用获客推广、客户售价、对外品牌和收益结算。</p>
              <div className="flex flex-col w-full gap-2 pt-1">
                <Button onClick={goSign} className="w-full">去签署协议</Button>
                <Button variant="ghost" onClick={() => navigate(-1)} className="w-full">稍后再说</Button>
              </div>
              <p className="text-xs text-muted-foreground">签署前不会产生费用。</p>
              {/* 移动端首屏只放上面三行 · 详细说明折叠 */}
              <button
                type="button"
                onClick={() => setDetailOpen(v => !v)}
                className="text-xs text-muted-foreground inline-flex items-center gap-1 mt-1"
              >
                {detailOpen ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                {detailOpen ? '收起说明' : '查看说明'}
              </button>
              {detailOpen && (
                <p className="text-xs text-muted-foreground leading-relaxed text-left">
                  服务商经营功能协议明确平台与服务商的职责边界:平台保障工具可用、算力对账准确、数据按系统检测结果展示;
                  服务商对客户的额外承诺自行负责。签署后即可解锁服务方经营功能。
                </p>
              )}
            </>
          )}

          {state === 'not_agent' && (
            <>
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <AlertTriangle className="w-6 h-6 text-muted-foreground" />
              </div>
              <h2 className="text-lg font-semibold">当前账号暂未开通服务商权限</h2>
              <p className="text-sm text-muted-foreground">开通后可使用获客推广、客户售价和收益结算。</p>
              <Button onClick={() => navigate('/partner/about')} className="w-full">查看服务商权益</Button>
            </>
          )}

          {state === 'error' && (
            <>
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10">
                <AlertTriangle className="w-6 h-6 text-destructive" />
              </div>
              <h2 className="text-lg font-semibold">页面暂时打不开</h2>
              <p className="text-sm text-muted-foreground">网络或服务暂时异常,请稍后重试。</p>
              <Button onClick={check} className="w-full gap-1.5">
                <RefreshCw className="w-4 h-4" /> 重新加载
              </Button>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
