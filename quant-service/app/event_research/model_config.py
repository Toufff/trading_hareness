"""Explicit external credential reference; never copy credentials into reports."""
import os
from pathlib import Path
import yaml

def settings():
    base=os.getenv('EVENT_RESEARCH_API_BASE','').rstrip('/')
    model=os.getenv('EVENT_RESEARCH_MODEL','')
    key=os.getenv('EVENT_RESEARCH_API_KEY','')
    location=os.getenv('EVENT_RESEARCH_API_KEY_FILE','')
    if not key and location:
        # Only the explicitly configured local file is read; no credential discovery.
        data=yaml.safe_load(Path(location).read_text(encoding='utf-8'))
        ref=os.getenv('EVENT_RESEARCH_API_KEY_REF','DEEPSEEK_API_KEY')
        key=data.get('refs',{}).get(ref,'') if isinstance(data,dict) else ''
    if not isinstance(key,str):raise ValueError('Credential reference must resolve to text')
    thinking=os.getenv('EVENT_RESEARCH_THINKING','')
    if thinking not in ('','enabled','disabled'):raise ValueError('Invalid thinking option')
    options={'thinking':{'type':thinking}} if thinking else {}
    return base,key,model,options
