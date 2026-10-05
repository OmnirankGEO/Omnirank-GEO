/**
 * 兼容旧邀请落地页 · 读取一次不透明活动码后立即从地址栏清除。
 * [2026-05-30 B 方案 · 老板真机:摄像头扫码点开是登录页/官网不带邀请码 → 公开落地页]
 * 邀请码仅用于活动归因，不作为商业服务归属证据。
 */
import { useEffect, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Gift, ArrowRight, Loader2, ShieldCheck } from 'lucide-react';
import { OssAttribution } from '@/components/common/OssAttribution';

export default function InvitePage() {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const code = sp.get('ref') || '';
  const [brand, setBrand] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!code) { setLoading(false); return; }
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('ref');
    window.history.replaceState(window.history.state, '', `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
    try { localStorage.removeItem('omnirank_locked_ref'); } catch { /* legacy cleanup */ }
    fetch(`/api/public/invite/${encodeURIComponent(code)}`)
      .then((r) => r.json())
      .then((d) => { if (d?.valid) setBrand(d.service_brand); })
      .catch(() => { /* 静默 · 仍可注册 */ })
      .finally(() => setLoading(false));
  }, [code]);

  const goRegister = () =>
    navigate('/login?mode=register', { state: { inviteCode: code } });
  const goLogin = () =>
    navigate('/login', { state: { inviteCode: code } });

  return (
    <div className="min-h-[100dvh] flex items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardContent className="py-8 px-6 space-y-6 text-center">
          {loading ? (
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground mx-auto" />
          ) : (
            <>
              <div className="flex flex-col items-center gap-2">
                <div className="h-14 w-14 rounded-2xl bg-emerald-500/10 flex items-center justify-center">
                  <Gift className="h-7 w-7 text-emerald-500" />
                </div>
                <h1 className="text-xl font-bold text-foreground">
                  {brand ? `${brand} 活动邀请` : 'OmniRank 活动邀请'}
                </h1>
                <p className="text-sm text-muted-foreground">
                  AI 搜索可见度优化 · 让品牌更容易被 AI 推荐
                </p>
                {!brand && (
                  <p className="text-[11px] text-muted-foreground/70">
                    由 OmniRank 平台提供服务
                  </p>
                )}
              </div>

              {code ? (
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
                  <p className="text-sm text-emerald-600">活动入口已验证</p>
                </div>
              ) : (
                <p className="text-sm text-destructive">邀请码缺失 · 请重新获取活动链接</p>
              )}

              <div className="space-y-2">
                <Button
                  onClick={goRegister}
                  disabled={!code}
                  className="w-full bg-foreground text-background hover:bg-foreground/90"
                >
                  注册领取算力 <ArrowRight className="ml-1 h-4 w-4" />
                </Button>
                <Button onClick={goLogin} variant="outline" className="w-full">
                  已有账号 · 登录
                </Button>
              </div>

              <p className="text-[11px] text-muted-foreground flex items-center justify-center gap-1">
                <ShieldCheck className="h-3 w-3" /> 邀请码仅用于活动归因，不影响账户价格或服务配置
              </p>
            </>
          )}
          {/* WO_329 开源版署名位;开关关 ⇒ 不渲染 */}
          <OssAttribution />
        </CardContent>
      </Card>
    </div>
  );
}
