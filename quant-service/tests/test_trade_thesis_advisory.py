from datetime import datetime, timezone
from decimal import Decimal
import json

from app.trade_thesis.advisory import project_advisory


def test_real_bound_hard_risk_kept_with_supported_research_and_json_safe():
    result = {'states': {'thesis_state': 'supported', 'entry_state': 'eligible'}}
    bound = {'status': 'bound', 'binding': {'account_key': 'test'},
        'plan': {'plan_kind': 'holding', 'plan_id': 'p', 'as_of_at': datetime.now(timezone.utc),
                 'valid_until': datetime.now(timezone.utc), 'lines': [{'kind': 'hard_stop', 'label': '硬止损', 'price': Decimal('10')}]},
        'risk': {'hard_risk': True, 'plan_state': 'exit_signalled'}}
    projection = project_advisory({}, result, bound)
    assert result['states']['thesis_state'] == 'supported'
    assert projection['holding']['hard_risk'] is True
    assert '退出' in projection['holding']['note']
    assert projection['holding']['action_quantity'] is None
    assert projection['chart_lines'][0]['price'] == 10
    json.dumps(projection)


def test_stale_plan_cannot_add_executable_chart_lines_or_infer_quantity():
    projection = project_advisory({}, {'states': {'entry_state': 'eligible'}},
        {'status': 'stale', 'plan': {'plan_kind': 'holding', 'position': {'quantity': 1000},
                                   'lines': [{'kind': 'hard_stop', 'price': 10}]},
         'risk': {'hard_risk': True, 'plan_state': 'exit_signalled'}})
    assert projection['chart_lines'] == []
    assert projection['holding']['is_holding'] is False
    assert projection['holding']['action_quantity'] is None
    assert projection['scenario_projection']['research_entry_state'] == 'eligible'
    assert projection['scenario_projection']['combined_entry_state'] == 'unknown'


def test_new_buy_binding_does_not_claim_a_position():
    projection = project_advisory({}, {'states': {'entry_state': 'waiting'}},
        {'status': 'bound', 'plan': {'plan_kind': 'new_buy'}, 'risk': {'plan_state': 'active'}})
    assert not projection['holding']['is_holding']
    assert '不代表已经持仓' in projection['holding']['note']
