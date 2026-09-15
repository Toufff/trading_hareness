"""Small, explicit billed model probe. Never emits credentials."""
import json
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))
from app.event_research.model_config import settings
load_dotenv('G:/StockPlatform/config/runtime.env',override=True)
base,key,model,options=settings()
start=time.monotonic()
with requests.Session() as session:
    session.trust_env=False
    response=session.post(base+'/chat/completions',headers={'Authorization':'Bearer '+key},
        json={'model':model,**options,'messages':[{'role':'user','content':'Return a JSON object with the field ok set to true.'}],
        'response_format':{'type':'json_object'},'max_tokens':64},timeout=(10,30))
    response.raise_for_status()
    data=response.json()
    print(json.dumps({'model':data.get('model'),'seconds':round(time.monotonic()-start,1),
        'choices':data.get('choices'),'usage':data.get('usage')},ensure_ascii=False))
