import { useEffect, useState } from 'react';
import { FlaskConical, ShieldCheck, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { DemoActionPreview } from '@/lib/demoMode';

export function DemoActionPreviewDialog() {
  const [preview, setPreview] = useState<DemoActionPreview | null>(null);

  useEffect(() => {
    const show = (event: Event) => setPreview((event as CustomEvent<DemoActionPreview>).detail);
    window.addEventListener('omnirank-demo-action-preview', show);
    return () => window.removeEventListener('omnirank-demo-action-preview', show);
  }, []);

  if (!preview) return null;
  return <div className="fixed inset-0 z-[260] grid place-items-center bg-black/55 p-4" role="dialog" aria-modal="true" aria-labelledby="demo-preview-title" data-testid="demo-action-preview">
    <section className="max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-2xl border bg-background p-5 shadow-2xl sm:p-6">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-amber-500/15 text-amber-600"><FlaskConical className="h-5 w-5" /></span>
          <div><h2 id="demo-preview-title" className="font-semibold">演示流程预览</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">{preview.message}</p></div>
        </div>
        <button className="rounded-md p-1 text-muted-foreground hover:bg-muted" onClick={() => setPreview(null)} aria-label="关闭演示预览"><X className="h-4 w-4" /></button>
      </div>
      <div className="mt-5 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-sm">
        <div className="flex items-center gap-2 font-medium text-emerald-700 dark:text-emerald-300"><ShieldCheck className="h-4 w-4" />本次操作的硬保证</div>
        <p className="mt-2 leading-6 text-muted-foreground">未保存、未扣费、未调用模型/搜索/外部服务，也没有创建后台任务。页面中的临时输入可继续用于讲解，刷新后即消失。</p>
      </div>
      <div className="mt-5"><h3 className="text-sm font-medium">真实模式下会发生</h3><ol className="mt-2 space-y-2 text-sm text-muted-foreground">{preview.real_mode_effects.map((item, index) => <li key={`${index}-${item}`} className="flex gap-2"><span className="text-foreground">{index + 1}.</span><span>{item}</span></li>)}</ol></div>
      <div className="mt-5 flex justify-end"><Button onClick={() => setPreview(null)}>继续演示</Button></div>
    </section>
  </div>;
}

export default DemoActionPreviewDialog;
