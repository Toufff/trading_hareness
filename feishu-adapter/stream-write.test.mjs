import assert from 'node:assert/strict';
import test from 'node:test';
import { Writable } from 'node:stream';
import { endWritable, writeChunk } from './stream-write.mjs';

test('writeChunk removes its error listener after each backpressure drain', async () => {
	const sink = new Writable({
		highWaterMark: 1,
		write(_chunk, _encoding, callback) { setImmediate(callback); },
	});

	for (let index = 0; index < 24; index++) {
		await writeChunk(sink, Buffer.from(String(index)));
		assert.equal(sink.listenerCount('error'), 0);
	}
	await endWritable(sink);
	assert.equal(sink.listenerCount('error'), 0);
});
