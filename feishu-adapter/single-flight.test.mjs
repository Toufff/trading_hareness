import assert from 'node:assert/strict';
import test from 'node:test';
import { singleFlight } from './single-flight.mjs';

test('singleFlight coalesces overlapping delivery ticks and allows the next tick', async () => {
	let calls = 0;
	let release;
	const blocked = new Promise((resolve) => { release = resolve; });
	const run = singleFlight(async () => { calls += 1; await blocked; return calls; });
	const first = run();
	const second = run();
	assert.equal(calls, 0);
	release();
	assert.equal(await first, 1);
	assert.equal(await second, 1);
	assert.equal(await run(), 2);
});
