import test from 'node:test';
import assert from 'node:assert/strict';

import { validateShadowRelease } from './verify-ten-day-shadow-release.mjs';

const healthy = {
  status: 'ok',
  optional_background_tasks: { background_tasks_enabled: false },
  runtime_loops: {},
};

test('accepts a research-only snapshot from a no-background candidate', () => {
  const result = validateShadowRelease(healthy, {
    run: { status: 'blocked' },
    intraday: {
      pool_run: { status: 'completed' },
      latest_batch: {
        observed_count: 20,
        shadow_eligible_count: 0,
        decision_eligible_count: 0,
        quote_sources: ['tencent_free'],
      },
    },
    scope: 'research_only_no_orders',
  }, { requireIntraday: true, requireNoBackgroundTasks: true });

  assert.equal(result.observedCount, 20);
  assert.equal(result.backgroundTasksEnabled, false);
});

test('rejects a response that crosses the research-only decision boundary', () => {
  assert.throws(() => validateShadowRelease(healthy, {
    intraday: { latest_batch: { observed_count: 1, shadow_eligible_count: 0, decision_eligible_count: 1, quote_sources: ['tencent_free'] } },
    scope: 'research_only_no_orders',
  }), /decision_eligible_count must remain 0/);
});

test('requires a real intraday snapshot only when explicitly requested', () => {
  assert.throws(() => validateShadowRelease(healthy, {
    intraday: { latest_batch: null },
    scope: 'research_only_no_orders',
  }, { requireIntraday: true }), /latest intraday batch is required/);
});
