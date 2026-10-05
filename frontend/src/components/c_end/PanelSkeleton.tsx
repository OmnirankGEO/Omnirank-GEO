/**
 * PanelSkeleton — 旧 C 端嵌入面板 加载占位
 *
 * 替代 iframe 时代的纯白屏，让用户看到"在加载"而不是"卡死了"
 * 简洁布局贴合大多数页面（标题 + 卡片网格 + 长 block）
 */

export function PanelSkeleton() {
  return (
    <div className="p-4 space-y-4 animate-pulse">
      <div className="h-6 w-32 rounded bg-muted/60" />
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="h-24 rounded-lg bg-muted/40" />
        <div className="h-24 rounded-lg bg-muted/40" />
        <div className="h-24 rounded-lg bg-muted/40" />
      </div>
      <div className="h-32 rounded-lg bg-muted/40" />
      <div className="h-32 rounded-lg bg-muted/40" />
    </div>
  );
}
