"""Add delisting-aware point-in-time research universes.

Revision ID: 20260816_0037
Revises: 20260816_0036
"""

from alembic import op


revision = "20260816_0037"
down_revision = "20260816_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.universe_membership_history (
            universe_key text NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            effective_from date NOT NULL,
            effective_to date,
            source text NOT NULL,
            priority integer NOT NULL DEFAULT 100,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(universe_key,symbol,effective_from),
            CHECK(effective_to IS NULL OR effective_to>=effective_from)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS universe_membership_history_date_idx
            ON quant.universe_membership_history(universe_key,effective_from,effective_to,symbol)
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS universe_membership_history_open_idx
            ON quant.universe_membership_history(universe_key,symbol)
            WHERE effective_to IS NULL
    """)
    op.execute("""
        WITH a_share_bars AS (
            SELECT bar.symbol,min(bar.trading_date) AS first_date,max(bar.trading_date) AS last_date
              FROM quant.canonical_bars_daily bar
             WHERE bar.symbol ~ '^((60[0135]|68[89])[0-9]{3}\\.SH|(000|001|002|003|300|301)[0-9]{3}\\.SZ|[489][0-9]{5}\\.BJ)$'
             GROUP BY bar.symbol
        ), current_all_a AS (
            SELECT symbol FROM quant.universe_members
             WHERE universe_key='all_a' AND enabled
        ), desired AS (
            SELECT 'all_a'::text AS universe_key,bars.symbol,bars.first_date,
                   CASE WHEN current.symbol IS NOT NULL THEN NULL ELSE bars.last_date END AS effective_to,
                   CASE WHEN current.symbol IS NOT NULL
                        THEN 'canonical_presence_plus_current_universe'
                        ELSE 'canonical_presence_delisting_proxy' END AS source,
                   jsonb_build_object(
                       'effective_from_basis','first_canonical_bar',
                       'effective_to_basis',CASE WHEN current.symbol IS NOT NULL
                                                  THEN 'current_active_snapshot'
                                                  ELSE 'last_canonical_bar' END,
                       'delist_date_quality',CASE WHEN current.symbol IS NOT NULL
                                                  THEN 'not_applicable'
                                                  ELSE 'inferred' END) AS metadata
              FROM a_share_bars bars LEFT JOIN current_all_a current USING(symbol)
            UNION ALL
            SELECT 'core',bars.symbol,bars.first_date,
                   CASE WHEN current.symbol IS NOT NULL THEN NULL ELSE bars.last_date END,
                   CASE WHEN current.symbol IS NOT NULL
                        THEN 'canonical_presence_plus_current_universe'
                        ELSE 'canonical_presence_delisting_proxy' END,
                   jsonb_build_object(
                       'effective_from_basis','first_canonical_bar',
                       'effective_to_basis',CASE WHEN current.symbol IS NOT NULL
                                                  THEN 'current_active_snapshot'
                                                  ELSE 'last_canonical_bar' END,
                       'delist_date_quality',CASE WHEN current.symbol IS NOT NULL
                                                  THEN 'not_applicable'
                                                  ELSE 'inferred' END)
              FROM a_share_bars bars
              JOIN quant.universe_members core ON core.universe_key='core' AND core.symbol=bars.symbol
              LEFT JOIN current_all_a current ON current.symbol=bars.symbol
        )
        INSERT INTO quant.universe_membership_history(
            universe_key,symbol,effective_from,effective_to,source,priority,metadata)
        SELECT universe_key,symbol,first_date,effective_to,source,100,metadata FROM desired
        ON CONFLICT(universe_key,symbol,effective_from) DO UPDATE SET
          effective_to=EXCLUDED.effective_to,source=EXCLUDED.source,
          metadata=EXCLUDED.metadata,updated_at=now()
    """)
    op.execute("""
        UPDATE quant.factor_registry
           SET implementation='native_sql',version='factor-sql-v2',
               metadata=metadata || jsonb_build_object(
                   'evaluator','sql_cross_section_v2','bounded_memory',true),
               updated_at=now()
         WHERE factor_key IN (
            'momentum_5d','momentum_20d','reversal_5d','sma_gap_20d',
            'volatility_20d','volume_ratio_20d','intraday_strength')
    """)
    op.execute("""
        UPDATE quant.factor_registry SET implementation='post_close_structure',updated_at=now()
         WHERE factor_key='base_contraction_30d';
        UPDATE quant.factor_registry SET implementation='point_in_time_flow',updated_at=now()
         WHERE factor_key='moneyflow_dc_rate';
        UPDATE quant.factor_registry SET implementation='analyst_research',updated_at=now()
         WHERE factor_key LIKE 'analyst_%';
        UPDATE quant.research_frameworks
           SET integration_mode='database_streaming',
               metadata=metadata || jsonb_build_object(
                   'engine','sql_cross_section_v2','bounded_memory',true),
               updated_at=now()
         WHERE framework_key='native_factor_lab'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE quant.factor_registry SET implementation='native',version='factor-v1',updated_at=now()
         WHERE factor_key IN (
            'momentum_5d','momentum_20d','reversal_5d','sma_gap_20d',
            'volatility_20d','volume_ratio_20d','intraday_strength',
            'base_contraction_30d','moneyflow_dc_rate')
    """)
    op.execute("UPDATE quant.factor_registry SET implementation='native',updated_at=now() WHERE factor_key LIKE 'analyst_%'")
    op.execute("DROP INDEX IF EXISTS quant.universe_membership_history_open_idx")
    op.execute("DROP INDEX IF EXISTS quant.universe_membership_history_date_idx")
    op.execute("DROP TABLE IF EXISTS quant.universe_membership_history")
