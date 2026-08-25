/** Typed transport boundary for the Feishu workbench feature. */

import { getJson, postJson } from './http';

export const feishuWorkbenchApi = {
  status: <T>() => getJson<T>('/api/feishu-workbench/status'),
  messages: <T>() => getJson<T>('/api/feishu-workbench/messages?limit=80'),
  inspectApplication: <T>() => postJson<T>('/api/feishu-workbench/application-inspection'),
  updateMessageState: (sourceMessageId: string, action: string) => postJson('/api/feishu-workbench/actions', {
    source_message_id: sourceMessageId, action,
  }),
  searchMessages: <T>(query: string) => postJson<T>('/api/feishu-workbench/message-search', { query }),
  submit: (path: string, body: Record<string, unknown> = {}) => postJson(path, body),
};
