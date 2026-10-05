import type { InventoryPurchaseCatalogItem } from '@/lib/v35w2Api';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

export const MAX_AGENT_PURCHASE_AMOUNT_CENTS = 2_000_000_000;
export const MAX_AGENT_PURCHASE_AMOUNT_YUAN = '20000000';

export interface InventoryPurchaseCatalogDraft extends InventoryPurchaseCatalogItem {
  draft_id: string;
  amount_yuan: string;
}

export function centsToYuanInput(cents: number): string {
  const whole = Math.trunc(cents / 100);
  const fraction = Math.abs(cents % 100);
  return fraction === 0 ? String(whole) : `${whole}.${String(fraction).padStart(2, '0').replace(/0$/, '')}`;
}

/** Parse a yuan input without floating-point money arithmetic. */
export function yuanInputToCents(value: string): number | null {
  const normalized = value.trim();
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(normalized);
  if (!match) return null;
  const whole = Number(match[1]);
  const fraction = Number((match[2] || '').padEnd(2, '0'));
  if (!Number.isSafeInteger(whole) || whole > Math.floor(Number.MAX_SAFE_INTEGER / 100)) return null;
  const cents = whole * 100 + fraction;
  return Number.isSafeInteger(cents) && cents > 0 && cents <= MAX_AGENT_PURCHASE_AMOUNT_CENTS ? cents : null;
}

export function toCatalogDraft(item: InventoryPurchaseCatalogItem): InventoryPurchaseCatalogDraft {
  return { ...item, draft_id: item.option_id || safeRandomUUID(), amount_yuan: centsToYuanInput(item.amount_cents) };
}

export function sortCatalogDrafts(rows: InventoryPurchaseCatalogDraft[]): InventoryPurchaseCatalogDraft[] {
  return [...rows].sort((a, b) =>
    a.sort_order - b.sort_order
      || a.amount_cents - b.amount_cents
      || String(a.option_id || '').localeCompare(String(b.option_id || ''))
  );
}

export function validateCatalogDrafts(rows: InventoryPurchaseCatalogDraft[]): string | null {
  const enabledCount = rows.filter((row) => row.is_enabled).length;
  if (rows.length === 0 || enabledCount === 0) return '至少保留一个启用档位';
  if (enabledCount > 3) return '最多启用 3 个固定进货档位；服务商仍可使用自由金额进货';
  const amounts = new Set<number>();
  for (const row of rows) {
    const cents = yuanInputToCents(row.amount_yuan);
    if (cents == null) return `进货金额必须大于 0、不超过 ${MAX_AGENT_PURCHASE_AMOUNT_YUAN} 元，且最多保留两位小数`;
    if (!Number.isSafeInteger(row.sort_order) || row.sort_order < 0) return '排序必须是大于或等于 0 的整数';
    if (amounts.has(cents)) return '所有档位的进货金额不能重复';
    amounts.add(cents);
  }
  return null;
}

export function describeCatalogChanges(
  before: InventoryPurchaseCatalogItem[],
  after: InventoryPurchaseCatalogDraft[],
): string[] {
  const previous = new Map(before.map((row) => [row.option_id, row]));
  const changes: string[] = [];
  for (const row of sortCatalogDrafts(after)) {
    const cents = yuanInputToCents(row.amount_yuan) ?? row.amount_cents;
    const old = row.option_id ? previous.get(row.option_id) : undefined;
    if (!old) {
      changes.push(`新增：¥${centsToYuanInput(cents)}（${row.is_enabled ? '启用' : '停用'}，排序 ${row.sort_order}）`);
      continue;
    }
    const details: string[] = [];
    if (old.amount_cents !== cents) details.push(`金额 ¥${centsToYuanInput(old.amount_cents)} → ¥${centsToYuanInput(cents)}`);
    if (old.is_enabled !== row.is_enabled) details.push(`${old.is_enabled ? '启用' : '停用'} → ${row.is_enabled ? '启用' : '停用'}`);
    if (old.sort_order !== row.sort_order) details.push(`排序 ${old.sort_order} → ${row.sort_order}`);
    if (details.length) changes.push(`${centsToYuanInput(cents)} 元档：${details.join('；')}`);
  }
  return changes;
}
