/**
 * ManualOpenDialog — 弹窗被系统拦掉时的兜底:给一个能直接点的链接
 *
 * [WO_IOS_TOUCH_UX 2026-08-05]
 * 铁律 feedback_hint_must_help_or_hide:不许只丢一句「打开失败」——
 * URL 已经在手里了,必须让用户能**一下点开**,或者长按复制走。
 *
 * 「在新页面打开」是新的用户手势 → 此刻 URL 已在手里,同步栈里直接 <a target="_blank">,
 * 真机上这一下就能成(第一次失败只是因为手势被 await 耗掉了)。
 */
import { useEffect, useRef, useState } from 'react';
import { Check, Copy, ExternalLink } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import { copyToClipboard } from '@/lib/copyUtils';

export function ManualOpenDialog({
    url,
    onClose,
    title = '浏览器拦住了新窗口',
    hint = '链接已经生成好了。点下面的按钮直接打开，或长按链接复制。',
}: {
    /** 要打开的链接;null/空 = 关闭弹窗 */
    url: string | null;
    onClose: () => void;
    title?: string;
    hint?: string;
}) {
    const inputRef = useRef<HTMLInputElement | null>(null);
    const [copied, setCopied] = useState(false);

    useEffect(() => {
        setCopied(false);
    }, [url]);

    const selectAll = () => {
        const el = inputRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(0, el.value.length);
    };

    const handleCopy = () => {
        if (!url) return;
        // 按钮点击 = 新手势,URL 已在手里 → 同步栈里直接写剪贴板
        void copyToClipboard(url).then((ok) => {
            if (ok) setCopied(true);
            else selectAll();
        });
    };

    return (
        <Dialog open={Boolean(url)} onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle>{title}</DialogTitle>
                    <DialogDescription>{hint}</DialogDescription>
                </DialogHeader>
                <input
                    ref={inputRef}
                    readOnly
                    value={url ?? ''}
                    onFocus={selectAll}
                    onClick={selectAll}
                    data-testid="manual-open-url"
                    className="w-full select-all rounded-md border border-border bg-muted/40 px-3 py-2 font-mono text-base text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                />
                <div className="flex items-center justify-end gap-2">
                    <Button variant="outline" size="sm" onClick={handleCopy} disabled={copied}>
                        {copied
                            ? (<><Check className="mr-1.5 h-4 w-4" aria-hidden="true" />已复制</>)
                            : (<><Copy className="mr-1.5 h-4 w-4" aria-hidden="true" />复制链接</>)}
                    </Button>
                    {/* 真链接而不是 button+window.open:<a> 是浏览器原生导航,不受弹窗拦截器管 */}
                    <Button asChild size="sm">
                        <a
                            href={url ?? '#'}
                            target="_blank"
                            rel="noopener noreferrer"
                            data-testid="manual-open-link"
                            onClick={onClose}
                        >
                            <ExternalLink className="mr-1.5 h-4 w-4" aria-hidden="true" />
                            在新页面打开
                        </a>
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
