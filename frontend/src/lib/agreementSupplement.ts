export interface AgreementDocument {
  agreement_type: 'user_terms' | 'privacy';
  agreement_version: string;
  content_hash: string;
  title: string;
  url: string;
}

export interface AgreementRequirement {
  agreement_session_token: string;
  expires_at: number;
  agreements: AgreementDocument[];
}

export interface PendingAgreementSession extends AgreementRequirement {
  return_to: string;
}

const STORAGE_KEY = 'omnirank_registration_agreement_session';
let volatileReturnFragment = '';

function normalizeReturnTo(value: string): string {
  const path = String(value || '').trim();
  if (!path.startsWith('/') || path.startsWith('//')) return '/';
  if (path === '/login' || path.startsWith('/agreement-update')) return '/';
  return path;
}

export function savePendingAgreementSession(
  requirement: AgreementRequirement,
  returnTo: string,
): void {
  const normalizedReturnTo = normalizeReturnTo(returnTo);
  const fragmentIndex = normalizedReturnTo.indexOf('#');
  volatileReturnFragment = fragmentIndex >= 0 ? normalizedReturnTo.slice(fragmentIndex) : '';
  const payload: PendingAgreementSession = {
    ...requirement,
    return_to: fragmentIndex >= 0 ? normalizedReturnTo.slice(0, fragmentIndex) || '/' : normalizedReturnTo,
  };
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

export function loadPendingAgreementSession(): PendingAgreementSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as PendingAgreementSession;
    if (
      !value
      || typeof value.agreement_session_token !== 'string'
      || !Number.isFinite(value.expires_at)
      || !Array.isArray(value.agreements)
    ) {
      clearPendingAgreementSession();
      return null;
    }
    return {
      ...value,
      return_to: `${normalizeReturnTo(value.return_to)}${volatileReturnFragment}`,
    };
  } catch {
    clearPendingAgreementSession();
    return null;
  }
}

export function clearPendingAgreementSession(): void {
  volatileReturnFragment = '';
  sessionStorage.removeItem(STORAGE_KEY);
}
