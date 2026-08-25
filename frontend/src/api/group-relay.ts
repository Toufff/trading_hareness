/** Typed transport boundary for managed group-relay routes. */

import { deleteJson, getJson, postJson, putJson } from './http';

export type GroupRelayRouteInput = {
  chat_name: string;
  chat_id?: string;
  tag: string;
  target_chat_ids?: string[];
  target_chat_names?: string[];
  enabled: boolean;
};

export const groupRelayApi = {
  status: <T>() => getJson<T>('/api/group-relay/status'),
  upsertRoute: <T>(key: string, input: GroupRelayRouteInput) => (
    key
      ? putJson<T>(`/api/group-relay/routes/${encodeURIComponent(key)}`, input)
      : postJson<T>('/api/group-relay/routes', input)
  ),
  removeRoute: <T>(key: string) => deleteJson<T>(`/api/group-relay/routes/${encodeURIComponent(key)}`),
};
