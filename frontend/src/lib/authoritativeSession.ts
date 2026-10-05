/**
 * Browser-session authority boundary.
 *
 * localStorage is only the cross-tab token carrier. A token is not usable by
 * business transports until AuthContext has confirmed the same candidate with
 * /api/auth/me in this tab. The opaque epoch is random and never persisted, so
 * reloads and same-user logins cannot collide with old sessionStorage keys.
 */

export const SESSION_TOKEN_KEY = 'omnirank_token';
export const SESSION_CANDIDATE_EVENT = 'omnirank-session-candidate-changed';

export type SessionCandidateSource =
  | 'bootstrap'
  | 'login'
  | 'refresh'
  | 'password-change'
  | 'logout'
  | 'storage'
  | 'storage-mismatch';

export interface SessionCandidateEventDetail {
  source: SessionCandidateSource;
  epoch: string;
}

export interface SessionAuthority {
  token: string;
  epoch: string;
}

/**
 * [BUG-3 2026-07-27] 确认失败的三态。
 *
 * 事故复盘：原来"未确认"只有一种表达 —— 直接 throw —— 于是 /me 只要慢一点，
 * 任何调用点都会把异常冒到 ErrorBoundary，整页白屏（客户门户表现为"数据全丢失"）。
 * 三种语义完全不同的情况必须分开：在途要【等】，401 要【跳登录】，超时要【重试后可恢复】。
 *
 * 🔴 本次只改"确认失败之后怎么办"，不改"要不要确认"。
 *    未经 /api/auth/me 确认的 token 在任何分支都不得进 Authorization 头（fail-closed 不变）。
 */
export type SessionConfirmationPhase =
  | 'anonymous'             // 没有 token —— 匿名放行，本来就不是错误
  | 'pending'               // /me 在途（含退避重试中）—— 应当等待，不是错误
  | 'confirmed'             // /me 已确认 —— 唯一可以用 token 的状态
  | 'failed_unauthorized'   // /me 明确 401/403 —— 真未登录，清 token 走登录页
  | 'failed_unreachable';   // /me 超时 / 网络错 / 5xx —— 可恢复错误，给用户重试出口

/**
 * 确认阶段的重试参数 —— 与 AuthContext 的 /me 自检共用同一套常量，避免两处口径漂移。
 *
 * 取值理由：
 * - 退避 2s：事故实测"新建连接慢 2.0~2.7s、连接复用后仅 0.57s"，2s 后重试大概率能复用连接。
 * - 只重试 1 次（共 2 次请求）：/me 是每次进页面必发的请求，加码重试会在"后端真的挂了"时
 *   把请求量翻倍，正好踩在最脆弱的时刻放大限流。耗尽后不再重试，而是升级为可恢复错误
 *   （保 token、给"重试"按钮），这比多试一次更有用。
 * - 等待上限 15s：> 首请求超时 + 2s 退避 + 重试的最坏路径，防调用方在 AuthContext
 *   意外不落定时永久挂起（挂起等于另一种形式的白屏）。
 */
export const SESSION_CONFIRM_RETRY_LIMIT = 1;
export const SESSION_CONFIRM_RETRY_BACKOFF_MS = 2000;
export const SESSION_CONFIRM_WAIT_TIMEOUT_MS = 15000;

export class AuthoritativeSessionPendingError extends Error {
  readonly code = 'AUTHORITATIVE_SESSION_PENDING';
  /** 在途不是故障：调用方应当等待或显示 loading，绝不能当致命错误炸页面。 */
  readonly recoverable = true;

  constructor() {
    super('Stored session token has not been confirmed by /api/auth/me in this tab');
    this.name = 'AuthoritativeSessionPendingError';
  }
}

/** /me 超时 / 网络错 / 5xx 且重试已耗尽 —— 可恢复，调用方应给"重试"出口而不是白屏。 */
export class AuthoritativeSessionUnavailableError extends Error {
  readonly code = 'AUTHORITATIVE_SESSION_UNAVAILABLE';
  readonly recoverable = true;

  constructor() {
    super('Session could not be confirmed by /api/auth/me (timeout or network); retry is possible');
    this.name = 'AuthoritativeSessionUnavailableError';
  }
}

/** 该错误是否属于"等一等/重试就能好"，供 ErrorBoundary 与调用方分流。 */
export function isRecoverableSessionError(error: unknown): boolean {
  return error instanceof AuthoritativeSessionPendingError
    || error instanceof AuthoritativeSessionUnavailableError;
}

