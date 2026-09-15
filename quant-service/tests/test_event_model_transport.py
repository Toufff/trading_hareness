import json
import pytest
from app.event_research.model_transport import read_completion

class Response:
    def __init__(self,lines):self.lines=lines
    def iter_lines(self,**kw):return iter(self.lines)

def chunk(content='',finish=None):
    return 'data: '+json.dumps({'model':'test','choices':[{'index':0,'delta':{'content':content},'finish_reason':finish}]})

def test_stream_completion():
    r=Response(['',':keepalive',chunk('{"ok":'),chunk('true}','stop'),'data: [DONE]'])
    assert read_completion(r,2,lambda:1)[0]=={'ok':True}

def test_stream_deadline_and_truncation():
    with pytest.raises(TimeoutError):read_completion(Response(['']),1,lambda:2)
    with pytest.raises(ValueError):read_completion(Response([chunk('{}')]),2,lambda:1)
    with pytest.raises(ValueError):read_completion(Response([chunk('{}','length')]),2,lambda:1)
def test_deadline_retains_safe_stream_progress():
    from app.event_research.model_transport import ModelDeadlineExceeded
    ticks=iter([0,2])
    with pytest.raises(ModelDeadlineExceeded) as error:
        read_completion(Response([chunk('partial'), '']),1,lambda:next(ticks))
    assert error.value.progress=={'received_chars':7,'stream_chunks':1}
