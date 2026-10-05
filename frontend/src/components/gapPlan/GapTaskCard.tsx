/**
 * 任务卡(B3 + B7)
 *
 * 合同 §4 第二层:四列布局 —— 序号状态 112px / 内容 ≥420px / 证据链 380px / 操作 224px。
 * 合同 §10 四档响应式:≥1440 四列 · 1024-1439 两行 · 768-1023 单列 · <768 三行摘要。
 *
 * 🔴 零死状态:每个状态都紧邻至少一个动作。按钮从**服务端 actions 数组**渲染,
 *    前端不新增、不改名、不按容量自己藏 —— 容量守卫在服务端(判据 #10)。
 * 🔴 "报价评估待接入" 是 enabled=false 的占位:点击不发任何网络写请求(合同 §13.3)。
 */
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import type { GapAction, GapEvidenceBlock, GapPlanItem } from '@/lib/gapPlanApi';
import { EvidenceChain } from './EvidenceChain';
import { GapTaskRationale } from './GapTaskRationale';
import { toneClasses } from './tone';

interface Props {
  item: GapPlanItem;
  evidence?: GapEvidenceBlock;
  busy?: boolean;
  onAction: (action: GapAction, item: GapPlanItem, payload?: { url?: string }) => void;
}

export function GapTaskCard({ item, evidence, busy, onAction }: Props) {
  const [linkOpen, setLinkOpen] = useState(false);
  const [url, setUrl] = useState('');
  const tone = toneClasses(item.status.tone);

  const handle = (action: GapAction) => {
    if (!action.enabled) return;                    // 占位按钮:不发任何请求
    if (action.action_id === 'submit_publication_link' && !linkOpen) {
      setLinkOpen(true);
      return;
    }
    onAction(action, item);
  };

  return (
    <div
      className="@container rounded-lg border border-border bg-card p-4"
      data-help-target="gap-plan-item"
      data-plan-item-id={item.plan_item_id}
    >
      {/* 四列并排的真实需要宽度 = 112+420+380+224 + 3×16 gap = **1184px**。
          🔴 原来断点写的是 `xl:`(视口 ≥1280)—— 视口不等于这张卡拿到的宽度:
             报价页左侧另占 `md:w-72`(288px),再扣两层 padding,1366/1440 笔记本上
             卡片实宽 ~1000px 就已经进了四列档 → 每次都撑破。
          修法两道(照 OnlineQuoteFlow.tsx 已验证模式,不自创):
            ① 断点改问**容器**宽度(`@container` + `@min-[1184px]:`),问的是
               "这张卡真的有 1184px 吗",而不是"窗口有多宽";
            ② 外面仍包一层 `overflow-x-auto` 兜底 —— 哪天有人把某列宽度调大,
               后果是这张卡内部可横向滚动,而不是把整页布局撑破。
          反向对照:`@min-[1184px]` 在容器不足时**不生效**,此时布局是 flex-col,
          四列宽度类一条都不参与 —— 所以"不渲染四列即不撑破"是结构性的,不靠 overflow 遮丑。 */}
      <div className="overflow-x-auto">
        <div className="flex flex-col gap-4 @min-[1184px]:flex-row @min-[1184px]:items-start">
        {/* 1. 序号与状态 · 固定 112px */}
        <div className="shrink-0 @min-[1184px]:w-[112px]">
          <div className="text-xs text-muted-foreground">第 {item.ordinal} 篇</div>
          <span
            className={cn(
              'mt-1 inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
              tone.badge,
            )}
          >
            <span className={cn('h-1.5 w-1.5 rounded-full', tone.dot)} aria-hidden="true" />
            {/* 状态永远有文字,不只靠颜色 */}
            {item.status.label}
          </span>
        </div>

        {/* 2. 任务内容 · 最小 420px。🔴 `min-w-0` 保住:没有它,flex 子项的
            min-width:auto 会让长文案把行撑到内容宽,四列一起被顶出容器。 */}
        <div className="min-w-0 flex-1 @min-[1184px]:min-w-[420px]">
          <GapTaskRationale item={item} />
        </div>

        {/* 3. 证据链进度 · 固定 380px */}
        <div className="min-w-0 shrink-0 @min-[1184px]:w-[380px]">
          <EvidenceChain evidence={evidence} vertical={false} className="hidden sm:block" />
          <EvidenceChain evidence={evidence} vertical className="sm:hidden" />
        </div>

        {/* 4. 操作 · 固定 224px。主按钮 ≥40px(移动端 44px) */}
        <div className="min-w-0 shrink-0 space-y-2 @min-[1184px]:w-[224px]">
          {item.actions.map((action, idx) => (
            <div key={action.action_id}>
              <Button
                variant={idx === 0 ? 'default' : 'outline'}
                size="sm"
                className="w-full h-11 sm:h-10"
                disabled={!action.enabled || busy}
                onClick={() => handle(action)}
                title={action.hint || undefined}
              >
                {action.label}
              </Button>
              {!action.enabled && action.hint && (
                <p className="mt-1 text-[11px] text-muted-foreground">{action.hint}</p>
              )}
            </div>
          ))}

          {linkOpen && (
            <div className="space-y-2 rounded-md border border-border bg-muted/30 p-2">
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="粘贴已发布页面的公开链接"
                className="h-9 text-xs"
                aria-label="发布链接"
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  className="h-9 flex-1"
                  disabled={!url.trim() || busy}
                  onClick={() => {
                    onAction(
                      { action_id: 'submit_publication_link', label: '已发布，填链接',
                        confirmation: true, enabled: true },
                      item,
                      { url: url.trim() },
                    );
                    setLinkOpen(false);
                    setUrl('');
                  }}
                >
                  确认
                </Button>
                <Button size="sm" variant="ghost" className="h-9"
                        onClick={() => { setLinkOpen(false); setUrl(''); }}>
                  取消
                </Button>
              </div>
            </div>
          )}
        </div>
        </div>
      </div>
    </div>
  );
}

export default GapTaskCard;
