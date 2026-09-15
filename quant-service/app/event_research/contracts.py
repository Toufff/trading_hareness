"""Pure timestamp, provenance and content identity contracts."""
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import html
import re
from urllib.parse import urlsplit

VERSION = 'event-research-20260914'

def timestamp(value):
    if isinstance(value, datetime):
        dt=value
    elif str(value).isdigit():
        dt=datetime.fromtimestamp(int(value),timezone.utc)
    else:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('Timezone required')
    return dt.astimezone(timezone.utc)

def clean(value):
    text=re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', str(value or ''), flags=re.S|re.I)
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>',' ',text))).strip()

def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()

def normalize(raw, received_at):
    published=timestamp(raw['Time']); received=timestamp(received_at)
    body=clean(raw.get('Content')); title=clean(raw.get('Title')) or body[:100]
    if not body or not raw.get('CID'): raise ValueError('Missing news body/ID')
    if '\ufffd' in body: raise ValueError('Corrupt news encoding')
    url=str(raw.get('PushUrl') or '')
    if urlsplit(url).scheme not in ('http','https'):url=''
    content_hash=digest(body)
    return dict(document_id=digest('longhu:'+str(raw['CID'])+':'+content_hash),
        provider='longhu_pcnewsflash',provider_id=str(raw['CID']),source=clean(raw.get('Source')),
        title=title,body=body,url=url,published_at=published.isoformat(),
        body_truncated=bool(re.search(r'\.\.\.展开|…展开|阅读全文',body)),
        first_seen_at=received.isoformat(),available_at=max(published,received).isoformat(),
        content_hash=content_hash,entities=raw.get('Stocks') or [],
        provenance='供应商转载快讯；未自动认定为一手核验',version=VERSION)

def eligible(doc, cutoff, lookback_days=7):
    cutoff=timestamp(cutoff)
    return (timestamp(doc['available_at']) <= cutoff and
            cutoff-timedelta(days=lookback_days)<=timestamp(doc['published_at'])<=cutoff)

def unique_documents(documents):
    seen=set();out=[]
    for doc in sorted(documents,key=lambda d:d['available_at']):
        if doc['content_hash'] not in seen:out.append(doc);seen.add(doc['content_hash'])
    return out
