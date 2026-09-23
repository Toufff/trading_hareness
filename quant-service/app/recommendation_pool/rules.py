"""Pure, replayable recommendation contract. No new alpha weights or broker I/O."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..short_term_lanes.research_queue import lane_representatives

VERSION = 'recommendation-pool-20260917'
STAGES = {'accumulation', 'initial_breakout', 'strong_pullback', 'post_limit', 'other'}
# Every recommendation must answer the strongest same-sector candidates of the
# same scan, and show at least one piece of recently published information
# gathered for this decision instead of recycling an old periodic report.
SECTOR_PEER_REFERENCE = 3
# Answering only the board top three lets a low ranked pick ignore everybody who
# actually beat it, so every candidate ranked ahead is required too.  The cap
# keeps the workload bounded on very large boards; it is not a quality claim.
SECTOR_PEER_LIMIT = 6
FRESH_INFORMATION_DAYS = 30
# Free text shorter than this is a label, not an explanation that can be checked.
MIN_REASON_CHARS = 20
BOARD_METRICS = ('change_pct', 'return_5d', 'return_10d', 'amount', 'turnover', 'net5_amount_pct')
SECTOR_ASSESSMENTS = ('strong', 'neutral', 'weak')
# Shanghai 5xxxxx and Shenzhen 15xxxx-19xxxx are the A-share ETF code ranges.
ETF_SYMBOL = re.compile(r'^(5\d{5}\.SH|1[5-9]\d{4}\.SZ)$')
RESEARCH_SOURCE_KINDS = ('information_check', 'sector_etf_proxy')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _scan_evidence(scan):
    """Return only evidence owned by the scanner.

    ``short_term_lanes.build`` enriches display rows with ``company_review``
    after the pure screen has completed.  Replaying the same settled session
    can also regenerate prose/report fields.  Neither operation changes the
    market screen that a recommendation review was based on, so those fields
    must not invalidate an otherwise identical decision.

    Candidate population, ranks/metrics, displayed representative order,
    event evidence, market regime, coverage and settings remain in the hash.
    A real rescan change therefore still invalidates the decision.
    """
    lane_evidence = []
    for lane in scan.get('lanes', []):
        display = {}
        for section in ('selected', 'caution_list', 'observation_list'):
            display[section] = [
                {k: deepcopy(v) for k, v in row.items() if k != 'company_review'}
                for row in lane.get(section, [])
            ]
        lane_evidence.append({
            'key': lane.get('key'),
            'status': lane.get('status'),
            'total_matches': lane.get('total_matches'),
            'tracking_candidates': deepcopy(lane.get('tracking_candidates')),
            'factor_policy': deepcopy(lane.get('factor_policy')),
            'data_gaps': deepcopy(lane.get('data_gaps')),
            'regime_route': deepcopy(lane.get('regime_route')),
            **display,
        })
    return {
        k: deepcopy(scan.get(k))
        for k in ('as_of_date', 'version', 'settings', 'coverage', 'market')
    } | {'lanes': lane_evidence}


def scan_hash(scan):
    return digest(_scan_evidence(scan))


def intake(scan, run_id, groups, user_tracking=(), next_session=None, *,
           source_kind='post_close', source_cutoff=None, evidence_eligibility=None):
    if scan.get('status') != 'completed' or not scan.get('as_of_date'):
        raise ValueError('scan_not_completed')
    rows, required = {}, set(groups.get('推荐', []))
    for lane in scan.get('lanes', []):
        candidates = lane.get('tracking_candidates')
        if candidates is None:
            raise ValueError('full_candidate_population_missing:' + str(lane.get('key')))
        for rank, c in enumerate(candidates, 1):
            s = c['symbol']
            row = rows.setdefault(s, {'symbol': s, 'name': c.get('name', s), 'memberships': [], 'evidence': deepcopy(c), 'sources': [],
                                      'sector_key': None, 'sector_label': None})
            row['sector_key'] = row['sector_key'] or c.get('sector_key')
            row['sector_label'] = row['sector_label'] or c.get('sector_label')
            row['memberships'].append({'lane': lane['key'], 'rank': rank, 'population': len(candidates), 'state': c.get('state'),
                                       'rank_score': c.get('rank_score')})
            if 'scan' not in row['sources']:
                row['sources'].append('scan')
    required.update(row['symbol'] for row in lane_representatives(scan))
    for source, symbols in (('previous_recommendation', groups.get('推荐', [])), ('previous_observation', groups.get('观察', [])), ('user_tracking', user_tracking)):
        for s in symbols:
            row = rows.setdefault(s, {'symbol': s, 'name': s, 'memberships': [], 'evidence': {}, 'sources': []})
            row['sources'].append(source)
    # This is a review queue, NOT an investment score. Rank positions only order
    # questions within each strategy; multi-lane hits do not add points.
    rows = sorted(rows.values(), key=lambda r: (r['symbol'] not in required, min((m['rank'] for m in r['memberships']), default=9999), r['symbol']))
    context = {'version': VERSION, 'run_id': str(run_id), 'as_of_date': scan['as_of_date'], 'scan_hash': scan_hash(scan),
               'valid_until': str(next_session) + 'T15:00:00+08:00' if next_session else None,
               'source_kind': source_kind, 'source_cutoff': source_cutoff,
               'evidence_eligibility': evidence_eligibility or {},
               'baseline': {g: sorted(set(groups.get(g, []))) for g in ('推荐', '观察')},
               'user_tracking': sorted(set(user_tracking)), 'market': scan.get('market'), 'settings': scan.get('settings'),
               'sector_overview': scan.get('sector_overview') or {},
               'candidates': rows, 'sector_boards': sector_boards(rows, scan.get('sector_overview') or {}),
               'required_reviews': sorted(required), 'candidate_count': len(rows)}
    context['context_hash'] = digest(context)
    return context


def best_membership(memberships):
    """Best within-lane position as a fraction of that lane's population.

    This orders a comparison reference only. It is not a new cross-strategy
    score and multi-lane hits still add nothing.
    """
    return min(memberships, key=lambda m: ((m['rank'] - 1) / m['population'], m['rank'], m['lane']), default=None)


def sector_boards(rows, overview=None):
    boards = {}
    for row in rows:
        key = row.get('sector_key')
        best = best_membership(row['memberships'])
        if not key or key == 'unknown' or best is None:
            continue
        metrics = (row.get('evidence') or {}).get('metrics') or {}
        board = boards.setdefault(key, {'sector_key': key, 'label': row.get('sector_label') or key, 'members': [],
                                        # Whole-sector trend/flow computed by the scanner over every listed
                                        # member, not only the candidates on this board. None means the
                                        # scanner has no aggregate and a thematic ETF proxy must be used.
                                        'overview': deepcopy((overview or {}).get(key))})
        board['members'].append({'symbol': row['symbol'], 'name': row['name'],
                                 'best_lane': best['lane'], 'best_rank': best['rank'], 'best_population': best['population'],
                                 'memberships': [{k: m[k] for k in ('lane', 'rank', 'population')} for m in row['memberships']],
                                 'metrics': {k: metrics.get(k) for k in BOARD_METRICS}})
    for board in boards.values():
        board['members'].sort(key=lambda m: ((m['best_rank'] - 1) / m['best_population'], m['best_rank'], m['symbol']))
        for position, member in enumerate(board['members'], 1):
            member['sector_position'] = position
    return dict(sorted(boards.items()))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _long(value):
    return _text(value) and len(value.strip()) >= MIN_REASON_CHARS


def _global_peers(context):
    """Every candidate of this scan ordered by the same relative-position key.

    Used only to fill a comparison up to three names when the stock's own board
    is too small (unknown sector, sector absent, tiny board).  A cross-sector
    name is a reference, never a claim that the two stocks are substitutes.
    """
    placed = {m['symbol']: (board.get('label'), m['sector_position'])
              for board in (context.get('sector_boards') or {}).values() for m in board.get('members', [])}
    peers = []
    for row in context.get('candidates') or []:
        best = best_membership(row.get('memberships') or [])
        if best is None:
            continue
        label, position = placed.get(row['symbol'], (row.get('sector_label'), None))
        metrics = (row.get('evidence') or {}).get('metrics') or {}
        peers.append({'symbol': row['symbol'], 'name': row['name'], 'sector_key': row.get('sector_key'),
                      'sector_label': label or row.get('sector_label'), 'sector_position': position,
                      'best_lane': best['lane'], 'best_rank': best['rank'], 'best_population': best['population'],
                      'metrics': {k: metrics.get(k) for k in BOARD_METRICS}})
    peers.sort(key=lambda p: ((p['best_rank'] - 1) / p['best_population'], p['best_rank'], p['symbol']))
    return peers


def recommendation_reference(context, row):
    """System-owned ranking facts shown beside every editorial recommendation."""
    board = context.get('sector_boards', {}).get(row.get('sector_key')) or {}
    members = board.get('members', [])
    own = next((m for m in members if m['symbol'] == row['symbol']), None)
    position = own['sector_position'] if own else None
    peers = [m for m in members if m['symbol'] != row['symbol']]
    outranked = [m for m in peers if position is not None and m['sector_position'] < position]
    # Answer everybody the scan put ahead of this stock plus the board top three;
    # the cap bounds the reading load, it does not shorten the ranking itself.
    chosen = {m['symbol'] for m in outranked} | {m['symbol'] for m in peers[:SECTOR_PEER_REFERENCE]}
    required = [{'symbol': m['symbol'], 'name': m['name'], 'sector_key': board.get('sector_key'),
                 'sector_label': board.get('label'), 'sector_position': m['sector_position'],
                 'best_lane': m['best_lane'], 'best_rank': m['best_rank'], 'best_population': m['best_population'],
                 'metrics': deepcopy(m['metrics']), 'scope': 'sector'}
                for m in peers if m['symbol'] in chosen][:SECTOR_PEER_LIMIT]
    if len(required) < SECTOR_PEER_REFERENCE:
        taken = {row['symbol']} | {p['symbol'] for p in required}
        for candidate in _global_peers(context):
            if len(required) >= SECTOR_PEER_REFERENCE:
                break
            if candidate['symbol'] in taken:
                continue
            required.append({**deepcopy(candidate), 'scope': 'global'})
    market = context.get('market') or {}
    return {'lane_rankings': [{k: m.get(k) for k in ('lane', 'rank', 'population', 'state', 'rank_score')} for m in row['memberships']],
            'sector_key': row.get('sector_key'), 'sector_label': board.get('label') or row.get('sector_label'),
            'sector_position': position, 'sector_candidates': len(members),
            'outranked_count': len(outranked),
            'sector_overview': deepcopy(board.get('overview') or (context.get('sector_overview') or {}).get(row.get('sector_key'))),
            'market': {k: market.get(k) for k in ('median_return10', 'up_fraction')},
            'required_peers': required}


def note_template(context, symbol):
    """Pre-fill everything the system already knows so the agent answers, not transcribes.

    The strategy positions are copied from the scan itself, so the strict
    equality check on ``lane_rankings`` now guards against tampering with a
    published decision instead of proving that somebody read the context.
    """
    row = next((r for r in (context.get('candidates') or []) if r['symbol'] == symbol), None)
    if row is None:
        raise ValueError('unknown_symbol:' + str(symbol))
    reference = recommendation_reference(context, row)
    overview = reference.get('sector_overview')
    template = {
        'lane_rankings': [{k: m[k] for k in ('lane', 'rank', 'population')} for m in row['memberships']],
        'rank_assessment': '',
        'sector_peers': [{'symbol': p['symbol'], 'name': p['name'], 'scope': p['scope'],
                          'sector_label': p['sector_label'], 'sector_position': p['sector_position'], 'why_not': ''}
                         for p in reference['required_peers']],
        'sector_view': {'assessment': '', 'trend': '', 'volume_price': '',
                        'proxy': {'kind': 'members'} if overview else
                                 {'kind': 'etf', 'symbol': '', 'name': '', 'url': '', 'published_date': ''}},
        'information_checks': [],
        'entry_reason': '',
        'priority_reason': '',
    }
    if symbol in ((context.get('baseline') or {}).get('推荐') or []):
        template['carry_over_reason'] = ''
    return template


def _etf_proxy_problems(context, proxy):
    if not ETF_SYMBOL.match(str(proxy.get('symbol') or '')) or not _text(proxy.get('name')):
        return ['recommendation_note_invalid_sector_etf_proxy']
    if not str(proxy.get('url') or '').startswith('https://'):
        return ['recommendation_note_invalid_sector_etf_proxy']
    try:
        published = date.fromisoformat(str(proxy.get('published_date')))
    except ValueError:
        return ['recommendation_note_invalid_sector_etf_proxy']
    as_of = date.fromisoformat(context['as_of_date'])
    if published > as_of or (as_of - published).days > FRESH_INFORMATION_DAYS:
        return ['recommendation_note_invalid_sector_etf_proxy']
    return []


def _sector_view_problems(context, note, reference):
    """The whole sector, not only the same-scan candidates, must be looked at.

    ``sector_boards`` only compares the handful of stocks this scan surfaced.
    A recommendation also has to state how the entire industry trades, and when
    the scanner has no aggregate for that sector the agent must bring a thematic
    ETF as the proxy instead of quietly skipping the question.
    """
    view = note.get('sector_view')
    if not isinstance(view, dict):
        return ['recommendation_note_missing_sector_view']
    problems = []
    if view.get('assessment') not in SECTOR_ASSESSMENTS:
        problems.append('recommendation_note_invalid_sector_view')
    for key in ('trend', 'volume_price'):
        if not _text(view.get(key)):
            problems.append('recommendation_note_invalid_sector_view')
        elif not _long(view.get(key)):
            problems.append('recommendation_note_reason_too_short:sector_view.' + key)
    overview = reference.get('sector_overview')
    proxy = view.get('proxy')
    if not isinstance(proxy, dict) or proxy.get('kind') not in ('members', 'etf'):
        problems.append('recommendation_note_invalid_sector_view')
    elif proxy['kind'] != 'etf' and not overview:
        problems.append('recommendation_note_sector_etf_proxy_required')
    elif proxy['kind'] == 'etf':
        problems += _etf_proxy_problems(context, proxy)
    if view.get('assessment') == 'weak':
        if not _text(note.get('weak_sector_entry_reason')):
            problems.append('recommendation_note_missing_weak_sector_entry_reason')
        elif not _long(note.get('weak_sector_entry_reason')):
            problems.append('recommendation_note_reason_too_short:weak_sector_entry_reason')
    if (overview or {}).get('relative_strength') == 'weak' and view.get('assessment') != 'weak':
        if not _text(note.get('sector_disagreement_reason')):
            problems.append('recommendation_note_missing_sector_disagreement_reason')
        elif not _long(note.get('sector_disagreement_reason')):
            problems.append('recommendation_note_reason_too_short:sector_disagreement_reason')
    return problems


def validate_recommendation_note(context, row, item, reference):
    """Require a complete, checkable explanation for entering the recommendation group.

    Editorial judgment may override the scan ordering, but it must state the
    exact strategy rankings, answer every same-sector candidate ranked ahead,
    judge the whole sector's trend and volume/price, bring recently published
    information and give a final entry reason.

    Every problem found for this stock is reported at once.  Fixing one hidden
    error at a time turns a single review into many publish attempts, and each
    attempt tempts the author to weaken the answer instead of completing it.
    """
    note = item.get('recommendation_note')
    if not isinstance(note, dict):
        raise ValueError('recommendation_note_required')
    problems = []
    for key in ('rank_assessment', 'entry_reason', 'priority_reason'):
        if not _text(note.get(key)):
            problems.append('recommendation_note_missing_' + key)
        elif not _long(note.get(key)):
            problems.append('recommendation_note_reason_too_short:' + key)
    stated = note.get('lane_rankings')
    if not isinstance(stated, list):
        problems.append('recommendation_note_missing_lane_rankings')
    else:
        try:
            stated_set = {(str(r['lane']), int(r['rank']), int(r['population'])) for r in stated}
        except (KeyError, TypeError, ValueError):
            stated_set = None
            problems.append('recommendation_note_invalid_lane_rankings')
        if stated_set is not None:
            actual_set = {(m['lane'], m['rank'], m['population']) for m in row['memberships']}
            if stated_set != actual_set or len(stated) != len(actual_set):
                problems.append('recommendation_note_lane_rankings_do_not_match_scan')
    peers = note.get('sector_peers')
    if not isinstance(peers, list):
        problems.append('recommendation_note_missing_sector_peers')
    else:
        answered = set()
        known = {r['symbol'] for r in (context.get('candidates') or [])}
        for peer in peers:
            if not isinstance(peer, dict) or peer.get('symbol') not in known or peer['symbol'] == row['symbol'] or not _text(peer.get('why_not')):
                problems.append('recommendation_note_invalid_sector_peer')
                continue
            if not _long(peer['why_not']):
                problems.append('recommendation_note_reason_too_short:why_not:' + peer['symbol'])
            answered.add(peer['symbol'])
        unanswered = [p['symbol'] for p in reference['required_peers'] if p['symbol'] not in answered]
        if unanswered:
            problems.append('recommendation_note_unanswered_sector_peers:' + ','.join(unanswered))
    problems += _sector_view_problems(context, note, reference)
    checks = note.get('information_checks')
    if not isinstance(checks, list) or not checks:
        problems.append('recommendation_note_missing_information_checks')
    else:
        as_of = date.fromisoformat(context['as_of_date'])
        fresh = False
        for check in checks:
            if not isinstance(check, dict) or not all(_text(check.get(k)) for k in ('topic', 'finding')) \
                    or not str(check.get('url', '')).startswith('https://'):
                problems.append('recommendation_note_invalid_information_check')
                continue
            try:
                published = date.fromisoformat(str(check.get('published_date')))
            except ValueError:
                problems.append('recommendation_note_invalid_information_check')
                continue
            if published > as_of:
                problems.append('recommendation_note_future_information')
                continue
            fresh = fresh or (as_of - published).days <= FRESH_INFORMATION_DAYS
        if not fresh:
            problems.append('recommendation_note_requires_recent_information')
    if row['symbol'] in context['baseline']['推荐']:
        if not _text(note.get('carry_over_reason')):
            problems.append('recommendation_note_missing_carry_over_reason')
        elif not _long(note.get('carry_over_reason')):
            problems.append('recommendation_note_reason_too_short:carry_over_reason')
    if problems:
        raise ValueError('recommendation_note:' + ';'.join(dict.fromkeys(problems)))


def merged_recommendation_sources(item, note):
    """Everything researched for this decision must reach the evidence ledger.

    ``information_checks`` and the sector ETF proxy are primary work done for
    this recommendation.  They travel with the review's own sources so
    ``short_term_lanes.reviews`` persists them into ``quant.market_events``
    instead of leaving them only in a report file.
    """
    sources = [deepcopy(source) for source in (item.get('sources') or [])]
    seen = {str(source.get('url')) for source in sources}
    extra = [{'url': check.get('url'), 'published_date': check.get('published_date'),
              'topic': check.get('topic'), 'finding': check.get('finding'), 'kind': 'information_check'}
             for check in ((note or {}).get('information_checks') or []) if isinstance(check, dict)]
    proxy = ((note or {}).get('sector_view') or {}).get('proxy') or {}
    if proxy.get('kind') == 'etf' and proxy.get('url'):
        extra.append({'url': proxy['url'], 'published_date': proxy.get('published_date'),
                      'topic': '板块ETF代理', 'kind': 'sector_etf_proxy',
                      'finding': f"{proxy.get('name')}（{proxy.get('symbol')}）作为该板块整体走势与量价的代理"})
    for source in extra:
        if str(source['url']) in seen:
            continue
        seen.add(str(source['url']))
        sources.append(source)
    return sources


def compile_decision(context, review):
    """Compile reviewed decisions; unknown symbols stay visible, never 'excluded'.

    Every candidate has a screen record. Company verification is mandatory for
    priorities, old recommendations and the scan's first representatives. Other
    unresearched candidates remain explicitly eligible for later challenge.
    """
    if context.get('context_hash') != digest({k: v for k, v in context.items() if k != 'context_hash'}):
        raise ValueError('context_hash_mismatch')
    if 'sector_boards' not in context:
        raise ValueError('context_predates_recommendation_note_contract')
    if review.get('context_hash') != context['context_hash']:
        raise ValueError('review_for_different_context')
    if not review.get('author') or not review.get('market_assessment'):
        raise ValueError('author_and_market_assessment_required')
    if context.get('source_kind') == 'noon':
        cutoff = datetime.fromisoformat(context['source_cutoff'])
        reviewed_at = datetime.fromisoformat(review.get('reviewed_at', ''))
        if reviewed_at.tzinfo is None or reviewed_at < cutoff:
            raise ValueError('noon_review_time_invalid')
    rows = {r['symbol']: r for r in context['candidates']}
    decisions, errors = {}, {}
    for item in review.get('items', []):
        s = item['symbol']
        if s in decisions or s in errors or s not in rows:
            raise ValueError('duplicate_or_unknown_symbol:' + s)
        try:
            for key in ('why_now', 'comparison', 'invalidation', 'business', 'company_risk', 'sector_assessment', 'sources'):
                if not item.get(key):
                    raise ValueError('missing_' + key)
            if item.get('data_date') != context['as_of_date']:
                raise ValueError('stale_review')
            if item.get('stage') not in STAGES or item.get('decision') not in ('recommend', 'observe', 'exclude'):
                raise ValueError('invalid_decision_or_stage')
            for src in item['sources']:
                if not str(src.get('url', '')).startswith('https://') or not src.get('published_date') or src['published_date'] > context['as_of_date']:
                    raise ValueError('invalid_source_date')
            if item['decision'] == 'recommend':
                if context.get('source_kind') == 'noon':
                    status = (context.get('evidence_eligibility') or {}).get(s) or {}
                    if not status.get('ready'):
                        raise ValueError('noon_market_evidence_incomplete:' + ','.join(status.get('gaps') or ['missing_stock_evidence']))
                    evidence_at = datetime.fromisoformat(item.get('evidence_available_at', ''))
                    if evidence_at.tzinfo is None or evidence_at > reviewed_at:
                        raise ValueError('noon_company_evidence_time_invalid')
                if not item.get('trigger') or not item.get('peer_comparison'):
                    raise ValueError('recommendation_requires_trigger_and_peer_comparison')
                if not isinstance(item.get('priority'), int) or item['priority'] < 1:
                    raise ValueError('positive_editorial_priority_required')
                metrics = rows[s]['evidence'].get('metrics', {})
                if not all(isinstance(metrics.get(k), (int, float)) and math.isfinite(metrics[k]) and metrics[k] > 0 for k in ('close', 'amount')):
                    raise ValueError('current_price_and_amount_required')
                reference = recommendation_reference(context, rows[s])
                validate_recommendation_note(context, rows[s], item, reference)
                item = {**item, 'sources': merged_recommendation_sources(item, item['recommendation_note']),
                        'ranking_reference': reference}
            if item['decision'] == 'exclude' and s in context['baseline']['观察']:
                if not all(item.get(k) for k in ('old_thesis', 'invalidated_evidence', 'alternative_value_review')):
                    raise ValueError('observation_removal_requires_full_invalidation_review')
                if s in context['user_tracking']:
                    raise ValueError('user_tracking_requires_explicit_cancellation')
            decisions[s] = {**deepcopy(item), 'name': item.get('name') or rows[s]['name'], 'sources_of_selection': rows[s]['sources'],
                            'memberships': rows[s]['memberships'], 'buy_authorized': False}
        except ValueError as exc:
            errors[s] = str(exc)
    missing = sorted(set(context['required_reviews']) - set(decisions))
    recommended = sorted((v for v in decisions.values() if v['decision'] == 'recommend'), key=lambda v: (v['priority'], v['symbol']))
    budget = review.get('attention_budget', 5)
    if not isinstance(budget, int) or not 1 <= budget <= 12 or len(recommended) > budget:
        raise ValueError('attention_budget_exceeded')
    if len({v['priority'] for v in recommended}) != len(recommended):
        raise ValueError('duplicate_editorial_priority')
    for item in recommended:
        peers = [v for v in recommended if v['symbol'] != item['symbol'] and v.get('sector') == item.get('sector')]
        if peers and not item.get('same_sector_reason'):
            errors[item['symbol']] = 'same_sector_recommendation_requires_reason'
    # Research coverage is not a command to fill the human-readable THS group.
    observed = set(context['baseline']['观察']) | {s for s, v in decisions.items() if v['decision'] == 'recommend' or (v['decision'] == 'observe' and v.get('display_observation') is True)}
    observed -= {s for s, v in decisions.items() if v['decision'] == 'exclude'}
    ready = not errors and not missing
    result = {'version': VERSION, 'run_id': context['run_id'], 'as_of_date': context['as_of_date'],
              'valid_until': context.get('valid_until'),
              'source_kind': context.get('source_kind', 'post_close'),
              'source_cutoff': context.get('source_cutoff'),
              'scan_hash': context['scan_hash'], 'context_hash': context['context_hash'], 'baseline': context['baseline'],
              'status': 'ready' if ready else 'partial', 'sync_allowed': ready, 'research_only': True,
              'author': review['author'], 'market_assessment': review['market_assessment'],
              'attention_budget': budget, 'recommended': recommended, 'reviewed': sorted(decisions.values(), key=lambda v: v['symbol']),
              'target_groups': {'推荐': sorted(v['symbol'] for v in recommended), '观察': sorted(observed)},
              'coverage': {'candidates': len(rows), 'reviewed': len(decisions), 'required': len(context['required_reviews']), 'missing': missing, 'errors': errors},
              'screening': [{'symbol': s, 'name': r['name'], 'memberships': r['memberships'],
                             'state': decisions[s]['decision'] if s in decisions else 'not_deep_reviewed',
                             'reason': decisions[s]['comparison'] if s in decisions else '保留完整策略候选及原始量价证据；本次没有完成公司比较，不等于不值得观察或已被排除。'} for s, r in sorted(rows.items())],
              'notice': '优先级是本轮带证据的人工/模型判断，不是新量化分数或收益概率；尚未触发条件的股票不能按收盘价假设成交。'}
    result['decision_id'] = digest(result)
    return result


def current_view(scan, bundle, now=None):
    if not bundle:
        return {'status': 'unavailable', 'sync_allowed': False, 'notice': '本轮尚无正式推荐决策；策略候选不等于推荐池。'}
    if bundle.get('scan_hash') != scan_hash(scan):
        return {**bundle, 'status': 'stale', 'sync_allowed': False, 'notice': '扫描证据已变化，以下是历史推荐，不能同步为本轮结果。'}
    if bundle.get('valid_until') and (now or datetime.now(ZoneInfo('Asia/Shanghai'))) >= datetime.fromisoformat(bundle['valid_until']):
        return {**bundle, 'status': 'expired', 'sync_allowed': False, 'notice': '推荐有效观察窗口已结束；旧名单仅供跟踪，不冒充当前建议。'}
    return bundle


def sync_plan(bundle, current_groups):
    if bundle.get('status') != 'ready' or not bundle.get('sync_allowed'):
        raise ValueError('decision_not_ready')
    actions = []
    for g in ('推荐', '观察'):
        actual = sorted(current_groups.get(g, []))
        target = bundle['target_groups'][g]
        # Idempotent replay is allowed only at the exact initial or final state.
        if actual not in (bundle['baseline'][g], target):
            raise ValueError('concurrent_group_change:' + g)
        if g not in current_groups:
            actions.append({'op': 'ensure_group', 'group': g})
        actions += [{'op': 'add', 'group': g, 'symbol': s} for s in sorted(set(target) - set(actual))]
        actions += [{'op': 'remove', 'group': g, 'symbol': s} for s in sorted(set(actual) - set(target))]
    return {'purpose': 'strategy_refresh', 'request': '同轮推荐决策投射，仅推荐/观察', 'decision_id': bundle['decision_id'],
            'decision_date': bundle['as_of_date'], 'run_id': bundle['run_id'], 'actions': actions}
