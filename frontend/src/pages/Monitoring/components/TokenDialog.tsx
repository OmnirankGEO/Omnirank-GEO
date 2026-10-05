/**
 * Token management dialog for client portal access.
 */
import { useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Copy, Check, ExternalLink, Sparkles } from 'lucide-react';
import { copyToClipboard } from '@/lib/copyUtils';
import { lazyToast } from '@/lib/lazyToast';
import type { TokenInfo } from '../types';

interface TokenDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    tokenInfo: TokenInfo | null;
    copied: boolean;
    onCopyToken: () => void;
    onGenerateToken: () => void;
    /** 沙盒教程收尾模式: 加白标链接讲解 + 收尾按钮, 锁住关闭 */
    tutorialMode?: boolean;
    /** 教程模式下点"教程全部走完啦"的回调 */
    onTutorialFinish?: () => void;
}

// [CTO-15.23 2026-05-06 P1-3] Token 状态三态(原 badge "已过期" 误报)
// 老板 E2E 测试:刚生成 token expires_at=未来 30 天 · UI 显示"已过期"
// 根因:is_active=0 实际是"被覆盖的旧 token"(同 quote_id 多次生成)· 不是过期
//      文案写死 is_active ? '有效' : '已过期' · 真过期判断在后端 verify 用 expires_at < now
// 修法:三态判定 · 真过期 = expires_at < now · is_active=0 = 已失效(被覆盖)· 有效
function computeTokenStatus(t: { is_active?: boolean | number; expires_at?: string }): {
    label: string; variant: 'default' | 'secondary' | 'destructive';
} {
    if (t.expires_at) {
        try {
            const expDate = new Date(t.expires_at);
            if (!isNaN(expDate.getTime()) && expDate.getTime() < Date.now()) {
                return { label: '已过期', variant: 'destructive' };
            }
        } catch { /* fall through */ }
    }
    if (!t.is_active) return { label: '已失效', variant: 'secondary' };
    return { label: '有效', variant: 'default' };
}

function formatExpiresAt(exp?: string): string {
    if (!exp) return '-';
    try {
        const d = new Date(exp);
        if (isNaN(d.getTime())) return exp;
        const pad = (n: number) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch { return exp; }
}

export function TokenDialog({ open, onOpenChange, tokenInfo, copied, onCopyToken, onGenerateToken, tutorialMode = false, onTutorialFinish }: TokenDialogProps) {
    const status = tokenInfo ? computeTokenStatus(tokenInfo) : null;
    const portalLink = tokenInfo?.token ? `${window.location.origin}/portal/${tokenInfo.token}` : '';
    const [linkCopied, setLinkCopied] = useState(false);
    const copyPortalLink = async () => {
        if (!portalLink) return;
        const ok = await copyToClipboard(portalLink);
        if (ok) { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 2000); }
        else { lazyToast.error('复制失败,请手动复制'); }
    };
    return (
        <Dialog open={open} onOpenChange={(next) => { if (!next && tutorialMode) return; onOpenChange(next); }}>
            <DialogContent className={`max-w-[95vw] sm:max-w-md${tutorialMode ? ' [&>button]:hidden' : ''}`}>
                <DialogHeader>
                    <DialogTitle>客户门户设置</DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    {tutorialMode && (
                        <div className="rounded-lg border border-brand/30 bg-brand/5 p-3 text-sm leading-6 text-foreground">
                            <span className="font-medium text-brand">这是什么</span>:下面是这个客户的专属"白标链接"。把它发微信给客户, 他打开就能<strong>随时自己看</strong>这些词的实时出现率看板 —— 不用登录、看不到别的客户、也不用你手动导数据。
                            {portalLink && (
                                <div className="mt-2 break-all rounded bg-background/70 px-2 py-1.5 font-mono text-xs text-muted-foreground">{portalLink}</div>
                            )}
                        </div>
                    )}
                    {tutorialMode ? null : tokenInfo && status ? (
                        <div className="p-4 bg-muted rounded-lg">
                            <div className="flex items-center justify-between mb-2">
                                <span className="text-sm font-medium">客户访问链接</span>
                                <Badge variant={status.variant}>{status.label}</Badge>
                            </div>
                            <div className="flex items-center gap-2 font-mono text-sm sm:text-lg break-all">
                                <span className="flex-1">{tokenInfo.token}</span>
                                <Button variant="outline" size="sm" onClick={onCopyToken}>
                                    {copied ? <Check className="h-4 w-4 text-green-400" /> : <Copy className="h-4 w-4" />}
                                </Button>
                            </div>
                            <p className="text-xs text-muted-foreground mt-2">
                                过期时间: {formatExpiresAt(tokenInfo.expires_at)}
                            </p>
                        </div>
                    ) : (
                        <div className="p-4 bg-muted rounded-lg text-center text-muted-foreground">
                            还没有客户门户链接,点下方按钮生成
                        </div>
                    )}
                    {tutorialMode ? (
                        <div className="flex flex-wrap items-center justify-end gap-2">
                            <Button variant="outline" onClick={copyPortalLink}>
                                {linkCopied ? <Check className="h-4 w-4 mr-1 text-green-400" /> : <Copy className="h-4 w-4 mr-1" />}
                                {linkCopied ? '已复制' : '复制链接'}
                            </Button>
                            <Button className="bg-amber-500 hover:bg-amber-600 text-white" onClick={() => onTutorialFinish?.()}>
                                <Sparkles className="h-4 w-4 mr-1.5" />
                                教程全部走完啦 →
                            </Button>
                        </div>
                    ) : (
                        <div className="flex flex-wrap justify-end gap-2">
                            {/* [CTO-15.3 2026-04-20] 快捷打开客户门户(新 tab 走 /portal/:token 自动填 token)
                                老板反馈"客户门户按钮之前放哪找不到了" — 集中到 Token 管理弹窗里 */}
                            {tokenInfo?.token && tokenInfo?.is_active && (
                                <Button
                                    variant="outline"
                                    onClick={() => window.open(`/portal/${tokenInfo.token}`, '_blank', 'noopener,noreferrer')}
                                    title="新 tab 打开该 Token 对应的客户门户,预览客户视角"
                                >
                                    <ExternalLink className="h-4 w-4 mr-1" />
                                    打开客户门户
                                </Button>
                            )}
                            <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
                            <Button onClick={onGenerateToken}>
                                {tokenInfo ? '重新生成' : '生成Token'}
                            </Button>
                        </div>
                    )}
                </div>
            </DialogContent>
        </Dialog>
    );
}
