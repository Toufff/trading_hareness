"""Combine settled history with an observed quote's real open/high/low/last.

No highs/lows are inferred from minute samples. A quote timestamp is provider
refresh time, not an assertion of a last exchange trade at that second.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite


def merge(histories,quotes,cutoff,captured_at):
    cutoff=datetime.fromisoformat(cutoff);observed=datetime.fromisoformat(captured_at)
    if cutoff.date()!=observed.date() or (cutoff.hour<15 and (observed.hour>=15 or observed-cutoff>timedelta(minutes=10))):
        raise ValueError('Quote cannot be backfilled into earlier intraday window')
    out=deepcopy(histories);errors={};current=0;ready=0
    by={r['ts_code']:r for r in quotes}
    for symbol,bars in histories.items():
        q=by.get(symbol)
        try:
            if not q or q.get('trade_date')!=cutoff.strftime('%Y%m%d'):raise ValueError('quote_wrong_day_or_missing')
            stamp=datetime.strptime(q.get('trade_time') or '', '%Y%m%d%H%M%S').replace(tzinfo=cutoff.tzinfo)
            expected=min(observed,observed.replace(hour=15,minute=0,second=0,microsecond=0))
            if stamp>observed+timedelta(seconds=5) or stamp<expected-timedelta(minutes=10):raise ValueError('quote_stale_or_future')
            values=[q.get(k) for k in ('open','high','low','price','amount')]
            if any(not isinstance(v,(int,float)) or not isfinite(v) or v<=0 for v in values):raise ValueError('quote_missing_ohlc')
            o,h,l,p,a=values
            if not l<=min(o,p)<=max(o,p)<=h:raise ValueError('quote_invalid_ohlc')
            prior=[b for b in bars if b['date']<str(cutoff.date())]
            if not prior or not q.get('pre_close') or abs(prior[-1]['close']/q['pre_close']-1)>.001:
                raise ValueError('history_quote_adjustment_mismatch')
            bar=dict(date=str(cutoff.date()),open=o,high=h,low=l,close=p,amount=a,
                     source='longhuvip:GetStockPanKou',available_at=captured_at,provider_refresh_at=stamp.isoformat(),
                     provisional=cutoff.hour<15)
            out[symbol]=prior+[bar];current+=1;ready+=int(len(out[symbol])>=40)
        except (ValueError,TypeError,KeyError) as exc:
            errors[symbol]=str(exc)
            # Never call a prior-day history 'current OHLC'. Existing actual
            # same-day daily bars may still be used, with their capture time.
            if bars and bars[-1]['date']==str(cutoff.date()):
                current+=1;ready+=int(len(bars)>=40)
    return out,dict(requested=len(histories),ready=ready,current_day_ready=current,
                    historical_ready=sum(len(b)>=40 for b in histories.values()),errors=errors,
                    short_history={s:len(b) for s,b in out.items() if len(b)<40},
                    quote_received=len(quotes),source='Longhu historical daily + timestamped quote OHLC')
