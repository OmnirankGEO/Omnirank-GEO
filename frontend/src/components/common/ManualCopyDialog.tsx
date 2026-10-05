/**
 * ManualCopyDialog — 复制失败兜底:把链接/文本以可选中形式亮出来
 *
 * [WO_WHITELABEL_COPY_UX 项2 2026-08-05]
 * iOS Safari / 微信 webview 里剪贴板写入可能被系统拒(手势上下文过期 / 权限)。
 * 铁律 feedback_hint_must_help_or_hide:失败不许只丢一句「复制失败」——
 * 必须把已经拿到的链接显示出来,让用户能长按选中 / 点「再试一次」。
 *
 * 「再试一次」按钮是新的用户手势 → 文本已在手里,同步栈里直接 copyToClipboard,
 * 在真机上通常这一下就能成(第一次失败多是因为手势被 await 耗掉了)。
 */
import { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import { copyToClipboard } from '@/lib/copyUtils';

export function ManualCopyDialog({
    text,
    onClose,
    title = '自动复制没成功',
    hint = '链接已生成。请长按下方文本框全选复制,或点「再试一次」。',
}: {
    /** 要复制的文本;null/空 = 关闭弹窗 */
    text: string | null;
    onClose: () => void;
    title?: string;
    hint?: string;
}) {
    const inputRef = useRef<HTMLInputElement | null>(null);
    const [retried, setRetried] = useState(false);

    useEffect(() => {
        setRetried(false);
    }, [text]);

    const selectAll = () => {
        const el = inputRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(0, el.value.length);
    };

    const handleRetry = () => {
        if (!text) return;
        // 按钮点击 = 新的用户手势,同步栈里直接写剪贴板
        void copyToClipboard(text).then((ok) => {
            if (ok) {
                setRetried(true);
            } else {
                selectAll();
            }
        });
    };

    return (
        <Dialog open={Boolean(text)} onOpenChange={(open) => { if (!open) onClose(); }}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle>{title}</DialogTitle>
                    <DialogDescription>{hint}</DialogDescription>
                </DialogHeader>
                <input
                    ref={inputRef}
                    readOnly
                    value={text ?? ''}
                    onFocus={selectAll}
                    onClick={selectAll}
                    data-testid="manual-copy-text"
                    className="w-full select-all rounded-md border border-border bg-muted/40 px-3 py-2 font-mono text-xs text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                />
                <div className="flex items-center justify-end gap-2">
                    <Button variant="outline" size="sm" onClick={onClose}>关闭</Button>
                    <Button size="sm" onClick={handleRetry} disabled={retried}>
                        {retried
                            ? (<><Check className="mr-1.5 h-4 w-4" aria-hidden="true" />已复制</>)
                            : (<><Copy className="mr-1.5 h-4 w-4" aria-hidden="true" />再试一次</>)}
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
