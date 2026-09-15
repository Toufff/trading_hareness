"""Longhu-only <=300 transport; exact date/window validation, no daily writes."""
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor,as_completed
from ..longhu_vendor_source import LonghuVendorSource,SharedLonghuReadSource,parse_industry_stock_row,parse_stock_minute_payload

def session_cutoff(now):
    now=now.astimezone(ZoneInfo('Asia/Shanghai'))
    t=now.strftime('%H%M')
    if t<'0931':raise ValueError('Continuous-session data not yet available')
    if '1130'<=t<'1301':return now.replace(hour=11,minute=30,second=0,microsecond=0)
    if t>='1500':return now.replace(hour=15,minute=0,second=0,microsecond=0)
    from datetime import timedelta
    completed=now.replace(second=0,microsecond=0)-timedelta(minutes=1)
    return completed.replace(minute=completed.minute//5*5)

def window_params(plate_id,end,offset=0,size=300):
    return dict(Order=1,TSZB=0,a='ZhiShuStockList_W8',st=min(300,size),c='ZhiShuRanking',
        PhoneOSNew=1,old=1,VerSion='5.21.0.2',IsZZ=0,Index=offset,REnd=end,
        apiv='w42',Type=1,IsKZZType=0,TSZB_Type=0,filterType=0,PlateID=plate_id,RStart='0925')

def available_cutoff(now):
    requested=session_cutoff(now)
    source=LonghuVendorSource()
    response=source.raw_call(dict(target='longhu_market_wide',params=window_params('881270',requested.strftime('%H%M'),size=1)))
    payload=response['pages'][0]['payload']
    if str(payload.get('errcode',0))!='0' or payload.get('Day')!=[str(requested.date())]:
        raise ValueError('No current-day provider window')
    end=str(payload.get('Max',''))
    if len(end)!=4 or not end.isdigit():raise ValueError('Invalid provider availability time')
    available=requested.replace(hour=int(end[:2]),minute=int(end[2:]))
    cutoff=min(requested,available)
    # Lunch uses 11:30 legitimately for the entire break; continuous-session
    # stale windows are rejected instead of being relabelled current.
    if requested-cutoff>__import__('datetime').timedelta(minutes=10):raise ValueError('Provider window stale')
    return cutoff

class WindowSource(LonghuVendorSource):
    def __init__(self,cutoff):
        super().__init__();self.cutoff=cutoff

    def plate_day(self,plate_id,trade_date,*,live):
        if not live:raise ValueError('Intraday window requires actual current trading day')
        result=[];end=self.cutoff.strftime('%H%M')
        for offset in range(0,3000,300):
            response=self.raw_call(dict(target='longhu_market_wide',params=window_params(plate_id,end,offset)))
            payload=response['pages'][0]['payload']
            if str(payload.get('errcode',0))!='0':raise ValueError('vendor code '+str(payload.get('errcode')))
            if payload.get('Day')!=[str(trade_date)] or str(payload.get('Max',''))<end:
                raise ValueError('vendor date/window mismatch')
            batch=payload.get('list') or []
            for raw in batch:
                row=parse_industry_stock_row(raw,trade_date,plate_id)
                if row:result.append(row)
            if len(batch)<300:return result
        raise ValueError('Pagination limit reached; refusing silent truncation')

def capture(cutoff):
    source=WindowSource(cutoff);catalog=source.industry_plate_catalog()
    rows,health=source.full_market_vendor_rows(cutoff.date(),plate_ids=[r['sector_key'] for r in catalog])
    labels={r['sector_key']:r['label'] for r in catalog}
    return [dict(r,sector_label=labels.get(r['plate_id'])) for r in rows.values()],health

def fetch_minutes(symbols,cutoff):
    def get(symbol):
        source=SharedLonghuReadSource(base_url='http://127.0.0.1:5681')
        raw=source.raw_call(dict(target='longhu_market_wide',params=dict(a='GetStockTrendIncremental',
            c='StockL2Data',apiv='w41',Type=1,StockID=symbol.split('.')[0])))
        payload=raw['pages'][0]['payload']
        if str(payload.get('errcode',0))!='0' or str(payload.get('day'))!=cutoff.strftime('%Y%m%d'):
            raise ValueError('Minute date/vendor code mismatch')
        rows=parse_stock_minute_payload(payload,symbol)
        return [r for r in rows if '0930'<=r['time']<=cutoff.strftime('%H%M')]
    out={};errors={}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(get,s):s for s in sorted(set(symbols))}
        for future in as_completed(futures):
            s=futures[future]
            try:out[s]=future.result()
            except Exception as exc:errors[s]=type(exc).__name__
    return out,errors
