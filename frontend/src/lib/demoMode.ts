import { useSyncExternalStore } from 'react';

export interface DemoSelection {
  brandId: number;
  caseId: string;
}

export interface DemoActionPreview {
  code: 'DEMO_ACTION_PREVIEW';
  message: string;
  access_mode: 'demo';
  action: string;
  blocked_reason: string;
  saved: false;
  charged: false;
  provider_called: false;
  external_service_called: false;
  job_created: false;
  refresh_discards_local_preview: true;
  real_mode_effects: string[];
  request_id: string;
}

let activeDemoSelection: DemoSelection | null = null;

export function setActiveDemoSelection(selection: DemoSelection | null): void {
  if (
    activeDemoSelection?.brandId === selection?.brandId
    && activeDemoSelection?.caseId === selection?.caseId
  ) return;
  activeDemoSelection = selection;
  window.dispatchEvent(new CustomEvent('omnirank-demo-selection-changed', { detail: selection }));
}

export function getActiveDemoSelection(): DemoSelection | null {
  return activeDemoSelection;
}

export function useActiveDemoSelection(): DemoSelection | null {
  return useSyncExternalStore(
    (onStoreChange) => {
      window.addEventListener('omnirank-demo-selection-changed', onStoreChange);
      return () => window.removeEventListener('omnirank-demo-selection-changed', onStoreChange);
    },
    getActiveDemoSelection,
    () => null,
  );
}

const DEMO_SNAPSHOT_MODULES = new Set([
  'clients',
  'diagnosis',
  'pricing',
  'quote',
  'writing',
  'publish',
  'monitoring',
  'reports',
]);

export function allowsDemoSnapshotModule(module: string | undefined): boolean {
  return Boolean(module && DEMO_SNAPSHOT_MODULES.has(module));
}

export function shouldAttachDemoSelection(input: string): boolean {
  let path = input;
  try { path = new URL(input, window.location.origin).pathname; } catch { /* retain the relative value */ }
  // Account/session and independent organization controls must remain usable
  // while a salesperson is presenting a demo customer. Customer/artifact APIs
  // keep the header; their backend guards remain the authoritative boundary.
  return !(
    /^\/api\/auth(?:\/|$)/.test(path)
    || path === '/api/client-context/list'
    || path === '/api/portal/verify'
    || /^\/api\/portal\/demo(?:\/|$)/.test(path)
    || /^\/api\/(?:organization|notifications|wallet)(?:\/|$)/.test(path)
    || /^\/api\/user\/notifications(?:\/|$)/.test(path)
  );
}

function isDemoActionPreview(value: unknown): value is DemoActionPreview {
  return Boolean(
    value
      && typeof value === 'object'
      && (value as { code?: unknown }).code === 'DEMO_ACTION_PREVIEW',
  );
}

export function notifyDemoActionPreview(payload: unknown): boolean {
  const nested = payload && typeof payload === 'object'
    ? (payload as { detail?: unknown }).detail
    : undefined;
  const detail = isDemoActionPreview(nested)
    ? nested
    : isDemoActionPreview(payload) ? payload : null;
  if (!detail) return false;
  window.dispatchEvent(new CustomEvent<DemoActionPreview>('omnirank-demo-action-preview', { detail }));
  return true;
}
