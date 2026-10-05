import { authFetch } from '@/lib/api';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Lock, ArrowRight, AlertCircle, Loader2 } from 'lucide-react';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { sweepLegacyPortalOwnerKeys } from '@/pages/Portal/PortalDashboard';
import { OssAttribution } from '@/components/common/OssAttribution';

export default function PortalLogin() {
    const navigate = useNavigate();
    const location = useLocation();
    const { token: urlToken } = useParams();
    const [token, setToken] = useState(urlToken || '');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(
        new URLSearchParams(location.search).get('reason') === 'demo-authority-lost'
            ? '演示授权已失效' : '',
    );
    const requestGenerationRef = useRef(0);
    const requestAbortRef = useRef<AbortController | null>(null);
    const mountedRef = useRef(true);

    const verifyAndEnter = useCallback(async (t: string) => {
        if (!t) return;
        const generation = ++requestGenerationRef.current;
        requestAbortRef.current?.abort();
        const controller = new AbortController();
        requestAbortRef.current = controller;
        setLoading(true);
        setError('');
        try {
            const res = await authFetch('/api/portal/verify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: t }),
                signal: controller.signal,
            });
            const data = await res.json();
            if (!mountedRef.current || controller.signal.aborted || generation !== requestGenerationRef.current) return;
            if (data.status === 'success' && data.valid) {
                const isDemo = data.access_mode === 'demo';
                const sessionToken = isDemo ? data.demo_transport_entry : t;
                if (!sessionToken || (isDemo && !/^D[A-Z2-7]{32}$/.test(sessionToken))) {
                    localStorage.removeItem('portal_token');
                    localStorage.removeItem('portal_demo_transport_entry');
                    setError('演示入口无效或已撤回');
                    return;
                }
                localStorage.setItem('portal_token', sessionToken);
                localStorage.setItem('portal_quote_id', data.quote_id.toString());
                localStorage.setItem('portal_brand_id', data.brand_id ? data.brand_id.toString() : '');
                localStorage.setItem('portal_brand_name', data.brand_name || '');
                localStorage.setItem('portal_access_mode', isDemo ? 'demo' : 'live');
                if (isDemo) {
                    localStorage.setItem('portal_demo_transport_entry', sessionToken);
                } else {
                    localStorage.removeItem('portal_demo_transport_entry');
                }
                // [audit #10 返修] 不再存 portal_owner_user_id —— PortalDashboard 改用 portal_quote_id
                //   经后端解析白标(后端 verify 已去 owner_user_id 字段防 brand→agent 串联枚举)。
                // [板块 C · Owner 2026-07-22 D2] 换号/新登录成功即前缀清扫一切 portal_owner_user_id*
                //   历史残留（含按 token 变体 portal_owner_user_id:{token}），杜绝跨账号残留。
                sweepLegacyPortalOwnerKeys();
                navigate('/portal/dashboard');
            } else {
                setError(data.message || 'Token无效或已过期');
            }
        } catch (caught) {
            if (
                !mountedRef.current
                || controller.signal.aborted
                || generation !== requestGenerationRef.current
                || (caught instanceof DOMException && caught.name === 'AbortError')
            ) return;
            setError('验证失败,请重试');
        } finally {
            if (mountedRef.current && generation === requestGenerationRef.current) {
                setLoading(false);
            }
        }
    }, [navigate]);

    useEffect(() => {
        // React 18 dev StrictMode runs setup -> cleanup -> setup. Restore the mounted
        // guard on every setup so the second (real) lifecycle may commit its response.
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            requestGenerationRef.current += 1;
            requestAbortRef.current?.abort();
        };
    }, []);

    // [P2-16 fix 2026-05-23] urlToken 存在时自动 verify + 跳 dashboard
    // 老板:代理发的客户门户链接应该"一点即看",不应该让客户手动再点"进入门户"
    useEffect(() => {
        setToken(urlToken || '');
        if (urlToken) {
            void verifyAndEnter(urlToken);
        } else {
            requestGenerationRef.current += 1;
            requestAbortRef.current?.abort();
            setLoading(false);
            if (new URLSearchParams(location.search).get('reason') === 'demo-authority-lost') {
                setError('演示授权已失效');
            }
        }
        return () => requestAbortRef.current?.abort();
    }, [location.search, urlToken, verifyAndEnter]);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        await verifyAndEnter(token);
    };

    return (
        <div className="min-h-screen bg-muted flex items-center justify-center p-4 relative">
            {/* [CTO-15.3 2026-04-21] 客户门户主题切换(与 PortalDashboard 一致体验) */}
            <div className="absolute top-4 right-4">
                <ThemeToggle />
            </div>
            <Card className="w-full max-w-md bg-card rounded-xl border border-border">
                <CardHeader className="text-center pb-2">
                    <div className="mx-auto w-16 h-16 bg-brand rounded-full flex items-center justify-center mb-4">
                        <Lock className="h-8 w-8 text-white" />
                    </div>
                    <CardTitle className="text-lg sm:text-xl md:text-2xl font-bold">客户门户</CardTitle>
                    <CardDescription>请输入您的专属访问令牌</CardDescription>
                </CardHeader>
                <CardContent>
                    <form onSubmit={handleSubmit} className="space-y-4">
                        <div>
                            <Input
                                type="text"
                                placeholder="请输入访问令牌"
                                value={token}
                                onChange={e => setToken(e.target.value.toUpperCase())}
                                className="text-center text-lg tracking-widest font-mono h-12"
                                maxLength={64}
                                autoFocus
                            />
                            {error && (
                                <div className="flex items-center gap-2 text-red-500 text-sm mt-2">
                                    <AlertCircle className="h-4 w-4" />
                                    {error}
                                </div>
                            )}
                        </div>
                        <Button
                            type="submit"
                            className="w-full h-12 text-lg"
                            disabled={loading || token.length < 6}
                        >
                            {loading ? '验证中...' : (
                                <>
                                    进入门户
                                    <ArrowRight className="h-5 w-5 ml-2" />
                                </>
                            )}
                        </Button>
                    </form>
                    <p className="text-center text-xs text-muted-foreground mt-6">
                        访问令牌由 OmniRank 平台生成，有效期以发送页面提示为准
                    </p>
                    {/* WO_329 开源版署名位;开关关 ⇒ 不渲染 */}
                    <OssAttribution className="mt-2" />
                </CardContent>
            </Card>
        </div>
    );
}
