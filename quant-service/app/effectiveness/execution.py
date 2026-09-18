"""Explicit daily-open proxy, not execution of textual intraday conditions."""
from dataclasses import dataclass, asdict
from math import floor
from .rules import numeric


@dataclass(frozen=True)
class Costs:
    notional: float = 20000
    commission_rate: float = .0002
    minimum_commission: float = 5
    sell_tax_rate: float = .0005
    transfer_rate: float = .00001
    slippage_bps: float = 10

    def __post_init__(self):
        if any(numeric(v) is None or v<0 for v in asdict(self).values()) or self.notional<=0 or self.slippage_bps>=10000:
            raise ValueError('Explicit finite nonnegative cost assumptions required')


def simulate(bars, sessions, costs=Costs()):
    def out(status,**kw):return dict(status=status,**kw,live_effect='none',actual_trade=False)
    if len(sessions)<2:return out('t1_blocked')
    if sessions!=sorted(set(sessions)):raise ValueError('Ordered distinct exchange sessions required')
    by={b['date']:b for b in bars};path=[by.get(d) for d in sessions]
    # ``adj_factor`` is deliberately NOT in this tuple: a factor that has not
    # been fetched yet is a known-pending control on its own lane, not a data
    # outage, and reporting it as 'missing_execution_data' hid that difference.
    fields=('open','high','low','close','limit_up','limit_down')
    if any(not b or any(numeric(b.get(k)) is None or float(b[k])<=0 for k in fields) or b.get('is_suspended') is None for b in path):
        return out('missing_execution_data')
    if any(b['is_suspended'] for b in path):return out('suspended')
    if any(numeric(b.get('adj_factor')) is None or float(b['adj_factor'])<=0 for b in path):
        return out('adjustment_pending')
    if len({float(b['adj_factor']) for b in path})!=1:return out('corporate_action_unmodeled')
    first,last=path[0],path[-1]
    if first['open']>=first['limit_up']-.011 or first['open']<=first['limit_down']+.011:
        return out('entry_limit_blocked')
    if last['close']<=last['limit_down']+.011:return out('exit_limit_blocked')
    if any(b['low']>min(b['open'],b['close']) or b['high']<max(b['open'],b['close']) for b in path):
        return out('invalid_ohlc')
    buy=first['open']*(1+costs.slippage_bps/10000);sell=last['close']*(1-costs.slippage_bps/10000)
    if buy>first['limit_up'] or sell<last['limit_down']:return out('slippage_limit_blocked')
    shares=floor(costs.notional/(buy*100))*100
    if shares<100:return out('lot_size_blocked')
    def buy_cost(q):
        amount=q*buy
        return amount+max(costs.minimum_commission,amount*costs.commission_rate)+amount*costs.transfer_rate
    while shares>=100 and buy_cost(shares)>costs.notional:shares-=100
    if shares<100:return out('lot_size_blocked')
    paid=buy_cost(shares);amount=shares*sell
    proceeds=amount-max(costs.minimum_commission,amount*costs.commission_rate)-amount*(costs.sell_tax_rate+costs.transfer_rate)
    return out('simulated',entry_date=sessions[0],exit_date=sessions[-1],shares=shares,
        net_return_pct=(proceeds-paid)/paid*100,
        adverse_excursion_pct=(min(b['low'] for b in path)/buy-1)*100,costs=asdict(costs),
        method='次一交易日开盘买、窗口末收盘卖的日线模拟；不是原盘中条件成交，也不是实盘收益')
