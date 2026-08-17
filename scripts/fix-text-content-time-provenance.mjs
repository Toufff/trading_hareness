import { readFileSync, writeFileSync } from 'node:fs';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error('usage: fix-text-content-time-provenance.mjs INPUT OUTPUT');

const document = JSON.parse(readFileSync(inputPath, 'utf8'));
const workflow = Array.isArray(document) ? document[0] : document;
if (workflow.id !== 'xo3AHKRr4MFXrzFA') throw new Error('expected the Feishu text workflow');
const node = (name) => workflow.nodes.find((entry) => entry.name === name);
const code = node('Code in JavaScript');
const addText = node('HTTP Request1');
if (!code?.parameters?.jsCode || !addText?.parameters) throw new Error('text workflow shape is not recognized');

const oldTimeSetup = [
  "const contentDate = String(payload.content_date ?? new Date(payload.receivedAt ?? Date.now()).toISOString().slice(0, 10));",
  "const contentTime = String(payload.content_time ?? new Date(payload.receivedAt ?? Date.now()).toISOString().slice(11, 16));",
  "if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(contentDate) || !/^\\d{2}:\\d{2}$/.test(contentTime)) throw new Error('适配器提供的内容时间格式无效');",
].join('\n');
const newTimeSetup = [
  '// content_date/content_time mean an explicit user timestamp to the remote worker.',
  '// Receipt time is provenance, so omit both for ordinary Feishu analyst messages.',
  "const contentDate = payload.content_date == null ? '' : String(payload.content_date);",
  "const contentTime = payload.content_time == null ? '' : String(payload.content_time);",
  "if ((contentDate || contentTime) && (!/^\\d{4}-\\d{2}-\\d{2}$/.test(contentDate) || !/^\\d{2}:\\d{2}$/.test(contentTime))) {",
  "  throw new Error('适配器提供的显式内容时间格式无效');",
  '}',
].join('\n');
if (!code.parameters.jsCode.includes(oldTimeSetup)) throw new Error('unexpected content-time setup; aborting without a partial patch');
code.parameters.jsCode = code.parameters.jsCode
  .replace(oldTimeSetup, newTimeSetup)
  .replace('    content_date: contentDate,\n    content_time: contentTime,', '    ...(contentDate ? { content_date: contentDate, content_time: contentTime } : {}),');

addText.parameters.contentType = 'json';
addText.parameters.specifyBody = 'json';
addText.parameters.jsonBody = [
  '={{ JSON.stringify({',
  "  idempotency_key: $('Code in JavaScript').first().json.item_key,",
  "  content: $('Code in JavaScript').first().json.content,",
  "  content_sha256: $('Code in JavaScript').first().json.content_sha256,",
  "  ...($('Code in JavaScript').first().json.content_date ? {",
  "    content_date: $('Code in JavaScript').first().json.content_date,",
  "    content_time: $('Code in JavaScript').first().json.content_time,",
  '  } : {}),',
  "  source_label: $('Code in JavaScript').first().json.source_label,",
  '}) }}',
].join('\n');
delete addText.parameters.bodyParameters;

writeFileSync(outputPath, JSON.stringify(Array.isArray(document) ? [workflow] : workflow, null, 2) + '\n');
