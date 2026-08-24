import { pathToFileURL } from 'node:url';

function option(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

function nonNegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) throw new Error(`${label} must be a non-negative integer`);
}

export function validateShadowRelease(health, payload, {
  requireIntraday = false,
  requireNoBackgroundTasks = false,
} = {}) {
  if (health?.status !== 'ok') throw new Error(`health status must be ok, received ${health?.status ?? '<missing>'}`);
  const backgroundTasksEnabled = health?.optional_background_tasks?.background_tasks_enabled;
  if (typeof backgroundTasksEnabled !== 'boolean') throw new Error('health must publish optional_background_tasks.background_tasks_enabled');
  if (requireNoBackgroundTasks && backgroundTasksEnabled) throw new Error('candidate unexpectedly has background tasks enabled');
  if (payload?.scope !== 'research_only_no_orders') throw new Error('shadow endpoint must remain research_only_no_orders');

  const batch = payload?.intraday?.latest_batch;
  if (!batch) {
    if (requireIntraday) throw new Error('latest intraday batch is required');
    return { backgroundTasksEnabled, observedCount: 0, hasIntradayBatch: false };
  }
  nonNegativeInteger(batch.observed_count, 'observed_count');
  nonNegativeInteger(batch.shadow_eligible_count, 'shadow_eligible_count');
  nonNegativeInteger(batch.decision_eligible_count, 'decision_eligible_count');
  if (batch.shadow_eligible_count > batch.observed_count) throw new Error('shadow_eligible_count cannot exceed observed_count');
  if (batch.decision_eligible_count !== 0) throw new Error('decision_eligible_count must remain 0 for research-only shadow release');
  if (!Array.isArray(batch.quote_sources) || !batch.quote_sources.length) throw new Error('latest intraday batch must identify quote_sources');
  return { backgroundTasksEnabled, observedCount: batch.observed_count, hasIntradayBatch: true };
}

async function readJson(url, label) {
  const response = await fetch(url, {
    headers: { accept: 'application/json' },
    signal: AbortSignal.timeout(5_000),
  });
  if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}`);
  return await response.json();
}

export async function main() {
  if (process.argv.includes('--help')) {
    console.log('Usage: node scripts/verify-ten-day-shadow-release.mjs --url http://127.0.0.1:5683 [--require-intraday] [--require-no-background-tasks]');
    return;
  }
  const baseUrl = option('--url');
  if (!baseUrl) throw new Error('--url is required');
  const url = baseUrl.replace(/\/$/, '');
  const [health, payload] = await Promise.all([
    readJson(`${url}/health`, 'health'),
    readJson(`${url}/api/v1/research/ten-day-leader-rotation/latest?limit=90`, 'shadow endpoint'),
  ]);
  const result = validateShadowRelease(health, payload, {
    requireIntraday: process.argv.includes('--require-intraday'),
    requireNoBackgroundTasks: process.argv.includes('--require-no-background-tasks'),
  });
  console.log(`shadow release verified: intraday=${result.hasIntradayBatch} observed=${result.observedCount} background_tasks=${result.backgroundTasksEnabled}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
