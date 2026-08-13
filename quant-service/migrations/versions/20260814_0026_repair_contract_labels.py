"""Repair contract display labels after the initial seed payload migration.

Revision ID: 20260814_0026
Revises: 20260814_0025
"""

from alembic import op


revision = "20260814_0026"
down_revision = "20260814_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """UPDATE quant.strategy_contracts AS c
           SET contract = jsonb_set(c.contract, '{label_text}', to_jsonb(v.label), true)
          FROM (VALUES
            ('watchlist_confirmation','显式观察池二次确认'),
            ('upside_breakout_eac','首次扩张与承接确认'),
            ('deep_reversal','深水反转与前收复'),
            ('green_reclaim','绿盘回收 VWAP/前收'),
            ('sector_surge','板块资金轮动共振'),
            ('limit_linkage','涨停锚点精确成员关联')
          ) AS v(strategy_key,label)
         WHERE c.strategy_key=v.strategy_key"""
    )


def downgrade() -> None:
    pass
