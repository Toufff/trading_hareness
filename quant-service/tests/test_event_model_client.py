import time
import requests
import pytest
from app.event_research import model_client


@pytest.mark.parametrize('mode,stage,code',[
    ('connect','connection','connect_timeout'),('http','http','http_429'),
    ('idle','generation','read_idle_timeout'),('json','response_format','invalid_json')])
def test_precise_errors_and_no_environment_proxy(monkeypatch,mode,stage,code):
    class Session:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kw):
            assert self.trust_env is False
            if mode=='connect':raise requests.ConnectTimeout('private ignored')
            if mode=='http':self.status_code=429
            return self
        def raise_for_status(self):
            if mode=='http':raise requests.HTTPError('private body ignored')
        def iter_lines(self,**kw):
            if mode=='idle':raise requests.ReadTimeout('private ignored')
            return iter(['data: not-json'])
    monkeypatch.setattr(model_client.requests,'Session',Session)
    with pytest.raises(model_client.ModelReviewFailure) as err:
        model_client.completion([],('https://invalid','secret','test',{}),time.monotonic()+20)
    assert err.value.diagnostics['failure_stage']==stage
    assert err.value.diagnostics['failure_code']==code
    assert 'private' not in str(err.value.diagnostics) and 'secret' not in str(err.value.diagnostics)
