/**
 * 开源版署名行(WO_329):应用界面页脚与客户门户页脚共用这一个组件。
 * - 开关关(主仓 / 线上)⇒ 返回 null,页面与改动前一致。
 * - 开 ⇒ 一行可见的链接。白标不影响它(许可证要求不得隐藏);样式只调淡,不许 hidden / 透明 / 零字号 / 移出视口,打印时同样出现。
 */
import { useOssAttribution } from '@/hooks/useOssAttribution';

export function OssAttribution({ className = '' }: { className?: string }) {
  const oss = useOssAttribution();
  if (!oss) return null;
  return (
    <div data-oss-attribution="" className={`w-full py-3 text-center text-xs text-muted-foreground ${className}`}>
      <a href={oss.href} target="_blank" rel="noopener noreferrer" className="hover:underline">
        {oss.text}
      </a>
    </div>
  );
}
