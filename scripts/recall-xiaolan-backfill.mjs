import fs from 'node:fs';
const path = process.argv[2] || '/tmp/xiaolan-backfill-20260815.json';
const records = JSON.parse(fs.readFileSync(path, 'utf8'));
const ids = [...new Set(Object.values(records).map((item) => item.target_message_id).filter(Boolean))];
const tokenResponse = await fetch('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal', {
  method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify({app_id: process.env.FEISHU_APP_ID, app_secret: process.env.FEISHU_APP_SECRET}),
});
const token = (await tokenResponse.json()).tenant_access_token;
let recalled = 0; const failures = [];
for (const id of ids) {
  const response = await fetch(`https://open.feishu.cn/open-apis/im/v1/messages/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: {Authorization: `Bearer ${token}`},
  });
  if (response.ok) recalled += 1; else failures.push({id, status: response.status, body: (await response.text()).slice(0, 160)});
  await new Promise((resolve) => setTimeout(resolve, 120));
}
console.log(JSON.stringify({total: ids.length, recalled, failures: failures.length, sample: failures.slice(0, 5)}));
