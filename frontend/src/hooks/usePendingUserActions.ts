/**
 * usePendingUserActions —— "挂起等你处理"的发布任务 + §13 合同。
 *
 * 数据源:GET /api/meijiehezi/pending-user-actions
 * 后端读面只按 status='awaiting_action' 过滤、**不看 reject_code**,所以这一条数据源
 * 同时覆盖内容漂移(PUBLISH_CONTENT_DRIFT)与代发卡单
 * (PUBLISH_AWAITING_SYNC_UNRESOLVED),以后新增的 §13 出口也自动进来。
 *
 * 模块级缓存 + 订阅:侧边栏角标与页面横幅共用同一份数据,一个会话只打一次接口,
 * 动作成功后由调用方 refresh() 主动刷新 —— 不做轮询(全站每页都轮询一个边缘态,
 * 代价远大于收益)。
 */
import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import type { GovernanceAlertContract } from '@/contracts/governanceAlert';

export const PENDING_USER_ACTIONS_ENDPOINT = '/api/meijiehezi/pending-user-actions';

export interface PendingUserActionItem {
  item_id: number;
  order_id: number;
  media_name: string | null;
  media_type: string | null;
  cost_points: number | null;
  status: string;
  reject_code: string | null;
  reject_user_message: string | null;
  reject_contract: GovernanceAlertContract | null;
  /** 非空 = 用户已经表过态,等人工核实中,不能重复提交。 */
  user_exit_claim: string | null;
  user_exit_claim_at: string | null;
  awaiting_sync_since: string | null;
  article_id: number | null;
  article_title: string | null;
}

interface CacheState {
  items: PendingUserActionItem[];
  loaded: boolean;
  loading: boolean;
  error: string | null;
}

let cache: CacheState = { items: [], loaded: false, loading: false, error: null };
let inFlight: Promise<void> | null = null;
const subscribers = new Set<(state: CacheState) => void>();

function publish(next: Partial<CacheState>): void {
  cache = { ...cache, ...next };
  subscribers.forEach((fn) => fn(cache));
}

async function load(): Promise<void> {
  if (inFlight) return inFlight;
  publish({ loading: true, error: null });
  inFlight = (async () => {
    try {
      const res = await authFetch(`${PENDING_USER_ACTIONS_ENDPOINT}?limit=50`);
      if (!res.ok) {
        // 没权限/未登录不是错误态,当作"没有待办",不要在页面上弹红
        publish({ items: [], loaded: true, loading: false, error: res.status >= 500 ? '暂时读不到待确认的发布任务' : null });
        return;
      }
      const data = await res.json();
      const items = Array.isArray(data?.items) ? (data.items as PendingUserActionItem[]) : [];
      publish({ items, loaded: true, loading: false, error: null });
    } catch {
      publish({ loaded: true, loading: false, error: '暂时读不到待确认的发布任务' });
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

/** 强制重新拉取(动作成功后调用)。 */
export async function refreshPendingUserActions(): Promise<void> {
  inFlight = null;
  publish({ loaded: false });
  await load();
}

export function usePendingUserActions() {
  const [state, setState] = useState<CacheState>(cache);

  useEffect(() => {
    subscribers.add(setState);
    if (!cache.loaded && !cache.loading) void load();
    return () => {
      subscribers.delete(setState);
    };
  }, []);

  const refresh = useCallback(() => refreshPendingUserActions(), []);

  return {
    items: state.items,
    total: state.items.length,
    // [2026-07-27 Review-CTO · Owner 生产实测] 已表过态(user_exit_claim 非空)的
    // 条目是"等平台人工核实",不是"等你"——继续计进角标会让用户以为点了没生效。
    actionableTotal: state.items.filter((i) => !i.user_exit_claim).length,
    loading: state.loading,
    error: state.error,
    refresh,
  };
}

/** 仅要数量的轻量订阅(侧边栏角标用)。只数还没表态的——等平台的不催用户。 */
export function usePendingUserActionCount(): number {
  return usePendingUserActions().actionableTotal;
}
