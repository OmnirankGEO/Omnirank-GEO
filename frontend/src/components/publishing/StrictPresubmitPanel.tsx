import { useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { GovernanceAlert } from '@/components/ui/governance-alert';
import type { GovernanceAlertAction } from '@/contracts/governanceAlert';
import {
  SWITCH_MEDIA_ACTION_ID,
  humanizeStrictFailureCode,
  toGovernanceContract,
  type StrictPresubmitBlock,
} from '@/contracts/strictMediaPresubmit';

interface StrictPresubmitPanelProps {
  block: StrictPresubmitBlock;
  onAction?: (action: GovernanceAlertAction) => void | boolean;
  /** 有可换的非严审媒体时才挂「换成非严审媒体」按钮。 */
  canSwitchMedia?: boolean;
  className?: string;
}

/**
 * 严审媒体「发布前预审被拦」面板(2026-07-30 工单 T3)。
 *
 * 取代原来那一句 `lazyToast.error(detail.message)`。后端本来就把
 * 三个动作、逐媒体 hard_failures、repair_hint 都发过来了,前端全丢了,
 * 于是用户"既不知道哪里不合格,也没有可点的下一步"。
 *
 * 🔴 这里**没有**、也不许有「人工审核放行 / 忽略继续投放」按钮(工单 §3.2)。
 *    `platform_profile_hard` 的语义是「仅对该发布档不可覆盖 · 换档重评」,
 *    出路是**修稿达标**或**换成非严审媒体**,不是绕过。
 */
export function StrictPresubmitPanel({
  block,
  onAction,
  canSwitchMedia = false,
  className,
}: StrictPresubmitPanelProps) {
  const [showEvidence, setShowEvidence] = useState(false);

  const extraActions: GovernanceAlertAction[] = canSwitchMedia
    ? [{ id: SWITCH_MEDIA_ACTION_ID, label: '换成非严审媒体', type: 'retry' }]
    : [];
  const contract = toGovernanceContract(block, extraActions);

  const handleAction = (action: GovernanceAlertAction) => {
    // 「查看定位」不跳走 —— 定位就在这张卡片里,展开语料依据即可。
    if (action.id === 'view_findings') {
      setShowEvidence(v => !v);
      return true;
    }
    return onAction?.(action);
  };

  return (
    <div data-testid="strict-presubmit-panel" className={className}>
      <GovernanceAlert
        contract={contract}
        onAction={handleAction}
        dismissible={false}
        tone="error"
        icon={<ShieldAlert className="h-4 w-4" />}
      />
      <div className="mt-2 space-y-2">
        {block.blocked_media.map((media, mi) => (
          <div
            key={`${media.media_id ?? media.media_name ?? mi}`}
            data-testid="strict-presubmit-media"
            className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs"
          >
            <div className="font-medium text-foreground">
              {media.media_name || media.family_label || '严审媒体'}
              {typeof media.observed_reject_rate === 'number' && (
                <span className="ml-1.5 font-normal text-muted-foreground">
                  这家历史拒稿率 {Math.round(media.observed_reject_rate * 100)}%
                </span>
              )}
            </div>
            <ul className="mt-1 space-y-0.5">
              {(media.hard_failures || []).map((f, fi) => (
                <li key={`${f.code}-${fi}`} data-testid="strict-presubmit-failure">
                  <span className="text-foreground">· {humanizeStrictFailureCode(f.code)}</span>
                  {f.detail && <span className="text-muted-foreground">（{f.detail}）</span>}
                  {showEvidence && f.evidence && (
                    <div className="ml-3 mt-0.5 text-[10px] text-muted-foreground">
                      依据：{f.evidence}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
