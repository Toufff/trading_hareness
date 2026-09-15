from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routers.intraday_scans import build_intraday_scans_router

async def executor(fn,*args,**kwargs):return fn(*args)

def test_read_contract_and_argument_validation():
    app=FastAPI();app.include_router(build_intraday_scans_router(object(),executor));client=TestClient(app)
    with patch('app.routers.intraday_scans.latest',return_value=dict(status='no_run',result=None,runs=[])) as read:
        assert client.get('/api/v1/intraday-scans').json()['status']=='no_run'
        assert read.call_count==1
        assert client.get('/api/v1/intraday-scans?run_id=wrong').status_code==422
        assert client.get('/api/v1/intraday-scans?symbol=600000').status_code==422
        assert client.post('/api/v1/intraday-scans').status_code==405
        assert read.call_count==1
