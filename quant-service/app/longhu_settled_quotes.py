"""Date-addressed licensed OHLC. Live quotes cannot repair historical days."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from math import isfinite


def number(value):
    try:
        result=float(value)
        return result if isfinite(result) else None
    except (ValueError,TypeError):
        return None


def normalized_quote(symbol, day, payload):
    dates=payload.get('x') or []
    values=payload.get('y') or []
    target=day.strftime('%Y%m%d')
    keys=[str(d).replace('-','') for d in dates]
    if target not in keys or len(keys)!=len(values):
        return None
    i=keys.index(target)
    if i<1 or len(values[i])<4:
        return None
    o,c,h,l=[number(x) for x in values[i][:4]]
    p=number(values[i-1][1])
    if any(v is None or v<=0 for v in (o,c,h,l,p)) or not l<=min(o,c)<=max(o,c)<=h:
        return None
    volumes,amounts=payload.get('vol') or [],payload.get('bal') or []
    return {'ts_code':symbol,'trade_date':target,'open':o,'close':c,'high':h,'low':l,'pre_close':p,
            'vol':number(volumes[i]) if i<len(volumes) else None,
            'amount':number(amounts[i]) if i<len(amounts) else None,
            'price_basis':'longhu_requested_Is_FS_0_cross_checked_vendor_close'}


def fetch(source, symbols, day, *, workers=8):
    unique=list(dict.fromkeys(symbols))
    quotes,errors=[],{}
    def one(symbol):
        envelope=source.raw_call({'target':'longhu_history','path':'/w1/api/index.php',
            'params':{'a':'GetKLineDay_W14','c':'StockLineData','apiv':'w40','StockID':symbol.split('.')[0],
                      'Type':'d','Is_FS':'0','st':min(300,max(20,(date.today()-day).days+10)),'Index':0}})
        for page in envelope.get('pages',[]):
            quote=normalized_quote(symbol,day,page.get('payload') or {})
            if quote is not None:
                return quote
        raise ValueError('requested_settled_day_missing_or_invalid_ohlc')
    with ThreadPoolExecutor(max_workers=max(1,min(workers,12))) as pool:
        tasks={pool.submit(one,s):s for s in unique}
        for task in as_completed(tasks):
            s=tasks[task]
            try: quotes.append(task.result())
            except Exception as exc: errors[s]=f'{type(exc).__name__}: {exc}'
    return quotes,{'provider':'longhuvip:GetKLineDay_W14','trade_date':str(day),'requested':len(unique),
                   'received':len(quotes),'failed':len(errors),'errors':dict(list(errors.items())[:30]),
                   'physical_page_limit':300,'no_live_quote_substitution':True}
