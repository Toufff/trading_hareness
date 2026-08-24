import { Pool } from 'pg';

export function completedAssetLookupSql() {
	return `SELECT DISTINCT a.content_sha256,a.remote_upload_id
		FROM ingestion_assets a
		JOIN ingestion_jobs j ON j.job_id = a.job_id
		WHERE a.state = 'completed'
			AND j.status = 'completed'
			AND j.remote_batch_id IS NOT NULL
			AND a.content_sha256 = ANY($1::text[])`;
}

export function createLedger(connectionString) {
	const pool = new Pool(connectionString ? { connectionString, max: 4, idleTimeoutMillis: 30_000 } : { max: 4, idleTimeoutMillis: 30_000 });
	return {
		async init(registry) {
			await pool.query(`
				CREATE EXTENSION IF NOT EXISTS pgcrypto;
				CREATE TABLE IF NOT EXISTS ingestion_topics (topic_key text PRIMARY KEY, label text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS ingestion_publishers (publisher_key text PRIMARY KEY, label text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS ingestion_source_profiles (source_tag text PRIMARY KEY, topic_key text NOT NULL REFERENCES ingestion_topics(topic_key), publisher_key text NOT NULL REFERENCES ingestion_publishers(publisher_key), analyst_id text NOT NULL, label text NOT NULL, enabled boolean NOT NULL DEFAULT true, config jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS ingestion_jobs (job_id uuid PRIMARY KEY, event_id text UNIQUE, message_id text UNIQUE, source_tag text NOT NULL REFERENCES ingestion_source_profiles(source_tag), topic_key text NOT NULL, publisher_key text NOT NULL, analyst_id text NOT NULL, content_sha256 text, remote_batch_id text, status text NOT NULL, stage text NOT NULL, attempt_count integer NOT NULL DEFAULT 0, last_http_status integer, error_class text, error_message text, payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS ingestion_content_items (item_id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE, content_type text NOT NULL, content_sha256 text, content_date date, content_time time, body text, remote_item_id text, state text NOT NULL DEFAULT 'pending', created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(job_id, content_type, content_sha256));
				CREATE TABLE IF NOT EXISTS ingestion_assets (asset_id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE, ordinal integer NOT NULL, filename text NOT NULL, media_type text NOT NULL, declared_bytes bigint NOT NULL, content_sha256 text NOT NULL, storage_path text, remote_upload_id text, state text NOT NULL DEFAULT 'pending', created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(job_id, ordinal), UNIQUE(content_sha256));
				CREATE TABLE IF NOT EXISTS ingestion_asset_parts (asset_id uuid NOT NULL REFERENCES ingestion_assets(asset_id) ON DELETE CASCADE, part_index integer NOT NULL, bytes integer NOT NULL, sha256 text NOT NULL, uploaded boolean NOT NULL DEFAULT false, remote_status integer, updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(asset_id, part_index));
				CREATE TABLE IF NOT EXISTS ingestion_errors (error_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid REFERENCES ingestion_jobs(job_id) ON DELETE SET NULL, execution_id text, workflow_id text, node_name text, http_status integer, error_class text, message text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS analysis_jobs (analysis_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE, status text NOT NULL DEFAULT 'pending', result jsonb NOT NULL DEFAULT '{}'::jsonb, error_message text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS feishu_group_relay_sources (source_key text PRIMARY KEY, chat_id text NOT NULL, cursor_create_time bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS feishu_group_relay_messages (source_message_id text PRIMARY KEY, source_key text NOT NULL, source_chat_id text NOT NULL, source_create_time bigint NOT NULL, target_chat_id text NOT NULL, route_tag text NOT NULL, message jsonb NOT NULL, status text NOT NULL, attempt_count integer NOT NULL DEFAULT 0, target_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb, error_message text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), forwarded_at timestamptz);
				CREATE TABLE IF NOT EXISTS feishu_group_relay_actions (action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), source_message_id text NOT NULL REFERENCES feishu_group_relay_messages(source_message_id) ON DELETE CASCADE, action text NOT NULL, actor_open_id text, created_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS feishu_group_relay_routes (source_key text PRIMARY KEY, chat_id text NOT NULL, chat_name text NOT NULL, route_tag text NOT NULL UNIQUE, enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
			CREATE TABLE IF NOT EXISTS feishu_group_relay_route_state (state_key text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
			CREATE TABLE IF NOT EXISTS feishu_summary_listener_state (listener_key text PRIMARY KEY, chat_id text NOT NULL, cursor_create_time bigint NOT NULL, last_source_create_time bigint, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
			CREATE TABLE IF NOT EXISTS feishu_relay_writer_ownership (singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), writer_id text NOT NULL, generation bigint NOT NULL CHECK (generation > 0), updated_at timestamptz NOT NULL DEFAULT now());
				CREATE TABLE IF NOT EXISTS feishu_user_oauth_tokens (token_key text PRIMARY KEY, access_ciphertext text NOT NULL, refresh_ciphertext text NOT NULL, access_expires_at timestamptz NOT NULL, refresh_expires_at timestamptz NOT NULL, scopes text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now());
				CREATE INDEX IF NOT EXISTS feishu_group_relay_messages_status_idx ON feishu_group_relay_messages(status, updated_at);
				CREATE INDEX IF NOT EXISTS feishu_group_relay_actions_message_idx ON feishu_group_relay_actions(source_message_id, created_at DESC);
				CREATE INDEX IF NOT EXISTS ingestion_jobs_status_idx ON ingestion_jobs(status, updated_at);
				ALTER TABLE ingestion_assets DROP CONSTRAINT IF EXISTS ingestion_assets_content_sha256_key;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS action_card_message_id text;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS workflow_state text NOT NULL DEFAULT 'new';
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS workflow_note text;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS workflow_actor_open_id text;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS source_update_time bigint;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS source_deleted boolean NOT NULL DEFAULT false;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS reconciled_at timestamptz;
				ALTER TABLE feishu_group_relay_routes ADD COLUMN IF NOT EXISTS target_chat_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
				ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS target_chat_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
			ALTER TABLE feishu_group_relay_messages ADD COLUMN IF NOT EXISTS intelligence jsonb NOT NULL DEFAULT '{}'::jsonb;
			ALTER TABLE feishu_summary_listener_state ADD COLUMN IF NOT EXISTS last_source_create_time bigint;
				CREATE INDEX IF NOT EXISTS ingestion_assets_sha256_idx ON ingestion_assets(content_sha256);
			`);
			for (const route of registry.routes ?? []) {
				await pool.query('INSERT INTO ingestion_topics(topic_key,label) VALUES($1,$2) ON CONFLICT(topic_key) DO NOTHING', [route.topic_key ?? registry.default_topic_key ?? 'general', route.topic_key ?? registry.default_topic_key ?? 'general']);
				await pool.query('INSERT INTO ingestion_publishers(publisher_key,label) VALUES($1,$2) ON CONFLICT(publisher_key) DO NOTHING', [route.publisher_key, route.label ?? route.publisher_key]);
				await pool.query(`INSERT INTO ingestion_source_profiles(source_tag,topic_key,publisher_key,analyst_id,label,enabled,config) VALUES($1,$2,$3,$4,$5,$6,$7)
					ON CONFLICT(source_tag) DO UPDATE SET topic_key=EXCLUDED.topic_key,publisher_key=EXCLUDED.publisher_key,analyst_id=EXCLUDED.analyst_id,label=EXCLUDED.label,enabled=EXCLUDED.enabled,config=EXCLUDED.config,updated_at=now()`,
					[String(route.tag).toLowerCase(), route.topic_key ?? registry.default_topic_key ?? 'general', route.publisher_key, route.remote_analyst_id, route.label ?? route.tag, route.enabled !== false, route]);
			}
		},
		async getOrCreateJob({ jobId, eventId, messageId, route, payload, contentSha256 }) {
			const existing = await pool.query('SELECT * FROM ingestion_jobs WHERE event_id=$1 OR message_id=$2 LIMIT 1', [eventId ?? null, messageId ?? null]);
			if (existing.rowCount) return { job: existing.rows[0], duplicate: true };
			const result = await pool.query(`INSERT INTO ingestion_jobs(job_id,event_id,message_id,source_tag,topic_key,publisher_key,analyst_id,content_sha256,status,stage,payload)
				VALUES($1,$2,$3,$4,$5,$6,$7,$8,'queued','received',$9) RETURNING *`, [jobId, eventId ?? null, messageId ?? null, route.tag, route.topic_key, route.publisher_key, route.remote_analyst_id, contentSha256 ?? null, payload]);
			return { job: result.rows[0], duplicate: false };
		},
		async getJobByMessageId(messageId) {
			if (!messageId) return null;
			const { rows } = await pool.query('SELECT * FROM ingestion_jobs WHERE message_id=$1', [messageId]);
			return rows[0] ?? null;
		},
		async getFeishuUserOauthToken() { const { rows } = await pool.query("SELECT * FROM feishu_user_oauth_tokens WHERE token_key='default'"); return rows[0] ?? null; },
		async saveFeishuUserOauthToken({ accessCiphertext, refreshCiphertext, accessExpiresAt, refreshExpiresAt, scopes }) {
			await pool.query(`INSERT INTO feishu_user_oauth_tokens(token_key,access_ciphertext,refresh_ciphertext,access_expires_at,refresh_expires_at,scopes)
				VALUES('default',$1,$2,$3,$4,$5) ON CONFLICT(token_key) DO UPDATE SET access_ciphertext=EXCLUDED.access_ciphertext,refresh_ciphertext=EXCLUDED.refresh_ciphertext,access_expires_at=EXCLUDED.access_expires_at,refresh_expires_at=EXCLUDED.refresh_expires_at,scopes=EXCLUDED.scopes,updated_at=now()`, [accessCiphertext, refreshCiphertext, accessExpiresAt, refreshExpiresAt, scopes]);
		},
		async relayWriterFence(writerId) {
			if (!/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(String(writerId ?? ''))) throw new Error('relay writer ID 格式无效');
			const { rows } = await pool.query(`
				INSERT INTO feishu_relay_writer_ownership(singleton,writer_id,generation)
				VALUES(true,$1,1)
				ON CONFLICT(singleton) DO UPDATE SET updated_at=feishu_relay_writer_ownership.updated_at
				RETURNING writer_id,generation,updated_at`, [writerId]);
			const row = rows[0];
			return { allowed: row.writer_id === writerId, writer_id: row.writer_id, generation: Number(row.generation), updated_at: row.updated_at };
		},
		async relayWriterStatus() {
			const { rows } = await pool.query('SELECT writer_id,generation,updated_at FROM feishu_relay_writer_ownership WHERE singleton=true');
			return rows[0] ? { writer_id: rows[0].writer_id, generation: Number(rows[0].generation), updated_at: rows[0].updated_at } : null;
		},
		async relaySourceState(sourceKey) { const { rows } = await pool.query('SELECT * FROM feishu_group_relay_sources WHERE source_key=$1', [sourceKey]); return rows[0] ?? null; },
		async summaryListenerState(listenerKey) { const { rows } = await pool.query('SELECT * FROM feishu_summary_listener_state WHERE listener_key=$1', [listenerKey]); return rows[0] ?? null; },
		async saveSummaryListenerCursor({ listenerKey, chatId, cursorCreateTime, lastSourceCreateTime = null }) {
			await pool.query(`INSERT INTO feishu_summary_listener_state(listener_key,chat_id,cursor_create_time,last_source_create_time) VALUES($1,$2,$3,$4)
				ON CONFLICT(listener_key) DO UPDATE SET chat_id=EXCLUDED.chat_id,cursor_create_time=GREATEST(feishu_summary_listener_state.cursor_create_time,EXCLUDED.cursor_create_time),last_source_create_time=CASE WHEN EXCLUDED.last_source_create_time IS NULL THEN feishu_summary_listener_state.last_source_create_time ELSE GREATEST(coalesce(feishu_summary_listener_state.last_source_create_time,0),EXCLUDED.last_source_create_time) END,updated_at=now()`, [listenerKey, chatId, cursorCreateTime, lastSourceCreateTime]);
		},
		async initializeRelayRoutes(routes) {
			// Keep this operation idempotent, but do not gate it on a one-time
			// initialization marker. New config-declared sources (for example a
			// group that is currently only known by name) must be materialized after
			// an upgrade without overwriting routes edited in the dashboard.
			await pool.query(`INSERT INTO feishu_group_relay_route_state(state_key) VALUES('initialized') ON CONFLICT DO NOTHING`);
			for (const route of routes) {
				await pool.query(`INSERT INTO feishu_group_relay_routes(source_key,chat_id,chat_name,route_tag,enabled,target_chat_ids) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(source_key) DO NOTHING`, [route.key, route.chatId, route.chatName, route.tag, route.enabled !== false, JSON.stringify(route.targetChatIds ?? [])]);
			}
			return this.relayRoutes();
		},
		async relayRoutes() {
			const { rows } = await pool.query(`SELECT source_key,chat_id,chat_name,route_tag,enabled,target_chat_ids,created_at,updated_at FROM feishu_group_relay_routes ORDER BY created_at,source_key`);
			return rows.map((row) => ({ key: row.source_key, chatId: row.chat_id, chatName: row.chat_name, tag: row.route_tag, targetChatIds: Array.isArray(row.target_chat_ids) ? row.target_chat_ids : [], enabled: row.enabled !== false, created_at: row.created_at, updated_at: row.updated_at }));
		},
		async createRelayRoute({ sourceKey, chatId, chatName, tag, targetChatIds = [], enabled = true }) {
			const { rows } = await pool.query(`INSERT INTO feishu_group_relay_routes(source_key,chat_id,chat_name,route_tag,enabled,target_chat_ids) VALUES($1,$2,$3,$4,$5,$6) RETURNING source_key,chat_id,chat_name,route_tag,enabled,target_chat_ids,created_at,updated_at`, [sourceKey, chatId, chatName, tag, enabled, JSON.stringify(targetChatIds)]);
			const row = rows[0];
			return { key: row.source_key, chatId: row.chat_id, chatName: row.chat_name, tag: row.route_tag, targetChatIds: row.target_chat_ids, enabled: row.enabled !== false, created_at: row.created_at, updated_at: row.updated_at };
		},
		async updateRelayRoute(sourceKey, { chatId, chatName, tag, targetChatIds = [], enabled }) {
			const { rows } = await pool.query(`UPDATE feishu_group_relay_routes SET chat_id=$2,chat_name=$3,route_tag=$4,enabled=$5,target_chat_ids=$6,updated_at=now() WHERE source_key=$1 RETURNING source_key,chat_id,chat_name,route_tag,enabled,target_chat_ids,created_at,updated_at`, [sourceKey, chatId, chatName, tag, enabled, JSON.stringify(targetChatIds)]);
			if (!rows[0]) return null;
			const row = rows[0];
			return { key: row.source_key, chatId: row.chat_id, chatName: row.chat_name, tag: row.route_tag, targetChatIds: row.target_chat_ids, enabled: row.enabled !== false, created_at: row.created_at, updated_at: row.updated_at };
		},
		async deleteRelayRoute(sourceKey) { const { rowCount } = await pool.query('DELETE FROM feishu_group_relay_routes WHERE source_key=$1', [sourceKey]); return rowCount === 1; },
		async saveRelaySourceCursor({ sourceKey, chatId, cursorCreateTime }) {
			await pool.query(`INSERT INTO feishu_group_relay_sources(source_key,chat_id,cursor_create_time) VALUES($1,$2,$3)
				ON CONFLICT(source_key) DO UPDATE SET chat_id=EXCLUDED.chat_id,cursor_create_time=GREATEST(feishu_group_relay_sources.cursor_create_time,EXCLUDED.cursor_create_time),updated_at=now()`, [sourceKey, chatId, cursorCreateTime]);
		},
		async skipRelayMessage(record) {
			await pool.query(`INSERT INTO feishu_group_relay_messages(source_message_id,source_key,source_chat_id,source_create_time,target_chat_id,route_tag,message,status,source_update_time,source_deleted)
				VALUES($1,$2,$3,$4,$5,$6,$7,'skipped_bootstrap',$8,$9) ON CONFLICT(source_message_id) DO NOTHING`, [record.sourceMessageId, record.sourceKey, record.sourceChatId, record.sourceCreateTime, record.targetChatId, record.routeTag, record.message, record.sourceUpdateTime ?? null, Boolean(record.message?.deleted)]);
		},
		async filterRelayMessage(record, reason) {
			await pool.query(`INSERT INTO feishu_group_relay_messages(source_message_id,source_key,source_chat_id,source_create_time,target_chat_id,route_tag,message,status,source_update_time,source_deleted,error_message)
				VALUES($1,$2,$3,$4,$5,$6,$7,'filtered_system',$8,$9,$10)
				ON CONFLICT(source_message_id) DO UPDATE SET source_key=EXCLUDED.source_key,source_chat_id=EXCLUDED.source_chat_id,source_create_time=EXCLUDED.source_create_time,target_chat_id=EXCLUDED.target_chat_id,route_tag=EXCLUDED.route_tag,message=EXCLUDED.message,status='filtered_system',source_update_time=coalesce(EXCLUDED.source_update_time,feishu_group_relay_messages.source_update_time),source_deleted=EXCLUDED.source_deleted,error_message=EXCLUDED.error_message,updated_at=now()`, [record.sourceMessageId ?? record.source_message_id, record.sourceKey ?? record.source_key, record.sourceChatId ?? record.source_chat_id, record.sourceCreateTime ?? record.source_create_time, record.targetChatId ?? record.target_chat_id, record.routeTag ?? record.route_tag, record.message, record.sourceUpdateTime ?? record.source_update_time ?? null, Boolean(record.message?.deleted), reason]);
		},
		async claimRelayMessage(record) {
			const { rows } = await pool.query(`INSERT INTO feishu_group_relay_messages(source_message_id,source_key,source_chat_id,source_create_time,target_chat_id,route_tag,message,status,attempt_count,source_update_time)
				VALUES($1,$2,$3,$4,$5,$6,$7,'processing',1,$8)
				ON CONFLICT(source_message_id) DO UPDATE SET source_key=EXCLUDED.source_key,source_chat_id=EXCLUDED.source_chat_id,source_create_time=EXCLUDED.source_create_time,target_chat_id=EXCLUDED.target_chat_id,route_tag=EXCLUDED.route_tag,message=EXCLUDED.message,source_update_time=coalesce(EXCLUDED.source_update_time,feishu_group_relay_messages.source_update_time),status='processing',attempt_count=feishu_group_relay_messages.attempt_count+1,error_message=null,updated_at=now()
				WHERE (feishu_group_relay_messages.status='failed' AND feishu_group_relay_messages.updated_at <= now() - interval '10 seconds' * power(2, least(greatest(feishu_group_relay_messages.attempt_count - 1, 0), 5)))
					OR (feishu_group_relay_messages.status='processing' AND feishu_group_relay_messages.updated_at < now() - interval '5 minutes')
				RETURNING *`, [record.sourceMessageId ?? record.source_message_id, record.sourceKey ?? record.source_key, record.sourceChatId ?? record.source_chat_id, record.sourceCreateTime ?? record.source_create_time, record.targetChatId ?? record.target_chat_id, record.routeTag ?? record.route_tag, record.message, record.sourceUpdateTime ?? record.source_update_time ?? null]);
			return rows[0] ?? null;
		},
		async markRelayMessage(sourceMessageId, { status, targetMessageIds = [], errorMessage = null }) {
			await pool.query(`UPDATE feishu_group_relay_messages SET status=$2,target_message_ids=$3,error_message=$4,forwarded_at=CASE WHEN $2='sent' THEN now() ELSE forwarded_at END,updated_at=now() WHERE source_message_id=$1`, [sourceMessageId, status, JSON.stringify(targetMessageIds), errorMessage]);
		},
		async getRelayMessage(sourceMessageId) {
			const { rows } = await pool.query(`SELECT message.*, route.chat_name AS source_chat_name
				FROM feishu_group_relay_messages message
				LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key
				WHERE message.source_message_id=$1`, [sourceMessageId]);
			return rows[0] ?? null;
		},
		async getRelayMessageByActionCard(actionCardMessageId) {
			const { rows } = await pool.query(`SELECT message.*, route.chat_name AS source_chat_name
				FROM feishu_group_relay_messages message
				LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key
				WHERE message.action_card_message_id=$1`, [actionCardMessageId]);
			return rows[0] ?? null;
		},
		async setRelayActionCard(sourceMessageId, actionCardMessageId) {
			const { rows } = await pool.query(`UPDATE feishu_group_relay_messages SET action_card_message_id=$2,updated_at=now() WHERE source_message_id=$1 RETURNING *`, [sourceMessageId, actionCardMessageId]);
			return rows[0] ?? null;
		},
		async updateRelayWorkflow(sourceMessageId, { workflowState, workflowNote, actorOpenId = null, action }) {
			const { rows } = await pool.query(`UPDATE feishu_group_relay_messages SET workflow_state=$2,workflow_note=$3,workflow_actor_open_id=$4,updated_at=now() WHERE source_message_id=$1 RETURNING *`, [sourceMessageId, workflowState, workflowNote, actorOpenId]);
			if (!rows[0]) return null;
			await pool.query(`INSERT INTO feishu_group_relay_actions(source_message_id,action,actor_open_id) VALUES($1,$2,$3)`, [sourceMessageId, action, actorOpenId]);
			const result = await pool.query(`SELECT message.*, route.chat_name AS source_chat_name FROM feishu_group_relay_messages message LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key WHERE message.source_message_id=$1`, [sourceMessageId]);
			return result.rows[0];
		},
		async updateRelaySourceMessage(sourceMessageId, { message, sourceUpdateTime, sourceDeleted = false }) {
			const { rows } = await pool.query(`UPDATE feishu_group_relay_messages SET message=$2,source_update_time=$3,source_deleted=$4,reconciled_at=now(),updated_at=now() WHERE source_message_id=$1 RETURNING *`, [sourceMessageId, message, sourceUpdateTime ?? null, sourceDeleted]);
			if (!rows[0]) return null;
			const result = await pool.query(`SELECT message.*, route.chat_name AS source_chat_name FROM feishu_group_relay_messages message LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key WHERE message.source_message_id=$1`, [sourceMessageId]);
			return result.rows[0];
		},
		async recordRelayIntelligence(sourceMessageId, kind, value) {
			const { rows } = await pool.query(`UPDATE feishu_group_relay_messages
				SET intelligence=coalesce(intelligence, '{}'::jsonb) || jsonb_build_object($2::text, $3::jsonb), updated_at=now()
				WHERE source_message_id=$1 RETURNING *`, [sourceMessageId, kind, JSON.stringify(value)]);
			if (!rows[0]) return null;
			const result = await pool.query(`SELECT message.*, route.chat_name AS source_chat_name FROM feishu_group_relay_messages message LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key WHERE message.source_message_id=$1`, [sourceMessageId]);
			return result.rows[0] ?? null;
		},
		async relayRetryQueue(limit = 20) { const { rows } = await pool.query(`SELECT * FROM feishu_group_relay_messages WHERE status='failed' AND updated_at <= now() - interval '10 seconds' * power(2, least(greatest(attempt_count - 1, 0), 5)) ORDER BY updated_at ASC LIMIT $1`, [Math.max(1, Math.min(100, Number(limit) || 20))]); return rows; },
		async portableInteractiveSummaryUpgradeQueue(limit = 20) {
			const { rows } = await pool.query(`SELECT * FROM feishu_group_relay_messages
				WHERE status='sent' AND message->>'msg_type'='interactive'
					AND coalesce(intelligence->>'portable_summary_version', '') <> 'interactive-text-summary-v1'
				ORDER BY updated_at ASC LIMIT $1`, [Math.max(1, Math.min(100, Number(limit) || 20))]);
			return rows;
		},
		async markPortableSummaryVersion(sourceMessageId, version) {
			await pool.query(`UPDATE feishu_group_relay_messages
				SET intelligence=jsonb_set(coalesce(intelligence, '{}'::jsonb), '{portable_summary_version}', to_jsonb($2::text), true), updated_at=now()
				WHERE source_message_id=$1`, [sourceMessageId, version]);
		},
		async recentRelayMessages(limit = 50) {
			const { rows } = await pool.query(`SELECT message.source_message_id,message.source_key,message.source_chat_id,message.source_create_time,message.target_chat_id,message.route_tag,message.message,message.status,message.target_message_ids,message.error_message,message.forwarded_at,message.updated_at,message.action_card_message_id,message.workflow_state,message.workflow_note,message.source_deleted,message.source_update_time,message.intelligence,route.chat_name AS source_chat_name
				FROM feishu_group_relay_messages message
				LEFT JOIN feishu_group_relay_routes route ON route.source_key=message.source_key
				ORDER BY message.updated_at DESC LIMIT $1`, [Math.max(1, Math.min(200, Number(limit) || 50))]);
			return rows;
		},
		async relayStatus() {
			const { rows } = await pool.query(`
				SELECT source.source_key, source.updated_at AS last_polled_at,
					to_timestamp(latest.source_create_time / 1000.0) AS last_source_message_at,
					latest.status AS last_message_status,
					forwarded.last_forwarded_at,
					coalesce(failures.failed_count, 0)::int AS failed_count,
					failures.latest_failure_at, failures.latest_failure_error
				FROM feishu_group_relay_sources source
				LEFT JOIN LATERAL (
					SELECT source_create_time, status
					FROM feishu_group_relay_messages
					WHERE source_key = source.source_key
					ORDER BY source_create_time DESC, updated_at DESC
					LIMIT 1
				) latest ON true
				LEFT JOIN LATERAL (
					SELECT max(forwarded_at) AS last_forwarded_at
					FROM feishu_group_relay_messages
					WHERE source_key = source.source_key AND status = 'sent'
				) forwarded ON true
				LEFT JOIN LATERAL (
					SELECT count(*) FILTER (WHERE status = 'failed') AS failed_count,
						max(updated_at) FILTER (WHERE status = 'failed') AS latest_failure_at,
						(array_agg(error_message ORDER BY updated_at DESC) FILTER (WHERE status = 'failed'))[1] AS latest_failure_error
					FROM feishu_group_relay_messages
					WHERE source_key = source.source_key
				) failures ON true
				ORDER BY source.source_key`);
			return rows;
		},
		async getJob(jobId) {
			const { rows } = await pool.query(`SELECT j.*, coalesce(json_agg(a ORDER BY a.ordinal) FILTER (WHERE a.asset_id IS NOT NULL), '[]') AS assets FROM ingestion_jobs j LEFT JOIN ingestion_assets a ON a.job_id=j.job_id WHERE j.job_id=$1 GROUP BY j.job_id`, [jobId]);
			return rows[0] ?? null;
		},
		async assetParts(assetId) { const { rows } = await pool.query(`SELECT asset_id,part_index,bytes,sha256,uploaded,remote_status,updated_at FROM ingestion_asset_parts WHERE asset_id=$1 ORDER BY part_index`, [assetId]); return rows; },
		async findCompletedAssets(hashes) {
			if (!hashes.length) return [];
			const { rows } = await pool.query(completedAssetLookupSql(), [hashes]);
			return rows;
		},
		async recordContentItem(jobId, item) {
			await pool.query(`INSERT INTO ingestion_content_items(item_id,job_id,content_type,content_sha256,content_date,content_time,body,state) VALUES(gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7) ON CONFLICT(job_id,content_type,content_sha256) DO UPDATE SET body=EXCLUDED.body`, [jobId, item.content_type, item.content_sha256 ?? null, item.content_date ?? null, item.content_time ?? null, item.body ?? null, item.state ?? 'pending']);
		},
		async recordAsset(jobId, ordinal, asset) {
			const result = await pool.query(`INSERT INTO ingestion_assets(asset_id,job_id,ordinal,filename,media_type,declared_bytes,content_sha256,storage_path,state)
				VALUES(gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,'pending') ON CONFLICT(job_id,ordinal) DO UPDATE SET filename=EXCLUDED.filename,media_type=EXCLUDED.media_type,declared_bytes=EXCLUDED.declared_bytes,content_sha256=EXCLUDED.content_sha256,storage_path=EXCLUDED.storage_path,updated_at=now() RETURNING asset_id`, [jobId, ordinal, asset.filename, asset.media_type, asset.declared_bytes, asset.content_sha256, asset.path ?? null]);
			const assetId = result.rows[0].asset_id;
			for (const [partIndex, part] of asset.parts.entries()) await pool.query(`INSERT INTO ingestion_asset_parts(asset_id,part_index,bytes,sha256) VALUES($1,$2,$3,$4) ON CONFLICT(asset_id,part_index) DO NOTHING`, [assetId, partIndex, part.bytes, part.sha256]);
			return assetId;
		},
		async updateJob(jobId, patch) {
			const allowed = ['status', 'stage', 'remote_batch_id', 'attempt_count', 'last_http_status', 'error_class', 'error_message'];
			const entries = Object.entries(patch).filter(([key]) => allowed.includes(key));
			if (!entries.length) return;
			const sets = entries.map(([key], index) => `${key}=$${index + 2}`).join(', ');
			await pool.query(`UPDATE ingestion_jobs SET ${sets}, updated_at=now() WHERE job_id=$1`, [jobId, ...entries.map(([, value]) => value)]);
		},
		async recordPart(assetId, index, status) { await pool.query('UPDATE ingestion_asset_parts SET uploaded=true,remote_status=$3,updated_at=now() WHERE asset_id=$1 AND part_index=$2', [assetId, index, status]); },
		async recordRemoteParts(assetId, receivedParts) { await pool.query('UPDATE ingestion_asset_parts SET uploaded=true,remote_status=200,updated_at=now() WHERE asset_id=$1 AND part_index = ANY($2::int[])', [assetId, receivedParts]); },
		async updateAssetSession(assetId, state, remoteUploadId = null) { await pool.query(`UPDATE ingestion_assets SET state=$2,remote_upload_id=coalesce($3,remote_upload_id),updated_at=now() WHERE asset_id=$1`, [assetId, state, remoteUploadId]); },
		async markAssets(jobId, state, remoteUploadId = null) { await pool.query('UPDATE ingestion_assets SET state=$2,remote_upload_id=coalesce($3,remote_upload_id),updated_at=now() WHERE job_id=$1', [jobId, state, remoteUploadId]); },
		async retryJob(jobId) { const { rows } = await pool.query(`UPDATE ingestion_jobs SET status='queued',stage='manual_retry',error_class=null,error_message=null,updated_at=now() WHERE job_id=$1 AND status IN ('failed','retryable_failed','duplicate','queued') RETURNING *`, [jobId]); return rows[0] ?? null; },
		async retryQueue() { const { rows } = await pool.query(`SELECT j.*, coalesce(json_agg(json_build_object('asset',a,'parts',coalesce((SELECT json_agg(p ORDER BY p.part_index) FROM ingestion_asset_parts p WHERE p.asset_id=a.asset_id),'[]'))) FILTER (WHERE a.asset_id IS NOT NULL),'[]') AS resources FROM ingestion_jobs j LEFT JOIN ingestion_assets a ON a.job_id=j.job_id WHERE j.status='queued' AND j.stage='manual_retry' GROUP BY j.job_id ORDER BY j.updated_at LIMIT 5`); return rows; },
		async markRetryRunning(jobId) { const { rowCount } = await pool.query(`UPDATE ingestion_jobs SET status='uploading',stage='retry_running',attempt_count=attempt_count+1,updated_at=now() WHERE job_id=$1 AND status='queued' AND stage='manual_retry'`, [jobId]); return rowCount === 1; },
		async recordError(error) { await pool.query(`INSERT INTO ingestion_errors(job_id,execution_id,workflow_id,node_name,http_status,error_class,message,payload) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, [error.job_id ?? null, error.execution_id ?? null, error.workflow_id ?? null, error.node_name ?? null, error.http_status ?? null, error.error_class ?? 'n8n_error', error.message, error.payload ?? {}]); },
		async queueAnalysisByMessage(messageId, batchId) {
			await pool.query(`INSERT INTO analysis_jobs(job_id) SELECT job_id FROM ingestion_jobs WHERE message_id=$1 ON CONFLICT(job_id) DO UPDATE SET updated_at=analysis_jobs.updated_at`, [messageId]);
			await pool.query(`UPDATE ingestion_jobs j SET status='analysis_pending',stage='analysis_queued',remote_batch_id=coalesce($2,remote_batch_id),updated_at=now()
				WHERE j.message_id=$1 AND EXISTS (SELECT 1 FROM analysis_jobs a WHERE a.job_id=j.job_id AND a.status='pending')`, [messageId, batchId ?? null]);
		},
		async pendingAnalysis() { const { rows } = await pool.query(`SELECT a.analysis_id,a.job_id,j.payload,j.remote_batch_id FROM analysis_jobs a JOIN ingestion_jobs j ON j.job_id=a.job_id WHERE a.status='pending' ORDER BY a.created_at LIMIT 20`); return rows; },
		async completeAnalysis(analysisId, result) { await pool.query(`UPDATE analysis_jobs SET status='completed',result=$2,updated_at=now() WHERE analysis_id=$1`, [analysisId, result]); await pool.query(`UPDATE ingestion_jobs SET status='completed',stage='analysis_completed',updated_at=now() WHERE job_id=(SELECT job_id FROM analysis_jobs WHERE analysis_id=$1)`, [analysisId]); },
		async pruneHistory(retentionDays = 90) {
			const days = Math.max(7, Math.min(3650, Number(retentionDays) || 90));
			const errors = await pool.query("DELETE FROM ingestion_errors WHERE created_at < now() - ($1 * interval '1 day')", [days]);
			const jobs = await pool.query("DELETE FROM ingestion_jobs WHERE status IN ('completed','duplicate') AND updated_at < now() - ($1 * interval '1 day')", [days]);
			return { errors: errors.rowCount ?? 0, jobs: jobs.rowCount ?? 0, retention_days: days };
		},
		async metrics() { const { rows } = await pool.query(`SELECT status,stage,count(*)::int AS count,coalesce(sum(attempt_count),0)::int AS attempts FROM ingestion_jobs GROUP BY status,stage ORDER BY status,stage`); return rows; },
		async observability() {
			const { rows } = await pool.query(`SELECT
				(SELECT count(*)::int FROM ingestion_jobs WHERE status IN ('queued','uploading','submitting','analysis_pending')) AS queue_depth,
				(SELECT count(*)::int FROM ingestion_jobs WHERE status='duplicate') AS duplicates,
				(SELECT count(*)::int FROM ingestion_jobs WHERE status='completed') AS completed,
				(SELECT count(*)::int FROM ingestion_jobs WHERE status IN ('failed','retryable_failed')) AS failed,
				(SELECT coalesce(sum(declared_bytes),0)::bigint FROM ingestion_assets WHERE state='completed') AS completed_media_bytes,
				(SELECT coalesce(avg(extract(epoch FROM updated_at-created_at)),0)::float FROM ingestion_jobs WHERE status='completed') AS completed_seconds`);
			return rows[0];
		},
		async ingestionStatusBySource() {
			const { rows } = await pool.query(`
				WITH counts AS (
					SELECT source_tag,
						count(*)::int AS job_count,
						count(*) FILTER (WHERE status='completed')::int AS completed_count,
						count(*) FILTER (WHERE status IN ('failed','retryable_failed'))::int AS failed_count,
						max(updated_at) AS last_updated_at
					FROM ingestion_jobs GROUP BY source_tag
				), latest AS (
					SELECT DISTINCT ON (source_tag) source_tag,status,stage,remote_batch_id,error_class,error_message,updated_at
					FROM ingestion_jobs ORDER BY source_tag,updated_at DESC,created_at DESC
				)
				SELECT counts.*,latest.status AS latest_status,latest.stage AS latest_stage,latest.remote_batch_id,latest.error_class,latest.error_message
				FROM counts JOIN latest USING(source_tag) ORDER BY counts.source_tag
			`);
			return rows;
		},
		async pendingJobs() { const { rows } = await pool.query(`SELECT * FROM ingestion_jobs WHERE status IN ('queued','retryable_failed','uploading') ORDER BY updated_at ASC LIMIT 100`); return rows; },
		async referencedStoragePaths() { const { rows } = await pool.query(`SELECT storage_path FROM ingestion_assets WHERE storage_path IS NOT NULL AND state IN ('pending','uploading','retryable_failed')`); return new Set(rows.map((row) => row.storage_path)); },
		close: () => pool.end(),
	};
}
