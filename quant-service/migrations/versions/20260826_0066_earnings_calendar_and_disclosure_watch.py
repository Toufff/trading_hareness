"""Scheduled-disclosure calendar, earnings guidance, and the disclosure-day watch source.

Nothing in the pipeline read the reporting calendar before this.  Every
selection strategy worked from price/volume history alone, so a name whose
report lands tomorrow looked identical to one with no scheduled event, and
the intraday watchlist could never be positioned ahead of a known catalyst.

Revision ID: 20260826_0066
Revises: 20260825_0065
"""

from alembic import op


revision = "20260826_0066"
down_revision = "20260825_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pre_date is the exchange-registered *scheduled* disclosure date and is
    # known days ahead; actual_date is filled once the report lands.  Keeping
    # both is what makes a point-in-time "as of yesterday, who reports
    # tomorrow" question answerable after the fact.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.disclosure_schedule (
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            period date NOT NULL,
            provider text NOT NULL DEFAULT 'tushare',
            pre_date date,
            actual_date date,
            modify_date date,
            available_at timestamptz NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol, period, provider)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS disclosure_schedule_pre_date_idx
            ON quant.disclosure_schedule(pre_date) WHERE pre_date IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS disclosure_schedule_actual_date_idx
            ON quant.disclosure_schedule(actual_date) WHERE actual_date IS NOT NULL
    """)
    # Guidance (业绩预告 / 业绩快报).  These are stored to establish what the
    # market already knew before a scheduled report, not to predict its
    # contents: measured over 2026-07-20..2026-08-25, disclosers carrying
    # prior guidance limit-up at 1.60% versus 3.77% for those without.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.earnings_forecasts (
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            period date NOT NULL,
            ann_date date NOT NULL,
            provider text NOT NULL DEFAULT 'tushare',
            forecast_type text,
            p_change_min numeric,
            p_change_max numeric,
            net_profit_min numeric,
            net_profit_max numeric,
            last_parent_net numeric,
            first_ann_date date,
            summary text,
            change_reason text,
            available_at timestamptz NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol, period, ann_date, provider)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS earnings_forecasts_period_idx
            ON quant.earnings_forecasts(period, ann_date)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.earnings_express (
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            period date NOT NULL,
            ann_date date NOT NULL,
            provider text NOT NULL DEFAULT 'tushare',
            revenue numeric,
            operate_profit numeric,
            total_profit numeric,
            n_income numeric,
            total_assets numeric,
            diluted_eps numeric,
            diluted_roe numeric,
            yoy_net_profit numeric,
            perf_summary text,
            available_at timestamptz NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol, period, ann_date, provider)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS earnings_express_period_idx
            ON quant.earnings_express(period, ann_date)
    """)
    # The proposals table previously had exactly one producer, the scored
    # strategy ledger.  A disclosure-day name is not a scored candidate - it
    # is a scheduled event with no score at all - so the score columns become
    # nullable and every row records which producer emitted it.
    op.execute("""
        ALTER TABLE quant.strategy_watchlist_proposals
            ADD COLUMN IF NOT EXISTS proposal_source text NOT NULL DEFAULT 'strategy_ledger'
    """)
    op.execute("ALTER TABLE quant.strategy_watchlist_proposals ALTER COLUMN strategy_percentile DROP NOT NULL")
    op.execute("ALTER TABLE quant.strategy_watchlist_proposals ALTER COLUMN score_scale DROP NOT NULL")
    # Every declared strategy must carry a promotion-registry row, seeded
    # disabled at zero weight (see 20260825_0060).  disclosure_day_watch emits
    # no score and no direction, but the invariant is "declared implies
    # registered", so it is seeded on the same P0 safety default.
    op.execute("""
        INSERT INTO quant.strategy_promotion_registry(
            strategy_key,methodology_version,status,max_live_weight,reason,evidence)
        VALUES('disclosure_day_watch','disclosure-day-watch-v1','disabled',0,
               'P0 safety default: only an explicitly approved research version may supply a nonzero live weight.',
               '{"live_strategy_effect":"none"}'::jsonb)
        ON CONFLICT(strategy_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_promotion_registry WHERE strategy_key='disclosure_day_watch'")
    # Unscored event rows exist only because of this revision, and the
    # restored NOT NULL constraints cannot hold while they are present.
    op.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE proposal_source<>'strategy_ledger'")
    op.execute("ALTER TABLE quant.strategy_watchlist_proposals DROP COLUMN IF EXISTS proposal_source")
    op.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE strategy_percentile IS NULL OR score_scale IS NULL")
    op.execute("ALTER TABLE quant.strategy_watchlist_proposals ALTER COLUMN strategy_percentile SET NOT NULL")
    op.execute("ALTER TABLE quant.strategy_watchlist_proposals ALTER COLUMN score_scale SET NOT NULL")
    op.execute("DROP TABLE IF EXISTS quant.earnings_express")
    op.execute("DROP TABLE IF EXISTS quant.earnings_forecasts")
    op.execute("DROP TABLE IF EXISTS quant.disclosure_schedule")
