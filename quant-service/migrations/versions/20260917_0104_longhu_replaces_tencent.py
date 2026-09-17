"""Longhu replaces every Tencent read: register its intraday providers, retire Tencent.

Revision ID: 20260917_0104
Revises: 20260917_0103

Watch quotes, depth, minute tapes, study daily bars and the index fallback now
come from the licensed Longhu endpoints.  Tencent rows already stored keep their
``tencent_*`` provenance; only the provider's capabilities are disabled.  The
close composite's label/config no longer claims a Tencent OHLC cross-check: since
2026-09-07 it uses Longhu's own dated kline.
"""

from alembic import op


revision = "20260917_0104"
down_revision = "20260917_0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.providers(provider_key,label,enabled,config)
        VALUES
          ('longhuvip','开盘啦授权行情（盘口/分钟/日K）',true,
           '{"secret_source":"external_config_file","volume_unit":"lot","depth_levels":5}'::jsonb),
          ('longhuvip_index','开盘啦指数日K（策略指数兜底）',true,'{"price_basis":"unadjusted_index"}'::jsonb)
        ON CONFLICT(provider_key) DO UPDATE SET
          label=EXCLUDED.label,enabled=true,config=EXCLUDED.config,updated_at=now();

        INSERT INTO quant.provider_capabilities(provider_key,capability,market,priority,enabled,rate_limit_per_minute)
        VALUES
          ('longhuvip','realtime_quote','cn',20,true,240),
          ('longhuvip','order_book_quote','cn',20,true,240),
          ('longhuvip','intraday_minute','cn',20,true,120),
          ('longhuvip','daily_bar','cn',26,true,60),
          ('longhuvip_index','daily_bar','cn',45,true,20)
        ON CONFLICT(provider_key,capability,market) DO UPDATE SET
          priority=EXCLUDED.priority,enabled=true,rate_limit_per_minute=EXCLUDED.rate_limit_per_minute;

        UPDATE quant.provider_capabilities SET enabled=false
         WHERE provider_key IN ('tencent_free','tencent_index_free');
        UPDATE quant.providers SET label='腾讯财经（已停用，仅保留历史数据）',enabled=false,updated_at=now()
         WHERE provider_key IN ('tencent_free','tencent_index_free');

        UPDATE quant.providers
           SET label='LonghuVIP industry cross-section + Longhu dated kline OHLC',
               config=(config - 'quote_crosscheck') || '{"ohlc_source":"longhuvip:GetKLineDay_W14","amount_unit":"thousand_cny"}'::jsonb,
               updated_at=now()
         WHERE provider_key='longhuvip_composite';
        UPDATE quant.provider_api_capabilities
           SET metadata=(metadata - 'ohlc_crosscheck' - 'crosscheck') || '{"ohlc_source":"longhuvip:GetKLineDay_W14"}'::jsonb,
               note=CASE WHEN api_name='realtime_quote'
                         THEN 'Same-session Longhu dated kline cross-check; persisted as close snapshot evidence.'
                         ELSE note END,
               last_checked_at=now()
         WHERE provider_key='longhuvip_composite';

        DROP INDEX IF EXISTS quant.intraday_order_book_recent_idx;
        CREATE INDEX IF NOT EXISTS intraday_order_book_recent_idx
            ON quant.intraday_quote_observations(symbol, observed_at DESC)
            WHERE source_name='longhuvip_order_book';
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS quant.intraday_order_book_recent_idx;
        CREATE INDEX IF NOT EXISTS intraday_order_book_recent_idx
            ON quant.intraday_quote_observations(symbol, observed_at DESC)
            WHERE source_name='tencent_order_book';
        UPDATE quant.provider_capabilities SET enabled=true WHERE provider_key IN ('tencent_free','tencent_index_free');
        UPDATE quant.providers SET enabled=true WHERE provider_key IN ('tencent_free','tencent_index_free');
    """)
