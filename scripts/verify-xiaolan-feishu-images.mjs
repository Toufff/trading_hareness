const chatId = process.argv[2] || 'oc_523b7e9e29854acba64272a948cb8eda';
const tokenResponse = await fetch('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify({app_id: process.env.FEISHU_APP_ID, app_secret: process.env.FEISHU_APP_SECRET}),
});
const tokenBody = await tokenResponse.json();
if (!tokenResponse.ok || tokenBody.code || !tokenBody.tenant_access_token) throw new Error('token_failed:' + JSON.stringify(tokenBody));
const token = tokenBody.tenant_access_token;
let pageToken = ''; const messages = [];
for (let page = 0; page < 20; page += 1) {
  const url = new URL('https://open.feishu.cn/open-apis/im/v1/messages');
  url.searchParams.set('container_id_type', 'chat'); url.searchParams.set('container_id', chatId);
  url.searchParams.set('page_size', '50'); url.searchParams.set('sort_type', 'ByCreateTimeDesc');
  if (pageToken) url.searchParams.set('page_token', pageToken);
  const response = await fetch(url, {headers: {Authorization: 'Bearer ' + token}});
  const body = await response.json(); if (!response.ok || body.code) throw new Error('history_failed:' + JSON.stringify(body));
  messages.push(...(body.data?.items || [])); pageToken = body.data?.page_token || '';
  if (!body.data?.has_more || !pageToken) break;
}
const start = Date.parse('2026-08-29T00:00:00+08:00');
const end = Date.parse('2026-08-29T12:00:00+08:00');
const rows = messages.filter((item) => {
  const ms = Number(item.create_time); const content = String(item.body?.content || '');
  return !item.deleted && ms >= start && ms < end && content.includes('#xiaolan');
});
const details = rows.map((item) => {
  let content = {}; try { content = JSON.parse(item.body?.content || '{}'); } catch {}
  const nodes = (content.content || []).flat();
  return {id: item.message_id, create_time: item.create_time, msg_type: item.msg_type,
    image_nodes: nodes.filter((node) => node.tag === 'img').map(({image_key, width, height}) => ({image_key, width, height})),
    text: nodes.filter((node) => node.tag === 'text').map((node) => node.text).join('\\n').slice(0, 160)};
});
const candidates = details.flatMap((row) => row.image_nodes.map((image) => ({...image, message_id: row.id})));
const downloads = [];
for (const image of candidates) {
  const response = await fetch('https://open.feishu.cn/open-apis/im/v1/images/' + encodeURIComponent(image.image_key), {
    headers: {Authorization: 'Bearer ' + token},
  });
  const bytes = Buffer.from(await response.arrayBuffer());
  downloads.push({message_id: image.message_id, image_key: image.image_key, declared_size: (image.width || '?') + 'x' + (image.height || '?'),
    status: response.status, content_type: response.headers.get('content-type'), bytes: bytes.length,
    magic: bytes.subarray(0, 12).toString('hex')});
}
console.log(JSON.stringify({history_items: messages.length, morning_xiaolan: rows.length,
  posts_with_images: details.filter((row) => row.image_nodes.length).length,
  posts_without_images: details.filter((row) => !row.image_nodes.length).length,
  missing_image_posts: details.filter((row) => !row.image_nodes.length).slice(0, 20),
  tested_downloads: downloads.length, downloads}, null, 2));
