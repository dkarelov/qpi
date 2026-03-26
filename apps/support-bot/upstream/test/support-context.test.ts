import {
  extractStartPayload,
  formatSupportContextSummary,
  parseSupportContextPayload,
} from '../src/supportContext';

describe('Support context helpers', () => {
  it('parses valid support payloads', () => {
    expect(parseSupportContextPayload('buyer_purchase_P31_L21_S11')).toEqual({
      role: 'buyer',
      topic: 'purchase',
      refs: ['P31', 'L21', 'S11'],
      label: 'покупатель · покупка · P31, L21, S11',
    });
  });

  it('extracts payload from /start commands', () => {
    expect(extractStartPayload('/start seller_listing_L21_S11')).toBe('seller_listing_L21_S11');
  });

  it('returns null for invalid payloads', () => {
    expect(parseSupportContextPayload('buyer_purchase_bad-ref')).toBeNull();
    expect(parseSupportContextPayload('')).toBeNull();
  });

  it('formats user-facing context summary', () => {
    expect(
      formatSupportContextSummary({
        role: 'seller',
        topic: 'deposit',
        refs: ['D91'],
        label: 'продавец · пополнение · D91',
      })
    ).toBe('Контекст обращения: продавец · пополнение · D91');
  });
});