function newOpaqueEpoch(): string {
  try {
    // [工单 2026-08-06 §4] 原来这里首选 `crypto.randomUUID()`,而它要 Chrome 92 —— 超出
    // Owner 拍的底线(chrome >= 90)。这里**不引入** lib/safeRandomUUID:
    // 🔴 本文件被 scripts/test-authoritative-session-failmode.mjs 单独转译后直接跑,
    //    那个 harness 不解析 `@/` 别名,加 import 会让 npm run build 整条链挂掉(实测踩过)。
    // 直接用 getRandomValues 即可:它 Chrome 11 / Safari 5 就有,远在底线内,
    // 而且这里本来就只需要一段不可猜的随机串,不需要 UUID 形状。
    const bytes = new Uint32Array(4);
    crypto.getRandomValues(bytes);
    return `auth-${Array.from(bytes, value => value.toString(16).padStart(8, '0')).join('')}`;
  } catch {
    return `auth-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
}

function readStorage(): string | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage.getItem(SESSION_TOKEN_KEY);
  } catch {
    return null;
  }
}

let candidateToken: string | null = readStorage();
let confirmedToken: string | null = null;
let authorizationEpoch = newOpaqueEpoch();

// [BUG-3] 有 token 时初始态是 pending —— AuthContext 挂载后立刻会发 /me。
// 初始态若写成 failed，首屏就会报错；写成 confirmed 则是 fail-open，两者都不行。
let confirmationPhase: SessionConfirmationPhase = candidateToken === null ? 'anonymous' : 'pending';
let confirmationWaiters: Array<(phase: SessionConfirmationPhase) => void> = [];

function settleConfirmation(phase: SessionConfirmationPhase): void {
  confirmationPhase = phase;
  if (phase === 'pending') return;
  const waiters = confirmationWaiters;
  confirmationWaiters = [];
  for (const resolve of waiters) resolve(phase);
}

export function getSessionConfirmationPhase(): SessionConfirmationPhase {
  return confirmationPhase;
}

/** /me 开始飞（或退避重试中）→ 让等待方继续等，而不是拿旧的失败态提前放弃。 */
export function markSessionConfirmationPending(): void {
  confirmationPhase = 'pending';
}

/** /me 落定为失败。unauthorized = 真未登录；unreachable = 超时/网络/5xx，可重试。 */
export function markSessionConfirmationFailed(kind: 'unauthorized' | 'unreachable'): void {
  settleConfirmation(kind === 'unauthorized' ? 'failed_unauthorized' : 'failed_unreachable');
}

/**
 * 等确认落定。pending 时挂起，落定或超时后 resolve。
 * 超时按 failed_unreachable 处理（可恢复），绝不无限等 —— 永久挂起是另一种白屏。
 */
export function waitForSessionConfirmation(
  timeoutMs: number = SESSION_CONFIRM_WAIT_TIMEOUT_MS,
): Promise<SessionConfirmationPhase> {
  if (confirmationPhase !== 'pending') return Promise.resolve(confirmationPhase);
  return new Promise<SessionConfirmationPhase>(resolve => {
    let done = false;
    const finish = (phase: SessionConfirmationPhase) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(phase);
    };
    const timer = setTimeout(() => finish('failed_unreachable'), timeoutMs);
    confirmationWaiters.push(finish);
  });
}

function announce(source: SessionCandidateSource): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent<SessionCandidateEventDetail>(SESSION_CANDIDATE_EVENT, {
    detail: { source, epoch: authorizationEpoch },
  }));
}

function replaceCandidate(nextToken: string | null, source: SessionCandidateSource): string {
  candidateToken = nextToken;
  confirmedToken = null;
  authorizationEpoch = newOpaqueEpoch();
  // [BUG-3] 候选换了 → 确认从头开始:有 token 进 pending(等 /me),没 token 直接匿名。
  // 登出/清 token 时必须 settle('anonymous'),否则此前挂起的等待方会一直吊到超时。
  // 换成新 token 时保持 pending 不唤醒:等待方本来就该等新候选的 /me 结果,
  // 而 awaitConfirmedSessionToken 在唤醒后会重新比对 storage 与 confirmedToken,不会用错 token。
  settleConfirmation(nextToken === null ? 'anonymous' : 'pending');
  announce(source);
  return authorizationEpoch;
}

export function readStoredSessionToken(): string | null {
  return readStorage();
}

export function getAuthorizationEpoch(): string {
  return authorizationEpoch;
}

export function isCurrentSessionCandidate(authority: SessionAuthority): boolean {
  return readStorage() === authority.token
    && candidateToken === authority.token
    && authorizationEpoch === authority.epoch;
}

export function stageAuthoritativeSessionToken(
  token: string,
  source: Exclude<SessionCandidateSource, 'logout' | 'storage' | 'storage-mismatch'>,
): string {
  localStorage.setItem(SESSION_TOKEN_KEY, token);
  return replaceCandidate(token, source);
}

export function adoptExternalSessionToken(token: string | null): string {
  return replaceCandidate(token, 'storage');
}

export function confirmAuthoritativeSessionToken(token: string): string | null {
  const stored = readStorage();
  if (stored !== token || candidateToken !== token) return null;
  confirmedToken = token;
  settleConfirmation('confirmed');
  return authorizationEpoch;
}

export function clearAuthoritativeSessionToken(
  expectedToken?: string | null,
  expectedEpoch?: string,
): boolean {
  const stored = readStorage();
  if (expectedToken !== undefined && stored !== expectedToken) return false;
  if (expectedEpoch !== undefined && authorizationEpoch !== expectedEpoch) return false;
  try {
    localStorage.removeItem(SESSION_TOKEN_KEY);
  } finally {
    replaceCandidate(null, 'logout');
  }
  return true;
}

function detectUnmanagedStorageChange(): string | null {
  const stored = readStorage();
  if (stored !== candidateToken) replaceCandidate(stored, 'storage-mismatch');
  return stored;
}

/** Return the token only after this tab confirmed that exact candidate via /me. */
export function getConfirmedSessionToken(): string | null {
  const stored = detectUnmanagedStorageChange();
  return stored !== null && stored === confirmedToken ? stored : null;
}

export function getConfirmedSessionAuthority(): SessionAuthority | null {
  const token = getConfirmedSessionToken();
  return token ? { token, epoch: authorizationEpoch } : null;
}

export function isCurrentSessionAuthority(authority: SessionAuthority): boolean {
  const current = getConfirmedSessionAuthority();
  return current?.token === authority.token && current.epoch === authority.epoch;
}

/**
 * Anonymous requests are allowed; a present-but-unconfirmed token fails closed.
 *
 * [BUG-3] 仍然 fail-closed(未确认绝不返回 token),但按三态抛不同的错,
 * 让调用方能分流成 等待 / 跳登录 / 重试,而不是一律炸成白屏。
 * 能 await 的调用点请改用 awaitConfirmedSessionToken()。
 */
export function requireConfirmedSessionToken(): string | null {
  const stored = detectUnmanagedStorageChange();
  if (stored === null) return null;
  if (stored !== confirmedToken) {
    if (confirmationPhase === 'failed_unreachable') throw new AuthoritativeSessionUnavailableError();
    throw new AuthoritativeSessionPendingError();
  }
  return stored;
}

/**
 * 异步取 token:在途就等,不抛错。
 *
 * 返回 null = 可以按匿名发请求(无 token / 已确认未登录)。
 * 抛 AuthoritativeSessionUnavailableError = 确认不了且重试已耗尽,调用方给"重试"出口。
 *
 * 🔴 fail-open 守卫:任何分支都只在 `stored === confirmedToken` 时才返回 token。
 *    "等到了就放行"是错的 —— 等到的可能是 failed,那种情况必须走 null 或抛错。
 */
export async function awaitConfirmedSessionToken(): Promise<string | null> {
  const stored = detectUnmanagedStorageChange();
  if (stored === null) return null;
  if (stored === confirmedToken) return stored;

  const phase = await waitForSessionConfirmation();
  if (phase === 'confirmed') {
    const settled = detectUnmanagedStorageChange();
    return settled !== null && settled === confirmedToken ? settled : null;
  }
  if (phase === 'anonymous' || phase === 'failed_unauthorized') return null;
  throw new AuthoritativeSessionUnavailableError();
}

/**
 * 只读快照,永不抛错。给"只需要 epoch / 只想顺带做一次 storage 漂移检测"的地方用
 * (例如 dedupe key 计算),它们本来就不该因为会话在途而炸。
 */
export function peekConfirmedSessionToken(): string | null {
  const stored = detectUnmanagedStorageChange();
  return stored !== null && stored === confirmedToken ? stored : null;
}

export function isAuthoritativeSessionToken(token: string | null): boolean {
  return token !== null && token === getConfirmedSessionToken();
}
