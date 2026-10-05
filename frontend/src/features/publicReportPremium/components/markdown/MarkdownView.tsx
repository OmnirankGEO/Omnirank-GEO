/**
 * MarkdownView — publicReportPremium 叙事区 Markdown 渲染
 *
 * 2026-07-22 sink census F17：委托全站 SSOT `@/components/SafeMarkdown`
 *  - 链接仅 http/https 无 userinfo · 非法链接降级文本 · 外链 target=_blank rel=noopener
 *  - 表格 .safe-markdown-table 横向滚动 · details 受控结构化渲染 · 打印自动展开
 *  - 图片不加载(文字占位,防跟踪像素) · 无 rehype-raw 任意 HTML
 * 本文件仅保留 premium 报告版心/节奏样式与局部元素皮肤(pre/code/blockquote)；
 * 安全策略全部归 SSOT。
 *  - Markdown 只承载叙事；数值指标一律从结构化 DTO 读取
 */
import SafeMarkdown, { type SafeMarkdownComponents as Components } from '@/components/SafeMarkdown';
import { cn } from '@/lib/utils';

/** premium 局部皮肤：仅非受保护元素；a/img/table/details 由 SafeMarkdown 强制接管 */
const skinComponents: Components = {
    pre({ children }) {
        return (
            <pre className="my-3 overflow-x-auto rounded-md bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">
                {children}
            </pre>
        );
    },
    code({ className, children }) {
        // react-markdown v10:代码块由 pre 包裹，行内代码不带 language 类
        const isBlock = /language-/.test(className ?? '');
        if (isBlock) {
            return <code className={className}>{children}</code>;
        }
        return (
            <code className="rounded bg-slate-100 px-1 py-0.5 text-[0.85em] text-slate-800 break-all">
                {children}
            </code>
        );
    },
    blockquote({ children }) {
        return (
            <blockquote className="my-3 border-l-2 border-slate-300 pl-4 text-slate-600">
                {children}
            </blockquote>
        );
    },
};

export function MarkdownView({ markdown, className }: { markdown: string; className?: string }) {
    return (
        <div
            className={cn(
                'prp-markdown max-w-none text-[15px] leading-7 text-slate-700',
                // 长 URL / 中英连排可换行
                'break-words [overflow-wrap:anywhere]',
                // 标题/列表/分隔线节奏(不依赖 prose 全局污染，作用域到本容器)
                '[&_h1]:mt-6 [&_h1]:mb-3 [&_h1]:text-xl [&_h1]:font-bold [&_h1]:text-slate-900',
                '[&_h2]:mt-6 [&_h2]:mb-3 [&_h2]:text-lg [&_h2]:font-bold [&_h2]:text-slate-900',
                '[&_h3]:mt-5 [&_h3]:mb-2 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-slate-900',
                '[&_h4]:mt-4 [&_h4]:mb-2 [&_h4]:text-sm [&_h4]:font-semibold [&_h4]:text-slate-800',
                '[&_p]:my-3 [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-6',
                '[&_li]:my-1 [&_li]:pl-1 [&_hr]:my-6 [&_hr]:border-slate-200',
                className,
            )}
        >
            <SafeMarkdown components={skinComponents}>{markdown}</SafeMarkdown>
        </div>
    );
}
