DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_edge_export') THEN
        CREATE ROLE quant_edge_export LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE quant_intraday_edge TO quant_edge_export;
GRANT USAGE ON SCHEMA quant TO quant_edge_export;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA quant FROM quant_edge_export;

GRANT SELECT ON TABLE
    quant.ten_day_leader_rotation_runs,
    quant.ten_day_leader_rotation_candidates,
    quant.intraday_scan_runs,
    quant.intraday_signal_episodes,
    quant.intraday_quote_observations,
    quant.intraday_minute_sessions,
    quant.intraday_board_flow_snapshots,
    quant.intraday_board_reports,
    quant.intraday_board_rotation_events,
    quant.intraday_signal_events,
    quant.intraday_rule_input_snapshots,
    quant.ten_day_leader_rotation_intraday_observations,
    -- The sequence journal is a transport cursor only. It carries rows from
    -- the allowlisted tables above and is needed for bounded delta exports.
    quant.edge_evidence_changes
TO quant_edge_export;
