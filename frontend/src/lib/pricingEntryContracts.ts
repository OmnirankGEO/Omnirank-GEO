import contractDocument from '@/contracts/pricing-entry-contracts.json';

export type GeoPricingEntryId = 'customer-recharge' | 'agent-inventory-purchase';

export interface GeoPricingEntryContract {
  entry_id: GeoPricingEntryId;
  active_route: string;
  catalog_endpoint: string;
  quote_endpoints: Record<string, string>;
  order_endpoint: string;
  order_method: 'POST';
  quote_type: 'retail' | 'procurement';
  required_order_field: 'price_quote_id';
}

const entries = contractDocument.entries as GeoPricingEntryContract[];

export function getGeoPricingEntryContract(entryId: GeoPricingEntryId): GeoPricingEntryContract {
  const entry = entries.find((candidate) => candidate.entry_id === entryId);
  if (!entry || entry.required_order_field !== 'price_quote_id' || entry.order_method !== 'POST') {
    throw new Error(`GEO pricing entry contract invalid: ${entryId}`);
  }
  return entry;
}

export function buildQuotedOrderBody(
  entryId: GeoPricingEntryId,
  params: {
    priceQuoteId: string;
    idempotencyKey: string;
    channel?: string;
    paymentMethod?: string;
  },
): Record<string, string> {
  const entry = getGeoPricingEntryContract(entryId);
  return {
    [entry.required_order_field]: params.priceQuoteId,
    idempotency_key: params.idempotencyKey,
    channel: params.channel || 'auto',
    ...(params.paymentMethod ? { payment_method: params.paymentMethod } : {}),
  };
}

export const GEO_PRICING_ENTRY_CONTRACT_VERSION = contractDocument.contract_version;
