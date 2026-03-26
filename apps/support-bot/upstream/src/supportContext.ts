import { SupportContext } from './interfaces';

const CONTEXT_ROLES = new Set(['seller', 'buyer']);
const CONTEXT_TOPICS = new Set(['generic', 'shop', 'listing', 'purchase', 'withdraw', 'deposit']);
const REF_RE = /^(S|L|P|W|D|TX)\d+$/i;

const ROLE_LABELS: Record<string, string> = {
  seller: 'продавец',
  buyer: 'покупатель',
};

const TOPIC_LABELS: Record<string, string> = {
  generic: 'общий вопрос',
  shop: 'магазин',
  listing: 'объявление',
  purchase: 'покупка',
  withdraw: 'вывод',
  deposit: 'пополнение',
};

export function extractStartPayload(text?: string | null): string | null {
  const raw = (text || '').trim();
  if (!raw) return null;
  const match = raw.match(/^\/start(?:@\w+)?(?:\s+(.+))?$/i);
  if (!match || !match[1]) return null;
  const payload = match[1].trim();
  return payload || null;
}

export function parseSupportContextPayload(payload?: string | null): SupportContext | null {
  const normalized = (payload || '').trim();
  if (!normalized) return null;
  const parts = normalized.split('_').filter(Boolean);
  if (parts.length < 2) return null;

  const role = parts[0].toLowerCase();
  const topic = parts[1].toLowerCase();
  if (!CONTEXT_ROLES.has(role) || !CONTEXT_TOPICS.has(topic)) {
    return null;
  }

  const refs = parts.slice(2).map((item) => item.toUpperCase());
  if (refs.some((item) => !REF_RE.test(item))) {
    return null;
  }

  return {
    role,
    topic,
    refs,
    label: formatSupportContextLabel(role, topic, refs),
  };
}

export function formatSupportContextLabel(
  role: string,
  topic: string,
  refs: string[] = [],
): string {
  const roleLabel = formatSupportRoleLabel(role);
  const topicLabel = TOPIC_LABELS[topic] || topic;
  return refs.length > 0
    ? `${roleLabel} · ${topicLabel} · ${refs.join(', ')}`
    : `${roleLabel} · ${topicLabel}`;
}

export function formatSupportRoleLabel(role: string): string {
  return ROLE_LABELS[role] || role;
}

export function formatSupportContextSummary(context?: SupportContext | null): string | null {
  if (!context) return null;
  return `Контекст обращения: ${context.label}`;
}
