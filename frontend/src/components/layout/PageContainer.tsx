/**
 * PageContainer — 页面内容容器
 * 提供: 滚动区域、标准内边距、可选的页面标题
 */

import { cn } from '@/lib/utils';
import { ScrollArea } from '@/components/ui/scroll-area';

interface PageContainerProps {
  children: React.ReactNode;
  className?: string;
  scrollable?: boolean;
}

export function PageContainer({
  children,
  className,
  scrollable = true,
}: PageContainerProps) {
  if (scrollable) {
    return (
      <ScrollArea className="h-[calc(100dvh-3rem)]">
        <div className={cn('p-4 md:px-6', className)}>
          {children}
        </div>
      </ScrollArea>
    );
  }

  return (
    <div className={cn('p-4 md:px-6', className)}>
      {children}
    </div>
  );
}
