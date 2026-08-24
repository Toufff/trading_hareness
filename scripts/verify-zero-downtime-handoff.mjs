import { readFileSync } from 'node:fs';

function option(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

function requireText(source, expected, label) {
  if (!source.includes(expected)) throw new Error(`${label} missing required handoff guard: ${expected}`);
}

async function readHealth(baseUrl, label) {
  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/health`, {
    headers: { accept: 'application/json' },
    signal: AbortSignal.timeout(5_000),
  });
  if (!response.ok) throw new Error(`${label} health returned HTTP ${response.status}`);
  const body = await response.json();
  if (body.status !== 'ok') throw new Error(`${label} health status must be ok, received ${body.status ?? '<missing>'}`);
  return body;
}

function activeLoopKeys(health, label) {
  const loops = health.runtime_loops;
  if (!loops || typeof loops !== 'object') throw new Error(`${label} does not publish runtime_loops`);
  const entries = Object.entries(loops);
  if (!entries.length) throw new Error(`${label} has no active runtime loops`);
  for (const [name, state] of entries) {
    if (!state || typeof state !== 'object') throw new Error(`${label}:${name} has no loop state`);
    if (!['running', 'lease_owned', 'waiting_for_lease', 'standby'].includes(String(state.state))) {
      throw new Error(`${label}:${name} is not handoff-safe (${state.state ?? '<missing>'})`);
    }
    if (state.last_error) throw new Error(`${label}:${name} has an active error: ${state.last_error}`);
  }
  return entries.map(([name]) => name).sort();
}

const nginx = readFileSync('deploy/quant-research-gateway.nginx.conf', 'utf8');
const backend = readFileSync('deploy/quant-research-gateway-backend.conf', 'utf8');
const compose = readFileSync('compose.yaml', 'utf8');
const handoffCompose = readFileSync('deploy/compose.quant-handoff.yaml', 'utf8');
const handoffPreflight = readFileSync('scripts/preflight-quant-handoff.sh', 'utf8');

requireText(nginx, 'proxy_next_upstream off;', 'gateway nginx config');
requireText(nginx, 'proxy_pass http://quant_research_backend;', 'gateway nginx config');
requireText(nginx, 'proxy_read_timeout 420s;', 'gateway nginx config');
requireText(nginx, 'proxy_send_timeout 420s;', 'gateway nginx config');
requireText(backend, 'server quant-research:8000;', 'gateway default backend');
requireText(backend, 'keepalive 16;', 'gateway default backend');
requireText(compose, 'QUANT_BACKGROUND_TASKS_ENABLED: "false"', 'preflight compose config');
requireText(compose, 'profiles: ["preflight"]', 'preflight compose config');
requireText(handoffCompose, 'quant-research-handoff:', 'handoff candidate compose config');
requireText(handoffCompose, 'ports: !reset []', 'handoff candidate compose config');
requireText(handoffCompose, 'QUANT_HANDOFF_BACKGROUND_TASKS_ENABLED:-false', 'handoff candidate compose config');
if (handoffCompose.includes('container_name: n8n-quant-research\n')) {
  throw new Error('handoff candidate must never reuse the live quant-research container name');
}
requireText(handoffPreflight, 'QUANT_BACKGROUND_TASKS_ENABLED == "false"', 'handoff candidate preflight');
requireText(handoffPreflight, "payload['scope'] == 'research_only_no_orders'", 'handoff candidate preflight');

const activeUrl = option('--active');
const gatewayUrl = option('--gateway');
if (Boolean(activeUrl) !== Boolean(gatewayUrl)) {
  throw new Error('--active and --gateway must be supplied together');
}

if (activeUrl && gatewayUrl) {
  const [activeHealth, gatewayHealth] = await Promise.all([
    readHealth(activeUrl, 'active service'),
    readHealth(gatewayUrl, 'gateway'),
  ]);
  const activeLoops = activeLoopKeys(activeHealth, 'active service');
  const gatewayLoops = activeLoopKeys(gatewayHealth, 'gateway');
  if (activeLoops.join(',') !== gatewayLoops.join(',')) {
    throw new Error(`gateway loop view diverged from active service: active=${activeLoops.join(',')} gateway=${gatewayLoops.join(',')}`);
  }
  console.log(`live handoff health verified: ${activeLoops.length} active loops via direct service and gateway`);
} else {
  console.log('static zero-downtime handoff guards verified');
}
