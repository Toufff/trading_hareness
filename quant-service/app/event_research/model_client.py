"""One bounded model transport with secret-free, stage-specific diagnostics."""
import json
import time
import requests
from .model_transport import read_completion, ModelDeadlineExceeded


class ModelReviewFailure(RuntimeError):
    def __init__(self, stage, code, **details):
        super().__init__(f'{stage}:{code}')
        self.diagnostics=dict(failure_stage=stage,failure_code=code,**details)


def completion(messages,connection,deadline):
    base,key,model,options=connection
    remaining=deadline-time.monotonic()
    if remaining<=1:raise ModelReviewFailure('generation','total_deadline_exceeded')
    stage='connection';status=None
    try:
        with requests.Session() as session:
            session.trust_env=False
            with session.post(base+'/chat/completions',headers={'Authorization':'Bearer '+key},
                json={'model':model,'messages':messages,'response_format':{'type':'json_object'},
                      'max_tokens':8192,'stream':True,'stream_options':{'include_usage':True},**options},
                stream=True,timeout=(min(10,remaining),min(20,remaining))) as response:
                status=response.status_code
                response.raise_for_status();stage='generation'
                return read_completion(response,deadline)
    except ModelReviewFailure:raise
    except ModelDeadlineExceeded as exc:
        raise ModelReviewFailure(stage,'total_deadline_exceeded',http_status=status,progress=exc.progress) from exc
    except requests.HTTPError as exc:
        raise ModelReviewFailure('http',f'http_{status}',http_status=status) from exc
    except requests.Timeout as exc:
        raise ModelReviewFailure(stage,'connect_timeout' if stage=='connection' else 'read_idle_timeout',http_status=status) from exc
    except requests.RequestException as exc:
        raise ModelReviewFailure(stage,'transport_error',http_status=status,error_type=type(exc).__name__) from exc
    except json.JSONDecodeError as exc:
        raise ModelReviewFailure('response_format','invalid_json',http_status=status) from exc
    except ValueError as exc:
        # Transport only emits fixed messages here; never include raw HTTP bodies.
        code={'Event model stream error':'provider_stream_error',
              'Event model stream ended before completion':'stream_incomplete',
              'Event model response too large':'response_size_limit'}.get(str(exc),'incomplete_generation')
        raise ModelReviewFailure('generation',code,http_status=status) from exc
