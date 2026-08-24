"""Add a profile-gated, monotonic journal for edge evidence handoff.

Revision ID: 20260824_0058
Revises: 20260824_0057
"""

from hashlib import sha1

from alembic import op


revision = "20260824_0058"
down_revision = "20260824_0057"
branch_labels = None
depends_on = None


_TRIGGERS = (
    ("ten_day_leader_rotation_runs", "run_id"),
    ("ten_day_leader_rotation_candidates", "run_id", "symbol"),
    ("intraday_scan_runs", "scan_id"),
    ("intraday_signal_episodes", "episode_id"),
    ("intraday_quote_observations", "quote_observation_id"),
    ("intraday_minute_sessions", "symbol", "trading_date", "minute_bucket", "source_name"),
    ("intraday_board_flow_snapshots", "flow_snapshot_id"),
    ("intraday_board_reports", "board_report_id"),
    ("intraday_board_rotation_events", "rotation_event_id"),
    ("intraday_signal_events", "signal_event_id"),
    ("intraday_rule_input_snapshots", "rule_input_snapshot_id"),
    ("ten_day_leader_rotation_intraday_observations", "observation_id"),
)


def _trigger_name(table_name: str) -> str:
    """Keep trigger identifiers below PostgreSQL's 63-byte identifier limit."""
    return f"edge_ev_{sha1(table_name.encode('utf-8')).hexdigest()[:16]}"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.edge_evidence_changes (
            sequence_id bigserial PRIMARY KEY,
            table_name text NOT NULL,
            record_key jsonb NOT NULL,
            row_data jsonb NOT NULL,
            changed_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS edge_evidence_changes_sequence_idx
            ON quant.edge_evidence_changes(sequence_id);
        CREATE INDEX IF NOT EXISTS edge_evidence_changes_changed_at_idx
            ON quant.edge_evidence_changes(changed_at DESC);

        CREATE OR REPLACE FUNCTION quant.capture_edge_evidence_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            payload jsonb := to_jsonb(NEW);
            identity jsonb := '{}'::jsonb;
            key_name text;
        BEGIN
            -- The research workstation imports into the same schema.  It must
            -- apply evidence idempotently without producing an echo journal.
            IF current_setting('app.quant_runtime_profile', true) IS DISTINCT FROM 'intraday_edge' THEN
                RETURN NEW;
            END IF;
            FOREACH key_name IN ARRAY TG_ARGV LOOP
                identity := identity || jsonb_build_object(key_name, payload -> key_name);
            END LOOP;
            INSERT INTO quant.edge_evidence_changes(table_name, record_key, row_data)
            VALUES (TG_TABLE_NAME, identity, payload);
            RETURN NEW;
        END;
        $$;
    """)
    for table_name, *keys in _TRIGGERS:
        trigger_name = _trigger_name(table_name)
        quoted_keys = ", ".join(f"'{key}'" for key in keys)
        op.execute(f"""
            DROP TRIGGER IF EXISTS {trigger_name} ON quant.{table_name};
            CREATE TRIGGER {trigger_name}
            AFTER INSERT OR UPDATE ON quant.{table_name}
            FOR EACH ROW EXECUTE FUNCTION quant.capture_edge_evidence_change({quoted_keys});
        """)


def downgrade() -> None:
    for table_name, *_ in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {_trigger_name(table_name)} ON quant.{table_name}")
    op.execute("DROP FUNCTION IF EXISTS quant.capture_edge_evidence_change()")
    op.execute("DROP TABLE IF EXISTS quant.edge_evidence_changes")
