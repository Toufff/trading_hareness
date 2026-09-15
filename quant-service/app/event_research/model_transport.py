"""Bounded streaming JSON completion; keepalives cannot extend the deadline."""
import json
import time

class ModelDeadlineExceeded(TimeoutError):
    def __init__(self, received_chars, chunks):
        super().__init__('Event model total deadline exceeded')
        self.progress = {'received_chars': received_chars, 'stream_chunks': chunks}


def read_completion(response,deadline,clock=time.monotonic):
    content=[];size=0;finished=False;usage={};returned_model=None;chunks=0
    response.encoding='utf-8'
    for line in response.iter_lines(chunk_size=1,decode_unicode=True):
        if clock()>deadline:raise ModelDeadlineExceeded(size,chunks)
        if not line or not line.startswith('data:'):continue
        raw=line[5:].strip()
        if raw=='[DONE]':break
        chunk=json.loads(raw)
        chunks+=1
        if 'error' in chunk:raise ValueError('Event model stream error')
        returned_model=chunk.get('model') or returned_model
        usage=chunk.get('usage') or usage
        for choice in chunk.get('choices',[]):
            if choice.get('index',0)!=0:continue
            reason=choice.get('finish_reason')
            if reason and reason!='stop':raise ValueError('Event model incomplete completion: '+str(reason))
            finished=finished or reason=='stop'
            part=choice.get('delta',{}).get('content') or ''
            size+=len(part)
            if size>100000:raise ValueError('Event model response too large')
            content.append(part)
    if not finished:raise ValueError('Event model stream ended before completion')
    return json.loads(''.join(content)),usage,returned_model
