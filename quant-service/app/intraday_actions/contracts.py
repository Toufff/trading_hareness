"""Versioned, fail-closed contract for manual intraday action observations.

All timestamps are timezone-aware ISO-8601. No input is an order authorization.
Amounts and prices are CNY; quantities are shares, minute volume is shares.

Context: now/account_key='citics-primary'/symbol/name/role ('holding' or
'recommendation'), quote, minutes, plan, account, market, policy,
previous_states (persist returned states verbatim), optional t_episode.
quote: price/as_of/source/bid_available/ask_available.
minutes: ordered rows end_at/close/low/high/vwap/volume/completed/source.
Only explicitly completed rows ending <= now are eligible; every confirmation
row must end AFTER plan.created_at (and after actual first fill for a T second
leg). Repeated completed timestamps are rejected, never counted twice.
policy: max_quote_age_seconds/max_minute_age_seconds/max_account_age_seconds/
max_market_age_seconds/max_price_deviation_pct, all positive explicit values.
market: session_open/sector_confirmed/sector_change_pct/as_of/source/sector_key.
plan: id/version/created_at/expires_at/quality_status='accepted'/hard_stop/
allow_buy/allow_add/risk_budget_pct=0.05/rules/source_rank/decision_id/snapshot_id.
Add always requires thesis_valid=True plus a newly confirmed minute structure,
cash and unchanged stop/risk gates. Below-position-cost is not independently a
veto: only explicit plan.forbid_add_below_cost=True imposes that extra gate;
when imposed, missing position_cost blocks verification instead of guessing.
Each rules[action]: basis='completed_1m', kind, confirmation_minutes (>=3),
reference, quantity (incremental shares), version; buy rules additionally
max_buy_price, volume_baseline, min_volume_ratio, sector_min_change;
Optional rule.created_at is its actual later availability when enriching a
partial plan; confirming minutes must follow BOTH plan and rule creation.
kind: breakout, pullback_reclaim, breakdown, hard_stop, time_exit, exhaustion.
pullback_reclaim requires a completed low<=reference BEFORE its confirming tail.
time_exit requires deadline and failure_condition='not_reclaimed_reference';
arrival at a deadline without failed recovery is NOT an exit. exhaustion requires peak_reference and
min_pullback_pct AND a falling close sequence below reference/VWAP.
T rules additionally target_price, min_net_profit, second_leg rule; second_leg
must itself declare the full minute contract. There is no implied recapture at
a rising price: t_sell_first second leg must meet net-profit and max-buy limits.
t_sell_first first leg requires explicit min_sell_price (exhaustion.reference
is an upper weakness threshold, NOT the minimum allowed sale price). Net spread
uses the maximum allowed buy price and minimum allowed sell price; after an
actual first fill it uses that leg's verified volume-weighted execution price.
t_buy_first second_leg must be breakout; its reference is the sale floor and
the latest quote must still hold it. A target price never substitutes for these
worst-allowed price bounds. Estimated spread remains non-executable research.
account: snapshot_id/as_of/total_assets/stock_market_value/allocated_buy_cash/
held_quantity/sellable_quantity/position_cost/position_risk/quantity_verified,
lot_size and costs. allocated_buy_cash is ALL unresolved buy cash reservations
from the shared account allocator. Research cash = assets-stock value-reserved;
it is NOT the broker's executable cash. position_risk is existing holding loss
at the CURRENT hard_stop. Snapshot/verified-fill reconciliation is upstream.
costs: source/as_of/commission_rate/min_commission/stamp_tax_sell_rate/
transfer_rate/slippage_bps; no default tax/commission values.
recommendation: formal_recommendation=True and plan.decision_id required.
holding: plan.snapshot_id must equal account.snapshot_id.

Result: status (ready/partial/blocked), events (new confirmed transitions ONLY),
confirmed_candidates (audit-only current winner even if baseline/consumed; NOT notifications),
blockers [{action,code}], coverage [{action,state,blockers}], states keyed by
action. Events have event_key/action/leg/quantity/locked_quantity/price/
price_range/evidence_at/quote_at/account_at/plan_id/plan_version/source_rank/
decision_id/snapshot_id/reasons/live_effect. First sight of an already-confirmed
condition establishes a suppressed baseline. A consumed plan/action cannot
re-arm without a new plan version or explicit reset_token. Exit wins over all
other actions; reduce wins over buys/T. Events never claim fills or profits.
"""

VERSION = "intraday-actions-v1"
ACTIONS = ("first_buy", "add", "reduce", "exit", "t_buy_first", "t_sell_first")
BUY_ACTIONS = frozenset({"first_buy", "add", "t_buy_first"})
T_ACTIONS = frozenset({"t_buy_first", "t_sell_first"})
PRIORITY = {"exit": 0, "reduce": 1, "add": 2, "first_buy": 3,
            "t_buy_first": 4, "t_sell_first": 5}
LABELS = {"first_buy": "首次买入", "add": "加仓", "reduce": "减仓",
          "exit": "退出", "t_buy_first": "先买后卖旧仓", "t_sell_first": "先卖后接回"}
