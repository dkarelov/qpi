import cache from './cache';
import * as log from 'fancy-log';

const METADATA_TOKEN_URL = 'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token';
const MONITORING_WRITE_URL = 'https://monitoring.api.cloud.yandex.net/monitoring/v2/data/write';
const TOKEN_REFRESH_MARGIN_SECONDS = 60;

let cachedToken: { value: string; expiresAtMs: number } | null = null;

async function getIamToken(): Promise<string> {
  const now = Date.now();
  if (cachedToken && cachedToken.expiresAtMs > now) {
    return cachedToken.value;
  }

  const response = await fetch(METADATA_TOKEN_URL, {
    headers: { 'Metadata-Flavor': 'Google' },
  });
  if (!response.ok) {
    throw new Error(`metadata token HTTP ${response.status}`);
  }
  const payload: any = await response.json();
  const accessToken = String(payload.access_token || '').trim();
  if (!accessToken) {
    throw new Error('metadata token response did not include access_token');
  }
  const expiresIn = Number(payload.expires_in || TOKEN_REFRESH_MARGIN_SECONDS);
  cachedToken = {
    value: accessToken,
    expiresAtMs: now + Math.max(0, expiresIn - TOKEN_REFRESH_MARGIN_SECONDS) * 1000,
  };
  return accessToken;
}

export function recordSupportMetric(
  name: string,
  labels: Record<string, string> = {},
  value = 1,
) {
  const folderId = String(cache.config.yc_folder_id || '').trim();
  log.info('support_metric_recorded', {
    metric_name: name,
    labels,
    value,
    monitoring_enabled: Boolean(folderId),
  });
  if (!folderId) {
    return;
  }

  void writeMetric(folderId, name, labels, value).catch((err) => {
    log.warn('support_metric_write_failed', {
      metric_name: name,
      error_type: err?.constructor?.name || 'Error',
    });
  });
}

async function writeMetric(
  folderId: string,
  name: string,
  labels: Record<string, string>,
  value: number,
) {
  const token = await getIamToken();
  const url = `${MONITORING_WRITE_URL}?folderId=${encodeURIComponent(folderId)}&service=custom`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      metrics: [
        {
          name,
          labels,
          type: 'DGAUGE',
          value,
        },
      ],
    }),
  });
  if (!response.ok) {
    throw new Error(`monitoring write HTTP ${response.status}`);
  }
  await response.text();
}
