import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('scripts/deploy-feishu-relay-edge-release.sh', 'utf8');
assert.match(source, /git -C "\$repo_root" rev-parse --verify "\$\{release_sha\}\^\{commit\}"/);
assert.match(source, /trading-hareness-feishu-adapter:\$\{release_sha\}/);
assert.match(source, /RELAY_EDGE_IMAGE_PULL_TIMEOUT_SECONDS:-300/);
assert.match(source, /timeout "\$\{pull_timeout_seconds\}s" docker pull/);
console.log('relay release resolves abbreviated SHAs to immutable image tags');
