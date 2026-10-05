export const ALERT_ACTION_CONTRACT_VERSION = 'alert-action-v1' as const;

export interface GovernableAlertAction {
  action: string;
  target: string;
  permission: string;
  recovery: string;
  evidence?: never;
  confirmStatus?: never;
}

export interface EvidenceAlertAction {
  evidence: string;
  action?: never;
  target?: never;
  permission?: never;
  recovery?: never;
  confirmStatus?: string;
}

export interface StatusAlertAction {
  confirmStatus: string;
  evidence?: string;
  action?: never;
  target?: never;
  permission?: never;
  recovery?: never;
}

export type AlertActionContract = GovernableAlertAction | EvidenceAlertAction | StatusAlertAction;

export function alertActionData(contract: AlertActionContract): Record<string, string> {
  return {
    'data-alert-contract': ALERT_ACTION_CONTRACT_VERSION,
    ...('action' in contract && contract.action ? {
      'data-alert-action': contract.action,
      'data-alert-target': contract.target,
      'data-alert-permission': contract.permission,
      'data-alert-recovery': contract.recovery,
    } : {}),
    ...('evidence' in contract && contract.evidence ? { 'data-alert-evidence': contract.evidence } : {}),
    ...('confirmStatus' in contract && contract.confirmStatus ? { 'data-alert-status': contract.confirmStatus } : {}),
  };
}
