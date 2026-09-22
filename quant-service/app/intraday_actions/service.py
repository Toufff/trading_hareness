"""Explicit local shadow orchestration; deliberately not wired to live runtime.

Transport is injected, never imported. The verifier supplies no sending callable.
All production reads must be inside a caller-owned READ ONLY transaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .repository import ShadowRepository


def _time(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed


def _buy_cash(event: dict, context: dict) -> float:
    buying = event['action'] in {'first_buy', 'add', 't_buy_first'}
    if event.get('leg') == 'second':
        buying = event['action'] == 't_sell_first'
    if not buying:
        return 0.0
    price_range = event.get('price_range') or []
    if isinstance(price_range, dict):
        upper = price_range.get('max', price_range.get('high', event.get('price')))
    else:
        upper = max(price_range) if price_range else event.get('price')
    notional = float(upper) * int(event['quantity'])
    costs = context.get('account', {}).get('costs') or {}
    return (notional + max(float(costs.get('min_commission', 0)), notional * float(costs.get('commission_rate', 0)))
            + notional * (float(costs.get('transfer_rate', 0)) + float(costs.get('slippage_bps', 0)) / 10000))


class ActionShadowService:
    def __init__(self, repository: ShadowRepository, *, evaluator: Callable | None = None):
        self.repository = repository
        if evaluator is None:
            from .engine import evaluate
            evaluator = evaluate
        self.evaluator = evaluator

    def _bind_episode(self, context: dict) -> None:
        if context.get('t_episode_id'):
            episode_id = str(context['t_episode_id'])
            account = str(context['account_key'])
            context['t_episode'] = self.repository.episode(account, episode_id) or {'id': episode_id}
            context['other_episode_fill_ids'] = self.repository.other_episode_fill_ids(account, episode_id)

    def begin_episode(self, episode: dict, *, now: datetime) -> dict:
        """Explicitly bind a planned T episode; a signal alone never starts fills."""
        from .episodes import reduce_episode
        if episode.get('fills'):
            raise ValueError('create episode empty; import actual fills separately')
        reduced = reduce_episode(episode, [], now)
        if reduced['blockers']:
            raise ValueError(','.join(reduced['blockers']))
        with self.repository.transaction():
            existing = self.repository.episode(episode['account_key'], episode['id'])
            if existing is not None:
                raise ValueError('episode already exists; read it back instead of overwriting')
            self.repository.save_episode(reduced)
        return reduced

    def apply_actual_fill(self, *, account_key: str, episode_id: str, fill: dict, now: datetime) -> dict:
        from .episodes import reduce_episode
        with self.repository.transaction():
            episode = self.repository.episode(account_key, episode_id)
            if episode is None:
                raise ValueError('explicit existing episode binding required')
            reduced = reduce_episode(episode, [fill], now,
                claimed_fill_ids=self.repository.other_episode_fill_ids(account_key, episode_id))
            if reduced['blockers']:
                raise ValueError(','.join(reduced['blockers']))
            self.repository.consume_fill(account_key, episode_id, fill)
            self.repository.save_episode(reduced)
        return reduced

    def apply_imported_fill(self, *, account_key: str, episode_id: str, leg: str, record: dict, now: datetime) -> dict:
        """Normalize an immutable broker_trade_records row, with EXPLICIT leg binding.

        Caller must obtain record from the verified import repository. An order
        snapshot, cumulative filled quantity or inferred position delta is invalid.
        """
        required = ('trade_key', 'account_key', 'symbol', 'trade_date', 'trade_time', 'quantity', 'price', 'observed_at', 'source_sha256')
        if any(record.get(key) is None for key in required) or record['account_key'] != account_key:
            raise ValueError('complete immutable broker trade row required')
        occurred = datetime.fromisoformat(f"{record['trade_date']}T{record['trade_time']}").replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        fill = {'fill_id': str(record['trade_key']), 'account_key': account_key, 'symbol': record['symbol'],
                'leg': leg, 'side': record['side'], 'quantity': float(record['quantity']), 'price': float(record['price']),
                'occurred_at': occurred.isoformat(), 'observed_at': _time(record['observed_at']).isoformat(),
                'source': 'broker_import', 'source_sha256': record['source_sha256'], 'verified': True}
        return self.apply_actual_fill(account_key=account_key, episode_id=episode_id, fill=fill, now=now)

    def evaluate_batch(self, contexts: list[dict]) -> list[dict]:
        results = []
        # One transaction serializes shared cash allocation across symbols and workers.
        with self.repository.transaction():
            for original in contexts:
                ctx = deepcopy(original)
                account, symbol = str(ctx['account_key']), str(ctx['symbol'])
                plan = ctx.get('plan') or {}
                plan_id, version = str(plan.get('id', 'missing')), str(plan.get('version', 'missing'))
                ctx['previous_states'] = self.repository.states(account, symbol, plan_id, version)
                self._bind_episode(ctx)
                ctx.setdefault('account', {})['allocated_buy_cash'] = self.repository.reserved_cash(account)
                result = self.evaluator(ctx)
                result['assembly_blockers'] = list(ctx.get('assembly_blockers') or [])
                self.repository.save_states(account, symbol, plan_id, version, result.get('states') or {})
                self.repository.save_coverage(account, symbol, str(ctx['now']), result)
                for raw in result.get('events') or []:
                    if plan.get('mode') == 'local_preview':
                        continue
                    event = {**raw, 'account_key': account, 'symbol': symbol, 'mode': 'isolated_shadow'}
                    # A delivery can never outlive the quote that justified it.
                    quote_expiry = _time(ctx['quote']['as_of']) + timedelta(seconds=float(ctx['policy']['max_quote_age_seconds']))
                    expiry = min(quote_expiry, _time(plan['expires_at']))
                    self.repository.enqueue(event, expires_at=expiry.isoformat(), reserve_cash=_buy_cash(event, ctx))
                results.append({'symbol': symbol, 'mode': 'isolated_shadow', **result})
        return results

    def dispatch(self, *, now: datetime, current_context: Callable, send: Callable) -> list[dict]:
        """Test adapter only; ambiguous sends are quarantined, not blindly retried.

        Production must replace this local outbox with the existing durable Feishu
        adapter. A crash after claiming leaves 'sending' quarantined on restart.
        """
        results = []
        for row in self.repository.pending():
            event, key = row['payload'], row['event_key']
            with self.repository.transaction():
                if not self.repository.is_pending(key):
                    continue
                if _time(row['expires_at']) <= _time(now):
                    self.repository.mark(key, 'expired', 'quote_or_plan_expired')
                    results.append({'event_key': key, 'status': 'expired'})
                    continue
                fresh = current_context(event)
                current = (fresh or {}).get('plan') or {}
                if (not fresh or current.get('mode') == 'local_preview'
                        or str(current.get('id')) != str(event.get('plan_id')) or str(current.get('version')) != str(event.get('plan_version'))):
                    self.repository.mark(key, 'invalidated', 'plan_changed_or_removed')
                    results.append({'event_key': key, 'status': 'invalidated'})
                    continue
                fresh = deepcopy(fresh)
                self._bind_episode(fresh)
                fresh['now'] = now.isoformat()
                fresh['previous_states'] = self.repository.states(event['account_key'], event['symbol'], str(current['id']), str(current['version']))
                fresh.setdefault('account', {})['allocated_buy_cash'] = max(0, self.repository.reserved_cash(event['account_key']) - row['reserve_cash'])
                checked = self.evaluator(fresh)
                candidates = checked.get('confirmed_candidates') or []
                comparisons = ('event_key', 'action', 'leg', 'quantity', 'locked_quantity', 'price_range',
                               'reference', 'hard_stop', 'max_buy_price', 'min_sell_price',
                               'plan_id', 'plan_version', 'decision_id', 'snapshot_id', 'episode_id')
                same_action = any(all(c.get(field) == event.get(field) for field in comparisons) for c in candidates)
                if not same_action:
                    self.repository.mark(key, 'invalidated', 'latest_scope_quote_risk_or_account_changed')
                    results.append({'event_key': key, 'status': 'invalidated'})
                    continue
                if not self.repository.claim(key):
                    continue
            try:
                accepted = send(event)
                status = 'delivered' if accepted is True else ('pending' if accepted is False else 'uncertain')
                reason = None if accepted is True else 'transport_not_confirmed'
            except Exception as exc:
                status, reason = 'uncertain', type(exc).__name__
            with self.repository.transaction():
                self.repository.mark(key, status, reason)
            results.append({'event_key': key, 'status': status})
        return results


def load_readonly_source(connection: Any, *, account_key: str, as_of: datetime) -> dict:
    """Read formal scope and frozen plans. No inferred fills or mutable account cash."""
    from ..intraday_advisory.scope import load_scope
    from ..trade_discipline.repository import PLAN_COLUMNS
    scope = load_scope(connection, account_key=account_key, as_of=as_of)
    snapshot = connection.execute('''SELECT snapshot_id,observed_at,total_asset,total_market_value,verification
        FROM quant.broker_portfolio_snapshots WHERE account_key=%s AND snapshot_id=%s
        AND verification='verified_exact' AND observed_at<=%s''', (account_key, scope.snapshot_id, as_of)).fetchone()
    later_fills = []
    if snapshot:
        later_fills = connection.execute('''SELECT trade_key,symbol,trade_date,trade_time,side,quantity,price,observed_at
            FROM quant.broker_trade_records WHERE account_key=%s AND observed_at<=%s
            AND ((trade_date+coalesce(trade_time,time '23:59:59')) AT TIME ZONE 'Asia/Shanghai')>%s
            ORDER BY trade_date,trade_time,trade_key''', (account_key, as_of, snapshot['observed_at'])).fetchall()
    plans = connection.execute(f'''SELECT DISTINCT ON(symbol,plan_kind) {PLAN_COLUMNS}
        FROM quant.discipline_plans WHERE account_key=%s AND created_at<=%s AND as_of_at<=%s
        ORDER BY symbol,plan_kind,as_of_at DESC,created_at DESC''', (account_key, as_of, as_of)).fetchall()
    return {'account_key': account_key, 'as_of': as_of.isoformat(), 'snapshot': dict(snapshot) if snapshot else None,
            'snapshot_id': scope.snapshot_id, 'decision_id': scope.decision_id,
            'items': [{'symbol': i.symbol, 'name': i.name, 'role': i.source, 'facts': i.facts} for i in scope.items],
            'scope_blockers': list(scope.blockers), 'discipline_plans': [dict(p) for p in plans],
            'known_post_snapshot_fills': [dict(f) for f in later_fills]}


def assemble_contexts(source: dict, *, quotes: dict, minutes: dict, markets: dict,
                      costs: dict, policy: dict, compiler_policy: dict,
                      repository: ShadowRepository) -> list[dict]:
    """Assemble actual inputs only. Missing feeds survive as blockers, never defaults."""
    from .compiler import compile_action_plan
    contexts = []
    for item_index, item in enumerate(source['items'], 1):
        symbol, role = item['symbol'], item['role']
        rows = [p for p in source['discipline_plans'] if p['symbol'] == symbol
                and p['plan_kind'] == ('holding' if role == 'holding' else 'new_buy')]
        row = rows[0] if rows else {}
        per_symbol_policy = dict(compiler_policy)
        rank = item['facts'].get('priority') if role == 'recommendation' else item_index
        if isinstance(rank, int):
            per_symbol_policy.update(source_rank=rank, source_rank_source='formal_recommendation_priority' if role == 'recommendation' else 'holding_scope_market_value_order')
        fingerprint = [source['account_key'], symbol, row, per_symbol_policy,
                       source['snapshot_id'], source['decision_id'], str(source['as_of'])[:10]]
        cache_key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
        compiled = repository.cached_plan(cache_key)
        missing_baseline = compiled and any(b.get('code') == 'completed_minute_volume_baseline_unavailable' for b in compiled.get('blockers', []))
        if compiled is None or missing_baseline:
            candidate = compile_action_plan(row, account_key=source['account_key'], symbol=symbol,
                role=role, snapshot_id=source['snapshot_id'], decision_id=source['decision_id'],
                compiled_at=_time(source['as_of']), minutes=minutes.get(symbol, []), policy=per_symbol_policy)
            for rule in (candidate.get('plan') or {}).get('rules', {}).values():
                rule['created_at'] = source['as_of']
            if compiled is None:
                compiled = candidate
            elif candidate.get('plan'):
                # Stable existing rule identity; only newly available actions get a
                # later evidence boundary. Engine enforces each rule.created_at.
                existing_rules = compiled['plan']['rules']
                additions = {a: r for a, r in candidate['plan']['rules'].items() if a not in existing_rules}
                compiled['plan']['rules'].update(additions)
                if set(additions) & {'first_buy', 'add', 't_buy_first', 't_sell_first'}:
                    compiled['plan']['allow_buy'] = candidate['plan'].get('allow_buy') is True
                if set(additions) & {'add', 't_buy_first'}:
                    compiled['plan']['allow_add'] = candidate['plan'].get('allow_add') is True
                    compiled['plan']['thesis_valid'] = candidate['plan'].get('thesis_valid') is True
                    if (candidate.get('provenance') or {}).get('thesis_source'):
                        compiled.setdefault('provenance', {})['thesis_source'] = candidate['provenance']['thesis_source']
                compiled['blockers'] = [b for b in candidate.get('blockers', []) if b['action'] not in existing_rules]
                repository.enrich_cached_plan(cache_key, compiled)
            if (compiled.get('plan') or {}).get('rules'):
                compiled = repository.cache_plan(cache_key, compiled)
        snapshot, facts = source.get('snapshot') or {}, item['facts']
        age_limit = policy.get('max_account_age_seconds')
        account_fresh = (snapshot.get('verification') == 'verified_exact' and snapshot.get('observed_at') is not None
                         and isinstance(age_limit, (float, int)) and age_limit > 0
                         and 0 <= (_time(source['as_of']) - _time(snapshot['observed_at'])).total_seconds() <= age_limit)
        later_fills = source.get('known_post_snapshot_fills') or []
        # A later fill changes portfolio cash too, so even another symbol blocks exact sizing.
        verified = account_fresh and not later_fills
        plan = compiled.get('plan') or {}
        held = float(facts.get('quantity') or 0)
        stop = plan.get('hard_stop')
        quote = quotes.get(symbol) or {}
        quote_limit = policy.get('max_quote_age_seconds')
        mark_fresh = (quote.get('as_of') is not None and isinstance(quote_limit, (int, float)) and quote_limit > 0
                      and 0 <= (_time(source['as_of']) - _time(quote['as_of'])).total_seconds() <= quote_limit)
        mark = quote.get('price') if mark_fresh else None
        position_risk = max(0, held * (float(mark) - float(stop))) if mark is not None and stop is not None else (0 if role == 'recommendation' else None)
        lot_size = (policy.get('lot_size_by_symbol') or {}).get(symbol)
        numeric = lambda value: float(value) if value is not None else None
        account = {'snapshot_id': str(source['snapshot_id']) if source['snapshot_id'] else None, 'as_of': snapshot.get('observed_at'),
                   'total_assets': numeric(snapshot.get('total_asset')), 'stock_market_value': numeric(snapshot.get('total_market_value')),
                   'held_quantity': numeric(facts.get('quantity', 0)) if role == 'holding' else 0,
                   'sellable_quantity': numeric(facts.get('sellable_quantity', 0)) if role == 'holding' else 0,
                   'position_cost': numeric(facts.get('average_cost')), 'costs': costs,
                   'position_risk': position_risk, 'lot_size': lot_size,
                   'position_risk_basis': 'fresh_quote_to_existing_hard_stop',
                   'quantity_verified': verified, 'quantity_evidence': 'verified_snapshot_with_bounded_freshness_no_known_later_fill',
                   'unknown_unimported_fills_excluded': False}
        contexts.append({'now': source['as_of'], 'account_key': source['account_key'], **item,
                         'formal_recommendation': role == 'recommendation', 'account': account,
                         'quote': quotes.get(symbol, {}), 'minutes': minutes.get(symbol, []),
                         'market': markets.get(symbol, {}), 'policy': policy,
                         'plan': compiled.get('plan') or {}, 'assembly_blockers': compiled.get('blockers', [])})
    return contexts
