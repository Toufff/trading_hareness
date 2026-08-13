import { readFileSync, writeFileSync } from 'node:fs';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error('usage: split-remote-archive-sync-workflows.mjs INPUT OUTPUT');
const document = JSON.parse(readFileSync(inputPath, 'utf8'));
const source = Array.isArray(document) ? document[0] : document;
const commonNames = new Set(['交易时段与盘后同步远端报告', 'Read remote analysts', 'Fan out analysts']);
const reportNames = new Set(['Read report cursor', 'Read latest report page', 'Select changed report details', 'Read changed report detail', 'Import changed report', 'Advance report cursor']);
const messageNames = new Set(['Read message cursor', 'Read message pages', 'Select message delta to watermark', 'Read remote message detail', 'Import remote message', 'Advance message cursor']);

function build(kind, id, name, streamNames) {
  const names = new Set([...commonNames, ...streamNames]);
  const nodes = (source.nodes ?? []).filter((node) => names.has(node.name));
  const connections = {};
  for (const [from, value] of Object.entries(source.connections ?? {})) {
    if (!names.has(from)) continue;
    const main = (value.main ?? []).map((branch) => branch.filter((edge) => names.has(edge.node)));
    if (main.some((branch) => branch.length)) connections[from] = { ...value, main };
  }
  return {
    ...source,
    id,
    name,
    active: true,
    nodes,
    connections,
    metadata: { ...(source.metadata ?? {}), codex_stream: kind, source_workflow: source.id },
  };
}

const reportDeltaCode = source.nodes?.find((node) => node.name === 'Select changed report details')?.parameters?.jsCode ?? '';
if (!reportDeltaCode.includes('report_versions: versions')) throw new Error('report delta must advance a complete version snapshot');

writeFileSync(outputPath, JSON.stringify([
  build('reports', 'remoteArchiveReports123', '市场研究：同步远端分析师报告（报告流）', reportNames),
  build('messages', 'remoteArchiveMessages123', '市场研究：同步远端分析师消息（消息流）', messageNames),
], null, 2) + '\n');
