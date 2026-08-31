import test from 'node:test';
import assert from 'node:assert/strict';
import { args } from './baidu-pan-upload-file.mjs';

test('upload helper requires exactly a source file and remote path', () => {
	assert.deepEqual(args(['part.gz', '/archive/part.gz']), { file: 'part.gz', remotePath: '/archive/part.gz' });
	assert.throws(() => args(['part.gz']), /usage/);
});
