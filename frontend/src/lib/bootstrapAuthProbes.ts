import {
  getConfirmedSessionAuthority,
  isCurrentSessionAuthority,
  type SessionAuthority,
} from '@/lib/authoritativeSession';

type BootstrapAgreementProbe = {
  token: string;
  epoch?: string;
  promise: Promise<unknown>;
  controller?: AbortController;
};

type BootstrapWindow = Window & {
  __OMNIRANK_BOOTSTRAP_AGREEMENT__?: BootstrapAgreementProbe;
};

function bootstrapWindow(): BootstrapWindow | null {
  return typeof window === 'undefined' ? null : window as BootstrapWindow;
}

export function clearBootstrapAgreementProbe(): void {
  const target = bootstrapWindow();
  const probe = target?.__OMNIRANK_BOOTSTRAP_AGREEMENT__;
  if (!target || !probe) return;
  delete target.__OMNIRANK_BOOTSTRAP_AGREEMENT__;
  probe.controller?.abort();
}

export function bindBootstrapAgreementProbe(authority: SessionAuthority): void {
  const target = bootstrapWindow();
  const probe = target?.__OMNIRANK_BOOTSTRAP_AGREEMENT__;
  if (!probe) return;
  if (probe.token !== authority.token) {
    clearBootstrapAgreementProbe();
    return;
  }
  probe.epoch = authority.epoch;
}

export function takeBootstrapAgreementProbe<T>(): Promise<T> | null {
  const target = bootstrapWindow();
  const probe = target?.__OMNIRANK_BOOTSTRAP_AGREEMENT__;
  if (!target || !probe) return null;
  delete target.__OMNIRANK_BOOTSTRAP_AGREEMENT__;
  const current = getConfirmedSessionAuthority();
  if (!probe.epoch || !current
      || probe.token !== current.token
      || probe.epoch !== current.epoch) {
    probe.controller?.abort();
    return null;
  }
  const owner = { token: probe.token, epoch: probe.epoch };
  return probe.promise.then(value => {
    if (!isCurrentSessionAuthority(owner)) {
      throw new DOMException('Agreement bootstrap authority changed', 'AbortError');
    }
    return value as T;
  });
}
