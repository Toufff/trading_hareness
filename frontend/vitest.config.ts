import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    restoreMocks: true,
    // Bound jsdom workers on the shared Windows deployment host. Unbounded
    // imports exceeded the timeout and leaked a still-running test into the
    // next test's fetch mocks; keep assertions/timeouts unchanged.
    maxWorkers: 2,
    // Full-suite jsdom workers can contend while dashboard polling tests wait
    // on fake fetch batches.  Keep the default strict enough to catch hangs,
    // but do not let normal parallel CPU contention create a five-second flake.
    testTimeout: 15_000,
  },
});
