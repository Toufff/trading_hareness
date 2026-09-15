"""Existing licensed transport only, bounded <=300 pages and explicit coverage."""
from dataclasses import replace
from datetime import datetime, timezone
import requests
from ..licensed_stock_api import execute
from ..longhu_vendor_source import LonghuVendorConfig
from .contracts import normalize

def collect(*, pages=4, page_size=300, session=None, config=None):
    if not 1<=page_size<=300 or not 1<=pages<=6:raise ValueError('Invalid news batch budget')
    owned=session is None; session=session or requests.Session()
    if owned:session.trust_env=False
    config=replace(config or LonghuVendorConfig.load(),retries=1,timeout_seconds=12)
    documents=[]; errors=[]; calls=0; exhausted=False
    try:
        for page in range(pages):
            data=execute(session=session,config=config,target_key='longhu_article',path=None,
                params={'a':'GetList','c':'PCNewsFlash','apiv':'w44','Type':0,'st':page_size,'Index':page*page_size})
            calls+=data['calls']; payload=data['pages'][0]['payload']
            if not isinstance(payload,dict) or str(payload.get('errcode','0'))!='0' or not isinstance(payload.get('List'),list):
                raise ValueError('Longhu news schema/business error')
            received=datetime.now(timezone.utc)
            for row in payload['List']:
                try:documents.append(normalize(row,received))
                except (ValueError,TypeError,KeyError,OverflowError):errors.append('invalid_document')
            if len(payload['List'])<page_size:exhausted=True;break
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        if owned:session.close()
    return documents,dict(provider='longhu_pcnewsflash',status='partial' if errors and documents else 'failed' if errors else 'ok',
        calls=calls,physical_batch_limit=300,received=len(documents),errors=errors,
        exhausted=exhausted,coverage='有界快讯窗口；不宣称覆盖全部新闻或全部历史',
        oldest_published_at=min((r['published_at'] for r in documents),default=None),
        newest_published_at=max((r['published_at'] for r in documents),default=None))
