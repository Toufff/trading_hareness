import { Pool } from 'pg';

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
				CREATE INDEX IF NOT EXISTS ingestion_jobs_status_idx ON ingestion_jobs(status, updated_at);
				ALTER TABLE ingestion_assets DROP CONSTRAINT IF EXISTS ingestion_assets_content_sha256_key;
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
		async getJob(jobId) {
			const { rows } = await pool.query(`SELECT j.*, coalesce(json_agg(a ORDER BY a.ordinal) FILTER (WHERE a.asset_id IS NOT NULL), '[]') AS assets FROM ingestion_jobs j LEFT JOIN ingestion_assets a ON a.job_id=j.job_id WHERE j.job_id=$1 GROUP BY j.job_id`, [jobId]);
			return rows[0] ?? null;
		},
		async assetParts(assetId) { const { rows } = await pool.query(`SELECT asset_id,part_index,bytes,sha256,uploaded,remote_status,updated_at FROM ingestion_asset_parts WHERE asset_id=$1 ORDER BY part_index`, [assetId]); return rows; },
		async findCompletedAssets(hashes) {
			if (!hashes.length) return [];
			const { rows } = await pool.query(`SELECT content_sha256,remote_upload_id FROM ingestion_assets WHERE state='completed' AND content_sha256 = ANY($1::text[])`, [hashes]);
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
		async pendingJobs() { const { rows } = await pool.query(`SELECT * FROM ingestion_jobs WHERE status IN ('queued','retryable_failed','uploading') ORDER BY updated_at ASC LIMIT 100`); return rows; },
		async referencedStoragePaths() { const { rows } = await pool.query(`SELECT storage_path FROM ingestion_assets WHERE storage_path IS NOT NULL AND state IN ('pending','uploading','retryable_failed')`); return new Set(rows.map((row) => row.storage_path)); },
		close: () => pool.end(),
	};
}
