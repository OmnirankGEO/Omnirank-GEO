/**
 * 组织员工邀请公开链路 API helper（W1）。
 *
 * 与 organizationApi.ts 分区：本文件只封装邀请接受页与送达状态相关端点，
 * 不改动既有 owner 侧 helper。凭证只进请求体，绝不进 URL / storage。
 */
import {
  organizationPublicRequest,
  organizationRequest,
} from '@/lib/organizationApi';

export type InviteDeliveryState =
  | 'queued'
  | 'sending'
  | 'sent'
  | 'failed'
  | 'expired'
  | 'revoked'
  | 'superseded';

export interface PublicInviteInspect {
  status: 'pending';
  organization_name: string;
  role_name: string;
  target_kind: 'phone' | 'email' | 'username';
  account_mode: 'sign_in' | 'create_operator';
  account_available: boolean;
  expires_at: string;
  agreements: {
    user_terms_version: string;
    privacy_version: string;
  };
  request_id: string;
}

export interface InviteChallenge {
  challenge_id: number;
  status: string;
  expires_at: string;
  delivery_queued: boolean;
  replayed: boolean;
  test_code?: string;
}

export interface InviteVerification {
  challenge_id: number;
  status: 'verified';
  verification_receipt: string;
  replayed: boolean;
}

export interface ChallengeDeliveryStatus {
  challenge_id: number;
  challenge_status: string;
  delivery_state: InviteDeliveryState;
  failure_code: string | null;
  sent_at: string | null;
  expires_at: string;
  request_id: string;
}

export interface OnboardOperatorPayload {
  token: string;
  challenge_id: number;
  verification_receipt: string;
  password: string;
  display_name: string;
  request_id: string;
  terms_accepted: boolean;
  privacy_accepted: boolean;
  terms_version: string;
  privacy_version: string;
}

export interface OnboardOperatorResult {
  success: boolean;
  user_id: number;
  login_username: string;
  membership_id: number;
  organization_id: number;
  status: 'active';
  account_origin: 'organization_invite';
  replayed: boolean;
  auto_login: boolean;
  token?: string;
  user?: Record<string, unknown>;
}

export interface InviteDeliveryStateItem {
  invite_id: number;
  deliveries: Partial<
    Record<
      'invite_link' | 'verification_code',
      {
        state: InviteDeliveryState;
        failure_code: string | null;
        sent_at: string | null;
        updated_at: string;
        attempt_count: number;
      }
    >
  >;
}

export function inspectPublicInvite(token: string, requestId: string) {
  return organizationPublicRequest<PublicInviteInspect>('/invites/inspect', {
    body: JSON.stringify({ token, request_id: requestId }),
  });
}

export function createVerificationChallenge(token: string, requestId: string) {
  return organizationPublicRequest<InviteChallenge>('/invites/verification-challenges', {
    body: JSON.stringify({ token, request_id: requestId }),
  });
}

export function verifyInviteChallenge(
  challengeId: number,
  token: string,
  code: string,
  requestId: string,
) {
  return organizationPublicRequest<InviteVerification>(
    `/invites/verification-challenges/${challengeId}/verify`,
    { body: JSON.stringify({ token, code, request_id: requestId }) },
  );
}

export function getChallengeDeliveryStatus(
  challengeId: number,
  token: string,
  requestId: string,
) {
  return organizationPublicRequest<ChallengeDeliveryStatus>(
    `/invites/verification-challenges/${challengeId}/delivery-status`,
    { body: JSON.stringify({ token, request_id: requestId }) },
  );
}

export function onboardInviteOperator(payload: OnboardOperatorPayload) {
  return organizationPublicRequest<OnboardOperatorResult>('/invites/onboard', {
    body: JSON.stringify(payload),
  });
}

// [WP6] 用户名式邀请:无 challenge_id/verification_receipt,凭 token 直接自设密码开户。
export interface CredentialOnboardPayload {
  token: string;
  password: string;
  display_name: string;
  request_id: string;
  terms_accepted: boolean;
  privacy_accepted: boolean;
  terms_version: string;
  privacy_version: string;
}

export function onboardInviteOperatorCredential(payload: CredentialOnboardPayload) {
  return organizationPublicRequest<OnboardOperatorResult>('/invites/onboard-credential', {
    body: JSON.stringify(payload),
  });
}

/** owner 侧：邀请列表送达状态列数据源（供 W2 挂载 InviteDeliveryStatus） */
export async function getInviteDeliveryStates(inviteIds: number[]) {
  const response = await organizationRequest<{ items: InviteDeliveryStateItem[] }>(
    '/invites/delivery-states',
    { body: JSON.stringify({ invite_ids: inviteIds }) },
  );
  return response.items;
}
