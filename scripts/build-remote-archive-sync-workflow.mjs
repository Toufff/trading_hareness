import { readFileSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const [sourcePath, outputPath] = process.argv.slice(2);
if (!sourcePath || !outputPath) throw new Error('usage: build-remote-archive-sync-workflow.mjs SOURCE_TEXT_WORKFLOW OUTPUT');
const source = JSON.parse(readFileSync(sourcePath, 'utf8'));
const sourceWorkflow = Array.isArray(source) ? source[0] : source;
const credentials = sourceWorkflow.nodes?.find((node) => node.credentials?.httpBearerAuth)?.credentials;
if (!credentials?.httpBearerAuth) throw new Error('source workflow has no httpBearerAuth credential');

// Endpoint and bearer material are intentionally not versioned.  The operator
// supplies the base URL through n8n's non-exported environment configuration;
// credentials stay as a reference to the existing n8n bearer credential.
const quant = 'http://quant-research:8000/api/v1';
const remoteUrl = (suffixExpression) => `={{ ($env.REMOTE_ANALYST_ARCHIVE_BASE_URL || '').replace(/\\/$/, '') + ${suffixExpression} }}`;
const http = (name, position, parameters) => ({
  id: randomUUID(), name, type: 'n8n-nodes-base.httpRequest', typeVersion: 4.4, position,
  parameters: { authentication: 'genericCredentialType', genericAuthType: 'httpBearerAuth', options: { timeout: 120000, response: { includeInputData: true } }, ...parameters },
  credentials, retryOnFail: true, maxTries: 4, waitBetweenTries: 5000,
});
const local = (name, position, parameters) => ({
  id: randomUUID(), name, type: 'n8n-nodes-base.httpRequest', typeVersion: 4.4, position,
  parameters: { options: { timeout: 30000, response: { includeInputData: true } }, ...parameters }, retryOnFail: true, maxTries: 3, waitBetweenTries: 2000,
});

const schedule = { id: randomUUID(), name: '交易时段与盘后同步远端报告', type: 'n8n-nodes-base.scheduleTrigger', typeVersion: 1.2, position: [0, 0], parameters: { rule: { interval: [
  { field: 'cronExpression', expression: '*/15 9-11,13-14 * * 1-5' },
  { field: 'cronExpression', expression: '20 18 * * 1-5' },
] } } };
const analysts = http('Read remote analysts', [220, 0], { url: remoteUrl("'/analysts'"), method: 'GET' });
const fanout = { id: randomUUID(), name: 'Fan out analysts', type: 'n8n-nodes-base.code', typeVersion: 2, position: [440, 0], parameters: { mode: 'runOnceForAllItems', jsCode: "return ($input.first().json.items ?? []).filter((item) => item.analyst_id).map((item) => ({ json: item }));" } };

// Reports are an independent, bounded delta stream.  A list row produces a
// detail request only when its version/hash differs from the locally persisted
// cursor.  Any report failure ends only its own item; the message branch keeps
// running from the fanout's second output.
const reportCursor = local('Read report cursor', [650, -220], { url: `={{ '${quant}/remote-archive/sync-cursors/reports/' + encodeURIComponent($json.analyst_id) }}`, method: 'GET' });
// The cursor response uses `remote_analyst_id`; using analyst_id here would
// silently produce `/analysts/undefined/...` after the cursor node.
const reports = http('Read latest report page', [650, -80], { url: remoteUrl("'/analysts/' + encodeURIComponent($json.remote_analyst_id) + '/reports'"), method: 'GET', sendQuery: true, specifyQuery: 'json', jsonQuery: '={"limit":100,"offset":0}' });
const reportDelta = { id: randomUUID(), name: 'Select changed report details', type: 'n8n-nodes-base.code', typeVersion: 2, position: [880, -120], parameters: { mode: 'runOnceForAllItems', jsCode: `
const cursor = $('Read report cursor').item.json.report_versions ?? {};
const analystId = $('Read report cursor').item.json.remote_analyst_id;
const changed = [];
const versions = {};
for (const report of ($input.first().json.items ?? [])) {
  const key = String(report.date ?? '');
  const stamp = String(report.version ?? '') + ':' + String(report.content_hash ?? '');
  if (!key || !stamp) continue;
  versions[key] = stamp;
  if (cursor[key] !== stamp) changed.push({ analyst_id: analystId, date: report.date });
}
return changed.map((item) => ({ json: { ...item, report_versions: versions } }));` } };
const reportDetail = http('Read changed report detail', [1100, -120], { url: remoteUrl("'/analysts/' + encodeURIComponent($json.analyst_id) + '/reports/' + encodeURIComponent($json.date)"), method: 'GET' });
const reportImport = local('Import changed report', [1320, -120], { url: `${quant}/remote-archive/reports/import`, method: 'POST', sendBody: true, contentType: 'json', specifyBody: 'json', jsonBody: '={{ JSON.stringify({ report: $json }) }}', sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Quant-Write-Key', value: '={{ $env.QUANT_WRITE_API_KEY }}' }] } });
const reportCursorWrite = local('Advance report cursor', [1540, -120], { url: `${quant}/remote-archive/sync-cursors`, method: 'PUT', sendBody: true, contentType: 'json', specifyBody: 'json', jsonBody: '={{ JSON.stringify({ stream_key: "reports", analyst_id: $("Select changed report details").first().json.analyst_id, report_versions: $("Select changed report details").first().json.report_versions }) }}', sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Quant-Write-Key', value: '={{ $env.QUANT_WRITE_API_KEY }}' }] } });

// Messages use the remote archive's signed global change feed.  Do not use
// n8n's pagination node here: an empty terminal page previously caused an
// unbounded replay/OOM loop.  One run reads at most 100 summaries; the opaque
// cursor advances only after every detail has been imported locally.
const messageCursor = local('Read global message cursor', [650, 250], { url: `={{ '${quant}/remote-archive/sync-cursors-global/message_updates' }}`, method: 'GET' });
const messages = http('Read message updates', [650, 390], { url: remoteUrl("'/messages/updates'"), method: 'GET', sendQuery: true, specifyQuery: 'json', jsonQuery: '={{ JSON.stringify($json.remote_cursor ? { cursor: $json.remote_cursor, limit: 100 } : { received_after: $json.received_after || new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(), limit: 100 }) }}' });
const messageDelta = { id: randomUUID(), name: 'Select global message delta', type: 'n8n-nodes-base.code', typeVersion: 2, position: [900, 330], parameters: { mode: 'runOnceForAllItems', jsCode: `
const envelope = $input.first().json;
const items = Array.isArray(envelope.items) ? envelope.items : [];
const nextCursor = envelope.next_cursor ?? null;
return items.filter((message) => message.message_id && message.analyst_id).map((message) => ({
  json: { analyst_id: message.analyst_id, message_id: message.message_id,
    cursor_received_at: message.received_at ?? null, next_cursor: nextCursor,
    terminal: nextCursor === null, page_message_ids: items.map((item) => item.message_id).filter(Boolean) }
}));` } };
const messageDetail = http('Read remote message detail', [1120, 330], { url: remoteUrl("'/analysts/' + encodeURIComponent($json.analyst_id) + '/messages/' + encodeURIComponent($json.message_id)"), method: 'GET' });
const messageImport = local('Import remote message', [1340, 330], { url: `${quant}/remote-archive/messages/import`, method: 'POST', sendBody: true, contentType: 'json', specifyBody: 'json', jsonBody: '={{ JSON.stringify({ message: $json }) }}', sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Quant-Write-Key', value: '={{ $env.QUANT_WRITE_API_KEY }}' }] } });
// Advance the durable cursor only after every detail in the page has been
// imported. A partial page must be retried on the next run, never skipped.
const messagePageReady = { id: randomUUID(), name: 'Aggregate imported global message page', type: 'n8n-nodes-base.code', typeVersion: 2, position: [1560, 330], parameters: { mode: 'runOnceForAllItems', jsCode: `
const imported = $input.all();
const delta = $('Select global message delta').all();
if (!delta.length) return [];
const expected = new Set(delta.map((item) => item.json.message_id).filter(Boolean));
// The local HTTP node returns the import result rather than the original
// request body, so count is the stable completion contract here. Any detail
// or import error aborts the branch before this node runs.
if (imported.length !== expected.size) {
  throw new Error('global message page did not import every detail; cursor not advanced');
}
const tail = delta[delta.length - 1].json;
return [{ json: { stream_key: 'message_updates', cursor: tail.next_cursor, received_after: tail.cursor_received_at,
  terminal: tail.terminal, message_ids: [...expected] } }];` } };
const messageCursorWrite = local('Advance global message cursor', [1780, 330], { url: `${quant}/remote-archive/sync-cursors-global`, method: 'PUT', sendBody: true, contentType: 'json', specifyBody: 'json', jsonBody: '={{ JSON.stringify($json) }}', sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Quant-Write-Key', value: '={{ $env.QUANT_WRITE_API_KEY }}' }] } });

const workflow = { id: 'remoteArchiveSync123', name: '市场研究：同步远端分析师报告', active: true,
  nodes: [schedule, analysts, fanout, reportCursor, reports, reportDelta, reportDetail, reportImport, reportCursorWrite, messageCursor, messages, messageDelta, messageDetail, messageImport, messagePageReady, messageCursorWrite],
  connections: {
    [schedule.name]: { main: [[{ node: analysts.name, type: 'main', index: 0 }, { node: messageCursor.name, type: 'main', index: 0 }]] },
    [analysts.name]: { main: [[{ node: fanout.name, type: 'main', index: 0 }]] },
    [fanout.name]: { main: [[{ node: reportCursor.name, type: 'main', index: 0 }]] },
    [reportCursor.name]: { main: [[{ node: reports.name, type: 'main', index: 0 }]] },
    [reports.name]: { main: [[{ node: reportDelta.name, type: 'main', index: 0 }]] },
    [reportDelta.name]: { main: [[{ node: reportDetail.name, type: 'main', index: 0 }]] },
    [reportDetail.name]: { main: [[{ node: reportImport.name, type: 'main', index: 0 }]] },
    [reportImport.name]: { main: [[{ node: reportCursorWrite.name, type: 'main', index: 0 }]] },
    [messageCursor.name]: { main: [[{ node: messages.name, type: 'main', index: 0 }]] },
    [messages.name]: { main: [[{ node: messageDelta.name, type: 'main', index: 0 }]] },
    [messageDelta.name]: { main: [[{ node: messageDetail.name, type: 'main', index: 0 }]] },
    [messageDetail.name]: { main: [[{ node: messageImport.name, type: 'main', index: 0 }]] },
    [messageImport.name]: { main: [[{ node: messagePageReady.name, type: 'main', index: 0 }]] },
    [messagePageReady.name]: { main: [[{ node: messageCursorWrite.name, type: 'main', index: 0 }]] },
  },
  settings: { executionOrder: 'v1', timezone: 'Asia/Shanghai' },
};
writeFileSync(outputPath, JSON.stringify([workflow], null, 2) + '\n');
